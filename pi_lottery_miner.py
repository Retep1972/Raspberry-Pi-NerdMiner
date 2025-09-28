#!/usr/bin/env python3
"""
Pi Lottery Miner — Touch v3 (7" screen optimized)
Dark theme + optional Bitcoin background image
cpuminer/BFGMiner support • CPU temp • Human ET & odds • Jittery graph • Web /stats.json

Env (suggested):
  MINER_MODE=cpuminer            # cpuminer|bfgminer|auto|mock
  CPUMINER_LOG=/home/pi-miner/.local/share/cpuminer.log
  SHOW_ODDS_WHEN_ZERO=1
  MOCK_KHS_BASE=250
  MOCK_KHS_JITTER=25
  FULLSCREEN=1
  UI_SCALE=1.10
  WEB_PORT=8080
  BG_IMAGE=/home/pi-miner/piminer/bitcoin_bg.png
  BG_STIPPLE=gray50             # gray12..gray75 (more = darker)
"""

import json, math, os, random, re, socket, threading, time
from datetime import datetime
from typing import Optional, Tuple
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
try:
    import requests
except Exception:
    requests = None
from http.server import BaseHTTPRequestHandler, HTTPServer
APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------- Config ----------------
CONFIG = {
    "miner_mode": os.environ.get("MINER_MODE", "auto"),  # cpuminer|bfgminer|auto|mock
    "mock_khs_base": float(os.environ.get("MOCK_KHS_BASE", "250")),
    "mock_khs_jitter": float(os.environ.get("MOCK_KHS_JITTER", "25")),
    "api_host": os.environ.get("MINER_API_HOST", "127.0.0.1"),
    "api_port": int(os.environ.get("MINER_API_PORT", "4028")),
    "ui_refresh_s": 1,
    "miner_refresh_s": 1,
    "network_refresh_s": 120,
    "fullscreen": os.environ.get("FULLSCREEN", "1") == "1",
    "api_difficulty": os.environ.get("API_DIFFICULTY", "https://blockchain.info/q/getdifficulty"),
    "api_height": os.environ.get("API_HEIGHT", "https://mempool.space/api/blocks/tip/height"),
    "show_odds_when_zero": os.environ.get("SHOW_ODDS_WHEN_ZERO", "1") == "1",
    "web_port": int(os.environ.get("WEB_PORT", "8080")),
    "ui_scale": float(os.environ.get("UI_SCALE", "1.10")),
    "graph_jitter_pct": float(os.environ.get("GRAPH_JITTER_PCT", "0.06")),  # ±6% wiggle for fallback display
    "cpuminer_log": os.environ.get("CPUMINER_LOG", "/home/pi-miner/.local/share/cpuminer.log"),
    # Theme
    "theme_bg": os.environ.get("THEME_BG", "#0b0b10"),
    "theme_fg": os.environ.get("THEME_FG", "#e6e6e6"),
    "theme_accent": os.environ.get("THEME_ACCENT", "#00bfff"),
    #"bg_image": os.environ.get("BG_IMAGE", "/home/pi-miner/piminer/bitcoin_bg.png"),  # PNG/GIF (no JPG)
    "bg_image": os.environ.get("BG_IMAGE", os.path.join(APP_DIR, "bitcoin_bg.png")),
    "bg_stipple": os.environ.get("BG_STIPPLE", "gray50"),  # gray12..gray75 (more = darker)
}

def S(px: int) -> int:
    return int(round(px * CONFIG["ui_scale"]))

# --------------- Utils ------------------
def human_hashrate(hps: float) -> str:
    units = ["H/s","KH/s","MH/s","GH/s","TH/s","PH/s","EH/s"]
    v=hps; i=0
    while v>=1000 and i<len(units)-1: v/=1000; i+=1
    return (f"{v:.2f}" if v<10 else f"{v:.1f}" if v<100 else f"{v:.0f}") + " " + units[i]

