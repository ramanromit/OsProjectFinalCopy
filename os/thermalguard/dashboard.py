#!/usr/bin/env python3
"""ThermalGuard live dashboard.
Run (as root):  sudo python3 dashboard.py   then open http://localhost:8000
Only the Python standard library is used (plus PyYAML). Nothing else to install."""
import csv, json, os, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from classifier import Classifier

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "results")
IO_MAX = "/sys/fs/cgroup/tg/background/io.max"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
try:
    import yaml
    CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
    DEV = CFG["device"]
except Exception:
    DEV = "7:9"
    CFG = None

STATE = {"proc": None, "mode": None, "start": 0.0, "duration": 0}
CLASSIFIER_STATE = {"instance": None, "thread": None, "active": False}
LOCK = threading.Lock()
os.makedirs(RES, exist_ok=True)


def running():
    p = STATE["proc"]
    return p is not None and p.poll() is None


def read_rows(mode):
    rows = []
    try:
        with open(os.path.join(RES, f"{mode}.csv")) as f:
            for r in csv.reader(f):
                if len(r) != 7 or r[0] == "t":
                    continue
                try:
                    rows.append({"t": float(r[0]), "temp": float(r[1]), "state": r[2],
                                 "limit": float(r[3]), "bg": float(r[4]),
                                 "crit": float(r[5]), "psi": float(r[6])})
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def summary(mode):
    try:
        c = json.load(open(os.path.join(RES, f"{mode}_crit.json")))["jobs"][0]["read"]
        b = json.load(open(os.path.join(RES, f"{mode}_bg.json")))["jobs"][0]["write"]
        rows = read_rows(mode)
        return {"p99_us": c["clat_ns"]["percentile"]["99.000000"] / 1000.0,
                "iops": c["iops"], "bg_mbps": b["bw_bytes"] / 1e6,
                "peak": max([r["temp"] for r in rows] or [0])}
    except Exception:
        return None


def release_limit():
    try:
        with open(IO_MAX, "w") as f:
            f.write(f"{DEV} wbps=max")
    except OSError:
        pass


def start_run(mode, duration):
    valid_modes = ("baseline", "guard", "classified", "reactive", "static", "rogue")
    if mode not in valid_modes:
        return False, "Invalid mode"

    if STATE["proc"] and STATE["proc"].poll() is None:
        return False, "A run is already in progress"

    # For results, "classified" stores its data under the "guard" key
    result_key = "guard" if mode == "classified" else mode

    for suffix in (".csv", ".json"):
        path = os.path.join(RES, f"{result_key}{suffix}")
        if os.path.exists(path):
            os.remove(path)

    log_path = os.path.join(RES, "run.log")
    log = open(log_path, "w", buffering=1)

    print(f"\nStarting ThermalGuard {mode.upper()} run ({duration}s)", flush=True)
    print(f"Saving logs to {log_path}\n", flush=True)

    # Start the classifier for "classified" or "rogue" mode
    if mode in ("classified", "rogue") and CFG:
        clf = Classifier(CFG)
        clf.reset()
        CLASSIFIER_STATE["instance"] = clf
        CLASSIFIER_STATE["thread"] = clf.run_background()
        CLASSIFIER_STATE["active"] = True
        print("Classifier thread started — will auto-assign / quarantine processes", flush=True)
    else:
        CLASSIFIER_STATE["active"] = False

    # Pick the right experiment script
    if mode == "classified":
        script = os.path.join(ROOT, "scripts", "run_auto_experiment.sh")
        cmd = ["bash", script, "guard", str(duration)]
    elif mode == "rogue":
        script = os.path.join(ROOT, "scripts", "run_rogue_experiment.sh")
        cmd = ["bash", script, str(duration)]
    else:
        script = os.path.join(ROOT, "scripts", "run_experiment.sh")
        cmd = ["bash", script, mode, str(duration)]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        STATE["proc"] = proc
        STATE["mode"] = result_key
        STATE["start"] = time.time()
        STATE["duration"] = int(duration)

        def stream_logs(process, logfile):
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    logfile.write(line)
                    logfile.flush()
            except Exception as exc:
                print(f"Log streaming error: {exc}", flush=True)
            finally:
                if process.stdout:
                    process.stdout.close()
                logfile.close()
                # Stop classifier when the run finishes
                if CLASSIFIER_STATE["active"]:
                    CLASSIFIER_STATE["instance"].stop()
                    CLASSIFIER_STATE["active"] = False
                    print("Classifier stopped (run finished)", flush=True)

        threading.Thread(
            target=stream_logs,
            args=(proc, log),
            daemon=True,
        ).start()

        label = "classified (auto)" if mode == "classified" else mode
        return True, f"Started {label} run for {duration} seconds"

    except Exception as exc:
        log.close()
        STATE["proc"] = None
        STATE["mode"] = None
        return False, str(exc)


