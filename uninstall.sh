#!/usr/bin/env bash
set -euo pipefail
purge=0
case "${1:-}" in --purge) purge=1 ;; '') ;; *) echo 'Usage: uninstall.sh [--purge]' >&2; exit 2 ;; esac
[ "$(uname -s)" = Linux ] && [ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the Linux gateway host.' >&2; exit 1; }
root=/var/lib/fleet-gateway
log=/var/log/fleet-gateway/runs.jsonl
if [ -f /etc/fleet-gateway/config.env ]; then
    root=$(sed -n 's/^WORKER_ROOT=//p' /etc/fleet-gateway/config.env)
    log=$(sed -n 's/^FLEET_GATEWAY_AUDIT=//p' /etc/fleet-gateway/config.env)
fi
systemctl disable --now fleet-gateway.service 2>/dev/null || true
systemctl disable --now fleet-gateway-egress.service 2>/dev/null || true
# Refuse to report clean removal if Docker cannot confirm owned resources.
ids=$(docker ps -aq --filter label=fleet-gateway.job)
for cid in $ids; do docker rm -f "$cid"; done
images=$(docker images -q --filter label=fleet-gateway.image=worker-v1 | sort -u)
for image in $images; do docker image rm "$image"; done
rm -f /etc/systemd/system/fleet-gateway.service /etc/systemd/system/fleet-gateway-egress.service /etc/logrotate.d/fleet-gateway /usr/local/bin/fleet-run
rm -f /opt/fleet-gateway/fleet_gateway.py /etc/fleet-gateway/config.env /etc/fleet-gateway/egress.nft /etc/fleet-gateway/docker-log-defaults-added
rmdir /opt/fleet-gateway /etc/fleet-gateway 2>/dev/null || true
systemctl daemon-reload
if [ "$purge" -eq 1 ]; then
    # Delete only gateway-owned records; never recursively delete a configured root.
    python3 - "$root" "$log" <<'PY'
import json, pathlib, shutil, sys
root = pathlib.Path(sys.argv[1])
for record in (root / 'jobs').glob('*/job.json'):
    job = json.loads(record.read_text())
    if job.get('job_id') == record.parent.name and job.get('job_type') == 'shell':
        shutil.rmtree(record.parent)
for name in ('idempotency.json','gateway.lock'):
    (root / name).unlink(missing_ok=True)
pathlib.Path(sys.argv[2]).unlink(missing_ok=True)
PY
fi
echo 'Uninstalled owned gateway resources. Data retained unless --purge; shared Docker daemon settings preserved.'
