import glob, time

class Thermal:
    """Real NVMe sensor if configured and present, else a simulated thermal model.
    Model: dT/dt = a*write_MBps - b*(T - ambient)"""
    def __init__(self, cfg):
        s = cfg["sensor"]
        self.sim = s.get("simulate", True)
        self.path = None
        if not self.sim:
            m = glob.glob(s["hwmon_glob"])
            self.path = m[0] if m else None
            if not self.path:
                self.sim = True
        self.amb = s["ambient"]
        self.temp = float(self.amb)
        self.b = s["cooling"]
        self.a = (s["full_load_temp"] - self.amb) * self.b / cfg["full_speed_mbps"]
        self.last = time.time()

    def read(self, write_mbps):
        if self.sim:
            now = time.time(); dt = now - self.last; self.last = now
            self.temp += (self.a * write_mbps - self.b * (self.temp - self.amb)) * dt
            return self.temp
        try:
            return int(open(self.path).read()) / 1000.0
        except (OSError, ValueError):
            return None          # sensor failure -> policy releases limits
