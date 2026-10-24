"""
Automatic process classifier for ThermalGuard.

Scans for new processes doing I/O on the target device and assigns them to
the correct cgroup (critical or background) using a two-layer strategy:

  1. RULE-BASED  – A config list maps known program names / patterns to a tier.
                   This is how real systems work (Android, systemd, Windows QoS).

  2. HEURISTIC   – Unknown processes are observed for a short window. Their I/O
                   pattern (read-vs-write ratio, syscall frequency) determines
                   the tier automatically.  Read-heavy / low-throughput → critical.
                   Write-heavy / high-throughput → background.

Usage:
    sudo python3 classifier.py                          # standalone daemon
    sudo python3 classifier.py --config config.yaml     # custom config
    sudo python3 classifier.py --dry-run                # print decisions, don't move

The classifier can also be imported and used inside main.py or dashboard.py.
"""

import glob, os, re, time, threading, yaml, argparse

# ---------------------------------------------------------------------------
# Defaults – overridden by the "classifier" section in config.yaml
# ---------------------------------------------------------------------------
DEFAULT_RULES = [
    # pattern (matched against /proc/<pid>/comm or cmdline)  →  tier
    # ── critical (latency-sensitive) ──────────────────────────────────
    {"pattern": "game",        "tier": "critical"},
    {"pattern": "firefox",     "tier": "critical"},
    {"pattern": "chrome",      "tier": "critical"},
    {"pattern": "chromium",    "tier": "critical"},
    {"pattern": "code",        "tier": "critical"},   # VS Code
    {"pattern": "mysql",       "tier": "critical"},
    {"pattern": "postgres",    "tier": "critical"},
    {"pattern": "mongod",      "tier": "critical"},
    {"pattern": "redis",       "tier": "critical"},
    {"pattern": "nginx",       "tier": "critical"},
    {"pattern": "node",        "tier": "critical"},
    {"pattern": "python3",     "tier": "critical"},   # interactive / server
    # ── background (throughput-oriented) ──────────────────────────────
    {"pattern": "rsync",       "tier": "background"},
    {"pattern": "cp",          "tier": "background"},
    {"pattern": "dd",          "tier": "background"},
    {"pattern": "tar",         "tier": "background"},
    {"pattern": "gzip",        "tier": "background"},
    {"pattern": "zip",         "tier": "background"},
    {"pattern": "wget",        "tier": "background"},
    {"pattern": "curl",        "tier": "background"},
    {"pattern": "apt",         "tier": "background"},
    {"pattern": "dpkg",        "tier": "background"},
    {"pattern": "update",      "tier": "background"},
    {"pattern": "backup",      "tier": "background"},
    {"pattern": "sync",        "tier": "background"},
    {"pattern": "fio",         "tier": "background"},  # our benchmark default
]

# Heuristic thresholds
OBSERVE_SECONDS   = 3      # how long to watch an unknown process
WRITE_RATIO_THRESH = 0.70  # if writes > 70% of total bytes → background
SYSCALL_RATE_THRESH = 200  # reads/s above this → likely latency-sensitive


