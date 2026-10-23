import atexit, signal, sys

class Actuator:
    """Writes io.max for the background cgroup; always removes limits on exit."""
    def __init__(self, cfg):
        self.dev = cfg["device"]
        self.path = f'{cfg["cgroup_root"]}/{cfg["background"]}/io.max'
        self.cur = "unset"
        atexit.register(self.release)
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *a: sys.exit(0))

    def _write(self, txt):
        with open(self.path, "w") as f:
            f.write(txt)

    def set_limit(self, wbps):
        if wbps == self.cur:
            return
        self._write(f"{self.dev} wbps={'max' if wbps is None else wbps}")
        self.cur = wbps

    def release(self):
        try:
            self._write(f"{self.dev} wbps=max")
            self.cur = None
        except OSError:
            pass
