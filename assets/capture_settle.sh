#!/usr/bin/env bash
# Capture the settling scene for the README GIF.
#
# This is the one scene render_demo.py cannot reproduce on demand: it needs a
# GPFS filesystem and a minute of real drift. Writing the tree and re-stat'ing
# it takes ~2 minutes, so it is captured once and replayed.
#
#   ./assets/capture_settle.sh /scratch/midway3/$USER/sd-demo out.txt
#
# Measured on Midway3 scratch, 2026-08-02, with 6,000 x 8 KiB files:
#   du -s immediately after the write   81 MB
#   du -s once it settled              376 MB      <- 4.6x more
# slurmdisk flags the first reading as still moving instead of reporting it.
set -euo pipefail

TREE=${1:?usage: capture_settle.sh TREE OUTFILE}
OUT=${2:?usage: capture_settle.sh TREE OUTFILE}
REPO=$(cd "$(dirname "$0")/.." && pwd)

rm -rf "$TREE"
mkdir -p "$TREE"

python3 - "$TREE" <<'PY'
import os, sys
root = sys.argv[1]
payload = b"x" * 8192
for i in range(6000):
    d = os.path.join(root, "shard%02d" % (i // 500))
    if i % 500 == 0:
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "part%05d.bin" % i), "wb") as fh:
        fh.write(payload)
PY

# Exit 1 is the expected outcome, not a failure: slurmdisk returns
# EXIT_ATTENTION when the tree is still drifting, which is the entire point of
# this capture. Only 2 (a real error) is fatal here.
rc=0
# The first line is the command the GIF should show above the output, so the
# label can never drift away from the flags -- or the path -- that produced it.
# It was hardcoded to a stand-in path once, and the GIF then showed a command
# that did not match the tree in its own output.
printf '# sd %s -a --no-quota --no-deleted --settle-wait 60\n' "$TREE" >"$OUT"
COLUMNS=96 TERM=xterm-256color PYTHONPATH="$REPO/src" \
    python3 -m slurmdisk "$TREE" -a --no-quota --no-deleted --settle-wait 60 \
    -n 6 --color always --no-progress >>"$OUT" || rc=$?
if [ "$rc" -gt 1 ]; then
    echo "slurmdisk failed with exit $rc" >&2
    exit "$rc"
fi

echo "captured $(wc -l <"$OUT") lines to $OUT (exit $rc)"
echo "du now: $(du -sh "$TREE" | cut -f1)"