class Classifier:
    """Watches /proc for new processes and assigns them to cgroups."""

    def __init__(self, cfg, dry_run=False):
        self.dev        = cfg["device"]
        self.cg_root    = cfg["cgroup_root"]
        self.cg_names   = {"critical": cfg["critical"], "background": cfg["background"]}
        self.dry_run    = dry_run
        self.known_pids = set()         # PIDs we have already classified
        self.scan_interval = cfg.get("classifier", {}).get("scan_interval", 0.8)

        # Build compiled rule list with prefix word boundaries
        raw_rules = cfg.get("classifier", {}).get("rules", DEFAULT_RULES)
        self.rules = [(re.compile(r"\b" + r["pattern"], re.IGNORECASE), r["tier"]) for r in raw_rules]

        # Default tier for processes the heuristic can't decide on
        self.default_tier = cfg.get("classifier", {}).get("default_tier", "background")

        # Log (structured entries for dashboard consumption)
        self.log = []
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # 1. Reading process info from /proc
    # ------------------------------------------------------------------
    @staticmethod
    def _read_comm(pid):
        """Return the short command name (e.g. 'fio', 'rsync')."""
        try:
            return open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            return None

    @staticmethod
    def _read_cmdline(pid):
        """Return the full command line as a single string."""
        try:
            return open(f"/proc/{pid}/cmdline").read().replace("\x00", " ").strip()
        except OSError:
            return ""

    @staticmethod
    def _read_io(pid):
        """Return dict of /proc/<pid>/io counters, or None if unreadable."""
        try:
            d = {}
            for line in open(f"/proc/{pid}/io"):
                k, v = line.split(":")
                d[k.strip()] = int(v.strip())
            return d
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid):
        return os.path.isdir(f"/proc/{pid}")

    def _current_cgroup(self, pid):
        """Return which of our tiers this PID is already in, or None."""
        try:
            cg = open(f"/proc/{pid}/cgroup").read()
            for tier, name in self.cg_names.items():
                if f"/{name}" in cg:
                    return tier
        except OSError:
            pass
        return None

    # ------------------------------------------------------------------
    # 2. Rule-based classification
    # ------------------------------------------------------------------
    def classify_by_rules(self, pid):
        """Try to match the process name/cmdline against the rule list.
        Returns ('critical'|'background', matched_pattern) or (None, None)."""
        comm = self._read_comm(pid)
        cmdline = self._read_cmdline(pid)
        text = f"{comm} {cmdline}"
        for regex, tier in self.rules:
            if regex.search(text):
                return tier, regex.pattern
        return None, None

    # ------------------------------------------------------------------
    # 3. Heuristic classification (observe I/O pattern)
    # ------------------------------------------------------------------
    def classify_by_heuristic(self, pid):
        """Watch the process for a few seconds and classify by I/O pattern.

        Logic:
          - Mostly writes (>70% bytes)          → background
          - High read-syscall rate (>200/s)      → critical  (many small reads)
          - Mostly reads                         → critical
          - Can't tell / no I/O                  → default_tier
        """
        io1 = self._read_io(pid)
        if io1 is None:
            return self.default_tier, "no_io_counters"

        time.sleep(OBSERVE_SECONDS)

        io2 = self._read_io(pid)
        if io2 is None:
            return self.default_tier, "no_io_counters"

        dr = io2["read_bytes"]  - io1["read_bytes"]
        dw = io2["write_bytes"] - io1["write_bytes"]
        dsc_r = io2["syscr"]    - io1["syscr"]

        total = dr + dw
        if total < 5000000:       # Anti-flapping: < 5MB active I/O over window is considered transient
            return None, f"transient_burst_ignored ({total/1e6:.1f}MB)"

        write_ratio = dw / total
        read_syscall_rate = dsc_r / OBSERVE_SECONDS

        if write_ratio > WRITE_RATIO_THRESH:
            reason = f"write_ratio={write_ratio:.0%} (sustained bulk writer quarantined)"
            tier = "background"
        elif read_syscall_rate > SYSCALL_RATE_THRESH:
            reason = f"read_syscalls={read_syscall_rate:.0f}/s (interactive reads protected)"
            tier = "critical"
        elif dr > dw:
            reason = f"read_dominant (r={dr/1e6:.1f}MB w={dw/1e6:.1f}MB)"
            tier = "critical"
        else:
            reason = "ambiguous, using default"
            tier = self.default_tier

        return tier, reason

    # ------------------------------------------------------------------
    # 4. Assign a PID to a cgroup
    # ------------------------------------------------------------------
    def assign(self, pid, tier):
        """Move the process into the chosen cgroup."""
        path = f"{self.cg_root}/{self.cg_names[tier]}/cgroup.procs"
        if self.dry_run:
            self._log(pid, tier, "DRY-RUN, not moved")
            return True
        try:
            with open(path, "w") as f:
                f.write(str(pid))
            return True
        except OSError as e:
            self._log(pid, tier, f"FAILED to assign: {e}")
            return False

    # ------------------------------------------------------------------
    # 5. Full classification pipeline for one PID
    # ------------------------------------------------------------------
    def classify_and_assign(self, pid):
        """Classify a single PID and move it to the right cgroup."""
        comm = self._read_comm(pid)
        if comm is None:
            return  # process gone

        # Skip if already in one of our cgroups
        if self._current_cgroup(pid) is not None:
            return

        # Layer 1: rule-based
        tier, pattern = self.classify_by_rules(pid)
        method = "rule"
        detail = f"rule:{pattern}" if pattern else ""

        # Layer 2: heuristic fallback
        if tier is None:
            tier, heuristic_reason = self.classify_by_heuristic(pid)
            method = "heuristic"
            detail = heuristic_reason

        if tier is not None:
            self._log(pid, tier, f"{method} | {detail} | comm={comm}", method=method, detail=detail)
            self.assign(pid, tier)
        else:
            self._log(pid, "unassigned", f"anti-flapping | {detail} | comm={comm}", method=method, detail=detail)

    # ------------------------------------------------------------------
    # 6. Scanner – find new processes doing I/O on our device
    # ------------------------------------------------------------------
    def _pids_on_device(self):
        """Return PIDs that have open file descriptors on the target device."""
        major, minor = self.dev.split(":")
        target_dev = os.makedev(int(major), int(minor))
        pids = set()
        for pid_dir in glob.glob("/proc/[0-9]*"):
            pid = int(os.path.basename(pid_dir))
            if pid in self.known_pids:
                continue
            try:
                for fd in os.listdir(f"{pid_dir}/fd"):
                    try:
                        st = os.stat(f"{pid_dir}/fd/{fd}")
                        if os.major(st.st_dev) == int(major) and os.minor(st.st_dev) == int(minor):
                            pids.add(pid)
                            break
                    except (OSError, ValueError):
                        continue
            except OSError:
                continue
        return pids

    def scan_once(self):
        """One pass: find new processes and classify them."""
        new_pids = self._pids_on_device() - self.known_pids
        for pid in new_pids:
            self.classify_and_assign(pid)
            self.known_pids.add(pid)
        # Prune dead PIDs from memory
        self.known_pids = {p for p in self.known_pids if self._pid_alive(p)}

    def run_forever(self):
        """Blocking loop – scan for new processes continuously."""
        print(f"Classifier started (scan every {self.scan_interval}s, "
              f"{len(self.rules)} rules, default={self.default_tier})")
        print(f"  dry_run={self.dry_run}")
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.scan_interval)

    def run_background(self):
        """Start the scanner in a daemon thread (for embedding in main.py)."""
        self._stop.clear()
        t = threading.Thread(target=self.run_forever, daemon=True)
        t.start()
        return t

    def stop(self):
        """Signal the background scanner to stop."""
        self._stop.set()

    def reset(self):
        """Clear state for a fresh run."""
        self.known_pids.clear()
        self.log.clear()
        self._stop.clear()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, pid, tier, msg, method="rule", detail=""):
        entry = {
            "ts": time.time(),
            "pid": pid,
            "tier": tier,
            "msg": msg,
            "comm": self._read_comm(pid) or "?",
            "cmdline": self._read_cmdline(pid) or "",
            "method": method,
            "detail": detail
        }
        self.log.append(entry)
        print(f"[classifier] PID {pid:>6} → {tier:<12} {msg}")


# ======================================================================
# Standalone mode
# ======================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ThermalGuard process classifier daemon")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="Print decisions without moving processes")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    c = Classifier(cfg, dry_run=args.dry_run)
    try:
        c.run_forever()
    except KeyboardInterrupt:
        print("\nClassifier stopped.")

