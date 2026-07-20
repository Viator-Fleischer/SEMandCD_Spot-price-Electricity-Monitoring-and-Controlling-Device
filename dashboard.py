"""
dashboard.py — Web GUI for the spot-price electricity controller.

Run:  python dashboard.py
Then open http://<pi-ip>:5000 from any device on your home network.

Features:
  - Today's hourly price curve (SVG chart, no JS dependencies)
  - Live relay status with ON/OFF indicators
  - Edit thresholds per appliance and save to config.yaml instantly
  - Dry-run toggle and manual force-on/off per relay
  - Auto-refreshes every 60 seconds
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
CONFIG     = BASE_DIR / "config.yaml"
LOG_FILE   = BASE_DIR / "logs" / "controller.log"
OVERRIDES  = BASE_DIR / "overrides.json" 

app = Flask(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG) as f:
        return yaml.safe_load(f)

def save_config(cfg: dict):
    with open(CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

def get_prices() -> list[dict]:
    """Reuse PriceFetcher — cached so no extra API calls."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from price_fetcher import PriceFetcher
        cfg = load_config()
        fetcher = PriceFetcher(area=cfg.get("nordpool_area", "FI"),
                               currency=cfg.get("currency", "EUR"))
        return fetcher.get_todays_prices()
    except Exception as e:
        return []

def get_current_price(prices: list[dict]) -> float | None:
    now_hour = datetime.now().hour
    for p in prices:
        if p["hour"] == now_hour:
            return p["price_cKwh"]
    return None

def get_log_tail(n: int = 30) -> list[str]:
    try:
        lines = LOG_FILE.read_text().splitlines()
        meaningful = [
            l for l in lines
            if l.strip()
            and not all(c in "─ " for c in l.strip())
            and "Appliance" not in l
            and "Threshold  Status" not in l
        ]
        return meaningful[-n:]
    except Exception:
        return ["(lokitiedostoa ei löydy — onko ohjainta ajettu vielä?)"]

def load_overrides() -> dict:
    try:
        import json
        return json.loads(OVERRIDES.read_text())
    except Exception:
        return {}

def save_overrides(data: dict):
    import json
    OVERRIDES.write_text(json.dumps(data, indent=2))

def compute_active_hours(cfg_dict: dict, prices: list) -> set:
    """Set of hours a cheapest-hours appliance runs. Mirrors controller logic."""
    from collections import defaultdict
    buckets = defaultdict(list)
    for p in prices:
        if p.get("price_cKwh") is not None:
            buckets[p["hour"]].append(p["price_cKwh"])
    price_by_hour = {h: sum(v) / len(v) for h, v in buckets.items()}
    if not price_by_hour:
        return set()

    def pick_windowed(hours_list, window_hours, per_window):
        selected = set()
        wh = max(1, int(window_hours))
        pw = max(0, int(per_window))
        for i in range(0, len(hours_list), wh):
            window = hours_list[i:i + wh]
            ranked = sorted([h for h in window if h in price_by_hour],
                            key=lambda h: price_by_hour[h])
            selected.update(ranked[:pw])
        return selected

    if "night_start" in cfg_dict and "night_end" in cfg_dict:
        ns = int(cfg_dict["night_start"]) % 24
        ne = int(cfg_dict["night_end"]) % 24
        night_hours = []
        h = ns
        while h != ne:
            night_hours.append(h)
            h = (h + 1) % 24
        night_set = set(night_hours)
        day_hours = []
        h = ne
        for _ in range(24):
            if h not in night_set:
                day_hours.append(h)
            h = (h + 1) % 24
            if len(day_hours) >= (24 - len(night_set)):
                break
        active = set()
        active |= pick_windowed(night_hours,
                                cfg_dict.get("night_window_hours", len(night_hours) or 1),
                                cfg_dict.get("night_per_window", 1))
        active |= pick_windowed(day_hours,
                                cfg_dict.get("day_window_hours", len(day_hours) or 1),
                                cfg_dict.get("day_per_window", 1))
        return active

    if "window_hours" in cfg_dict:
        return pick_windowed(list(range(24)),
                             cfg_dict.get("window_hours", 3),
                             cfg_dict.get("per_window", 1))

    n = int(cfg_dict.get("cheapest_hours", 0))
    ranked = sorted(price_by_hour.keys(), key=lambda h: price_by_hour[h])
    return set(ranked[:n])


def cheapest_submode(r: dict) -> str:
    """Which cheapest-hours sub-mode a relay is in: 'nightday' | 'fullday' | 'plain'."""
    if "night_start" in r and "night_end" in r:
        return "nightday"
    if "window_hours" in r:
        return "fullday"
    return "plain"


def cheapest_params(r: dict) -> dict:
    """Current cheapest-hours parameters with sensible defaults for the edit form."""
    return {
        "window_hours":       int(r.get("window_hours", 3)),
        "per_window":         int(r.get("per_window", 1)),
        "night_start":        int(r.get("night_start", 22)),
        "night_end":          int(r.get("night_end", 7)),
        "night_window_hours": int(r.get("night_window_hours", 3)),
        "night_per_window":   int(r.get("night_per_window", 1)),
        "day_window_hours":   int(r.get("day_window_hours", 5)),
        "day_per_window":     int(r.get("day_per_window", 1)),
    }


def cheapest_label(r: dict) -> str:
    """Human-readable badge text describing the cheapest-hours sub-mode."""
    if "night_start" in r and "night_end" in r:
        return (f"AUTO — yö {r.get('night_per_window',1)}h/"
                f"{r.get('night_window_hours','?')}h · "
                f"päivä {r.get('day_per_window',1)}h/{r.get('day_window_hours','?')}h")
    if "window_hours" in r:
        return f"AUTO — {r.get('per_window',1)}h joka {r.get('window_hours')}h"
    return f"AUTO — {int(r.get('cheapest_hours',0))} halvinta tuntia"


