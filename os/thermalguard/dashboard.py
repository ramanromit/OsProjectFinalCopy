#!/usr/bin/env python3
"""ThermalGuard live dashboard.
Run (as root):  sudo python3 dashboard.py   then open http://localhost:8000
Only the Python standard library is used. Nothing to install."""
import csv, json, os, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "results")
IO_MAX = "/sys/fs/cgroup/tg/background/io.max"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
try:
    import yaml
    DEV = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))["device"]
except Exception:
    DEV = "7:9"

STATE = {"proc": None, "mode": None, "start": 0.0, "duration": 0}
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
    if mode not in ("baseline", "guard"):
        return False, "Invalid mode"

    if STATE["proc"] and STATE["proc"].poll() is None:
        return False, "A run is already in progress"

    for suffix in (".csv", ".json"):
        path = os.path.join(RES, f"{mode}{suffix}")
        if os.path.exists(path):
            os.remove(path)

    log_path = os.path.join(RES, "run.log")
    log = open(log_path, "w", buffering=1)

    print(f"\nStarting ThermalGuard {mode.upper()} run ({duration}s)", flush=True)
    print(f"Saving logs to {log_path}\n", flush=True)

    try:
        proc = subprocess.Popen(
            [
                "bash",
                os.path.join(ROOT, "scripts", "run_experiment.sh"),
                mode,
                str(duration),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        STATE["proc"] = proc
        STATE["mode"] = mode
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

        threading.Thread(
            target=stream_logs,
            args=(proc, log),
            daemon=True,
        ).start()

        return True, f"Started {mode} run for {duration} seconds"

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
    release_limit()
    return 200, "stopped"


def api_data():
    with LOCK:
        run, mode = running(), STATE["mode"]
        elapsed = time.time() - STATE["start"] if mode else 0
        dur = STATE["duration"]
    ref_mode = None if mode is None else ("baseline" if mode == "guard" else "guard")
    return {
        "running": run, "mode": mode, "elapsed": elapsed, "duration": dur,
        "setup_ok": os.path.isdir("/mnt/tg") and os.path.isdir("/sys/fs/cgroup/tg/background"),
        "is_root": os.geteuid() == 0,
        "rows": read_rows(mode) if mode else [],
        "ref_mode": ref_mode, "ref": read_rows(ref_mode) if ref_mode else [],
        "summaries": {"baseline": summary("baseline"), "guard": summary("guard")},
    }


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
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ThermalGuard Dashboard</title>
<style>
:root{--bg:#0e1117;--card:#161b26;--line:#252c3b;--txt:#e6e9f0;--mut:#8b93a7;
--red:#ff6b6b;--blue:#4dabf7;--green:#51cf66;--amber:#ffd43b;--orange:#ffa94d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1150px;margin:0 auto;padding:18px}
h1{margin:0;font-size:24px}.sub{color:var(--mut);margin:2px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
button{border:0;border-radius:10px;padding:10px 16px;font-size:14px;font-weight:600;cursor:pointer;color:#fff}
button:disabled{opacity:.4;cursor:not-allowed}
.b1{background:#2f9e44}.b2{background:#3b5bdb}.b3{background:#c92a2a}
select{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:9px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.kpi .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.kpi .v{font-size:26px;font-weight:700;margin-top:4px}
.badge{display:inline-block;padding:3px 12px;border-radius:99px;font-weight:700;font-size:18px}
.NORMAL{background:#12351c;color:var(--green)}.WARM{background:#3d3208;color:var(--amber)}
.HOT{background:#431313;color:var(--red)}.IDLE{background:#222a39;color:var(--mut)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:12px}
.charts h3{margin:0 0 6px;font-size:14px;color:var(--mut);font-weight:600}
canvas{width:100%;height:220px;display:block}
.prog{height:6px;background:var(--line);border-radius:9px;overflow:hidden;margin:0 0 14px}
.prog div{height:100%;width:0;background:var(--blue);transition:width .5s}
.warn{background:#431313;border:1px solid var(--red);color:#ffc9c9;border-radius:10px;padding:10px 14px;margin-bottom:12px;display:none}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:13px}.note{color:var(--mut);font-size:13px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>ThermalGuard</h1>
<div class="sub">Workload-aware NVMe I/O throttling with cgroups v2 &middot; temperature is <b>simulated</b> &middot; test disk is a RAM-backed loop device</div>
<div class="warn" id="warn"></div>
<div class="card bar">
  <button class="b1" id="bGuard">&#9654; Inject load with ThermalGuard</button>
  <button class="b2" id="bBase">&#9654; Inject load, no control (baseline)</button>
  <button class="b3" id="bStop">&#9632; Stop</button>
  <label style="color:var(--mut)">Duration
    <select id="dur"><option value="60">60 s</option><option value="90">90 s</option>
    <option value="150" selected>150 s</option></select></label>
  <span id="msg" style="color:var(--mut)"></span>
</div>
<div class="prog"><div id="prog"></div></div>
<div class="kpis">
  <div class="card kpi"><div class="l">State</div><div class="v"><span class="badge IDLE" id="kState">IDLE</span></div></div>
  <div class="card kpi"><div class="l">Temperature (sim)</div><div class="v" id="kTemp">--</div></div>
  <div class="card kpi"><div class="l">Background writes</div><div class="v" id="kBg">--</div></div>
  <div class="card kpi"><div class="l">Throttle limit</div><div class="v" id="kLim">--</div></div>
  <div class="card kpi"><div class="l">Critical reads</div><div class="v" id="kCrit">--</div></div>
  <div class="card kpi"><div class="l">Elapsed</div><div class="v" id="kTime">--</div></div>
</div>
<div class="charts">
  <div class="card"><h3>Temperature (&deg;C) &mdash; dashed lines: WARM 60, HOT 70</h3><canvas id="c1"></canvas></div>
  <div class="card"><h3>Background write speed vs limit (MB/s)</h3><canvas id="c2"></canvas></div>
  <div class="card"><h3>Critical workload read throughput (MB/s)</h3><canvas id="c3"></canvas></div>
  <div class="card"><h3>Results of completed runs</h3>
    <table><thead><tr><th>Metric</th><th>Baseline</th><th>ThermalGuard</th></tr></thead>
    <tbody id="tbl"><tr><td colspan="3" style="color:var(--mut)">Run baseline and guard to fill this table.</td></tr></tbody></table>
    <div class="note">Grey dashed line on the charts = the other mode's last run, for comparison.</div></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const COL={temp:'#ff6b6b',bg:'#4dabf7',lim:'#ffd43b',crit:'#51cf66',ref:'#8b93a7'};

function drawChart(cv,series,o){
  const dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
  cv.width=w*dpr;cv.height=h*dpr;
  const g=cv.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,w,h);
  const L=48,R=12,T=18,B=24,pw=w-L-R,ph=h-T-B;
  let ys=[];series.forEach(s=>s.data.forEach(p=>ys.push(p[1])));(o.hlines||[]).forEach(l=>ys.push(l.y));
  const ymin=o.ymin||0,ymax=Math.max(o.ymaxMin||1,...(ys.length?ys:[1]))*1.08,xmax=Math.max(o.xmax||10,10);
  const X=x=>L+x/xmax*pw,Y=y=>T+ph-(y-ymin)/(ymax-ymin)*ph;
  g.font='11px system-ui';g.fillStyle='#8b93a7';g.strokeStyle='#252c3b';g.lineWidth=1;
  for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=Y(v);
    g.beginPath();g.moveTo(L,y);g.lineTo(w-R,y);g.stroke();
    g.textAlign='right';g.fillText(v>=1000?(v/1000).toFixed(1)+'k':v.toFixed(0),L-6,y+4);}
  g.textAlign='center';
  for(let i=0;i<=5;i++){const v=xmax*i/5;g.fillText(v.toFixed(0)+'s',X(v),h-6);}
  (o.hlines||[]).forEach(l=>{g.strokeStyle=l.color;g.setLineDash([5,4]);g.beginPath();
    g.moveTo(L,Y(l.y));g.lineTo(w-R,Y(l.y));g.stroke();g.setLineDash([]);});
  series.forEach(s=>{if(!s.data.length)return;g.strokeStyle=s.color;g.lineWidth=2;
    g.setLineDash(s.dash?[6,4]:[]);g.beginPath();
    s.data.forEach((p,i)=>i?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1])));g.stroke();g.setLineDash([]);});
  let lx=L+8;g.textAlign='left';
  series.forEach(s=>{g.fillStyle=s.color;g.fillRect(lx,6,10,3);g.fillStyle='#8b93a7';
    g.fillText(s.label,lx+14,11);lx+=g.measureText(s.label).width+34;});
}

function render(d){
  const rows=d.rows,last=rows.length?rows[rows.length-1]:null;
  const warn=$('warn'),pr=[];
  if(!d.is_root)pr.push('Dashboard is not running as root. Restart with: sudo python3 dashboard.py');
  if(!d.setup_ok)pr.push('Test disk (/mnt/tg) or cgroups not found. Redo the loop-disk and cgroup setup steps.');
  warn.style.display=pr.length?'block':'none';warn.textContent=pr.join('  |  ');
  $('bGuard').disabled=$('bBase').disabled=d.running||!d.setup_ok||!d.is_root;$('bStop').disabled=!d.running;
  const st=last?last.state:'IDLE';const kb=$('kState');kb.textContent=d.running||last?st:'IDLE';kb.className='badge '+(d.running||last?st:'IDLE');
  $('kTemp').textContent=last?last.temp.toFixed(1)+' \u00b0C':'--';
  $('kBg').textContent=last?last.bg.toFixed(0)+' MB/s':'--';
  $('kLim').textContent=last?(last.limit>0?last.limit.toFixed(0)+' MB/s':'none'):'--';
  $('kCrit').textContent=last?last.crit.toFixed(0)+' MB/s':'--';
  $('kTime').textContent=d.mode?Math.min(d.elapsed,d.duration).toFixed(0)+' / '+d.duration+' s':'--';
  $('prog').style.width=(d.running&&d.duration?Math.min(100,d.elapsed/d.duration*100):0)+'%';
  const map=(r,k,f)=>r.filter(x=>f?f(x):true).map(x=>[x.t,x[k]]);
  const rl=d.ref_mode?d.ref_mode:'';
  const lastT=Math.max(d.duration||0,rows.length?rows[rows.length-1].t:0,d.ref.length?d.ref[d.ref.length-1].t:0);
  drawChart($('c1'),[{data:map(d.ref,'temp'),color:COL.ref,dash:1,label:rl?rl+' (previous)':'previous'},
    {data:map(rows,'temp'),color:COL.temp,label:d.mode||'current'}],
    {xmax:lastT,ymin:25,ymaxMin:75,hlines:[{y:60,color:'#ffa94d'},{y:70,color:'#ff6b6b'}]});
  drawChart($('c2'),[{data:map(d.ref,'bg'),color:COL.ref,dash:1,label:rl?rl+' (previous)':'previous'},
    {data:map(rows,'bg'),color:COL.bg,label:'background MB/s'},
    {data:map(rows,'limit',x=>x.limit>0),color:COL.lim,dash:1,label:'limit'}],{xmax:lastT,ymaxMin:100});
  drawChart($('c3'),[{data:map(d.ref,'crit'),color:COL.ref,dash:1,label:rl?rl+' (previous)':'previous'},
    {data:map(rows,'crit'),color:COL.crit,label:'critical MB/s'}],{xmax:lastT,ymaxMin:100});
  const b=d.summaries.baseline,gd=d.summaries.guard;
  if(b||gd){const f=(s,k,n,u)=>s?s[k].toFixed(n)+u:'--';
    const spec=[['Peak temperature (sim)','peak',1,' \u00b0C'],['Critical p99 latency','p99_us',1,' \u00b5s'],
      ['Critical read IOPS','iops',0,''],['Background write speed','bg_mbps',0,' MB/s']];
    $('tbl').innerHTML=spec.map(r=>'<tr><td>'+r[0]+'</td><td>'+f(b,r[1],r[2],r[3])+'</td><td>'+f(gd,r[1],r[2],r[3])+'</td></tr>').join('');}
}
async function post(url){
  try{const r=await fetch(url,{method:'POST'});const j=await r.json();$('msg').textContent=j.message;}
  catch(e){$('msg').textContent='request failed';}
  poll();
}
$('bGuard').onclick=()=>post('/api/start?mode=guard&duration='+$('dur').value);
$('bBase').onclick=()=>post('/api/start?mode=baseline&duration='+$('dur').value);
$('bStop').onclick=()=>post('/api/stop');
async function poll(){try{render(await (await fetch('/api/data')).json());}
  catch(e){$('msg').textContent='dashboard server not reachable';}}
setInterval(poll,1000);poll();window.addEventListener('resize',poll);
</script></body></html>
"""

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
