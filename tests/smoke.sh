#!/usr/bin/env bash
set -euo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
if ! docker image inspect fleet-gateway-worker:1 >/dev/null 2>&1; then
    docker build -t fleet-gateway-worker:1 -f "$base/gateway/Dockerfile.worker" "$base/gateway"
fi
python3 "$base/tests/integration.py" smoke