def relay_statuses(cfg: dict, current_price: float | None, prices: list = None) -> list[dict]:
    overrides = load_overrides()
    now = datetime.now().timestamp()
    results = []
    prices = prices or []
    for r in cfg.get("relays", []):
        name = r["name"]
        relay_mode = r.get("mode", "threshold")

        # ── Cheapest-hours appliances: read-only, automatic ──
        if relay_mode == "cheapest_hours":
            killed = bool(overrides.get(name, {}).get("killed"))
            active = compute_active_hours(r, prices)
            current_hour = datetime.now().hour
            if killed:
                is_on = False
            elif not active:
                is_on = None
            else:
                is_on = current_hour in active
            results.append({
                "name":           name,
                "gpio_pin":       r["gpio_pin"],
                "threshold":      0.0,
                "is_on":          is_on,
                "priority":       r.get("priority", 99),
                "mode":           "cheapest_hours",
                "remaining":      0,
                "expires":        0,
                "cheapest_hours": len(active),
                "cheapest_set":   sorted(active),
                "cheapest_label": cheapest_label(r),
                "submode":        cheapest_submode(r),
                "params":         cheapest_params(r),
                "killed":         killed,
            })
            continue

        # ── Threshold appliances (with manual override) ──
        thresh = float(r.get("threshold", 0.0))
        ov = overrides.get(name, {})
        mode = ov.get("mode", "auto")   # auto | manual_on | manual_off
        expires = ov.get("expires", 0)  # 0 = indefinite

        if mode != "auto" and expires > 0 and now > expires:
            mode = "auto"
            overrides[name] = {"mode": "auto", "expires": 0}
            save_overrides(overrides)

        killed = bool(ov.get("killed"))
        if killed:
            is_on = False
        elif mode == "manual_on":
            is_on = True
        elif mode == "manual_off":
            is_on = False
        elif current_price is None:
            is_on = None
        else:
            is_on = current_price <= thresh

        remaining = 0
        if mode != "auto" and expires > 0:
            remaining = max(0, int((expires - now) / 60))

        results.append({
            "name":           name,
            "gpio_pin":       r["gpio_pin"],
            "threshold":      thresh,
            "is_on":          is_on,
            "priority":       r.get("priority", 99),
            "mode":           mode,
            "remaining":      remaining,
            "expires":        expires,
            "cheapest_hours": 0,
            "cheapest_set":   [],
            "cheapest_label": "",
            "submode":        "",
            "params":         {},
            "killed":         killed,
        })
    return results

