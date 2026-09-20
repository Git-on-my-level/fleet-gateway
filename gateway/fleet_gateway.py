#!/usr/bin/env python3
"""Tokenless, single-worker disposable shell gateway. Python 3.10+, stdlib only."""
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

VERSION = '1.0.0'
ROOT = Path(os.environ.get('WORKER_ROOT', '/var/lib/fleet-gateway')).resolve()
AUDIT = Path(os.environ.get('FLEET_GATEWAY_AUDIT', '/var/log/fleet-gateway/runs.jsonl'))
IMAGE = os.environ.get('FLEET_GATEWAY_IMAGE', 'fleet-gateway-worker:1')
PORT = int(os.environ.get('FLEET_GATEWAY_PORT', '9494'))
CAPACITY = int(os.environ.get('FLEET_GATEWAY_QUEUE_CAPACITY', '8'))
RESERVE = int(os.environ.get('FLEET_GATEWAY_DISK_RESERVE', str(2 * 1024**3)))
RETENTION = int(os.environ.get('FLEET_GATEWAY_RETENTION_SECONDS', '3600'))
LOG_CAP = 8 * 1024**2
TAR_CAP = 512 * 1024**2
LABEL = 'fleet-gateway.job'
OWNER = 'fleet-gateway.owner=' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
CV = threading.Condition(threading.RLock())
AUDIT_LOCK = threading.Lock()
JOBS = {}
QUEUE = collections.deque()
KEYS = {}
ACTIVE = None
STOP = threading.Event()


def command(args, timeout=15):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, b'', str(exc).encode())


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def folder(job):
    return ROOT / 'jobs' / job['job_id']


def save(job):
    atomic(folder(job) / 'job.json', job)


def audit(event, job):
    try:
        with AUDIT_LOCK:
            AUDIT.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT.open('a') as out:
                out.write(json.dumps({'ts': time.time(), 'event': event,
                                      'job_id': job['job_id'], 'state': job['state'],
                                      'caller': job['caller'], 'reason': job.get('reason')}) + '\n')
    except OSError as exc:
        print(f'audit error: {exc}', file=sys.stderr, flush=True)


def whois(ip):
    result = command(['tailscale', 'whois', '--json', ip], 3)
    try:
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        node = data['Node']
        return {'status': 'ok', 'node_name': node.get('Name'),
                'node_id': node.get('ID'), 'user': data.get('UserProfile', {}).get('LoginName')}
    except (ValueError, KeyError, TypeError):
        return {'status': 'unavailable'}


def health():
    docker_ok = command(['docker', 'info', '--format', '{{.ServerVersion}}'], 5).returncode == 0
    image_ok = docker_ok and command(['docker', 'image', 'inspect', IMAGE], 5).returncode == 0
    free = shutil.disk_usage(ROOT).free
    with CV:
        return {'ok': docker_ok and image_ok and free >= RESERVE,
                'workers': [{'state': 'busy' if ACTIVE else 'idle', 'job_id': ACTIVE}],
                'queue_length': len(QUEUE), 'docker_ok': docker_ok, 'image_ok': image_ok,
                'disk_free_bytes': free, 'version': VERSION}


def remove_container(job_id):
    """Absence is proven by a successful daemon listing, never a failed inspect."""
    filt = f'{LABEL}={job_id}'
    result = command(['docker', 'ps', '-aq', '--filter', f'label={filt}', '--filter', f'label={OWNER}'])
    if result.returncode:
        return False
    for cid in result.stdout.decode().split():
        command(['docker', 'kill', cid])
        command(['docker', 'rm', '-f', cid])
    result = command(['docker', 'ps', '-aq', '--filter', f'label={filt}', '--filter', f'label={OWNER}'])
    return result.returncode == 0 and not result.stdout.strip()


def sweep_orphans():
    # Serialize with admission/worker selection so hourly cleanup cannot kill live work.
    with CV:
        result = command(['docker', 'ps', '-aq', '--filter', f'label={LABEL}', '--filter', f'label={OWNER}'])
        if result.returncode:
            return False
        for cid in result.stdout.decode().split():
            info = command(['docker', 'inspect', '--format', '{{index .Config.Labels "fleet-gateway.job"}}', cid])
            jid = info.stdout.decode().strip()
            if jid and jid != ACTIVE:
                if not remove_container(jid):
                    return False
        return True


class LimitedWriter:
    def __init__(self, stream):
        self.stream, self.count = stream, 0

    def write(self, data):
        self.count += len(data)
        if self.count > TAR_CAP:
            raise OverflowError('uncompressed artifact archive exceeds 512 MiB')
        return self.stream.write(data)