def human_duration(seconds: float) -> str:
    if not seconds or not math.isfinite(seconds):
        return "—"

    years = int(seconds // (365*86400))
    if years >= 10000:
        # format with thousands separator
        return f"{years:,}".replace(",", ".") + " years"

    # Fallback to detailed breakdown
    seconds -= years * 365*86400
    days = int(seconds // 86400); seconds -= days*86400
    hours = int(seconds // 3600); seconds -= hours*3600
    minutes = int(seconds // 60); sec = int(seconds - minutes*60)

    parts = []
    if years: parts.append(f"{years}y")
    if days and len(parts) < 3: parts.append(f"{days}d")
    if hours and len(parts) < 3: parts.append(f"{hours}h")
    if minutes and len(parts) < 3: parts.append(f"{minutes}m")
    if sec and len(parts) < 3: parts.append(f"{sec}s")

    return " ".join(parts) if parts else "0s"


def _fmt_one_in(n: float) -> str:
    if not math.isfinite(n) or n <= 0: return "1 in ∞"
    if n < 1e6: return f"1 in {int(round(n)):,}"
    exp = int(math.floor(math.log10(n))); mant = n / (10**exp)
    return f"1 in {mant:.2f}e{exp}"

def fmt_prob_human(p: float) -> str:
    """≥0.01% -> '0.02% (1 in N)'; else '1 in N'."""
    if p is None or not math.isfinite(p) or p <= 0: return "—"
    pct = p * 100.0; inv = 1.0 / p
    if pct >= 0.01:
        pct_str = "100%" if pct >= 99.995 else f"{pct:.2f}%"
        return f"{pct_str} ({_fmt_one_in(inv)})"
    else:
        return _fmt_one_in(inv)

def read_cpu_temp_c() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp","r") as f:
            return int(f.read().strip())/1000.0
    except Exception:
        return None

# -------------- BFGMiner API ---------------
def query_cgminer_api(host: str, port: int, cmd: str = "summary", timeout: float = 1.0) -> Optional[dict]:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(cmd.encode("ascii")); data = s.recv(65535)
        raw = data.decode("utf-8", errors="ignore")
        if "{" in raw and "}" in raw:
            jtxt = raw[raw.find("{"): raw.rfind("}") + 1]
            try: return json.loads(jtxt)
            except Exception: pass
        result={}
        for part in raw.replace("|", ",").split(","):
            if "=" in part:
                k,v=part.split("=",1); result[k.strip()]=v.strip()
        return result or None
    except Exception:
        return None

def get_bfgminer_hashrate_hps() -> Tuple[float, str]:
    resp = query_cgminer_api(CONFIG["api_host"], CONFIG["api_port"], "summary")
    if resp:
        for key in ("MHS av","MHS 5s","MHS 1m","MHS 5m","MHS 15m","GHS av","KHS 5s","KHS av"):
            if key in resp:
                try:
                    val=float(resp[key]); factor=1e6 if key.startswith("MHS") else (1e9 if key.startswith("GHS") else 1e3)
                    return val*factor, "BFGMiner API"
                except Exception: pass
        if "KHS" in resp: return float(resp["KHS"])*1e3, "BFGMiner API"
        if "GHS" in resp: return float(resp["GHS"])*1e9, "BFGMiner API"
    return 0.0, "BFGMiner API (no data)"

# -------------- cpuminer tail --------------
CPUMINER_RATE_RE = re.compile(r"(?:thread\s+\d+:\s+)?[\d,]+\s+hashes,\s*([\d.]+)\s*(khash/s|Mhash/s|kH/s|MH/s)", re.I)

class CpuMinerTail:
    """Tail cpuminer log and extract latest hashrate."""
    def __init__(self, path: str):
        self.path = path
        self.latest_hps = 0.0
        self.lock = threading.Lock()
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop:
            try:
                with open(self.path, "r") as f:
                    f.seek(0, os.SEEK_END)  # start at end
                    while not self.stop:
                        line = f.readline()
                        if not line:
                            time.sleep(0.5); continue
                        m = CPUMINER_RATE_RE.search(line)
                        if m:
                            val = float(m.group(1))
                            unit = m.group(2).lower()
                            hps = val * (1e6 if "mhash" in unit or unit.startswith("mh") else 1e3)
                            with self.lock:
                                self.latest_hps = hps
            except FileNotFoundError:
                time.sleep(1.0)
            except Exception:
                time.sleep(1.0)

    def get_hps(self) -> float:
        with self.lock:
            return self.latest_hps

# -------------- Network -----------------
def fetch_difficulty_and_height():
    if requests is None: return None, None, "requests missing"
    try:
        d=float(requests.get(CONFIG["api_difficulty"], timeout=5).text.strip())
    except Exception as e:
        return None, None, f"difficulty err: {e}"
    try:
        h=int(requests.get(CONFIG["api_height"], timeout=5).text.strip())
    except Exception as e:
        return d, None, f"height err: {e}"
    return d, h, "OK"

def net_hps_from_diff(d: float) -> float:
    return d * (2**32) / 600.0

def expected_time_s(hps: float, diff: float) -> float:
    if hps<=0 or not diff: return float("inf")
    return diff * (2**32) / hps

def prob_in_window(hps: float, diff: float, seconds: float) -> float:
    t=expected_time_s(hps, diff)
    if not math.isfinite(t) or t<=0: return 0.0
    lam=seconds/t
    return lam if lam<1e-6 else 1.0-math.exp(-lam)

# -------------- Web mini-dashboard ------
class StatsState:
    def __init__(self):
        self.lock=threading.Lock()
        self.state={"time":None,"hashrate_hps":0.0,"display_hashrate_hps":0.0,"source":"Mock",
                    "difficulty":None,"height":None,"network_hps":None,
                    "expected_seconds":None,"expected_human":None,"odds":{},
                    "cpu_temp_c":None}
    def snapshot(self):
        with self.lock: return json.dumps(self.state).encode()
    def update(self, **kw):
        with self.lock:
            self.state.update(kw); self.state["time"]=datetime.utcnow().isoformat()+"Z"
STATS=StatsState()

def _jitter(value: float, pct: float, prev: Optional[float]) -> float:
    if value <= 0 or pct <= 0: return value
    j = value * (1.0 + random.uniform(-pct, pct))
    if prev is not None: j = 0.70 * prev + 0.30 * j
    return max(0.0, j)

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = """<!doctype html><meta charset='utf-8'><title>Pi Lottery Miner</title>
<style>
  :root { color-scheme: dark; }
  body{
    background:#0b0b10; color:#e6e6e6;
    font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:14px
  }
  .kv{display:grid;grid-template-columns:180px 1fr;gap:6px 10px}
  .kv div:nth-child(odd){color:#b8b8b8}
</style>
<h2>Pi Lottery Miner</h2>
<div class="kv">
  <div>Updated</div><div id="t">-</div>
  <div>Hashrate</div><div id="h">-</div>
  <div>Source</div><div id="s">-</div>
  <div>CPU Temp</div><div id="ct">-</div>
  <div>Block Height</div><div id="bh">-</div>
  <div>Difficulty</div><div id="d">-</div>
  <div>Network Hashrate</div><div id="nh">-</div>
  <div>Expected Time</div><div id="e">-</div>
  <div>Odds (1d)</div><div id="o1">-</div>
  <div>Odds (1y)</div><div id="o2">-</div>
  <div>Odds (10y)</div><div id="o3">-</div>
</div>
<script>
(function(){
  function fmtHash(h){var u=['H/s','KH/s','MH/s','GH/s','TH/s','PH/s','EH/s'];var v=h,i=0;while(v>=1e3&&i<u.length-1){v/=1e3;i++;}return (v<10?v.toFixed(2):v<100?v.toFixed(1):Math.round(v))+' '+u[i];}
  function text(id,val){document.getElementById(id).textContent=(val!==undefined&&val!==null&&val!==''?val:'-');}
  function load(){fetch('/stats.json').then(r=>r.json()).then(function(s){
    text('t', s.time); text('h', fmtHash(s.display_hashrate_hps||s.hashrate_hps||0)); text('s', s.source);
    text('ct', s.cpu_temp_c ? (s.cpu_temp_c.toFixed(1)+' °C') : '-');
    text('bh', s.height); text('d', s.difficulty); text('nh', fmtHash(s.network_hps||0));
    text('e', s.expected_human); var o=s.odds||{}; text('o1', o.one_day); text('o2', o.one_year); text('o3', o.ten_years);
  }).catch(function(){});}
  setInterval(load, 2000); load();
})();
</script>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/stats.json":
            data = STATS.snapshot()
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()

def run_web_server():
    try:
        HTTPServer(("0.0.0.0", CONFIG["web_port"]), WebHandler).serve_forever()
    except Exception:
        pass

# -------------- Tk App --------------
class TouchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pi Lottery Miner — Touch v3")

        # Robust fullscreen
        if CONFIG["fullscreen"]:
            def _fs_on():
                try: self.root.attributes("-fullscreen", True)
                except: pass
            self.root.after(100, _fs_on)
            self.root.bind("<Map>", lambda e: _fs_on())
            self.root.after(1000, _fs_on)
            self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))

        # Scaling
        try: self.root.call('tk', 'scaling', CONFIG["ui_scale"])
        except Exception: pass

        # --------- THEME ----------
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Dark.TFrame", background=CONFIG["theme_bg"])
        style.configure("Dark.TLabel", background=CONFIG["theme_bg"], foreground=CONFIG["theme_fg"])
        style.configure("DarkBold.TLabel", background=CONFIG["theme_bg"], foreground=CONFIG["theme_fg"],
                        font=("DejaVu Sans", S(14), "bold"))
        # Larger detail fonts
        style.configure("DarkValue.TLabel", background=CONFIG["theme_bg"], foreground=CONFIG["theme_fg"],
                        font=("DejaVu Sans", S(18)))  # tweak 18 -> 16/20 to taste
        style.configure("DarkKey.TLabel", background=CONFIG["theme_bg"], foreground="#b8b8b8",
                        font=("DejaVu Sans", S(16), "bold"))
        # --------------------------

        # Plain dark background canvas (no image here so it doesn't sit behind graph)
        self.bg = tk.Canvas(root, highlightthickness=0, bd=0, bg=CONFIG["theme_bg"])
        self.bg.pack(fill="both", expand=True)
        def _draw_bg(event=None):
            # keep it simple & solid; all foreground UI sits above
            self.bg.configure(bg=CONFIG["theme_bg"])
        self.bg.bind("<Configure>", _draw_bg)

        # Foreground container ON TOP of bg
        container = ttk.Frame(root, padding=(S(8), S(6)), style="Dark.TFrame")
        container.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

        # ===== Header row with left (text) and right (logo) =====
        header = ttk.Frame(container, style="Dark.TFrame")
        header.pack(fill="x", pady=(0, S(6)))
        header.columnconfigure(0, weight=1)  # left side grows

        left = ttk.Frame(header, style="Dark.TFrame")
        left.grid(row=0, column=0, sticky="w")

        right = ttk.Frame(header, style="Dark.TFrame")
        right.grid(row=0, column=1, sticky="e")

        # Big labels on the left
        self.lbl_hash = ttk.Label(left, text="Hashrate: --",
                                font=("DejaVu Sans", S(24), "bold"), style="Dark.TLabel")
        self.lbl_hash.pack(anchor="w")
        self.lbl_etb  = ttk.Label(left, text="Expected time: —",
                                font=("DejaVu Sans", S(22), "bold"), style="Dark.TLabel")
        self.lbl_etb.pack(anchor="w")

        # Bitcoin logo on the right (inside header "box")
        self._header_img = None
        img_path = CONFIG.get("bg_image")
        try:
            if img_path and os.path.exists(img_path):
                pil = Image.open(img_path)
                # fit within a square, preserve aspect
                target = S(96)  # adjust size if you like
                pil.thumbnail((target, target), Image.LANCZOS)
                self._header_img = ImageTk.PhotoImage(pil)
                ttk.Label(right, image=self._header_img, style="Dark.TLabel").pack(anchor="e")
            else:
                # optional tiny placeholder text if missing
                ttk.Label(right, text="", style="Dark.TLabel").pack(anchor="e")
        except Exception as e:
            ttk.Label(right, text="[img error]", style="Dark.TLabel").pack(anchor="e")

        # ===== Details grid =====
        grid = ttk.Frame(container, style="Dark.TFrame"); grid.pack(fill="x")
        labels = [
            ("Source","src"),
            ("CPU Temp","ct"),
            ("Block Height","height"),
            ("Difficulty","diff"),
            ("Network Hashrate","nethash"),
        ]
        self.kv = {}
        for i,(k,key) in enumerate(labels):
            ttk.Label(grid, text=k + ":", style="DarkKey.TLabel").grid(
                row=i, column=0, sticky="w", padx=(0,S(8)), pady=(0,S(4))
            )
            lbl = ttk.Label(grid, text="--", style="DarkValue.TLabel")
            lbl.grid(row=i, column=1, sticky="w")
            self.kv[key] = lbl
        grid.columnconfigure(1, weight=1)

        # Odds
        self.lbl_odds = ttk.Label(container, text="Odds:\n—", style="Dark.TLabel",
                                font=("DejaVu Sans", S(14)), justify="left")
        self.lbl_odds.pack(anchor="w", pady=(S(6), S(4)))

        # Graph (dark)
        self.canvas = tk.Canvas(container, height=S(150), bg="#0d0f14", highlightthickness=0)
        self.canvas.pack(fill="x", pady=(S(4), 0))
        self.canvas.bind("<Configure>", lambda e: self._draw_graph())

        # Footer hint
        ttk.Label(container,
                text=f"Web :{CONFIG['web_port']} • SHOW_ODDS_WHEN_ZERO={int(CONFIG['show_odds_when_zero'])}",
                style="Dark.TLabel", font=("DejaVu Sans", S(12))
                ).pack(anchor="w", pady=(S(4), 0))

        # State
        self.graph_data=[]; self.miner_hps=0.0; self.miner_src="Mock"
        self.difficulty=None; self.height=None; self.net_hps=None; self.cpu_temp=None
        self._stop=False

        # cpuminer tail (if enabled)
        self.cpuminer = CpuMinerTail(CONFIG["cpuminer_log"]) if CONFIG["miner_mode"]=="cpuminer" else None

        # Threads & loops
        threading.Thread(target=self._miner_loop, daemon=True).start()
        threading.Thread(target=self._network_loop, daemon=True).start()
        threading.Thread(target=self._sensors_loop, daemon=True).start()
        threading.Thread(target=run_web_server, daemon=True).start()
        self._ui_loop()


    # Data loops
    def _miner_loop(self):
        while not self._stop:
            # 1) Read hashrate & set source
            if CONFIG["miner_mode"] == "cpuminer" and self.cpuminer:
                hps = self.cpuminer.get_hps(); src = "cpuminer"
            elif CONFIG["miner_mode"] in ("auto","bfgminer"):
                hps, src = get_bfgminer_hashrate_hps()
            else:
                base=CONFIG["mock_khs_base"]; jit=CONFIG["mock_khs_jitter"]
                hps = max(0.0, random.uniform(base-jit, base+jit)) * 1e3; src = "Mock"

            self.miner_hps, self.miner_src = hps, src

            # 2) Decide what to plot (don’t affect odds math)
            if hps > 0:
                g_hps = hps  # real value, no jitter
            else:
                g_hps = CONFIG["mock_khs_base"]*1e3 if CONFIG["show_odds_when_zero"] else 0.0
                prev_val = self.graph_data[-1][1] if self.graph_data else None
                g_hps = _jitter(g_hps, CONFIG["graph_jitter_pct"], prev_val)

            # 3) Append & sleep
            now = time.time()
            self.graph_data.append((now, g_hps))
            cutoff = now - 300
            self.graph_data = [(t, v) for (t, v) in self.graph_data if t >= cutoff]
            time.sleep(CONFIG["miner_refresh_s"])

    def _network_loop(self):
        while not self._stop:
            d,h,st=fetch_difficulty_and_height()
            if d is not None:
                self.difficulty=d; self.net_hps=net_hps_from_diff(d)
            if h is not None: self.height=h
            time.sleep(CONFIG["network_refresh_s"])

    def _sensors_loop(self):
        while not self._stop:
            self.cpu_temp = read_cpu_temp_c()
            time.sleep(2)

    # Drawing
    def _draw_graph(self):
        c=self.canvas; c.delete("all")
        w,h = c.winfo_width(), c.winfo_height()

        pad = S(10)
        # axes/grid colors tuned for dark
        axis="#3b3f4a"; grid="#222631"; line=CONFIG["theme_accent"]; dot="#9edcff"; label="#c8c8c8"
        c.create_line(pad, h-pad, w-pad, h-pad, fill=axis)
        c.create_line(pad, pad, pad, h-pad, fill=axis)
        if len(self.graph_data) < 2:
            c.create_text(w//2, h//2, text="Collecting data…", fill=label, font=("DejaVu Sans", S(12))); return
        times = [t for t,_ in self.graph_data]
        vals  = [v for _,v in self.graph_data]
        tmin, tmax = min(times), max(times)
        vmax = max(vals) if max(vals)>0 else 1.0
        def fmt(hps):
            u=["H/s","KH/s","MH/s","GH/s","TH/s","PH/s","EH/s"]; v=hps; i=0
            while v>=1000 and i<len(u)-1: v/=1000; i+=1
            return f"{v:.1f} {u[i]}"
        for frac in [0.25,0.5,0.75,1.0]:
            y = h-pad - (h-2*pad)*frac
            c.create_line(pad, y, w-pad, y, fill=grid)
            c.create_text(pad-2, y, text=fmt(vmax*frac), fill=label, anchor="e", font=("DejaVu Sans", S(10)))
        span = max(1.0, tmax-tmin)
        prev=None
        for (t,v) in self.graph_data:
            x = pad + (w-2*pad) * ((t - tmin) / span)
            y = h-pad - (h-2*pad) * (v / vmax if vmax>0 else 0)
            if prev: c.create_line(prev[0], prev[1], x, y, fill=line, width=2)
            c.create_oval(x-2, y-2, x+2, y+2, outline=dot, fill=dot)
            prev=(x,y)
        c.create_text(w-pad, pad, anchor="ne", text="Hashrate (last ~5 min)", fill=label, font=("DejaVu Sans", S(11)))

    # Helpers
    def _effective_hps_for_odds(self) -> Tuple[float,str]:
        if self.miner_hps>0: return self.miner_hps, self.miner_src
        if CONFIG["show_odds_when_zero"]:
            return CONFIG["mock_khs_base"]*1e3, "Mock (odds fallback)"
        return 0.0, self.miner_src

    # UI loop
    def _ui_loop(self):
        disp_hps = self.graph_data[-1][1] if self.graph_data else self.miner_hps
        self.lbl_hash.config(text=f"Hashrate: {human_hashrate(disp_hps)}")
        eff_hps, eff_src = self._effective_hps_for_odds()

        odds_dict = {}
        if self.difficulty and eff_hps>0:
            et = expected_time_s(eff_hps, self.difficulty)
            self.lbl_etb.config(text=f"Expected time: {human_duration(et)}")
            windows=[("1 day", 86400), ("1 year", 365*86400), ("10 years", 10*365*86400)]
            lines=[]
            for label,sec in windows:
                p=prob_in_window(eff_hps, self.difficulty, sec)
                pretty = fmt_prob_human(p)
                lines.append(f"• {label}: {pretty}")
                key = ("one_day" if sec==86400 else "one_year" if sec==365*86400 else "ten_years")
                odds_dict[key] = pretty
            self.lbl_odds.config(text="Odds:\n" + "\n".join(lines))
        else:
            self.lbl_etb.config(text="Expected time: —")
            self.lbl_odds.config(text="Odds:\n—")

        # Details
        self.kv["src"].config(text=self.miner_src)
        if self.cpu_temp is not None: self.kv["ct"].config(text=f"{self.cpu_temp:.1f} °C")
        if self.height is not None: self.kv["height"].config(text=f"{self.height:,}")
        if self.difficulty is not None:
            self.kv["diff"].config(text=f"{self.difficulty:,.0f}")
            self.kv["nethash"].config(text=human_hashrate(net_hps_from_diff(self.difficulty)))

        self._draw_graph()

        # Update web stats
        et_seconds = expected_time_s(eff_hps, self.difficulty) if (self.difficulty and eff_hps>0) else None
        STATS.update(
            hashrate_hps=self.miner_hps,
            display_hashrate_hps=disp_hps,
            source=self.miner_src,
            difficulty=self.difficulty,
            height=self.height,
            network_hps=net_hps_from_diff(self.difficulty) if self.difficulty else None,
            expected_seconds=et_seconds,
            expected_human=human_duration(et_seconds) if et_seconds else None,
            odds=odds_dict,
            cpu_temp_c=self.cpu_temp,
        )

        if not self._stop:
            self.root.after(int(CONFIG["ui_refresh_s"]*1000), self._ui_loop)

    def stop(self):
        self._stop=True

# -------------- Main --------------------
def main():
    if requests is None:
        root = tk.Tk()
        app = TouchApp(root)
        messagebox.showwarning("Missing dependency", "Install 'requests' for live difficulty:\n  pip3 install requests")
        root.protocol("WM_DELETE_WINDOW", app.stop)
        root.mainloop(); return
    root = tk.Tk()
    app = TouchApp(root)
    root.protocol("WM_DELETE_WINDOW", app.stop)
    root.mainloop()

if __name__ == "__main__":
    main()
