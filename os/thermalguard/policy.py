import time

class Policy:
    """Workload-aware throttling governor.
    Modes:
      1. 'predictive' (Novelty): Proactive governor using thermal rate-of-rise (dT/dt)
         and lookahead horizon to prevent thermal overshoot and momentum.
      2. 'reactive': Traditional threshold governor (NORMAL -> WARM -> HOT) with hysteresis.
      3. 'static': Fixed baseline cap (e.g. 50% io.max) for academic comparison.
    """
    def __init__(self, cfg, governor_override=None, static_cap=None):
        t = cfg["thresholds"]
        self.warn, self.hot, self.h = t["warn"], t["hot"], t["hysteresis"]
        self.full = cfg["full_speed_mbps"] * 1e6
        self.frac = cfg["limits"]
        
        gov_cfg = cfg.get("governor", {})
        self.mode = governor_override or gov_cfg.get("mode", "predictive")
        self.lookahead = gov_cfg.get("lookahead_sec", 5.0)
        self.static_cap = static_cap if static_cap is not None else gov_cfg.get("static_cap", 0.5)

        self.state = "STATIC" if self.mode == "static" else "NORMAL"
        self.prev_temp = None
        self.prev_t = None
        self.dT_dt = 0.0
        self.proj_temp = None

    def update(self, temp, timestamp=None):
        now = time.time() if timestamp is None else timestamp
        if temp is None:
            self.state = "NORMAL"
            return self.state

        if self.mode == "static":
            self.state = "STATIC"
            return self.state

        # Calculate thermal derivative dT/dt (rate of rise)
        if self.prev_t is not None and self.prev_temp is not None:
            dt = now - self.prev_t
            if dt > 0.1:
                self.dT_dt = (temp - self.prev_temp) / dt
        else:
            self.dT_dt = 0.0

        self.prev_temp = temp
        self.prev_t = now

        # Compute projected temperature lookahead
        effective_temp = temp
        if self.mode == "predictive":
            # Only project positive heating (cooling is handled by hysteresis)
            heating_rate = max(0.0, self.dT_dt)
            self.proj_temp = temp + heating_rate * self.lookahead
            effective_temp = self.proj_temp
        else:
            self.proj_temp = temp

        # State transition logic
        if effective_temp >= self.hot:
            self.state = "HOT"
        elif self.state == "HOT" and temp < self.hot - self.h:
            self.state = "WARM"
        elif self.state == "NORMAL" and effective_temp >= self.warn:
            self.state = "WARM"
        elif self.state == "WARM" and temp < self.warn - self.h:
            self.state = "NORMAL"

        return self.state

    def limit_wbps(self):
        if self.mode == "static":
            return int(self.full * self.static_cap)
        if self.state == "NORMAL":
            return None
        return int(self.full * self.frac["warn" if self.state == "WARM" else "hot"])
