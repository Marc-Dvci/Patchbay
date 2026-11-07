#!/bin/bash
# All host tests, with counts by kind: no QNX SDP, no guest, no network.
#   scripts/host_tests.sh          (PYTHON names the interpreter with requirements-dev.txt installed)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
make -s -C driver check | tail -n 1
make -s -C driver host >/dev/null
"$PY" -m pytest -q -p no:cacheprovider --cov --cov-branch | grep -E 'TOTAL|passed|failed'
for m in "not cross and not integration and not e2e" cross integration e2e; do
    printf '%-32s %s\n' "$m:" "$("$PY" -m pytest --collect-only -q -p no:randomly -p no:cacheprovider -m "$m" | tail -n 1)"
done
