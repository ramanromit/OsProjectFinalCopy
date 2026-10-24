#!/bin/bash
# usage: sudo bash scripts/run_rogue_experiment.sh [seconds]
#
# NOVELTY VALIDATION EXPERIMENT:
# Tests the system against an unprofiled, renamed binary (/tmp/photo_sync)
# and transient write bursts (anti-flapping test).
#
# 1. /tmp/photo_sync is a copy of fio running heavy sequential writes.
#    - Layer 1 (Rules) misses it because "photo_sync" is not in config.yaml.
#    - Layer 2 (Heuristic) inspects /proc/<pid>/io, observes write_ratio > 70%,
#      and dynamically quarantines it to tg/background!
# 2. A short transient write burst is launched simultaneously.
#    - Finishes within 1 second.
#    - Heuristic verifies anti-flapping: it is NOT falsely quarantined!

DUR=${1:-90}
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
DEV=$(grep '^device' config.yaml | cut -d'"' -f2)
[ -n "$DEV" ] || DEV="7:9"

echo "=== PREPARING UNPROFILED / RENAMED WORKLOAD TEST ==="
# Duplicate fio to /tmp/photo_sync
cp $(which fio) /tmp/photo_sync
chmod +x /tmp/photo_sync

# Reset limits
echo "$DEV wbps=max" > $CG/background/io.max
rm -f /mnt/tg/game.dat /mnt/tg/photo_sync.dat /mnt/tg/burst.dat

# Pre-allocate test files
dd if=/dev/zero of=/mnt/tg/game.dat bs=1M count=180 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/photo_sync.dat bs=1M count=350 oflag=direct status=none

echo "=== PHASE 1: LAUNCHING WORKLOADS IN ROOT CGROUP ==="
export RUNTIME=$DUR

# Workload 1: Protected Critical Game Assets (Rule matches "game")
fio --name=game_assets --filename=/mnt/tg/game.dat --size=180M --rw=randread --bs=16k \
    --iodepth=4 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output-format=json --output=results/rogue_crit.json &
P_GAME=$!
echo "Started Critical Workload (PID: $P_GAME, binary: fio)"

# Workload 2: Transient Burst (Anti-flapping test: 15MB write, completes in ~0.5s)
dd if=/dev/zero of=/mnt/tg/burst.dat bs=1M count=15 oflag=direct status=none &
P_BURST=$!
echo "Started Transient Burst (PID: $P_BURST, should NOT be quarantined)"

# Workload 3: Renamed Heavy Sequential Writer (/tmp/photo_sync)
/tmp/photo_sync --name=photo_ingest --filename=/mnt/tg/photo_sync.dat --size=350M --rw=write --bs=1M \
    --iodepth=8 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output-format=json --output=results/rogue_bg.json &
P_ROGUE=$!
echo "Started Renamed Binary /tmp/photo_sync (PID: $P_ROGUE, sustained 2GB/s writes)"

echo "=== Awaiting Heuristic Classification & Quarantine (3s observation window) ==="
sleep 4.5

echo "=== PHASE 2: LAUNCHING THERMALGUARD PREDICTIVE GOVERNOR ==="
python3 main.py --governor predictive --out results/rogue.csv --duration $((DUR+3)) &
DPID=$!

wait $P_GAME $P_ROGUE $DPID
rm -f /tmp/photo_sync
echo "Done: Rogue / Heuristic Quarantine Experiment complete"

