#!/usr/bin/env bash
set -euo pipefail
base=$(cd "$(dirname "$0")" && pwd)
bind=; install_docker=0; lockdown=0; preflight=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --bind) [ "$#" -ge 2 ] || exit 2; bind=$2; shift 2 ;;
        --install-docker) install_docker=1; shift ;;
        --egress-lockdown) lockdown=1; shift ;;
        --preflight) preflight=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
refuse() { echo "Preflight refused: $*. No installation files changed." >&2; echo 'Plan: use Ubuntu 24.04 with systemd, >= 2 GiB free, Docker reachable; rerun with sudo and --bind IP if Tailscale is absent.' >&2; exit 1; }
[ "$(uname -s)" = Linux ] || refuse 'Ubuntu 24.04 is required'
[ -r /etc/os-release ] || refuse 'missing /etc/os-release'
# shellcheck disable=SC1091
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || refuse 'Ubuntu 24.04 is required'
case "$(uname -m)" in x86_64|aarch64) ;; *) refuse 'unsupported architecture' ;; esac
[ "$(id -u)" -eq 0 ] || refuse 'run with sudo'
command -v systemctl >/dev/null && [ -d /run/systemd/system ] || refuse 'systemd is required'
command -v python3 >/dev/null || refuse 'python3 is required'
free=$(df -Pk /var | awk 'NR==2 {print $4}')
[ "$free" -ge 2097152 ] || refuse 'less than 2 GiB free'
command -v tailscale >/dev/null || echo 'Warning: tailscale unavailable; explicit --bind required on first install.' >&2
config=/etc/fleet-gateway/config.env
if [ ! -f "$config" ] && [ -z "$bind" ]; then bind=$(tailscale ip -4 2>/dev/null || true); fi
if [ ! -f "$config" ] && [ -z "$bind" ]; then refuse 'no Tailscale IPv4 (fail closed); pass --bind'; fi
need_docker=0
if ! command -v docker >/dev/null; then
    [ "$install_docker" -eq 1 ] || refuse 'Docker missing; opt in with --install-docker'
    [ "$preflight" -eq 0 ] || refuse 'Docker would be installed with --install-docker'
    need_docker=1
else
    docker info >/dev/null 2>&1 || refuse 'Docker daemon unreachable'
fi
# Validate a complete candidate before replacing any gateway configuration.
candidate=$(mktemp)
trap 'rm -f "$candidate"' EXIT
if [ -f "$config" ]; then cp "$config" "$candidate"; else cp "$base/config.env.example" "$candidate"; fi
python3 - "$candidate" "$bind" <<'PY'
import ipaddress, pathlib, sys
p = pathlib.Path(sys.argv[1]); text = p.read_text()
if sys.argv[2]:
    text = '\n'.join('FLEET_GATEWAY_BIND_HOST=' + sys.argv[2] if line.startswith('FLEET_GATEWAY_BIND_HOST=') else line for line in text.splitlines()) + '\n'
values = {}
for line in text.splitlines():
    if not line.strip() or line.lstrip().startswith('#'): continue
    key, value = line.split('=', 1)
    if key in values: raise SystemExit('duplicate config key: ' + key)
    values[key] = value
ipaddress.IPv4Address(values['FLEET_GATEWAY_BIND_HOST'])
for key, low, high in [('FLEET_GATEWAY_PORT',1,65535),('FLEET_GATEWAY_QUEUE_CAPACITY',1,100000),('FLEET_GATEWAY_DISK_RESERVE',0,2**63),('FLEET_GATEWAY_RETENTION_SECONDS',1,2**31)]:
    if not low <= int(values[key]) <= high: raise SystemExit('invalid ' + key)
for key in ('WORKER_ROOT', 'FLEET_GATEWAY_AUDIT'):
    if not pathlib.Path(values[key]).is_absolute(): raise SystemExit(key + ' must be absolute')
p.write_text(text)
PY
if [ "$preflight" -eq 1 ]; then echo 'Preflight passed. Plan: build image, install gateway/unit/config/logrotate, enable service.'; exit 0; fi
if [ "$need_docker" -eq 1 ]; then
    apt-get update
    apt-get install -y docker.io
    systemctl enable --now docker
    docker info >/dev/null 2>&1 || { echo 'Installed Docker, but daemon is unreachable.' >&2; exit 1; }
fi
docker build -t fleet-gateway-worker:1 -f "$base/gateway/Dockerfile.worker" "$base/gateway"
install -d /opt/fleet-gateway /etc/fleet-gateway /var/log/fleet-gateway /var/lib/fleet-gateway
install -m 644 "$base/gateway/fleet_gateway.py" /opt/fleet-gateway/fleet_gateway.py
install -m 644 "$base/systemd/fleet-gateway.service" /etc/systemd/system/fleet-gateway.service
install -m 644 "$candidate" /etc/fleet-gateway/config.env.new
mv /etc/fleet-gateway/config.env.new "$config"
install -m 755 "$base/client/fleet-run" /usr/local/bin/fleet-run
# Preserve operator daemon settings; defaults apply to subsequently created containers.
install -d /etc/docker
python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path('/etc/docker/daemon.json')
data = json.loads(p.read_text()) if p.exists() else {}
if data.get('log-driver', 'json-file') == 'json-file':
    data.setdefault('log-driver', 'json-file')
    opts = data.setdefault('log-opts', {})
    opts.setdefault('max-size', '10m')
    opts.setdefault('max-file', '3')
    tmp = p.with_suffix('.json.fleet-gateway.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n'); os.replace(tmp,p)
    pathlib.Path('/etc/fleet-gateway/docker-log-defaults-added').touch()
    print('Added daemon log defaults; effective after the next operator-scheduled Docker restart.')
PY
audit_path=$(sed -n 's/^FLEET_GATEWAY_AUDIT=//p' "$config")
cat > /etc/logrotate.d/fleet-gateway <<EOF
"$audit_path" {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
if [ "$lockdown" -eq 1 ]; then
    command -v nft >/dev/null || apt-get install -y nftables
    install -m 644 "$base/systemd/fleet-gateway-egress.nft" /etc/fleet-gateway/egress.nft
    install -m 644 "$base/systemd/fleet-gateway-egress.service" /etc/systemd/system/fleet-gateway-egress.service
    systemctl daemon-reload
    systemctl enable fleet-gateway-egress.service
    systemctl restart fleet-gateway-egress.service
fi
systemctl daemon-reload
systemctl enable fleet-gateway.service
systemctl restart fleet-gateway.service
systemctl --no-pager status fleet-gateway.service
bind=$(sed -n 's/^FLEET_GATEWAY_BIND_HOST=//p' "$config")
port=$(sed -n 's/^FLEET_GATEWAY_PORT=//p' "$config")
echo "Verify: curl --max-time 15 http://$bind:$port/health"
