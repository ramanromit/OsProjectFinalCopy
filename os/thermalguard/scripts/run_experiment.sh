#!/bin/bash
# usage: sudo bash scripts/run_experiment.sh baseline|guard [seconds]
MODE=${1:-baseline}; DUR=${2:-150}
cd "$(dirname "$0")/.."
CG=/sys/fs/cgroup/tg
echo "7:9 wbps=max" > $CG/background/io.max
rm -f /mnt/tg/game.dat /mnt/tg/bulk.dat
dd if=/dev/zero of=/mnt/tg/crit bs=1M count=200 oflag=direct status=none
dd if=/dev/zero of=/mnt/tg/bg bs=1M count=300 oflag=direct status=none
FLAG=""; [ "$MODE" = baseline ] && FLAG="--no-throttle"
python3 main.py --out results/$MODE.csv --duration $((DUR+3)) $FLAG &
DPID=$!
bash -c "echo \$\$ > $CG/critical/cgroup.procs; exec fio --name=crit --filename=/mnt/tg/crit --size=200M --rw=randread --bs=4k --iodepth=2 --ioengine=libaio --direct=1 --time_based --runtime=$DUR --output-format=json --output=results/${MODE}_crit.json" &
P1=$!
bash -c "echo \$\$ > $CG/background/cgroup.procs; exec fio --name=bg --filename=/mnt/tg/bg --size=300M --rw=write --bs=128k --iodepth=8 --ioengine=libaio --direct=1 --time_based --runtime=$DUR --output-format=json --output=results/${MODE}_bg.json" &
P2=$!
wait $P1 $P2 $DPID
echo "Done: $MODE"
