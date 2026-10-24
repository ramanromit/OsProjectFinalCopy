#!/bin/bash
# usage: sudo bash scripts/run_auto_experiment.sh guard|baseline [seconds]
#
# TWO-PHASE ARCHITECTURE:
# Phase 1: Partitioning Phase
#   Spawns realistic diverse processes (games, cloud sync, database, log collector).
#   The processes are detected in the root cgroup, analyzed by the 2-layer classifier,
#   and migrated into cgroups v2 (tg/critical or tg/background).
#
# Phase 2: ThermalGuard Simulation Phase
#   Once all processes are categorized into their cgroups, main.py is started.
#   It begins 1 Hz thermal monitoring and enforces io.max throttling on tg/background!

MODE=${1:-guard}; DUR=${2:-150}
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
DEV=$(grep '^device' config.yaml | cut -d'"' -f2)
[ -n "$DEV" ] || DEV="7:9"

echo "=== PHASE 1: PREPARING ENVIRONMENT & STORAGE ==="
# Reset cgroup limits
echo "$DEV wbps=max" > $CG/background/io.max

# Prepare test storage files
rm -f /mnt/tg/crit /mnt/tg/bg
dd if=/dev/zero of=/mnt/tg/game.dat bs=1M count=180 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/bulk.dat bs=1M count=350 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/db.dat   bs=1M count=120 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/log.dat  bs=1M count=150 oflag=direct status=none

FLAG=""; [ "$MODE" = baseline ] && FLAG="--no-throttle"
export RUNTIME=$((DUR + 15))

echo "=== PHASE 1: SPAWNING WORKLOADS FOR AUTO-PARTITIONING ==="
# Workload 1: Game texture streamer (Rule: "game" -> Critical)
fio --name=game_loader --filename=/mnt/tg/game.dat --size=180M --rw=randread --bs=16k \
    --iodepth=4 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output-format=json --output=results/${MODE}_crit.json &
P_GAME=$!
echo "Spawned Game Asset Loader (PID: $P_GAME)"
sleep 1.8

# Workload 2: Cloud Sync / Backup (Rule: "bulk" -> Background)
fio --name=bulk_sync --filename=/mnt/tg/bulk.dat --size=350M --rw=write --bs=1M \
    --iodepth=8 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output-format=json --output=results/${MODE}_bg.json &
P_BULK=$!
echo "Spawned Bulk Cloud Backup (PID: $P_BULK)"
sleep 1.8

# Workload 3: In-Memory SQL DB query engine (Rule: "mysql" -> Critical)
fio --name=mysql_service --filename=/mnt/tg/db.dat --size=120M --rw=randrw --rwmixread=90 \
    --bs=8k --iodepth=4 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output=/dev/null &
P_SQL=$!
echo "Spawned MySQL Query Engine (PID: $P_SQL)"
sleep 1.8

# Workload 4: Log Rotator & Archiver (Rule: "tar" -> Background)
fio --name=tar_archive --filename=/mnt/tg/log.dat --size=150M --rw=write --bs=512k \
    --iodepth=4 --ioengine=libaio --direct=1 --time_based --runtime=$RUNTIME \
    --output=/dev/null &
P_TAR=$!
echo "Spawned Log Rotator/Tar (PID: $P_TAR)"

echo "=== Awaiting Process Discovery & Cgroups Division Completion ==="
# Allow the 2-layer classifier to detect, inspect, and migrate all 4 processes
sleep 4.5

echo "=== PHASE 2: LAUNCHING THERMALGUARD SIMULATION & CONTROL LOOP ==="
# Now that division into tg/critical and tg/background is established, start the control loop
python3 main.py --out results/$MODE.csv --duration $((DUR+3)) $FLAG &
DPID=$!

# Wait for all workloads and thermal daemon to complete
wait $P_GAME $P_BULK $P_SQL $P_TAR $DPID
echo "Done: $MODE (partitioned and throttled)"
