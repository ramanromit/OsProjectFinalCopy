import time

class Monitor:
    """Reads per-cgroup io.stat (rates) and io.pressure (PSI)."""
    def __init__(self, cfg):
        self.dev = cfg["device"]
        root = cfg["cgroup_root"]
        self.paths = {n: f"{root}/{cfg[n]}" for n in ("critical", "background")}
        self.prev = {}; self.prev_t = None

    def _stat(self, p):
        try:
            for line in open(p + "/io.stat"):
                parts = line.split()
                if parts[0] == self.dev:
                    return {k: int(v) for k, v in (x.split("=") for x in parts[1:])}
        except OSError:
            pass
        return {}

    def _psi(self, p):
        try:
            for line in open(p + "/io.pressure"):
                if line.startswith("some"):
                    return float(line.split()[1].split("=")[1])
        except OSError:
            pass
        return 0.0

    def sample(self):
        now = time.time()
        cur = {n: self._stat(p) for n, p in self.paths.items()}
        dt = (now - self.prev_t) if self.prev_t else None
        out = {}
        for n in cur:
            for k in ("rbytes", "wbytes"):
                if dt and self.prev:
                    out[f"{n}_{k}"] = (cur[n].get(k, 0) - self.prev[n].get(k, 0)) / dt / 1e6
                else:
                    out[f"{n}_{k}"] = 0.0
        out["crit_psi"] = self._psi(self.paths["critical"])
        self.prev, self.prev_t = cur, now
        return out
