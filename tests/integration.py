#!/usr/bin/env python3
"""Real Docker acceptance checks, with isolated gateway state."""
import gzip
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
MODE = sys.argv[1]
TMP = Path(tempfile.mkdtemp(prefix='fleet-gateway-test-'))
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    PORT = sock.getsockname()[1]
HOST = f'http://127.0.0.1:{PORT}'
ENV = {**os.environ, 'WORKER_ROOT': str(TMP / 'data'), 'FLEET_GATEWAY_AUDIT': str(TMP / 'audit.jsonl'),
       'FLEET_GATEWAY_BIND_HOST': '127.0.0.1', 'FLEET_GATEWAY_PORT': str(PORT),
       'FLEET_GATEWAY_QUEUE_CAPACITY': '1', 'FLEET_GATEWAY_HOST': HOST}
LOG = (TMP / 'server.log').open('w')
SERVER = None
IDS = []


def request(method, path, body=None, expected=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HOST + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req, timeout=40)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        assert response.status == expected, (method, path, response.status, raw)
        return raw, dict(response.headers)


def obj(method, path, body=None, expected=200):
    return json.loads(request(method, path, body, expected)[0])


def submit(script, timeout=60, key=None):
    body = {'script': script, 'timeout_seconds': timeout}
    if key:
        body['idempotency_key'] = key
    jid = obj('POST', '/jobs', body, 202)['job_id']
    IDS.append(jid)
    return jid


def wait(jid, state=None):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        job = obj('GET', '/jobs/' + jid)
        if (state and job['state'] == state) or (not state and job['state'] not in ('queued', 'running')):
            return job
        time.sleep(.2)
    raise AssertionError(('wait expired', job))


def gone(jid):
    result = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=fleet-gateway.job=' + jid], capture_output=True, check=True)
    assert not result.stdout.strip(), result.stdout


def start():
    global SERVER
    SERVER = subprocess.Popen([sys.executable, str(REPO / 'gateway/fleet_gateway.py')], env=ENV, stdout=LOG, stderr=LOG)
    for _ in range(100):
        try:
            health = obj('GET', '/health')
            assert health['ok']
            return
        except (OSError, AssertionError):
            if SERVER.poll() is not None:
                raise AssertionError('gateway exited')
            time.sleep(.2)
    raise AssertionError('gateway startup timeout')


def client(*args, expected=0):
    result = subprocess.run(['/bin/bash', str(REPO / 'client/fleet-run'), *args], env=ENV,
                            cwd=TMP, capture_output=True, text=True, timeout=100)
    assert result.returncode == expected, (args, result.stdout, result.stderr)
    return result.stdout


