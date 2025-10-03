#!/usr/bin/env python3
"""
screen_power_therm_guard.py
- Auto blank/dim Raspberry Pi touchscreen on idle; wake on touch.
- Monitor CPU temperature; cleanly shutdown if threshold exceeded.

Requires: Python 3 on Linux (Raspberry Pi). No third-party libs.

Backlight control via /sys/class/backlight/*  (root required).
Touch detection via /dev/input/event*.
"""

import os, re, time, sys, glob, select, argparse, threading, subprocess
from datetime import datetime

# -------- Defaults (can be overridden by CLI flags or environment) --------
DEFAULT_TOUCH_RE        = os.environ.get("TOUCH_REGEX", r"(?i)(touch|fts|ft5406|goodix|capacitive)")
DEFAULT_IDLE_SECS       = int(os.environ.get("IDLE_SECS", "300"))       # 5 min
DEFAULT_MODE            = os.environ.get("MODE", "off").lower()         # "off" or "dim"
DEFAULT_DIM_BRIGHTNESS  = int(os.environ.get("DIM_BRIGHTNESS", "25"))
DEFAULT_BACKLIGHT_PATH  = os.environ.get("BACKLIGHT_PATH")              # e.g. /sys/class/backlight/rpi_backlight

DEFAULT_TEMP_THRESHOLD  = float(os.environ.get("TEMP_THRESHOLD", "75.0"))  # °C
DEFAULT_CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "30"))      # seconds
DEFAULT_LOG_FILE        = os.environ.get("LOG_FILE", "/var/log/pi_guard.log")
DEFAULT_GRACE_READS     = int(os.environ.get("GRACE_READS", "2"))          # consecutive hot reads before shutdown

PROC_INPUT = "/proc/bus/input/devices"

# ------------------------ Helpers: touch + backlight ------------------------
def find_touch_event(name_regex: str):
    """Find a touchscreen-like /dev/input/eventX via /proc/bus/input/devices."""
    try:
        with open(PROC_INPUT, "r", encoding="utf-8", errors="ignore") as f:
            blocks = f.read().split("\n\n")
        pat = re.compile(name_regex)
        for b in blocks:
            name_m = re.search(r'Name="([^"]+)"', b)
            if name_m and pat.search(name_m.group(1)):
                h = re.search(r'Handlers=.*?(event\d+)', b)
                if h:
                    ev = "/dev/input/" + h.group(1)
                    if os.path.exists(ev):
                        return ev, name_m.group(1)
    except Exception:
        pass
    # fallback: first event device
    cand = sorted(glob.glob("/dev/input/event*"))
    return (cand[0], os.path.basename(cand[0])) if cand else (None, None)

def find_backlight_path(explicit: str | None):
    if explicit and os.path.isdir(explicit):
        return explicit
    for path in sorted(glob.glob("/sys/class/backlight/*")):
        return path
    return None

def read_int(path, default=None):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return default