def publish(job):
    base = folder(job)
    dest = base / 'published'
    dest.mkdir()
    manifest = {}
    skipped = 0
    total = 0
    try:
        work = base / 'scratch' / 'work'
        if not stat.S_ISDIR(work.lstat().st_mode):
            raise OSError('work must remain a real directory, not a symlink')
        for parent, dirs, files in os.walk(work, followlinks=False):
            kept = []
            for name in dirs:
                path = Path(parent) / name
                if name.startswith('.') or path.is_symlink():
                    skipped += 1
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in files:
                source = Path(parent) / name
                if name.startswith('.') or not stat.S_ISREG(source.lstat().st_mode):
                    skipped += 1
                    continue
                relative = source.relative_to(base / 'scratch' / 'work').as_posix()
                # Archive itself has a reserved public name.
                if relative == 'work.tar.gz':
                    skipped += 1
                    continue
                target = dest / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as inp, target.open('wb') as out:
                    if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                        skipped += 1
                        continue
                    while True:
                        chunk = inp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > TAR_CAP:
                            raise OverflowError('artifact data exceeds 512 MiB')
                        out.write(chunk)
                target.chmod(0o444)
                manifest[relative] = 'published/' + relative
        with gzip.open(base / 'work.tar.gz.tmp', 'wb') as gz:
            with tarfile.open(fileobj=LimitedWriter(gz), mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for name in sorted(manifest):
                    archive.add(base / manifest[name], arcname=name, recursive=False)
        os.replace(base / 'work.tar.gz.tmp', base / 'work.tar.gz')
        manifest['work.tar.gz'] = 'work.tar.gz'
        job.update(manifest=manifest, artifacts=sorted(manifest), published_ok=True)
    except (OSError, OverflowError, tarfile.TarError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        (base / 'work.tar.gz.tmp').unlink(missing_ok=True)
        job['artifacts_truncated'] = isinstance(exc, OverflowError)
        job['publication_error'] = {'code': 'artifact_limit' if isinstance(exc, OverflowError) else 'storage_error', 'detail': str(exc)}
    job['artifacts_skipped'] = skipped


def finish(job, state, reason=None):
    job.update(state=state, finished_at=time.time())
    job['expires_at'] = job['finished_at'] + RETENTION
    if reason:
        job['reason'] = {'code': reason}
    save(job)
    audit('finished', job)
    CV.notify_all()


def execute(job):
    base = folder(job)
    name = 'fleet-gateway-' + job['job_id']
    proc = None
    reader = None
    state, reason = 'failed', 'docker_error'
    try:
        args = ['docker', 'create', '--rm', '--name', name, '--label', f'{LABEL}={job["job_id"]}', '--label', OWNER,
                '--memory', '2g', '--memory-swap', '2g', '--cpus', '2', '--pids-limit', '256',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--log-driver', 'json-file', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                '-w', '/work', '-v', f'{base / "scratch"}:/work', IMAGE, 'bash', '/task.sh']
        created = command(args, 60)
        if created.returncode:
            raise RuntimeError(created.stderr.decode(errors='replace'))
        copied = command(['docker', 'cp', str(base / 'task.sh'), name + ':/task.sh'])
        if copied.returncode:
            raise RuntimeError(copied.stderr.decode(errors='replace'))
        with CV:
            cancelled = job.get('cancel_requested', False)
        if cancelled:
            state, reason = 'cancelled', 'cancelled'
        else:
            proc = subprocess.Popen(['docker', 'start', '-a', name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            def drain():
                try:
                    with (base / 'log').open('wb') as out:
                        while True:
                            data = proc.stdout.read1(65536)
                            if not data:
                                break
                            with CV:
                                remaining = LOG_CAP - job['log_size']
                                part = data[:remaining]
                                out.write(part)
                                out.flush()
                                job['log_size'] += len(part)
                                if len(data) > remaining:
                                    job['log_truncated'] = True
                except OSError as exc:
                    job['log_error'] = str(exc)
                    # Keep draining so a full pipe cannot block container cleanup.
                    while proc.stdout.read(65536):
                        pass

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + job['timeout_seconds']
            while proc.poll() is None:
                with CV:
                    cancelled = job.get('cancel_requested', False) or STOP.is_set()
                if cancelled or time.monotonic() >= deadline:
                    state = 'cancelled' if cancelled else 'timed_out'
                    reason = state
                    break
                time.sleep(0.1)
            else:
                job['exit_code'] = proc.returncode
                job['execution_ok'] = proc.returncode == 0
                state = 'succeeded' if job['execution_ok'] else 'failed'
                reason = None if job['execution_ok'] else 'execution_error'
    except (OSError, RuntimeError) as exc:
        job['error_detail'] = str(exc)
    finally:
        # A dead daemon leaves this worker occupied until removal can be proven.
        while not remove_container(job['job_id']):
            with CV:
                job['reason'] = {'code': 'cleanup_pending'}
                save(job)
            time.sleep(2)
        if proc:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if reader:
            reader.join(timeout=10)
        with CV:
            if job.get('cancel_requested'):
                state, reason = 'cancelled', 'cancelled'
                job['execution_ok'] = False
        try:
            publish(job)
        except OSError as exc:
            job['publication_error'] = {'code': 'storage_error', 'detail': str(exc)}
        if job.get('log_error') and state == 'succeeded':
            state, reason = 'failed', 'storage_error'
        if not job['published_ok'] and state == 'succeeded':
            state, reason = 'failed', 'publication_error'
        with CV:
            if job.get('cancel_requested'):
                state, reason = 'cancelled', 'cancelled'
            finish(job, state, reason)


def worker():
    global ACTIVE
    while not STOP.is_set():
        with CV:
            CV.wait_for(lambda: QUEUE or STOP.is_set())
            if STOP.is_set():
                return
            jid = QUEUE.popleft()
            job = JOBS[jid]
            ACTIVE = jid
            job.update(state='running', started_at=time.time())
            save(job)
        try:
            execute(job)
        except Exception as exc:
            # Fail closed: do not release the worker on unexpected persistence errors.
            print(f'worker stopped: {exc}', file=sys.stderr, flush=True)
            return
        with CV:
            ACTIVE = None
            CV.notify_all()


def janitor():
    last_sweep = 0
    while not STOP.wait(10):
        now = time.time()
        with CV:
            for jid, job in list(JOBS.items()):
                if job.get('expires_at') and job['expires_at'] <= now and jid != ACTIVE:
                    shutil.rmtree(folder(job), ignore_errors=True)
                    del JOBS[jid]
            for key, entry in list(KEYS.items()):
                if entry['expires_at'] <= now:
                    del KEYS[key]
            atomic(ROOT / 'idempotency.json', KEYS)
        if now - last_sweep >= 3600:
            sweep_orphans()
            last_sweep = now


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send(self, code, value, headers=None):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != '/jobs':
            return self.send(404, {'error': 'not_found'})
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Transfer-Encoding unsupported')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1:
                raise ValueError('one Content-Length required')
            length = int(lengths[0])
            if length > 1024**2:
                return self.send(413, {'error': 'body_too_large'})
            if length <= 0:
                raise ValueError('empty body')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or not isinstance(data.get('script'), str) or not data['script'].strip():
                raise ValueError('explicit script string required')
            if data.get('job_type', 'shell') != 'shell':
                raise ValueError('only shell job_type supported')
            timeout = data.get('timeout_seconds', 900)
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                raise ValueError('timeout_seconds must be integer 1..3600')
            key = data.get('idempotency_key')
            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 256):
                raise ValueError('idempotency_key must be 1..256 characters')
        except (ValueError, OSError) as exc:
            return self.send(400, {'error': str(exc)})
        identity = whois(self.client_address[0])
        with CV:
            if key in KEYS and KEYS[key]['expires_at'] > time.time():
                return self.send(202, {'job_id': KEYS[key]['job_id']})
            status = health()
            if not status['ok']:
                return self.send(503, {'error': 'worker_unavailable', **status}, {'Retry-After': '5'})
            if len(QUEUE) >= CAPACITY:
                return self.send(429, {'error': 'queue_full'}, {'Retry-After': '5'})
            jid = uuid.uuid4().hex
            job = dict(job_id=jid, job_type='shell', state='queued', created_at=time.time(),
                       started_at=None, finished_at=None, exit_code=None, log_size=0,
                       log_truncated=False, artifacts=[], manifest={}, execution_ok=False,
                       published_ok=False, expires_at=None, artifacts_truncated=False,
                       timeout_seconds=timeout, idempotency_key=key,
                       caller={'ip': self.client_address[0], 'whois_status': identity['status'], 'tailscale': identity})
            try:
                base = folder(job)
                (base / 'scratch' / 'work').mkdir(parents=True)
                (base / 'scratch').chmod(0o777)
                (base / 'scratch' / 'work').chmod(0o777)
                (base / 'task.sh').write_text(data['script'])
                (base / 'task.sh').chmod(0o644)
                (base / 'log').touch()
                save(job)
                if key:
                    KEYS[key] = {'job_id': jid, 'expires_at': job['created_at'] + 86400}
                    atomic(ROOT / 'idempotency.json', KEYS)
            except OSError as exc:
                if key:
                    KEYS.pop(key, None)
                shutil.rmtree(folder(job), ignore_errors=True)
                return self.send(503, {'error': 'storage_error', 'detail': str(exc)})
            JOBS[jid] = job
            QUEUE.append(jid)
            audit('accepted', job)
            CV.notify_all()
            self.send(202, {'job_id': jid})

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/health':
            status = health()
            return self.send(200 if status['ok'] else 503, status)
        if parsed.path == '/whoami':
            return self.send(200, {'caller_ip': self.client_address[0], 'tailscale': whois(self.client_address[0])})
        parts = parsed.path.split('/')
        with CV:
            job = JOBS.get(parts[2]) if len(parts) >= 3 and parts[1] == 'jobs' else None
            if not job:
                return self.send(404, {'error': 'not_found'})
            if len(parts) == 3:
                return self.send(200, {k: v for k, v in job.items() if k not in ('manifest', 'idempotency_key')})
            offset = 0
            if len(parts) == 4 and parts[3] == 'log':
                try:
                    offset = int(parse_qs(parsed.query).get('offset', ['0'])[0])
                    if offset < 0:
                        raise ValueError()
                except ValueError:
                    return self.send(400, {'error': 'invalid_offset'})
                path = folder(job) / 'log'
                limit = job['log_size']
                content_type = 'text/plain; charset=utf-8'
            elif len(parts) >= 5 and parts[3] == 'artifacts':
                name = unquote('/'.join(parts[4:]))
                entry = job['manifest'].get(name)
                if not entry:
                    return self.send(404, {'error': 'not_in_manifest'})
                path = folder(job) / entry
                limit = path.stat().st_size
                content_type = 'application/octet-stream'
            else:
                return self.send(404, {'error': 'not_found'})
            try:
                inp = path.open('rb')
            except OSError:
                return self.send(404, {'error': 'expired'})
            truncated = job['log_truncated']
        with inp:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(max(0, limit - offset)))
            if parts[3] == 'log' and truncated:
                self.send_header('X-Log-Truncated', 'true')
            self.end_headers()
            inp.seek(offset)
            remaining = max(0, limit - offset)
            while remaining:
                chunk = inp.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_DELETE(self):
        parts = self.path.split('/')
        with CV:
            job = JOBS.get(parts[2]) if len(parts) == 3 and parts[1] == 'jobs' else None
            if not job:
                return self.send(404, {'error': 'not_found'})
            if job['state'] == 'queued':
                QUEUE.remove(job['job_id'])
                finish(job, 'cancelled', 'cancelled')
            elif job['state'] == 'running':
                job['cancel_requested'] = True
                save(job)
                # The worker owns create/start/kill sequencing; avoid a create-vs-kill race.
                done = CV.wait_for(lambda: job['state'] != 'running', timeout=25)
                if not done:
                    return self.send(202, {'job_id': job['job_id'], 'state': 'cancelling'})
            self.send(200, {'job_id': job['job_id'], 'state': job['state']})


def main():
    bind = os.environ.get('FLEET_GATEWAY_BIND_HOST', '')
    if not bind:
        result = command(['tailscale', 'ip', '-4'], 5)
        bind = result.stdout.decode().strip() if result.returncode == 0 else ''
    if not bind:
        sys.exit('No Tailscale IPv4; set FLEET_GATEWAY_BIND_HOST explicitly (fail closed).')
    if CAPACITY < 1 or RESERVE < 0 or RETENTION < 1:
        sys.exit('Invalid queue, reserve, or retention configuration')
    (ROOT / 'jobs').mkdir(parents=True, exist_ok=True)
    # Single process owns a root; prevents a second gateway reconciling live work.
    import fcntl
    lock = (ROOT / 'gateway.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (ROOT / 'idempotency.json').exists():
        KEYS.update(json.loads((ROOT / 'idempotency.json').read_text()))
    for path in (ROOT / 'jobs').glob('*/job.json'):
        job = json.loads(path.read_text())
        JOBS[job['job_id']] = job
        key = job.get('idempotency_key')
        if key:
            KEYS[key] = {'job_id': job['job_id'], 'expires_at': job['created_at'] + 86400}
        if job['state'] in ('queued', 'running'):
            with CV:
                finish(job, 'failed', 'gateway_restart')
    # Do not dispatch while an old container may still be running.
    def start_worker():
        while not sweep_orphans():
            if STOP.wait(2):
                return
        worker()
    server = ThreadingHTTPServer((bind, PORT), Handler)
    server.daemon_threads = True
    def stop(_signum, _frame):
        STOP.set()
        with CV:
            CV.notify_all()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    thread = threading.Thread(target=start_worker, daemon=True)
    thread.start()
    threading.Thread(target=janitor, daemon=True).start()
    print(f'fleet-gateway {VERSION} listening on {bind}:{PORT}', flush=True)
    server.serve_forever()
    thread.join(timeout=30)
    server.server_close()


if __name__ == '__main__':
    main()
