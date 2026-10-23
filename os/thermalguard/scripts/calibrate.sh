#!/bin/bash
# Measures unthrottled background write speed and stores it in config.yaml
cd "$(dirname "$0")/.."
echo "7:9 wbps=max" > /sys/fs/cgroup/tg/background/io.max
bash -c 'echo $$ > /sys/fs/cgroup/tg/background/cgroup.procs; exec fio --name=cal --filename=/mnt/tg/bg --size=300M --rw=write --bs=128k --iodepth=8 --ioengine=libaio --direct=1 --time_based --runtime=15 --output-format=json --output=results/cal.json' >/dev/null
V=$(python3 -c "import json;print(int(json.load(open('results/cal.json'))['jobs'][0]['write']['bw_bytes']/1e6))")
echo "Unthrottled background speed: $V MB/s"
sed -i "s/^full_speed_mbps:.*/full_speed_mbps: $V/" config.yaml
