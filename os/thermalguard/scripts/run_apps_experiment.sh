#!/bin/bash
# usage: sudo bash scripts/run_apps_experiment.sh apps_baseline|apps_guard [seconds]
# New mode. Writes only results/apps_* files. Does not touch baseline/guard results.
MODE=${1:-apps_baseline}; DUR=${2:-150}
case "$MODE" in
  apps_baseline|apps_guard) ;;
  *) echo "mode must be apps_baseline or apps_guard"; exit 1 ;;
esac
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
CFG=${CONFIG:-config_apps.yaml}
[ -f "$CFG" ] || { echo "missing $CFG (run: cp config.yaml config_apps.yaml)"; exit 1; }
DEV=$(grep '^device' "$CFG" | cut -d'"' -f2)
[ -n "$DEV" ] || { echo "could not read device from $CFG"; exit 1; }
[ -d /mnt/tg ] && [ -d $CG/critical ] && [ -d $CG/background ] || { echo "test disk or cgroups missing, redo setup"; exit 1; }
mkdir -p results
echo "$DEV wbps=max" > $CG/background/io.max
# the 1 GB test disk cannot hold the old and new files together; the old script recreates its own
rm -f /mnt/tg/crit /mnt/tg/bg
dd if=/dev/zero of=/mnt/tg/game.dat bs=1M count=256 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/bulk.dat bs=1M count=400 oflag=direct status=none
FLAG=""; [ "$MODE" = apps_baseline ] && FLAG="--no-throttle"
python3 main.py --config "$CFG" --out results/$MODE.csv --duration $((DUR+3)) $FLAG &
DPID=$!
export RUNTIME=$DUR
bash -c "echo \$\$ > $CG/critical/cgroup.procs; TG_FILE=/mnt/tg/game.dat exec fio --output-format=json --output=results/${MODE}_crit.json workloads/game_like.fio" &
P1=$!
bash -c "echo \$\$ > $CG/background/cgroup.procs; TG_FILE=/mnt/tg/bulk.dat exec fio --output-format=json --output=results/${MODE}_bg.json workloads/bulk_copy.fio" &
P2=$!
wait $P1 $P2 $DPID
echo "Done: $MODE"
