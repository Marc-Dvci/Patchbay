#!/bin/bash
# Breaks the driver core on purpose, one mutant at a time, in a copy outside the repository, and checks that the
# C unit tests or the cross tests fail for each. A mutant that survives both means a test is missing.
#
#   scripts/mutation_check.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# file @@ original text @@ broken text (literal, not patterns)
mutants=(
    'pb_vring.c@@q->num_free = (uint16_t)(q->num_free + q->chain_len[id]);@@q->num_free = (uint16_t)(q->num_free + q->chain_len[id] - 1u);'
    'pb_vring.c@@flags |= PB_DESC_F_NEXT;@@flags |= 0u;'
    'pb_vring.c@@if (id >= q->size || q->chain_len[id] == 0u) {@@if (id >= q->size) {'
    'pb_vring.c@@if (total > q->num_free) {@@if (total > q->size) {'
    'pb_i2c.c@@uint16_t addr = pb_le16((uint16_t)(s->addr << 1));@@uint16_t addr = pb_le16((uint16_t)(s->addr));'
    'pb_i2c.c@@(i + 1u < n ? PB_I2C_FLAGS_FAIL_NEXT : 0u)@@(0u)'
    'pb_i2c.c@@if (used[i] < r->in.len || status != PB_I2C_MSG_OK) {@@if (status != PB_I2C_MSG_OK) {'
    'pb_spi.c@@if ((qnx_mode & PB_QNX_BORDER_MSB) == 0u) {@@if ((qnx_mode & PB_QNX_BORDER_MSB) != 0u) {'
    'pb_spi.c@@if (cfg->max_freq_hz != 0u && freq_hz > cfg->max_freq_hz) {@@if (0) {'
)
survived=0
# Control: the unmodified core, built the same way, must pass; otherwise every "killed" below means nothing.
cp -r "$REPO/driver/core" "$TMP/core"
cc -O1 -shared -fPIC -std=c11 -I"$TMP/core" -o "$TMP/libmut.so" "$TMP"/core/*.c "$REPO/driver/tests/pb_host_shim.c"
cc -O1 -std=c11 -I"$TMP/core" -o "$TMP/test_mut" "$REPO/driver/tests/test_core.c" "$TMP"/core/*.c
if ! "$TMP/test_mut" >/dev/null || ! (cd "$REPO" && PB_CORE_LIB="$TMP/libmut.so" "$PY" -m pytest -q -x \
    -p no:randomly -p no:cacheprovider tests/cross >/dev/null 2>&1); then
    echo "control failed: the unmodified core does not pass" >&2
    exit 2
fi
echo "control   unmodified core passes"
for m in "${mutants[@]}"; do
    rm -rf "$TMP/core" && cp -r "$REPO/driver/core" "$TMP/core"
    if ! MUT="$m" DIR="$TMP/core" "$PY" - <<'EOF'
import os, sys
file, old, new = os.environ["MUT"].split("@@")
p = os.path.join(os.environ["DIR"], file)
src = open(p).read()
if src.count(old) != 1:
    sys.exit(f"pattern found {src.count(old)} times in {file}: {old}")
open(p, "w").write(src.replace(old, new))
EOF
    then
        survived=$((survived + 1))
        continue
    fi
    cc -O1 -shared -fPIC -std=c11 -I"$TMP/core" -o "$TMP/libmut.so" "$TMP"/core/*.c "$REPO/driver/tests/pb_host_shim.c"
    cc -O1 -std=c11 -I"$TMP/core" -o "$TMP/test_mut" "$REPO/driver/tests/test_core.c" "$TMP"/core/*.c
    label="${m%%@@*}: ${m##*@@}"
    if "$TMP/test_mut" >/dev/null 2>&1 && (cd "$REPO" && PB_CORE_LIB="$TMP/libmut.so" "$PY" -m pytest -q -x \
        -p no:randomly -p no:cacheprovider tests/cross >/dev/null 2>&1); then
        echo "SURVIVED  $label"
        survived=$((survived + 1))
    else
        echo "killed    $label"
    fi
done
echo "${#mutants[@]} mutants, $survived survived"
[ "$survived" -eq 0 ]