# ── HTML template ─────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<eeta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"> 
<title>⚡ Pörssisähkön Kontrolleri</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #0a0e17;
    --panel:     #111827;
    --border:    #1e293b;
    --accent:    #00e5ff;
    --accent2:   #ff6b35;
    --green:     #00ff88;
    --red:       #ff3355;
    --muted:     #4b5563;
    --text:      #e2e8f0;
    --text-dim:  #94a3b8;
    --mono:      'Share Tech Mono', monospace;
    --sans:      'Exo 2', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background-image: url('https://a-com.neocities.org/rosy_wallpaper.JPG');
    background-size: cover;
    color: var(--text);
    font-family: var(--sans);
    font-weight: 300;
    min-height: 100vh;
    padding: 0 0 4rem;
  }

  /* ── Header ── */
  header {
    background: linear-gradient(135deg, #0d1526 0%, #111827 100%);
    border-bottom: 1px solid var(--border);
    padding: 1.5rem 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .logo {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 1.4rem;
    letter-spacing: .05em;
    color: var(--accent);
    text-transform: uppercase;
  }
  .logo span { color: var(--text-dim); font-weight: 300; }
  .timestamp {
    font-family: var(--mono);
    font-size: .8rem;
   /* color: var(--muted); */
  }
  .pulse-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    margin-right: .5rem;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; transform:scale(1); }
    50%      { opacity:.4; transform:scale(.7); }
  }

  /* ── Layout ── */
  .container { max-width: 1300px; margin: 0 auto; padding: 2rem; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
  .grid-2.wide { grid-template-columns: 2fr 1fr; }
  @media(max-width:720px) {
    .grid-2, .grid-2.wide { grid-template-columns: 1fr; }
  }

  /* ── Panel ── */
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    position: relative;
    overflow: hidden;
  }
  .panel::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
  }
  .panel-title {
    font-family: var(--mono);
    font-size: .85rem;
    letter-spacing: .15em;
   /* color: var(--muted); */
    text-transform: uppercase;
    margin-bottom: 1.2rem;
  }

  /* ── Price hero ── */
  .price-hero {
    display: flex;
    align-items: baseline;
    gap: .4rem;
    margin-bottom: .3rem;
  }
  .price-big {
    font-size: 3.5rem;
    font-weight: 800;
    font-family: var(--mono);
    line-height: 1;
    color: var(--accent);
  }
  .price-unit { color: /*var(--text-dim) */;
  font-size: 1rem; }

  .price-avg  { color: /*var(--muted) */;
  font-size: .85rem; 
  font-family: var(--mono); 
  margin-top:.3rem; }

  /* ── Price chart ── */
  .chart-wrap { width: 100%; overflow-x: auto; margin-top: .5rem; }
  svg.price-chart { width: 100%; min-width: 500px; height: 180px; }

  /* ── Chart expand button ── */
  .chart-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.2rem;
  }
  .chart-header .panel-title { margin-bottom: 0; }
  .btn-expand {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-family: var(--mono);
    font-size: .7rem;
    padding: .3rem .7rem;
    border-radius: 5px;
    cursor: pointer;
    letter-spacing: .08em;
    transition: border-color .2s, color .2s;
  }
  .btn-expand:hover { border-color: var(--accent); color: var(--accent); }

  /* ── Chart modal ── */
  .chart-modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,.85);
    backdrop-filter: blur(4px);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }
  .chart-modal.open { display: flex; }
  .chart-modal-inner {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 1.5rem 2rem 2rem;
    width: 100%;
    max-width: 1100px;
    position: relative;
  }
  .chart-modal-inner::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    border-radius: 14px 14px 0 0;
    background: linear-gradient(90deg, var(--accent), transparent);
  }
  .chart-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.2rem;
  }
  .chart-modal svg.price-chart {
    width: 100%;
    min-width: unset;
    height: 420px;
  }
  .btn-close {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-family: var(--mono);
    font-size: .85rem;
    padding: .35rem .9rem;
    border-radius: 5px;
    cursor: pointer;
    transition: border-color .2s, color .2s;
  }
  .btn-close:hover { border-color: var(--red); color: var(--red); }

  /* ── Relay cards ── */
  .relay-list { display: flex; flex-direction: column; gap: .75rem; }
  .relay-card {
    display: flex;
    align-items: center;
    gap: 1rem;
    background: #0d1526;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: .9rem 1.1rem;
    transition: border-color .2s;
  }
  .relay-card:hover { border-color: var(--accent); }
  .relay-card { cursor: pointer; user-select: none; }
  .relay-card.manual-on  { border-color: var(--green); }
  .relay-card.manual-off { border-color: var(--red); }
  .relay-card.manual-on:hover  { border-color: var(--green); opacity: .85; }
  .relay-card.manual-off:hover { border-color: var(--red);   opacity: .85; }
  .relay-card.cheapest { cursor: default; border-color: var(--accent2); }
  .relay-card.cheapest:hover { border-color: var(--accent2); }
  .cheapest-badge {
    font-family: var(--mono);
    font-size: .68rem;
    letter-spacing: .05em;
    color: var(--accent2);
    border: 1px solid var(--accent2);
    border-radius: 4px;
    padding: .2rem .5rem;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .btn-edit {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-family: var(--mono);
    font-size: .68rem;
    padding: .3rem .6rem;
    border-radius: 5px;
    cursor: pointer;
    letter-spacing: .05em;
    transition: border-color .2s, color .2s;
  }
  .btn-edit:hover { border-color: var(--accent2); color: var(--accent2); }
  .cheapest-edit {
    background: #0d1526;
    border: 1px solid var(--accent2);
    border-radius: 8px;
    padding: 1rem;
    margin: .3rem 0 .5rem;
  }
  .cheapest-edit-row {
    display: flex;
    align-items: center;
    gap: .6rem;
    margin-bottom: .7rem;
  }
  .cheapest-edit-row label {
    font-family: var(--mono);
    font-size: .72rem;
    color: var(--text-dim);
  }
  .cheapest-fields { margin-bottom: .5rem; }
  .cheapest-field {
    display: flex;
    align-items: center;
    gap: .5rem;
    flex-wrap: wrap;
    margin-bottom: .5rem;
    font-family: var(--mono);
    font-size: .72rem;
    color: var(--text-dim);
  }
  .cheapest-hours-list {
    flex: 1;
    font-family: var(--mono);
    font-size: .68rem;
    color: var(--text-dim);
    line-height: 1.4;
  }
  .relay-mode {
    font-family: var(--mono);
    font-size: .65rem;
    letter-spacing: .08em;
    text-transform: uppercase;
    padding: .15rem .4rem;
    border-radius: 3px;
    margin-left: .3rem;
  }
  .relay-mode.auto    { color: var(--muted); border: 1px solid var(--border); }
  .relay-mode.manual-on  { color: var(--green); border: 1px solid var(--green); }
  .relay-mode.manual-off { color: var(--red);   border: 1px solid var(--red); }
  .relay-duration {
    display: flex;
    align-items: center;
    gap: .3rem;
    margin-top: .5rem;
    font-size: .75rem;
    color: var(--text-dim);
    font-family: var(--mono);
  }
  .relay-duration input {
    width: 55px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--accent);
    font-family: var(--mono);
    font-size: .8rem;
    padding: .2rem .4rem;
    text-align: right;
  }
  .relay-duration input:focus { outline: none; border-color: var(--accent); }
  .manual-toggle {
    display: flex;
    align-items: center;
    gap: .35rem;
    font-family: var(--mono);
    font-size: .72rem;
    color: var(--text-dim);
    cursor: pointer;
    user-select: none;
  }
  .manual-toggle input[type=checkbox] {
    width: 16px; height: 16px;
    accent-color: var(--green);
    cursor: pointer;
  }
  .manual-state {
    font-weight: 600;
    letter-spacing: .05em;
    text-transform: uppercase;
  }
  .manual-state.on  { color: var(--green); }
  .manual-state.off { color: var(--red); }
  .manual-label {
    font-family: var(--mono);
    font-size: .72rem;
    color: var(--text-dim);
    letter-spacing: .05em;
  }
  .manual-control { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
  .manual-options { display: flex; align-items: center; gap: .4rem; }
  .manual-select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text);
    font-family: var(--mono);
    font-size: .72rem;
    padding: .25rem .4rem;
    cursor: pointer;
  }
  .manual-select:focus { outline: none; border-color: var(--accent); }
  .countdown { font-family: var(--mono); font-size: .72rem; color: var(--accent2); }
  .manual-indef { font-family: var(--mono); font-size: .72rem; color: var(--text-dim); }
  .btn-start {
    background: transparent;
    border: 1px solid var(--green);
    color: var(--green);
    font-family: var(--sans);
    font-weight: 600;
    font-size: .72rem;
    padding: .3rem .8rem;
    border-radius: 5px;
    cursor: pointer;
    letter-spacing: .05em;
    text-transform: uppercase;
    transition: background .2s, color .2s;
  }
  .btn-start:hover { background: var(--green); color: var(--bg); }
  .relay-indicator {
    width: 12px; height: 12px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .relay-indicator.on  { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .relay-indicator.off { background: var(--red);   box-shadow: 0 0 8px var(--red); }
  .relay-indicator.unknown { background: var(--muted); }
  .relay-name  { flex: 1; font-weight: 600; font-size: .95rem; }
  .relay-thresh {
    font-family: var(--mono);
    font-size: .8rem;
    color: var(--text-dim);
  }
  .relay-status {
    font-family: var(--mono);
    font-size: .75rem;
    font-weight: 600;
    letter-spacing: .08em;
    min-width: 3.5rem;
    text-align: right;
  }
  .relay-status.on  { color: var(--green); }
  .relay-status.off { color: var(--red); }
  /* Killswitch button styling */
  .relay-status {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: .3rem .6rem;
    cursor: pointer;
    font-family: var(--mono);
    transition: border-color .2s, background .2s, color .2s;
  }
  .relay-status:hover { border-color: var(--red); }
  .relay-status.on:hover  { border-color: var(--red); }
  .relay-status.killed {
    background: #2a0a0a;
    border-color: var(--red);
    color: var(--red);
    font-weight: 700;
    letter-spacing: .05em;
    animation: killpulse 1.6s ease-in-out infinite;
  }
  @keyframes killpulse {
    0%,100% { opacity: 1; }
    50%     { opacity: .55; }
  }

  /* ── Threshold form ── */
  .threshold-form { display: flex; flex-direction: column; gap: .9rem; }
  .thresh-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: center;
    gap: .7rem;
    background: #0d1526;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: .75rem 1rem;
  }
  .thresh-label { font-size: .9rem; font-weight: 600; }
  .thresh-input {
    width: 90px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--accent);
    font-family: var(--mono);
    font-size: .95rem;
    padding: .4rem .6rem;
    text-align: right;
    transition: border-color .2s;
  }
  .thresh-input:focus { outline: none; border-color: var(--accent); }

  .thresh-unit { font-family: /* var(--mono) */;
  font-size: .75rem; 
  /* color: var(--muted); */
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: .4rem;
    background: transparent;
    border: 1px solid var(--accent);
    color: var(--accent);
    font-family: var(--sans);
    font-weight: 600;
    font-size: .85rem;
    padding: .6rem 1.4rem;
    border-radius: 6px;
    cursor: pointer;
    letter-spacing: .05em;
    transition: background .2s, color .2s;
    text-transform: uppercase;
  }
  .btn:hover { background: var(--accent); color: var(--bg); }
  .btn.danger { border-color: var(--red); color: var(--red); }
  .btn.danger:hover { background: var(--red); color: #fff; }
  .btn-row { display: flex; gap: .8rem; margin-top: .5rem; flex-wrap: wrap; }

  /* ── Log ── */
  .log-box {
    background: #070b12;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    font-family: var(--mono);
    font-size: .60rem;
    line-height: 0.5;
    color: var(--text-dim);
    max-height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-line.info    { color: #7dd3fc; }
  .log-line.warn    { color: #fbbf24; }
  .log-line.error   { color: var(--red); }
  .log-line.off     { color: var(--red); }
  .log-line.on      { color: var(--green); }

  /* ── Flash message ── */
  .flash {
    background: #052e16;
    border: 1px solid #16a34a;
    color: var(--green);
    padding: .75rem 1.25rem;
    border-radius: 8px;
    font-family: var(--mono);
    font-size: .85rem;
    margin-bottom: 1.5rem;
  }

  /* ── Misc ── */
  .section-gap { margin-top: 1.5rem; }
  .dry-run-badge {
    display: inline-block;
    background: #2d1b00;
    border: 1px solid var(--accent2);
    color: var(--accent2);
    font-family: var(--mono);
    font-size: .7rem;
    letter-spacing: .1em;
    padding: .2rem .6rem;
    border-radius: 4px;
    text-transform: uppercase;
    margin-left: .5rem;
  }
</style>
</head>
<body>

<header>
  <div class="logo">⚡ Pörssisähkö<span>Kontrolleri</span></div>
  <div class="timestamp"> 
    <span class="pulse-dot"></span> 
    {{ now }} &nbsp;·&nbsp; Sivupäivitys 60s jälkeen 
  </div>
</header>

<div class="container">

{% if flash %}
<div class="flash" id="flashMsg">
  {{ flash }}
  <span onclick="document.getElementById('flashMsg').style.display='none'"
        style="float:right;cursor:pointer;opacity:.6;margin-left:1rem">✕</span>
</div>
{% endif %}

  <!-- Row 1: price chart + log -->
  <div class="grid-2 wide">

    <!-- Left: current price + chart -->
    <div class="panel">
      <div class="chart-header">
        <div class="panel-title">Nord Pool FI — Tämän Päivän spotti-hinnat</div>
        <button class="btn-expand" onclick="document.getElementById('chartModal').classList.add('open')">
          ⤢ LAAJENNA
        </button>
      </div>
      <div class="price-hero">
        <div class="price-big">
          {% if current_price is not none %}{{ "%.2f"|format(current_price) }}{% else %}—{% endif %}
        </div>
        <div class="price-unit">c/kWh</div>
      </div>
      <div class="price-avg">
        Daily avg: {% if avg is not none %}{{ "%.2f"|format(avg) }} c/kWh{% else %}—{% endif %}
        &nbsp;·&nbsp; Min: {{ "%.2f"|format(min_p) }} &nbsp;·&nbsp; Max: {{ "%.2f"|format(max_p) }}
      </div>

      <div class="chart-wrap section-gap">
        {{ chart_svg | safe }}
      </div>
    </div>

    <!-- Chart expand modal -->
    <div class="chart-modal" id="chartModal" onclick="if(event.target===this)this.classList.remove('open')">
      <div class="chart-modal-inner">
        <div class="chart-modal-header">
          <div class="panel-title" style="margin-bottom:0">Nord Pool FI — Tämän Päivän spotti-hinnat</div>
          <button class="btn-close" onclick="document.getElementById('chartModal').classList.remove('open')">
            ✕ SULJE
          </button>
        </div>
        {{ chart_svg | safe }}
      </div>
    </div>

    <!-- Right: log -->
    <div class="panel">
      <div class="panel-title">Ohjaimen lokitiedot (Viimeiset 30 riviä)</div>
      <div class="log-box" id="log">
        {% for line in log_lines %}
        <div class="log-line
          {% if 'ERROR' in line %}error
          {% elif 'WARNING' in line %}warn
          {% elif '→ OFF' in line %}off
          {% elif '→ ON' in line %}on
          {% else %}info{% endif %}
        ">{{ line }}</div>
        {% endfor %}
      </div>
    </div>

  </div>

  <!-- Row 2: combined relay control + threshold editor -->
  <div class="section-gap">
    <div class="panel">
      <div class="panel-title">Releiden Ohjaus &amp; Raja-arvot</div>
      <form method="POST" action="/save_thresholds">
        <div class="relay-list">
          {% for r in relays %}
          {% if r.mode == 'cheapest_hours' %}
          <!-- Cheapest-hours appliance: automatic, with editable interval settings -->
          <div class="relay-card cheapest">
            <div class="relay-indicator {{ 'on' if r.is_on else ('off' if r.is_on is not none else 'unknown') }}"></div>
            <div class="relay-name">{{ r.name }}</div>
            <span class="cheapest-badge">{{ r.cheapest_label }}</span>
            <div class="cheapest-hours-list">
              {% if r.cheapest_set %}
                Tunnit: {% for h in r.cheapest_set %}{{ '%02d'|format(h) }}{% if not loop.last %}, {% endif %}{% endfor %}
              {% else %}
                (hintatietoja ei saatavilla)
              {% endif %}
            </div>
            <button type="button" class="btn-edit"
                    onclick="toggleEdit({{ loop.index0 }})">⚙ muokkaa</button>
            <button type="button"
                    class="relay-status {{ 'killed' if r.killed else ('on' if r.is_on else ('off' if r.is_on is not none else '')) }}"
                    title="{{ 'Huoltokytkin päällä — klikkaa palauttaaksesi' if r.killed else 'Klikkaa katkaistaksesi virran (huolto)' }}"
                    onclick="toggleKill('{{ r.name }}')">
              {% if r.killed %}HUOLTO{% elif r.is_on is none %}?{% elif r.is_on %}ON{% else %}OFF{% endif %}
            </button>
          </div>

          <!-- Collapsible interval/mode editor -->
          <div class="cheapest-edit" id="edit_{{ loop.index0 }}" style="display:none">
            <div class="cheapest-edit-row">
              <label>Tila:</label>
              <select class="manual-select" id="submode_{{ loop.index0 }}"
                      onchange="onSubmode({{ loop.index0 }}, this.value)">
                <option value="fullday" {{ 'selected' if r.submode != 'nightday' else '' }}>koko päivä</option>
                <option value="nightday" {{ 'selected' if r.submode == 'nightday' else '' }}>yö + päivä</option>
              </select>
            </div>

            <!-- Full-day fields -->
            <div class="cheapest-fields" id="fullday_{{ loop.index0 }}"
                 style="{{ '' if r.submode != 'nightday' else 'display:none' }}">
              <div class="cheapest-field">
                <span>tunteja per ikkuna:</span>
                <input type="number" min="0" class="thresh-input" style="width:60px"
                       id="fd_per_{{ loop.index0 }}" value="{{ r.params.per_window }}">
              </div>
              <div class="cheapest-field">
                <span>ikkunan koko (h):</span>
                <input type="number" min="1" max="24" class="thresh-input" style="width:60px"
                       id="fd_win_{{ loop.index0 }}" value="{{ r.params.window_hours }}">
              </div>
            </div>

            <!-- Night+day fields -->
            <div class="cheapest-fields" id="nightday_{{ loop.index0 }}"
                 style="{{ 'display:none' if r.submode != 'nightday' else '' }}">
              <div class="cheapest-field">
                <span>yö alkaa (h):</span>
                <input type="number" min="0" max="23" class="thresh-input" style="width:55px"
                       id="nd_ns_{{ loop.index0 }}" value="{{ r.params.night_start }}">
                <span>yö loppuu (h):</span>
                <input type="number" min="0" max="23" class="thresh-input" style="width:55px"
                       id="nd_ne_{{ loop.index0 }}" value="{{ r.params.night_end }}">
              </div>
              <div class="cheapest-field">
                <span>yö: tunteja/ikkuna:</span>
                <input type="number" min="0" class="thresh-input" style="width:50px"
                       id="nd_nper_{{ loop.index0 }}" value="{{ r.params.night_per_window }}">
                <span>ikkuna (h):</span>
                <input type="number" min="1" max="24" class="thresh-input" style="width:50px"
                       id="nd_nwin_{{ loop.index0 }}" value="{{ r.params.night_window_hours }}">
              </div>
              <div class="cheapest-field">
                <span>päivä: tunteja/ikkuna:</span>
                <input type="number" min="0" class="thresh-input" style="width:50px"
                       id="nd_dper_{{ loop.index0 }}" value="{{ r.params.day_per_window }}">
                <span>ikkuna (h):</span>
                <input type="number" min="1" max="24" class="thresh-input" style="width:50px"
                       id="nd_dwin_{{ loop.index0 }}" value="{{ r.params.day_window_hours }}">
              </div>
            </div>

            <div class="cheapest-edit-row">
              <button type="button" class="btn-start"
                      onclick="saveCheapest({{ loop.index0 }}, '{{ r.name }}')">💾 tallenna</button>
            </div>
          </div>
          {% else %}
          <div class="relay-card {{ 'manual-on' if r.mode == 'manual_on' else '' }}">
            <div class="relay-indicator {{ 'on' if r.is_on else ('off' if r.is_on is not none else 'unknown') }}"></div>
            <div class="relay-name">{{ r.name }}</div>

            <span class="manual-label">Manuaali:</span>
            <div class="manual-control">
              {% if r.mode == 'manual_on' %}
                <!-- ACTIVE manual override -->
                <label class="manual-toggle">
                  <input type="checkbox" checked onchange="setMode('{{ r.name }}', this.checked)">
                  <span class="manual-state on">päällä</span>
                </label>
                {% if r.remaining > 0 %}
                  <span class="countdown" data-expires="{{ (r.expires * 1000) | int }}">{{ r.remaining }} min jäljellä</span>
                {% elif r.expires > 0 %}
                  <span class="countdown" data-expires="{{ (r.expires * 1000) | int }}">&lt; 1 minuutti</span>
                {% else %}
                  <span class="manual-indef">toistaiseksi</span>
                {% endif %}
              {% else %}
                <!-- COLLAPSED auto state -->
                <label class="manual-toggle">
                  <input type="checkbox" onchange="onCheck({{ loop.index0 }}, this.checked)">
                  <span class="manual-state off" id="state_{{ loop.index0 }}">pois</span>
                </label>
                <div class="manual-options" id="opts_{{ loop.index0 }}" style="display:none">
                  <select class="manual-select" id="sel_{{ loop.index0 }}"
                          onchange="onDropdown({{ loop.index0 }}, this.value)">
                    <option value="">— valitse —</option>
                    <option value="indef">toistaiseksi</option>
                    <option value="timed">määräajaksi</option>
                  </select>
                  <input type="number" min="1" placeholder="min" class="thresh-input"
                         id="dur_{{ loop.index0 }}" style="display:none;width:70px">
                  <button type="button" class="btn-start" id="start_{{ loop.index0 }}"
                          style="display:none"
                          onclick="startManual('{{ r.name }}', {{ loop.index0 }})">käynnistä</button>
                </div>
              {% endif %}
            </div>

            <div style="display:flex;align-items:center;gap:.4rem">
              <span style="font-family:var(--mono);font-size:.75rem;color:var(--text-dim)">≤</span>
              <input
                class="thresh-input"
                type="number"
                step="0.1"
                min="0"
                max="100"
                name="threshold_{{ loop.index0 }}"
                value="{{ r.threshold }}"
                style="width:75px"
              >
              <span class="thresh-unit">c/kWh</span>
            </div>
            <button type="button"
                    class="relay-status {{ 'killed' if r.killed else ('on' if r.is_on else ('off' if r.is_on is not none else '')) }}"
                    title="{{ 'Huoltokytkin päällä — klikkaa palauttaaksesi' if r.killed else 'Klikkaa katkaistaksesi virran (huolto)' }}"
                    onclick="toggleKill('{{ r.name }}')">
              {% if r.killed %}HUOLTO{% elif r.is_on is none %}?{% elif r.is_on %}ON{% else %}OFF{% endif %}
            </button>
          </div>
          {% endif %}
          {% endfor %}
        </div>
        <div class="btn-row">
          <button type="submit" class="btn">💾 Tallenna Raja-arvot</button>
        </div>
      </form>
    </div>
  </div>

</div>

<script>
  // Auto-scroll log to bottom
  const log = document.getElementById('log');
  if (log) log.scrollTop = log.scrollHeight;

  // Reset collapsed-state checkboxes to unchecked on load (browsers preserve checkbox
  // state across reloads, which would otherwise leave a stale checkmark after expiry).
  document.querySelectorAll('.manual-state.off').forEach(function(el) {
    const cb = el.parentElement.querySelector('input[type=checkbox]');
    if (cb) cb.checked = false;
  });

  // Reveal/hide the cheapest-hours interval editor
  function toggleEdit(idx) {
    const el = document.getElementById('edit_' + idx);
    el.style.display = (el.style.display === 'none') ? 'block' : 'none';
  }

  // Switch between full-day and night+day field sets
  function onSubmode(idx, value) {
    document.getElementById('fullday_' + idx).style.display  = (value === 'fullday') ? 'block' : 'none';
    document.getElementById('nightday_' + idx).style.display = (value === 'nightday') ? 'block' : 'none';
  }

  // Save interval/mode settings to config.yaml
  async function saveCheapest(idx, name) {
    const submode = document.getElementById('submode_' + idx).value;
    const payload = {name: name, submode: submode};
    if (submode === 'fullday') {
      payload.per_window   = document.getElementById('fd_per_' + idx).value;
      payload.window_hours = document.getElementById('fd_win_' + idx).value;
    } else {
      payload.night_start        = document.getElementById('nd_ns_' + idx).value;
      payload.night_end          = document.getElementById('nd_ne_' + idx).value;
      payload.night_per_window   = document.getElementById('nd_nper_' + idx).value;
      payload.night_window_hours = document.getElementById('nd_nwin_' + idx).value;
      payload.day_per_window     = document.getElementById('nd_dper_' + idx).value;
      payload.day_window_hours   = document.getElementById('nd_dwin_' + idx).value;
    }
    const resp = await fetch('/save_cheapest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (resp.ok) location.reload();
  }

  // Killswitch — force relay OFF for maintenance, or restore normal operation
  async function toggleKill(name) {
    const resp = await fetch('/toggle_kill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (resp.ok) location.reload();
  }

  // Uncheck an active manual override → back to auto
  async function setMode(name, checked) {
    const resp = await fetch('/set_mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, mode: checked ? 'manual_on' : 'auto'})
    });
    if (resp.ok) location.reload();
  }

  // Checkbox toggled in collapsed (auto) state — reveal/hide options, no commit yet
  function onCheck(idx, checked) {
    const state = document.getElementById('state_' + idx);
    const opts  = document.getElementById('opts_' + idx);
    if (checked) {
      state.textContent = 'päällä';
      state.className = 'manual-state on';
      opts.style.display = 'flex';
    } else {
      state.textContent = 'pois';
      state.className = 'manual-state off';
      opts.style.display = 'none';
      document.getElementById('sel_' + idx).value = '';
      document.getElementById('dur_' + idx).style.display = 'none';
      document.getElementById('start_' + idx).style.display = 'none';
    }
  }

  // Dropdown choice — show duration input + start button as needed
  function onDropdown(idx, value) {
    const dur   = document.getElementById('dur_' + idx);
    const start = document.getElementById('start_' + idx);
    if (value === 'indef') {
      dur.style.display = 'none';
      start.style.display = 'inline-flex';
    } else if (value === 'timed') {
      dur.style.display = 'inline-block';
      start.style.display = 'inline-flex';
    } else {
      dur.style.display = 'none';
      start.style.display = 'none';
    }
  }

  // Käynnistä clicked — commit the manual override + duration, then reload
  async function startManual(name, idx) {
    const choice = document.getElementById('sel_' + idx).value;
    let minutes = 0;
    if (choice === 'timed') {
      minutes = parseInt(document.getElementById('dur_' + idx).value) || 0;
      if (minutes <= 0) { alert('Anna kelvollinen aika minuutteina.'); return; }
    } else if (choice !== 'indef') {
      return; // nothing selected
    }
    await fetch('/set_mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, mode: 'manual_on'})
    });
    await fetch('/set_duration', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, minutes: minutes})
    });
    location.reload();
  }

  // Auto-reload exactly when a countdown expires → card collapses back to "pois"
  document.querySelectorAll('.countdown').forEach(function(el) {
    const expiresMs = parseFloat(el.dataset.expires);
    const delay = expiresMs - Date.now();
    if (delay > 0 && delay < 3600000) {
      setTimeout(function() { location.reload(); }, delay + 500);
    }
  });

  // Set manual override duration
  async function setDuration(name, minutes) {
    await fetch('/set_duration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, minutes: parseInt(minutes) || 0})
    });
  }

  // Close chart modal with Escape key
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      document.getElementById('chartModal').classList.remove('open');
    }
  });
