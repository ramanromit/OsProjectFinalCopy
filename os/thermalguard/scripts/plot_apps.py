import csv, json
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

MODES = ("apps_baseline", "apps_guard")

def load(m):
    return list(csv.DictReader(open(f"results/{m}.csv")))
def col(r, k):
    return [float(x[k]) for x in r]

fig, ax = plt.subplots(3, 1, figsize=(9, 9), sharex=True)
for m, c in zip(MODES, ("tab:red", "tab:green")):
    r = load(m); t = col(r, "t")
    ax[0].plot(t, col(r, "temp"), c=c, label=m)
    ax[1].plot(t, col(r, "bg_wMBps"), c=c, label=m)
    ax[2].plot(t, col(r, "crit_rMBps"), c=c, label=m)
ax[0].axhline(60, ls="--", c="orange"); ax[0].axhline(70, ls="--", c="red")
ax[0].set_ylabel("Temp (C, simulated)"); ax[1].set_ylabel("Bulk copy write MB/s")
ax[2].set_ylabel("Game-like read MB/s"); ax[2].set_xlabel("Time (s)")
ax[0].legend(); ax[0].set_title("ThermalGuard vs no control (game-like + bulk copy workloads)")
plt.tight_layout(); plt.savefig("results/apps_timeline.png", dpi=120)

summ = {}
for m in MODES:
    c = json.load(open(f"results/{m}_crit.json"))["jobs"][0]["read"]
    b = json.load(open(f"results/{m}_bg.json"))["jobs"][0]["write"]
    summ[m] = dict(p99_us=c["clat_ns"]["percentile"]["99.000000"] / 1000,
                   crit_iops=c["iops"], bg_mbps=b["bw_bytes"] / 1e6,
                   peak_temp=max(col(load(m), "temp")))
fig, ax = plt.subplots(1, 3, figsize=(11, 3.5))
for i, (k, lbl) in enumerate((("p99_us", "Game-like p99 latency (us)"),
                              ("bg_mbps", "Bulk copy MB/s"), ("peak_temp", "Peak temp (C)"))):
    ax[i].bar(list(summ), [summ[m][k] for m in summ], color=["tab:red", "tab:green"])
    ax[i].set_title(lbl); ax[i].tick_params(axis="x", labelrotation=15)
plt.tight_layout(); plt.savefig("results/apps_summary.png", dpi=120)
with open("results/apps_summary.txt", "w") as f:
    for m, d in summ.items():
        line = f"{m}: " + ", ".join(f"{k}={v:.1f}" for k, v in d.items())
        print(line); f.write(line + "\n")