def write_str(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception as e:
        log(f"write {path} failed: {e}")
        return False

class Backlight:
    def __init__(self, base):
        self.base = base
        self.brightness = os.path.join(base, "brightness")
        self.max_brightness = read_int(os.path.join(base, "max_brightness"), 255)
        self.bl_power = os.path.join(base, "bl_power")  # 0=on, 1=off (most drivers)
        self.prev_brightness = read_int(self.brightness, self.max_brightness) or 200

    def on(self):
        write_str(self.brightness, self.prev_brightness)
        write_str(self.bl_power, 0)

    def off(self):
        cur = read_int(self.brightness, self.prev_brightness)
        if cur: self.prev_brightness = cur
        # Try power off; if not supported, set brightness 0
        if not write_str(self.bl_power, 1):
            write_str(self.brightness, 0)

    def dim(self, level):
        cur = read_int(self.brightness, self.prev_brightness)
        if cur: self.prev_brightness = cur
        write_str(self.bl_power, 0)  # ensure on
        level = max(1, min(level, self.max_brightness))
        write_str(self.brightness, level)

# --------------------------- Helpers: temperature ---------------------------
def get_cpu_temp_c() -> float | None:
    """Read CPU temperature in Celsius (vcgencmd preferred)."""
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        return float(out.replace("temp=", "").replace("'C\n", ""))
    except Exception:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp","r") as f:
                return int(f.read().strip())/1000.0
        except Exception:
            return None

# -------------------------------- Logging ----------------------------------
LOG_FILE = DEFAULT_LOG_FILE
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except PermissionError:
        # Not fatal; still printed to stdout
        pass

# ------------------------------- Threads -----------------------------------
class TouchIdleThread(threading.Thread):
    def __init__(self, idle_secs, mode, dim_brightness, backlight_path, touch_re):
        super().__init__(daemon=True)
        self.idle_secs = idle_secs
        self.mode = mode
        self.dim_brightness = dim_brightness
        self.backlight_path = backlight_path
        self.touch_re = touch_re
        self.stop_flag = threading.Event()

    def run(self):
        event_path, dev_name = find_touch_event(self.touch_re)
        if not event_path:
            log("No input event device found; TouchIdleThread exiting.")
            return
        bl_base = find_backlight_path(self.backlight_path)
        if not bl_base:
            log("No backlight path found; TouchIdleThread exiting.")
            return

        log(f"Touch: {event_path} ({dev_name})  •  Backlight: {bl_base}")
        bl = Backlight(bl_base)

        fd = os.open(event_path, os.O_RDONLY | os.O_NONBLOCK)
        poller = select.poll()
        poller.register(fd, select.POLLIN)

        last_activity = time.time()
        blanked = False
        dimmed = False

        try:
            while not self.stop_flag.is_set():
                events = poller.poll(500)  # 0.5s
                now = time.time()

                if events:
                    try: os.read(fd, 4096)
                    except BlockingIOError: pass
                    last_activity = now
                    if blanked or dimmed:
                        bl.on()
                        blanked = False
                        dimmed = False
                        time.sleep(0.05)  # debounce

                idle = now - last_activity
                if idle >= self.idle_secs:
                    if self.mode == "off":
                        if not blanked:
                            log("Idle exceeded: turning backlight OFF")
                            bl.off()
                            blanked = True
                            dimmed = False
                    else:
                        if not dimmed:
                            log(f"Idle exceeded: DIM backlight to {self.dim_brightness}")
                            bl.dim(self.dim_brightness)
                            dimmed = True
                            blanked = False
                time.sleep(0.05)
        finally:
            try: os.close(fd)
            except Exception: pass

class TempGuardThread(threading.Thread):
    def __init__(self, threshold_c, check_interval_s, grace_reads):
        super().__init__(daemon=True)
        self.thresh = threshold_c
        self.interval = check_interval_s
        self.grace_reads = grace_reads
        self.stop_flag = threading.Event()

    def run(self):
        consecutive_hot = 0
        while not self.stop_flag.is_set():
            t = get_cpu_temp_c()
            if t is not None:
                log(f"CPU Temp: {t:.1f}°C")
                if t > self.thresh:
                    consecutive_hot += 1
                    log(f"Above threshold ({self.thresh:.1f}°C): {consecutive_hot}/{self.grace_reads}")
                    if consecutive_hot >= self.grace_reads:
                        log("Threshold exceeded persistently. Initiating shutdown.")
                        # Use systemd to shutdown cleanly
                        os.system("systemctl poweroff")
                        return
                else:
                    consecutive_hot = 0
            else:
                log("Could not read CPU temperature.")
            for _ in range(self.interval * 10):
                if self.stop_flag.is_set(): return
                time.sleep(0.1)

# --------------------------------- Main ------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Touch idle backlight + CPU overheat guard.")
    ap.add_argument("--idle-secs", type=int, default=DEFAULT_IDLE_SECS)
    ap.add_argument("--mode", choices=["off","dim"], default=DEFAULT_MODE)
    ap.add_argument("--dim-brightness", type=int, default=DEFAULT_DIM_BRIGHTNESS)
    ap.add_argument("--backlight", default=DEFAULT_BACKLIGHT_PATH)
    ap.add_argument("--touch-re", default=DEFAULT_TOUCH_RE)

    ap.add_argument("--temp-threshold", type=float, default=DEFAULT_TEMP_THRESHOLD)
    ap.add_argument("--check-interval", type=int, default=DEFAULT_CHECK_INTERVAL)
    ap.add_argument("--grace-reads", type=int, default=DEFAULT_GRACE_READS)

    ap.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return ap.parse_args()

def main():
    global LOG_FILE
    args = parse_args()
    LOG_FILE = args.log_file

    log("Starting screen_power_therm_guard…")

    t1 = TouchIdleThread(
        idle_secs=args.idle_secs,
        mode=args.mode,
        dim_brightness=args.dim_brightness,
        backlight_path=args.backlight,
        touch_re=args.touch_re,
    )
    t2 = TempGuardThread(
        threshold_c=args.temp_threshold,
        check_interval_s=args.check_interval,
        grace_reads=args.grace_reads,
    )
    t1.start(); t2.start()

    try:
        while t1.is_alive() and t2.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        t1.stop_flag.set(); t2.stop_flag.set()
        log("Exiting screen_power_therm_guard.")

if __name__ == "__main__":
    main()