</script>
</body>
</html>
"""

# ── Chart builder ─────────────────────────────────────────────────────────────

def build_chart_svg(prices: list[dict], relays: list[dict], current_price: float | None) -> str:
    if not prices:
        return "<p style='color:#4b5563;font-size:.8rem'>Price data unavailable</p>"

    W, H = 700, 170
    PAD_L, PAD_R, PAD_T, PAD_B = 42, 10, 10, 28

    vals = [p["price_cKwh"] for p in prices if p["price_cKwh"] is not None]
    if not vals:
        return ""
    # Include visible threshold lines in the y-axis range so they never clip off-chart
    thresh_vals = [
        float(r.get("threshold", 0))
        for r in relays
        if r.get("mode") != "cheapest_hours" and float(r.get("threshold", 0)) > 0
    ]
    min_v = min(0, min(vals))
    max_v = (max(vals + thresh_vals) if thresh_vals else max(vals)) * 1.12 or 1

    chart_w = W - PAD_L - PAD_R
    chart_h = H - PAD_T - PAD_B
    now_hour = datetime.now().hour

    def x(hour):
        return PAD_L + (hour / 23) * chart_w

    def y(val):
        return PAD_T + chart_h - ((val - min_v) / (max_v - min_v)) * chart_h

    # Threshold lines
    thresh_lines = ""
    colours = ["#ff6b35", "#fbbf24", "#a78bfa", "#34d399"]
    for i, r in enumerate(relays):
        if r.get("mode") == "cheapest_hours" or float(r.get("threshold", 0)) <= 0:
            continue
        ty = y(r["threshold"])
        if PAD_T <= ty <= PAD_T + chart_h:
            col = colours[i % len(colours)]
            thresh_lines += (
                f'<line x1="{PAD_L}" y1="{ty:.1f}" x2="{W - PAD_R}" y2="{ty:.1f}" '
                f'stroke="{col}" stroke-width="1" stroke-dasharray="4,3" opacity=".7"/>'
                f'<text x="{W - PAD_R - 2}" y="{ty - 3:.1f}" '
                f'font-size="9" fill="{col}" text-anchor="end" font-family="Share Tech Mono">'
                f'{r["name"].split()[0]} {r["threshold"]:.1f}</text>'
            )

    # Bar chart
    bars = ""
    bar_w = max(2, chart_w / 24 - 2)
    for p in prices:
        if p["price_cKwh"] is None:
            continue
        bx = x(p["hour"]) - bar_w / 2
        bh = max(1, ((p["price_cKwh"] - min_v) / (max_v - min_v)) * chart_h)
        by = PAD_T + chart_h - bh
        is_now = p["hour"] == now_hour
        col = "#00e5ff" if is_now else "#1e3a5f"
        bars += (
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{col}" rx="2"/>'
        )

    # X-axis labels (every 3 hours)
    labels = ""
    for h in range(0, 24, 3):
        lx = x(h)
        labels += (
            f'<text x="{lx:.1f}" y="{H - 6}" text-anchor="middle" '
            f'font-size="9" fill="#FFFFFF" font-family="Share Tech Mono">{h:02d}</text>'
        )

    # Y-axis labels
    y_labels = ""
    for tick in [min_v, (min_v + max_v) / 2, max_v]:
        ty = y(tick)
        y_labels += (
            f'<text x="{PAD_L - 4}" y="{ty + 3:.1f}" text-anchor="end" '
            f'font-size="9" fill="#FFFFFF" font-family="Share Tech Mono">{tick:.0f}</text>'
        )

    # Current price marker
    now_marker = ""
    if current_price is not None:
        nx = x(now_hour)
        ny = y(current_price)
        now_marker = (
            f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="5" fill="#00e5ff" '
            f'stroke="#0a0e17" stroke-width="2"/>'
        )

    return f"""
