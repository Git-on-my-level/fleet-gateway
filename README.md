# Fleet Gateway

Run explicit shell scripts on a shared disposable Docker worker over your private
Tailscale network. Agents get a durable job ID, logs, and downloadable artifacts;
adding another task node uses the same installer and configuration.

## Quickstart (Ubuntu 24.04 task node)

Run these from this repository. Docker must already be installed and reachable;
use `--install-docker` explicitly if you want the installer to install it.

```bash
sudo ./install.sh
export FLEET_GATEWAY_HOST="http://$(tailscale ip -4):9494"
./client/fleet-run health
./client/fleet-run run 'echo hello-from-fleet > work/hi.txt; echo done'
ls ./fleet-run-artifacts-*/hi.txt
```

```text
Fleet agent -- Tailscale ACL --> :9494 tokenless HTTP gateway
                                  | durable FIFO (8 waiting)
                                  v
                            one disposable Docker worker
                                  | /work/work/*
                                  v
                      immutable artifacts + capped log + JSONL audit
```

## Add a new task box in 5 minutes

1. Provision Ubuntu 24.04 (amd64/arm64), join Tailscale, and allow only intended
   callers to reach TCP 9494 in the tailnet ACL. Allow enough disk for the image
   build and jobs; admission reserves 2 GiB.
2. Copy/clone this repository onto the host. Run `sudo ./install.sh`, or
   `sudo ./install.sh --install-docker` if Docker is absent. First image build
   can take more than five minutes on a slow connection.
3. The installer resolves the host's Tailscale IPv4. With no address it refuses;
   `--bind IP` is an explicit override. Review `/etc/fleet-gateway/config.env`.
4. Run the printed health command, then set `FLEET_GATEWAY_HOST=http://IP:9494`
   on callers and copy `client/fleet-run` onto their PATH.
5. Submit a script and inspect its artifacts. Re-run the installer to upgrade;
   existing config is preserved unless `--bind` explicitly changes the address.

`sudo ./install.sh --preflight` validates without installation. The installer
supports only Ubuntu 24.04; do not install on macOS. It installs systemd,
logrotate, the image, and client. Missing Docker json-file log-size defaults are added while preserving existing
settings and other log drivers; they take effect on the next operator-scheduled
Docker restart (the installer does not interrupt unrelated containers). Gateway
containers also receive explicit per-container log limits immediately.

## Client and API

Requires Bash 3.2+, curl, tar, and Python 3. All curl requests have a 40-second
limit. Run waits and polls every five seconds, prints progress/log tail/exit code
to stderr, and extracts a validated archive into a fresh
`./fleet-run-artifacts-<id>/`. `--json` prints the terminal status as JSON.

```bash
fleet-run run 'git --version; echo output > work/result.txt' --timeout 120
fleet-run run 'sleep 10; echo done' --no-wait --host http://100.64.0.1:9494
fleet-run status JOB_ID
fleet-run log JOB_ID
fleet-run cancel JOB_ID
fleet-run whoami
```

Exit codes: 0 successful operation/job, 1 failed/timed-out/cancelled job, 2
usage/transport/HTTP error. `status` returns one current snapshot for reconnecting;
`log` downloads current log bytes. A cancel response can be `202 cancelling` if
Docker cleanup is still pending; poll status until terminal.

- `POST /jobs`: JSON `script`, optional `timeout_seconds` (1–3600, default 900),
  `idempotency_key` (1–256 characters), `job_type` (only `shell`). Returns 202
  with `job_id`. Natural-language prompt fields are not execution inputs.
- `GET /jobs/ID`: state, timestamps (Unix seconds), caller, exit code, log size,
  artifacts, `execution_ok`, `published_ok`, `expires_at`, structured errors.
- `GET /jobs/ID/log?offset=0`: bytes; `X-Log-Truncated: true` when capped at 8 MiB.
- `GET /jobs/ID/artifacts/NAME`: exact manifest name, including `work.tar.gz`.
- `DELETE /jobs/ID`: cancels queued or running work; removal confirmed before
  releasing the worker.
- `GET /health`: daemon/image/disk checks and worker/queue state. Unhealthy is 503.
- `GET /whoami`: TCP peer plus best-effort Tailscale whois; no forwarded headers.

