# ThermalGuard: Workload-Aware NVMe I/O Throttling using Cgroups v2

A userspace daemon that watches (simulated) SSD temperature and throttles low-priority
workloads through cgroups v2 `io.max`, so latency-sensitive workloads stay fast and the drive
stays cooler.

## Architecture
thermal.py (temperature) + monitor.py (io.stat, io.pressure) -> policy.py (state machine)
-> actuator.py (writes io.max) -> main.py (1 Hz loop, CSV log)

## Policy
NORMAL -> WARN at 60 C (background limited to 80% of full speed)
WARN -> HOT at 70 C (background limited to 30%)
Release with 3 C hysteresis. Critical cgroup is never throttled.
Failsafe: unreadable sensor releases all limits. Limits are removed on exit (atexit, SIGINT, SIGTERM).

## Setup (Ubuntu, cgroups v2, root)
sudo apt install -y fio python3-yaml python3-matplotlib
Create a loop-device test disk, mount at /mnt/tg, create cgroups /sys/fs/cgroup/tg/{critical,background}
with the io controller enabled. Set the device MAJ:MIN in config.yaml.

## Run
sudo bash scripts/run_experiment.sh baseline 150
sudo bash scripts/run_experiment.sh guard 150
python3 scripts/plot.py
Outputs are in results/ (CSV logs, timeline.png, summary.png, summary.txt).