<svg class="price-chart" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <!-- Grid lines -->
  <line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T+chart_h}"
        stroke="#1e293b" stroke-width="1"/>
  <line x1="{PAD_L}" y1="{PAD_T+chart_h}" x2="{W-PAD_R}" y2="{PAD_T+chart_h}"
        stroke="#1e293b" stroke-width="1"/>
  {bars}
  {thresh_lines}
  {now_marker}
  {labels}
  {y_labels}
</svg>"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    flash = request.cookies.get("flash", "")
    cfg     = load_config()
    prices  = get_prices()
    current = get_current_price(prices)
    relays  = relay_statuses(cfg, current, prices)

    vals   = [p["price_cKwh"] for p in prices if p["price_cKwh"] is not None]
    avg    = round(sum(vals) / len(vals), 2) if vals else None
    min_p  = min(vals) if vals else 0
    max_p  = max(vals) if vals else 0

    chart  = build_chart_svg(prices, relays, current)
    logs   = get_log_tail(30)
    _FI_DAYS   = ["Maanantai","Tiistai","Keskiviikko","Torstai","Perjantai","Lauantai","Sunnuntai"]
    _FI_MONTHS = ["tammikuuta","helmikuuta","maaliskuuta","huhtikuuta","toukokuuta","kesäkuuta","heinäkuuta","elokuuta","syyskuuta","lokakuuta","marraskuuta","joulukuuta"]
    _now = datetime.now()
    now = f"{_FI_DAYS[_now.weekday()]} {_now.day}. {_FI_MONTHS[_now.month-1]} {_now.year}  {_now.strftime('%H:%M')}"

    resp = app.make_response(render_template_string(
        TEMPLATE,
        now=now,
        current_price=current,
        avg=avg,
        min_p=min_p,
        max_p=max_p,
        relays=relays,
        chart_svg=chart,
        log_lines=logs,
        flash=flash,
    ))
    if flash:
        resp.delete_cookie("flash")
    return resp


