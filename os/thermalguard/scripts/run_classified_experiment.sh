#!/bin/bash
# usage: sudo bash scripts/run_classified_experiment.sh guard|baseline [seconds]
#
# Unlike run_experiment.sh where the script hardcodes which fio goes in which
# cgroup, this version launches BOTH workloads in the ROOT cgroup and lets
# classifier.py automatically detect and assign them to critical / background
# based on their I/O patterns and process names.
#
# This demonstrates real-life automatic partitioning:
#   1. Classifier sees fio processes appear
#   2. Rule match on fio job names in cmdline (game_like → critical, bulk_copy → background)
#   3. If rules don't match, heuristic kicks in:
#      - read-heavy process  → critical
#      - write-heavy process → background

MODE=${1:-guard}; DUR=${2:-150}
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
CFG=config.yaml

# Reset limits
DEV=$(grep '^device' "$CFG" | cut -d'"' -f2)
echo "$DEV wbps=max" > $CG/background/io.max

# Prepare test files
dd if=/dev/zero of=/mnt/tg/game.dat bs=1M count=256 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/bulk.dat bs=1M count=400 oflag=direct status=none

FLAG=""; [ "$MODE" = baseline ] && FLAG="--no-throttle"

echo "=== Starting classifier daemon (auto-assigns processes to cgroups) ==="
python3 classifier.py --config "$CFG" &
CPID=$!

sleep 1  # give classifier a moment to start

echo "=== Starting ThermalGuard monitor ==="
python3 main.py --config "$CFG" --out results/${MODE}.csv --duration $((DUR+3)) $FLAG &
DPID=$!

echo "=== Launching workloads in root cgroup (classifier will move them) ==="

# NOTE: we do NOT write to cgroup.procs here — the classifier will detect
# these processes and assign them automatically.
export RUNTIME=$DUR

# Critical workload (game-like reads) — launched without cgroup assignment
TG_FILE=/mnt/tg/game.dat fio --output-format=json \
    --output=results/${MODE}_crit.json workloads/game_like.fio &
P1=$!

# Background workload (bulk copy writes) — launched without cgroup assignment
TG_FILE=/mnt/tg/bulk.dat fio --output-format=json \
    --output=results/${MODE}_bg.json workloads/bulk_copy.fio &
P2=$!

echo "=== Workloads running (PIDs: critical=$P1, background=$P2) ==="
echo "=== Classifier will auto-detect and assign them ==="

wait $P1 $P2 $DPID
kill $CPID 2>/dev/null
echo "Done: $MODE (with auto-classification)"

