#!/usr/bin/env bash
set -euo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
out=$(mktemp)
trap 'rm -f "$out"' EXIT
if [ "$(uname -s)" = Linux ] && [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ]; then
        echo 'SKIP: refusal test requires a non-Ubuntu-24.04 host'; exit 0
    fi
fi
if bash "$base/install.sh" --preflight > "$out" 2>&1; then
    echo 'FAIL: unsupported host accepted'; exit 1
fi
grep -q 'Preflight refused:' "$out"
grep -q 'No installation files changed' "$out"
grep -q 'Plan:' "$out"
cat "$out"
echo 'PASS installer refused unsupported host before mutations'