@app.route("/save_thresholds", methods=["POST"])
def save_thresholds():
    cfg = load_config()
    relays = cfg.get("relays", [])
    for i, relay in enumerate(relays):
        key = f"threshold_{i}"
        if key in request.form:
            try:
                relay["threshold"] = round(float(request.form[key]), 1)
            except ValueError:
                pass
    save_config(cfg)
    resp = redirect(url_for("index"), 303)
    resp.set_cookie("flash", "✓ Raja-arvot tallennettu config.yaml-tiedostoon", max_age=10)
    return resp

@app.route("/toggle_relay", methods=["POST"])
def toggle_relay():
    import json
    name = request.json.get("name")
    current_mode = request.json.get("current_mode", "auto")
    # Cycle: auto → manual_on → manual_off → auto
    cycle = {"auto": "manual_on", "manual_on": "manual_off", "manual_off": "auto"}
    new_mode = cycle.get(current_mode, "auto")
    overrides = load_overrides()
    overrides[name] = {"mode": new_mode, "expires": 0}
    save_overrides(overrides)
    return jsonify({"name": name, "mode": new_mode})

@app.route("/save_cheapest", methods=["POST"])
def save_cheapest():
    """Write cheapest-hours interval/mode settings for one appliance to config.yaml."""
    data = request.json
    name = data.get("name")
    submode = data.get("submode")  # "fullday" | "nightday"

    cfg = load_config()
    updated = False
    for r in cfg.get("relays", []):
        if r.get("name") != name:
            continue
        # Remove all cheapest-mode keys, then set the chosen sub-mode's keys
        for k in ("window_hours", "per_window", "cheapest_hours",
                  "night_start", "night_end", "night_window_hours",
                  "night_per_window", "day_window_hours", "day_per_window"):
            r.pop(k, None)

        def _int(key, default):
            try:
                return int(data.get(key, default))
            except (ValueError, TypeError):
                return default

        if submode == "fullday":
            r["window_hours"] = max(1, _int("window_hours", 3))
            r["per_window"]   = max(0, _int("per_window", 1))
        elif submode == "nightday":
            r["night_start"]        = _int("night_start", 22) % 24
            r["night_end"]          = _int("night_end", 7) % 24
            r["night_window_hours"] = max(1, _int("night_window_hours", 3))
            r["night_per_window"]   = max(0, _int("night_per_window", 1))
            r["day_window_hours"]   = max(1, _int("day_window_hours", 5))
            r["day_per_window"]     = max(0, _int("day_per_window", 1))
        r["mode"] = "cheapest_hours"
        updated = True
        break

    if updated:
        save_config(cfg)
    return jsonify({"ok": updated, "name": name, "submode": submode})

