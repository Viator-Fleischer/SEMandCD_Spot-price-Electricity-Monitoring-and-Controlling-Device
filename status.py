"""
status.py — Quick status viewer for the spot-price controller.

Run from the same directory:  python status.py [--config config.yaml]

Shows:
  • Today's hourly prices with current hour highlighted
  • Which relays are ON/OFF at the current price
  • Daily min / avg / max
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml


def load_config(path: str) -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(f"Config not found: {path}")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def bar(price: float, max_price: float, width: int = 20) -> str:
    if max_price <= 0:
        return " " * width
    filled = int(min(price / max_price, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def main():
    parser = argparse.ArgumentParser(description="Spot-price controller status viewer")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    area = cfg.get("nordpool_area", "FI")
    currency = cfg.get("currency", "EUR")
    relays = cfg.get("relays", [])

    try:
        from nordpool import elspot
        fetcher = elspot.Prices(currency=currency)
        data = fetcher.fetch(areas=[area], end_date=datetime.now())
        values = data["areas"][area]["values"]
    except ImportError:
        sys.exit("nordpool not installed. Run: pip install nordpool")
    except Exception as e:
        sys.exit(f"Failed to fetch prices: {e}")

    # Parse hourly entries
    entries = []
    for v in values:
        start_local = v["start"].astimezone()
        price_mwh = v["value"]
        price_c = None if price_mwh is None else round(price_mwh / 10, 2)
        entries.append({"hour": start_local.hour, "price": price_c, "start": start_local})

    now_hour = datetime.now().hour
    valid_prices = [e["price"] for e in entries if e["price"] is not None]
    avg = sum(valid_prices) / len(valid_prices) if valid_prices else 0
    min_p = min(valid_prices) if valid_prices else 0
    max_p = max(valid_prices) if valid_prices else 1
    current_price = next((e["price"] for e in entries if e["hour"] == now_hour), None)

    # ── Print header ──────────────────────────────────────────────────────────
    now_str = datetime.now().strftime("%A %d %B %Y  %H:%M")
    print(f"\n  ⚡ Spot Price Controller — {now_str}")
    print(f"  Area: {area}  |  Currency: {currency}\n")

    # ── Hourly price chart ────────────────────────────────────────────────────
    print(f"  {'Hr':>3}  {'c/kWh':>7}  {'':20}  ")
    print("  " + "─" * 45)
    for e in sorted(entries, key=lambda x: x["hour"]):
        h = e["hour"]
        p = e["price"]
        if p is None:
            print(f"  {h:>3}:00  {'N/A':>7}")
            continue
        is_now = (h == now_hour)
        b = bar(p, max_p)
        marker = " ◄ NOW" if is_now else ""
        highlight = "\033[1;33m" if is_now else ""
        reset = "\033[0m" if is_now else ""
        print(f"  {highlight}{h:>3}:00  {p:>7.2f}  {b}{marker}{reset}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("  " + "─" * 45)
    print(f"  Min: {min_p:.2f}  Avg: {avg:.2f}  Max: {max_p:.2f}  c/kWh\n")

    # ── Relay status at current price ─────────────────────────────────────────
    if current_price is not None:
        print(f"  Relay status at current price ({current_price:.2f} c/kWh):\n")
        print(f"  {'Appliance':<22}  {'Threshold':>10}  {'Status':>8}")
        print("  " + "─" * 46)
        for r in relays:
            thresh = float(r["threshold"])
            is_on = current_price <= thresh
            status = "\033[1;32m✓  ON\033[0m" if is_on else "\033[1;31m✗ OFF\033[0m"
            print(f"  {r['name']:<22}  {thresh:>8.1f} c  {status}")
        print()


if __name__ == "__main__":
    main()