Queue full returns 429; unavailable Docker/image or low disk returns 503 with
`Retry-After: 5`. Body limit is 1 MiB, socket read timeout 15 seconds. Persisted
jobs precede the 202 response. On restart queued and running jobs fail with
`gateway_restart`; no replay. Idempotency keys are gateway-wide, first request
wins, retained for 24 hours even after artifacts expire. Use unique keys per
logical operation. A duplicate can return an expired job's ID (status 404).

Scripts run as non-root in `/work`; put outputs in the existing `work/` subdir
(`/work/work`). The task script is copied separately to `/task.sh`. Publication
copies regular files to a private snapshot after container removal; hidden-prefix
entries, symlinks, FIFOs, and devices are excluded and counted. `work.tar.gz`
is reserved. Tar bytes before gzip are capped at 512 MiB, including tar metadata;
overflow drops publication, keeps logs, and sets `artifacts_truncated`.
`execution_ok` describes the script, `published_ok` the artifact publication.
A successful script with failed publication has state `failed` and
`execution_ok: true`. A janitor runs every 10 seconds, expiring data one hour
after completion; orphan labeled containers are swept at startup and hourly. An additional owner
label derived from the data-root path prevents separate gateway instances from
cleaning up each other's workers. Keep that path stable across restarts.

## Purpose-built task nodes

The gateway is role-agnostic: it runs whatever container image the node is
configured with. To turn a fresh VM into a special-purpose task box (media
transcoding, PDF pipelines, scraping, terraform runs, ...), keep the gateway
code untouched and ship a purpose image:

```dockerfile
# Dockerfile.media  (example: media task node)
FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates python3 && rm -rf /var/lib/apt/lists/*
RUN useradd -m worker
USER worker
WORKDIR /work
```

```bash
docker build -f Dockerfile.media -t fleet-gateway-worker:media .
# in /etc/fleet-gateway/config.env:
FLEET_GATEWAY_IMAGE=fleet-gateway-worker:media
```

Then run `install.sh` (it detects and keeps your configured image), and point
clients at the node: `FLEET_GATEWAY_HOST=http://<node>:9494 fleet-run run '...'`.
Each node keeps its own queue, caps, audit log, and identity. Scripts stay
plain shell — only the image changes per purpose. For a second workload on
the same VM, run a second gateway instance with a different port and image
via a copied unit file; no code changes needed.

## Security and operations

**The tailnet ACL is THE gate.** This service is tokenless and has no application
auth or TLS. All authorized callers can see/cancel all jobs; node identity cannot
distinguish agents sharing a machine. Never expose the listener publicly.

Containers provide convenience isolation, **not a hostile-code boundary**.
Memory/swap 2 GiB, 2 CPUs, 256 PIDs, non-root UID 10001, all capabilities dropped,
no-new-privileges, and Docker's default seccomp limit accidents, not attacks.
Only per-job scratch is mounted; neither the Docker socket nor gateway metadata
is mounted. Host disk consumption while a script runs is not quota-limited;
use a dedicated task host/filesystem. Artifact access uses exact immutable
manifest entries. Audit events record TCP peer and whois status in JSONL.

Containers have internet access by default. Optional
[egress lockdown](docs/egress-lockdown.md) blocks private/tailnet destinations;
apply only with `--egress-lockdown`. These rules affect all default Docker bridges
on that host, so a dedicated task host is recommended.

Configuration knobs are documented in [config.env.example](config.env.example).
Use `journalctl -u fleet-gateway -f` and `/var/log/fleet-gateway/runs.jsonl`.
A Docker outage keeps an occupied worker until container removal can be proven.

`sudo ./uninstall.sh` removes gateway-owned files, labeled containers and images,
leaving data. `--purge` also removes recognized job records/artifacts and the audit
file. Shared Docker installation/settings remain in place.

## Development / verification

On macOS or Linux with Docker, tests build and reuse `fleet-gateway-worker:1`,
choose a free localhost port, use temporary state, and remove their own containers.

```bash
python3 -m py_compile gateway/fleet_gateway.py tests/integration.py
for file in install.sh uninstall.sh client/fleet-run tests/*.sh; do bash -n "$file"; done
tests/smoke.sh
tests/timeout-test.sh
tests/installer-dryrun.sh
```

To start manually without installing, set `FLEET_GATEWAY_BIND_HOST=127.0.0.1`,
`WORKER_ROOT="$PWD/scratch"`, `FLEET_GATEWAY_AUDIT="$PWD/scratch/runs.jsonl"`, and
optionally `FLEET_GATEWAY_PORT`, then run `python3 gateway/fleet_gateway.py`.