@app.route("/toggle_kill", methods=["POST"])
def toggle_kill():
    name = request.json.get("name")
    overrides = load_overrides()
    entry = overrides.get(name, {})
    entry["killed"] = not entry.get("killed", False)
    # Killswitch overrides any manual override; clear stale manual state when killing
    if entry["killed"]:
        entry["mode"] = "auto"
        entry["expires"] = 0
    overrides[name] = entry
    save_overrides(overrides)
    return jsonify({"name": name, "killed": entry["killed"]})

@app.route("/set_mode", methods=["POST"])
def set_mode():
    name = request.json.get("name")
    mode = request.json.get("mode", "auto")
    if mode not in ("auto", "manual_on", "manual_off"):
        mode = "auto"
    overrides = load_overrides()
    overrides[name] = {"mode": mode, "expires": 0}
    save_overrides(overrides)
    return jsonify({"name": name, "mode": mode})

@app.route("/set_duration", methods=["POST"])
def set_duration():
    import json
    name = request.json.get("name")
    minutes = int(request.json.get("minutes", 0))
    overrides = load_overrides()
    if name in overrides:
        if minutes > 0:
            overrides[name]["expires"] = datetime.now().timestamp() + minutes * 60
        else:
            overrides[name]["expires"] = 0
        save_overrides(overrides)
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    """JSON endpoint — useful for future integrations."""
    cfg     = load_config()
    prices  = get_prices()
    current = get_current_price(prices)
    relays  = relay_statuses(cfg, current, prices)
    return jsonify({
        "current_price_cKwh": current,
        "timestamp": datetime.now().isoformat(),
        "relays": relays,
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (0.0.0.0 = all interfaces)")
    parser.add_argument("--port", default=5000, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print(f"\n  ⚡ Dashboard running at http://localhost:{args.port}")
    print(f"  On your home network: http://<pi-ip>:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)
