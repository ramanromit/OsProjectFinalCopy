class Policy:
    """NORMAL -> WARN -> HOT with hysteresis. Only the background tier is throttled."""
    def __init__(self, cfg):
        t = cfg["thresholds"]
        self.warn, self.hot, self.h = t["warn"], t["hot"], t["hysteresis"]
        self.full = cfg["full_speed_mbps"] * 1e6
        self.frac = cfg["limits"]
        self.state = "NORMAL"

    def update(self, temp):
        if temp is None:                                   # failsafe: no sensor -> no throttling
            self.state = "NORMAL"
        elif temp >= self.hot:
            self.state = "HOT"
        elif self.state == "HOT" and temp < self.hot - self.h:
            self.state = "WARM"
        elif self.state == "NORMAL" and temp >= self.warn:
            self.state = "WARM"
        elif self.state == "WARM" and temp < self.warn - self.h:
            self.state = "NORMAL"
        return self.state

    def limit_wbps(self):
        if self.state == "NORMAL":
            return None
        return int(self.full * self.frac["warn" if self.state == "WARM" else "hot"])