def stop_run():
    with LOCK:
        p = STATE["proc"]
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
    # Stop classifier if running
    if CLASSIFIER_STATE["active"]:
        CLASSIFIER_STATE["instance"].stop()
        CLASSIFIER_STATE["active"] = False
    release_limit()
    return 200, "stopped"


def api_data():
    with LOCK:
        run, mode = running(), STATE["mode"]
        elapsed = time.time() - STATE["start"] if mode else 0
        dur = STATE["duration"]
    ref_mode = None if mode is None else ("baseline" if mode == "guard" else "guard")

    # Classifier events for the dashboard
    clf = CLASSIFIER_STATE["instance"]
    clf_events = []
    if clf and clf.log:
        start_t = STATE["start"]
        for e in clf.log:
            clf_events.append({
                "t": round(e["ts"] - start_t, 1) if start_t else 0,
                "pid": e["pid"],
                "comm": e["comm"],
                "cmdline": e.get("cmdline", ""),
                "tier": e["tier"],
                "method": e.get("method", "rule"),
                "detail": e.get("detail", ""),
                "msg": e["msg"],
            })

    return {
        "running": run, "mode": mode, "elapsed": elapsed, "duration": dur,
        "setup_ok": os.path.isdir("/mnt/tg") and os.path.isdir("/sys/fs/cgroup/tg/background"),
        "is_root": os.geteuid() == 0,
        "rows": read_rows(mode) if mode else [],
        "ref_mode": ref_mode, "ref": read_rows(ref_mode) if ref_mode else [],
        "summaries": {
            "baseline": summary("baseline"),
            "static": summary("static"),
            "reactive": summary("reactive"),
            "guard": summary("guard"),
            "rogue": summary("rogue"),
        },
        "classifier_active": CLASSIFIER_STATE["active"],
        "classifier_events": clf_events,
    }


def execute_testcases_api(tc_arg):
    tests_dir = os.path.abspath(os.path.join(ROOT, "..", "..", "tests"))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        from run_all_test_cases import (
            run_tc1_victim_effect, run_tc2_autonomous_division,
            run_tc3_rogue_binary_quarantine, run_tc4_antiflapping_burst_guard,
            run_tc5_thermal_momentum_predictive
        )
        tc_map = {
            "1": ("TC-1", "Latency-Critical Read Protection (Victim Effect)", run_tc1_victim_effect),
            "2": ("TC-2", "Multi-Process Ingestion & Layer 1 Division", run_tc2_autonomous_division),
            "3": ("TC-3", "Zero-Day Rogue Binary Quarantine (Heuristics)", run_tc3_rogue_binary_quarantine),
            "4": ("TC-4", "Anti-Flapping Short Burst Guard (<5 MB)", run_tc4_antiflapping_burst_guard),
            "5": ("TC-5", "Thermal Momentum & Predictive Overshoot Control", run_tc5_thermal_momentum_predictive),
        }
        out = {}
        target_keys = ["1", "2", "3", "4", "5"] if tc_arg == "all" else [tc_arg]
        for k in target_keys:
            if k in tc_map:
                tid, name, fn = tc_map[k]
                passed, data = fn(live=False)
                out[tid] = {"id": tid, "name": name, "passed": passed, "data": data}
        return {"status": "ok", "results": out}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            p = os.path.join(ROOT, "dashboard_page.html")
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "dashboard_page.html not found"})
        elif path == "/demo-rogue":
            p = os.path.join(ROOT, "demo_rogue.html")
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "demo_rogue.html not found"})
        elif path == "/demo-reactive":
            p = os.path.join(ROOT, "demo_reactive.html")
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "demo_reactive.html not found"})
        elif path == "/demo-testcases":
            p = os.path.join(ROOT, "demo_testcases.html")
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "demo_testcases.html not found"})
        elif path == "/api/testcases/run":
            u = urlparse(self.path)
            q = parse_qs(u.query)
            tc_arg = q.get("tc", ["all"])[0]
            res = execute_testcases_api(tc_arg)
            self._send(200, res)
        elif path == "/animation":
            anim_path = os.path.join(ROOT, "pipeline_animation.html")
            if os.path.exists(anim_path):
                self._send(200, open(anim_path, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "animation file not found"})
        elif path == "/api/data":
            self._send(200, api_data())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/start":
            try:
                dur = max(30, min(300, int(q.get("duration", ["150"])[0])))
            except ValueError:
                dur = 150
            code, msg = start_run(q.get("mode", [""])[0], dur)
        elif u.path == "/api/stop":
            code, msg = stop_run()
        else:
            code, msg = 404, "not found"
        self._send(code, {"message": msg})


PAGE_FILE = os.path.join(ROOT, "dashboard_page.html")
if os.path.exists(PAGE_FILE):
    PAGE = open(PAGE_FILE, "r", encoding="utf-8").read()
else:
    PAGE = "<h1>dashboard_page.html not found</h1>"

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("WARNING: not root. Cgroup writes will fail. Run: sudo python3 dashboard.py")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"ThermalGuard dashboard running: http://localhost:{PORT}   (Ctrl+C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_run()