try:
    closed_env = dict(ENV)
    closed_env.pop('FLEET_GATEWAY_BIND_HOST')
    closed_env['PATH'] = '/nonexistent'
    closed = subprocess.run([sys.executable, str(REPO / 'gateway/fleet_gateway.py')],
                            env=closed_env, capture_output=True, timeout=10)
    assert closed.returncode != 0 and b'fail closed' in closed.stderr
    print('PASS bind fails closed without Tailscale or explicit override', flush=True)
    start()
    if MODE == 'timeout':
        jid = submit('sleep 300', timeout=5)
        job = wait(jid)
        assert job['state'] == 'timed_out', job
        assert not job['execution_ok']
        gone(jid)
        print('PASS timeout: timed_out after 5s; container confirmed absent')
    else:
        health = obj('GET', '/health')
        assert {'ok','workers','queue_length','docker_ok','image_ok','disk_free_bytes','version'} <= health.keys()
        assert json.loads(client('health'))['ok']
        assert obj('GET', '/whoami')['caller_ip'] == '127.0.0.1'
        request('POST', '/jobs', {'prompt': 'do something'}, 400)
        request('POST', '/jobs', {'script': 'true', 'timeout_seconds': True}, 400)
        conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=15)
        conn.putrequest('POST', '/jobs')
        conn.putheader('Content-Length', str(1024**2 + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()
        jid = submit('echo hello-from-fleet > work/hi.txt\necho log-from-fleet\nln -s /etc/passwd work/leak\nmkfifo work/pipe\nmkdir work/nested\necho nested > work/nested/file', key='dedupe-test')
        duplicate = submit('echo should-not-run', key='dedupe-test')
        assert duplicate == jid
        job = wait(jid)
        assert job['state'] == 'succeeded' and job['execution_ok'] and job['published_ok'], job
        assert {'hi.txt', 'nested/file', 'work.tar.gz'} == set(job['artifacts']), job
        assert job['artifacts_skipped'] == 2
        assert job['expires_at'] - job['finished_at'] == 3600
        assert 'log' not in job
        assert request('GET', f'/jobs/{jid}/artifacts/hi.txt')[0] == b'hello-from-fleet\n'
        assert request('GET', f'/jobs/{jid}/log?offset=4')[0] == b'from-fleet\n'
        request('GET', f'/jobs/{jid}/artifacts/leak', expected=404)
        request('GET', f'/jobs/{jid}/artifacts/..%2Fjob.json', expected=404)
        gzip.decompress(request('GET', f'/jobs/{jid}/artifacts/work.tar.gz')[0])
        events = [json.loads(line) for line in (TMP / 'audit.jsonl').read_text().splitlines()]
        assert any(e['event'] == 'finished' and e['job_id'] == jid and e['caller']['ip'] == '127.0.0.1' for e in events)
        gone(jid)
        print('PASS health/client health, explicit-shell validation, body cap, artifacts/symlink/FIFO/traversal, log offset, audit, idempotency')
        active = submit('echo ready; sleep 300', timeout=60)
        wait(active, 'running')
        for _ in range(100):
            info = subprocess.run(['docker', 'inspect', 'fleet-gateway-' + active], capture_output=True)
            if info.returncode == 0 and json.loads(info.stdout)[0]['State']['Running']:
                container = json.loads(info.stdout)[0]
                break
            time.sleep(.1)
        else: raise AssertionError('container did not become running')
        caps = container['HostConfig']
        assert caps['Memory'] == 2*1024**3 and caps['MemorySwap'] == 2*1024**3
        assert caps['NanoCpus'] == 2*10**9 and caps['PidsLimit'] == 256
        assert caps['CapDrop'] == ['ALL'] and 'no-new-privileges' in caps['SecurityOpt']
        assert container['Config']['User'] == 'worker' and container['Config']['WorkingDir'] == '/work'
        assert len(container['Mounts']) == 1 and container['Mounts'][0]['Destination'] == '/work'
        queued = submit('echo queued')
        _, headers = request('POST', '/jobs', {'script':'echo full'}, 429)
        assert headers['Retry-After'] == '5'
        cancelled = obj('DELETE', '/jobs/' + active)
        assert cancelled['state'] == 'cancelled', cancelled
        gone(active)
        assert wait(queued)['state'] == 'succeeded'
        print('PASS finite FIFO/429/Retry-After, running DELETE kills container and frees worker')
        jid = submit("python3 -c 'import sys; sys.stdout.write(\"x\" * (9*1024*1024))'")
        assert wait(jid)['log_truncated']
        raw, headers = request('GET', '/jobs/' + jid + '/log')
        assert len(raw) == 8*1024*1024 and headers['X-Log-Truncated'] == 'true'
        print('PASS hard log cap and truncation header')
        jid = submit('truncate -s 536870913 work/large; echo preserved-log')
        job = wait(jid)
        assert job['execution_ok'] and not job['published_ok'] and job['artifacts_truncated'], job
        assert job['state'] == 'failed' and job['artifacts'] == []
        assert b'preserved-log' in request('GET', '/jobs/' + jid + '/log')[0]
        request('GET', f'/jobs/{jid}/artifacts/work.tar.gz', expected=404)
        jid = submit('rmdir work; ln -s /etc work')
        job = wait(jid)
        assert job['execution_ok'] and not job['published_ok'] and job['artifacts'] == [], job
        print('PASS 512 MiB artifact overflow, separate execution/publication result, root symlink refusal')
        result = json.loads(client('run', 'echo client-log; echo client-artifact > work/client.txt', '--json'))
        assert result['execution_ok']
        assert (TMP / ('fleet-run-artifacts-' + result['job_id']) / 'client.txt').read_text() == 'client-artifact\n'
        client('run', 'exit 7', expected=1)
        nw = client('run', 'echo no-wait', '--no-wait').strip()
        wait(nw)
        assert json.loads(client('status', nw))['state'] == 'succeeded'
        print('PASS bash client wait/download/extract, failed exit code, no-wait and reconnect')
        active = submit('echo restart-test; sleep 300', key='restart-key')
        wait(active, 'running')
        # Wait for actual container execution before crash injection.
        for _ in range(100):
            if b'restart-test' in request('GET', '/jobs/' + active + '/log')[0]: break
            time.sleep(.1)
        else: raise AssertionError('container did not start')
        queued = submit('echo must-not-replay')
        SERVER.kill(); SERVER.wait()
        start()
        for interrupted in (active, queued):
            job = wait(interrupted)
            assert job['state'] == 'failed' and job['reason']['code'] == 'gateway_restart', job
        for _ in range(100):
            try: gone(active); break
            except AssertionError: time.sleep(.2)
        else: raise AssertionError('restart orphan remains')
        assert submit('echo not-repeated', key='restart-key') == active
        assert wait(submit('echo recovered'))['state'] == 'succeeded'
        print('PASS durable restart reconciliation, no replay, orphan cleanup and persistent dedupe')
        # Validate outage admission without stopping the shared host daemon.
        SERVER.terminate(); SERVER.wait(timeout=40)
        ENV['FLEET_GATEWAY_IMAGE'] = 'fleet-gateway-image-that-does-not-exist:1'
        SERVER = subprocess.Popen([sys.executable, str(REPO / 'gateway/fleet_gateway.py')], env=ENV, stdout=LOG, stderr=LOG)
        for _ in range(100):
            try:
                bad = obj('GET', '/health', expected=503)
                break
            except OSError: time.sleep(.1)
        assert bad['docker_ok'] and not bad['image_ok']
        request('POST', '/jobs', {'script':'true'}, 503)
        SERVER.terminate(); SERVER.wait(timeout=40)
        ENV['FLEET_GATEWAY_IMAGE'] = 'fleet-gateway-worker:1'
        ENV['DOCKER_HOST'] = 'unix://' + str(TMP / 'missing-docker.sock')
        SERVER = subprocess.Popen([sys.executable, str(REPO / 'gateway/fleet_gateway.py')], env=ENV, stdout=LOG, stderr=LOG)
        for _ in range(100):
            try:
                bad = obj('GET', '/health', expected=503)
                break
            except OSError: time.sleep(.1)
        assert not bad['docker_ok']
        request('POST', '/jobs', {'script':'true'}, 503)
        SERVER.terminate(); SERVER.wait(timeout=40)
        ENV.pop('DOCKER_HOST')
        ENV['FLEET_GATEWAY_DISK_RESERVE'] = str(2**62)
        SERVER = subprocess.Popen([sys.executable, str(REPO / 'gateway/fleet_gateway.py')], env=ENV, stdout=LOG, stderr=LOG)
        for _ in range(100):
            try:
                bad = obj('GET', '/health', expected=503)
                break
            except OSError: time.sleep(.1)
        assert bad['docker_ok'] and bad['image_ok'] and not bad['ok']
        request('POST', '/jobs', {'script':'true'}, 503)
        SERVER.terminate(); SERVER.wait(timeout=40)
        ENV.pop('FLEET_GATEWAY_DISK_RESERVE')
        ENV['FLEET_GATEWAY_RETENTION_SECONDS'] = '2'
        start()
        jid = submit('sleep 3; echo retained', key='expiry-key')
        job = wait(jid)
        assert job['finished_at'] - job['created_at'] >= 3
        assert job['expires_at'] - job['finished_at'] == 2
        # Creation-age expiry would have removed this job before completion.
        for _ in range(100):
            if not (TMP / 'data' / 'jobs' / jid).exists(): break
            time.sleep(.2)
        else: raise AssertionError('janitor did not expire completed data')
        request('GET', '/jobs/' + jid, expected=404)
        assert submit('echo must-not-run', key='expiry-key') == jid
        print('PASS missing-image/daemon/disk 503, completion-age janitor, dedupe outlives artifacts')
    print('ALL ' + MODE.upper() + ' CHECKS PASSED')
finally:
    if SERVER and SERVER.poll() is None:
        SERVER.terminate()
        try: SERVER.wait(timeout=40)
        except subprocess.TimeoutExpired: SERVER.kill(); SERVER.wait()
    for jid in IDS:
        result = subprocess.run(['docker','ps','-aq','--filter','label=fleet-gateway.job=' + jid], capture_output=True)
        for cid in result.stdout.decode().split():
            subprocess.run(['docker','rm','-f',cid], capture_output=True)
    LOG.close()
    if sys.exc_info()[0]:
        print((TMP / 'server.log').read_text(), file=sys.stderr)
        print('Failure evidence: ' + str(TMP), file=sys.stderr)
    else:
        shutil.rmtree(TMP)
