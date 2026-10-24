#!/bin/bash
# usage: sudo bash scripts/run_auto_experiment.sh guard|baseline [seconds]
#
# Same as run_experiment.sh BUT does NOT assign processes to cgroups.
# The classifier running inside dashboard.py detects and assigns them
# automatically based on rules + I/O heuristics.

MODE=${1:-guard}; DUR=${2:-150}
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
DEV=$(grep '^device' config.yaml | cut -d'"' -f2)
[ -n "$DEV" ] || DEV="7:9"

# Reset limits
echo "$DEV wbps=max" > $CG/background/io.max

# Prepare test files with app-style names
rm -f /mnt/tg/crit /mnt/tg/bg
dd if=/dev/zero of=/mnt/tg/game.dat bs=1M count=256 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/bulk.dat bs=1M count=400 oflag=direct status=none

FLAG=""; [ "$MODE" = baseline ] && FLAG="--no-throttle"

# Start ThermalGuard monitor
python3 main.py --out results/$MODE.csv --duration $((DUR+3)) $FLAG &
DPID=$!

# === KEY DIFFERENCE: no "echo $$ > cgroup.procs" here ===
# Both workloads launch in the ROOT cgroup.
# The classifier thread in dashboard.py will detect them and move them.

export RUNTIME=$DUR

# Critical workload (game-like random reads) — no cgroup pre-assignment
TG_FILE=/mnt/tg/game.dat fio --output-format=json \
    --output=results/${MODE}_crit.json workloads/game_like.fio &
P1=$!

# Background workload (bulk sequential writes) — no cgroup pre-assignment
TG_FILE=/mnt/tg/bulk.dat fio --output-format=json \
    --output=results/${MODE}_bg.json workloads/bulk_copy.fio &
P2=$!

echo "Workloads launched (crit=$P1 bg=$P2) — awaiting classifier assignment"
wait $P1 $P2 $DPID
echo "Done: $MODE (auto-classified)"

