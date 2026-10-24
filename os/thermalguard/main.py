import argparse, csv, time, yaml
from thermal import Thermal
from monitor import Monitor
from policy import Policy
from actuator import Actuator

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config.yaml")
ap.add_argument("--out", default="results/log.csv")
ap.add_argument("--duration", type=float, default=0)
ap.add_argument("--no-throttle", action="store_true", help="monitor only (baseline run)")
ap.add_argument("--governor", choices=["predictive", "reactive", "static"], default=None,
                help="override governor mode (predictive=dT/dt lookahead, reactive=threshold, static=fixed cap)")
ap.add_argument("--static-cap", type=float, default=None,
                help="fraction of full_speed_mbps for static cap (e.g. 0.5)")
a = ap.parse_args()

cfg = yaml.safe_load(open(a.config))
pol = Policy(cfg, governor_override=a.governor, static_cap=a.static_cap)
th, mon, act = Thermal(cfg), Monitor(cfg), Actuator(cfg)
act.release()
mon.sample()
th.last = time.time()
start = time.time()

with open(a.out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["t", "temp", "state", "limit_mbps", "bg_wMBps", "crit_rMBps", "crit_psi"])
    while a.duration == 0 or time.time() - start < a.duration:
        time.sleep(cfg["interval"])
        s = mon.sample()
        temp = th.read(s["background_wbytes"] + s["critical_wbytes"])
        state = pol.update(temp)
        lim = pol.limit_wbps()
        if not a.no_throttle:
            act.set_limit(lim)
        t = time.time() - start
        row = [round(t, 1), round(temp if temp is not None else -1, 2), state,
               round(lim / 1e6, 1) if lim else 0,
               round(s["background_wbytes"], 1), round(s["critical_rbytes"], 1), s["crit_psi"]]
        w.writerow(row); f.flush()
        rate_str = f"dT/dt={pol.dT_dt:+4.1f}C/s" if hasattr(pol, "dT_dt") else ""
        print(f"t={row[0]:6.1f}s temp={row[1]:5.1f}C {state:6s} bg={row[4]:7.1f}MB/s limit={row[3]} {rate_str}")
