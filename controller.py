"""
controller.py — Main daemon that ties price fetching and relay switching together.

Run with:  python controller.py [--config config.yaml] [--dry-run]
"""

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from gpio_driver import create_driver
from price_fetcher import PriceFetcher

def compute_active_hours(cfg_dict: dict, prices: list) -> set:
    """
    Return the set of hour-integers (0-23) during which a cheapest-hours
    appliance should be ON, given today's price entries.

    Three sub-modes, detected from the config keys present:

      1. Night/day split  (keys: night_start, night_end [+ *_window_hours,
         *_per_window]):  splits the day into a night range and a day range,
         then tiles each into windows and picks the cheapest per window.

      2. Fixed window      (key: window_hours [+ per_window]):  tiles the whole
         day into equal windows and picks the cheapest per window.

      3. Plain N cheapest  (key: cheapest_hours):  the N globally cheapest hours.
    """
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

    # ── Sub-mode 1: night/day split ──
    if "night_start" in cfg_dict and "night_end" in cfg_dict:
        ns = int(cfg_dict["night_start"]) % 24
        ne = int(cfg_dict["night_end"]) % 24
        night_hours = []
        h = ns
        while h != ne:
            night_hours.append(h)
            h = (h + 1) % 24
        night_set = set(night_hours)
        # day hours ordered starting from night_end
        day_hours = []
        h = ne
        for _ in range(24):
            if h not in night_set:
                day_hours.append(h)
            h = (h + 1) % 24
            if len(day_hours) >= (24 - len(night_set)):
                break

        active = set()
        active |= pick_windowed(
            night_hours,
            cfg_dict.get("night_window_hours", len(night_hours) or 1),
            cfg_dict.get("night_per_window", 1),
        )
        active |= pick_windowed(
            day_hours,
            cfg_dict.get("day_window_hours", len(day_hours) or 1),
            cfg_dict.get("day_per_window", 1),
        )
        return active

    # ── Sub-mode 2: fixed window across whole day ──
    if "window_hours" in cfg_dict:
        return pick_windowed(
            list(range(24)),
            cfg_dict.get("window_hours", 3),
            cfg_dict.get("per_window", 1),
        )

    # ── Sub-mode 3: plain N cheapest ──
    n = int(cfg_dict.get("cheapest_hours", 0))
    ranked = sorted(price_by_hour.keys(), key=lambda h: price_by_hour[h])
    return set(ranked[:n])


@dataclass
class RelayConfig:
    name: str
    gpio_pin: int
    threshold: float = 0.0
    normally_open: bool = True
    priority: int = 99
    mode: str = "threshold"          # "threshold" | "cheapest_hours"
    cheapest_hours: int = 0          # number of cheapest hours per day to run
    raw: dict = field(default_factory=dict)   # full yaml dict (window/night-day params)

@dataclass
class RelayState:
    config: RelayConfig
    is_on: bool = True
    last_change: Optional[datetime] = None
    last_price_seen: Optional[float] = None


class SpotPriceController:

    def __init__(self, config_path: str, dry_run: bool = False, gpio_backend: str = "auto"):
        self.dry_run = dry_run
        self.config_path = config_path
        self.cfg = self._load_config(config_path)
        self._setup_logging()
        self.logger = logging.getLogger("controller")

        if dry_run:
            self.logger.info("⚡ DRY-RUN mode — no GPIO will be touched.")

        self.relays: list[RelayState] = [
            RelayState(config=r) for r in self._parse_relays()
        ]

        self.fetcher = PriceFetcher(
            area=self.cfg.get("nordpool_area", "FI"),
            currency=self.cfg.get("currency", "EUR"),
        )

        if not dry_run:
            self.gpio = create_driver(gpio_backend)
            for rs in self.relays:
                self.gpio.setup(rs.config.gpio_pin, rs.config.normally_open)
                self.gpio.set_relay(rs.config.gpio_pin, True, rs.config.normally_open)
                rs.is_on = True
        else:
            self.gpio = None

        self._running = False

    def run_once(self):
        price = self.fetcher.get_current_price()
        if price is None:
            self.logger.warning("Current price unavailable — keeping relays unchanged.")
            return

        avg = self.fetcher.get_daily_average()
        self.logger.info(
            f"Current price: {price:.2f} c/kWh  |  "
            f"Daily avg: {avg:.2f} c/kWh  |  "
            f"{datetime.now().strftime('%H:%M')}"
        )

        todays_prices = self.fetcher.get_todays_prices()
        for rs in self.relays:
            self._evaluate_relay(rs, price, todays_prices)

        self._log_status_table(price, avg)

    def run_loop(self):
        poll_secs = self.cfg.get("poll_interval", 60)
        self._config_mtime = Path(self.config_path).stat().st_mtime
        self._running = True
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.logger.info(
            f"Controller started — polling every {poll_secs}s. "
            f"Press Ctrl+C to stop."
        )
        while self._running:
            try:
                current_mtime = Path(self.config_path).stat().st_mtime
                if current_mtime != self._config_mtime:
                    self.logger.info("config.yaml changed — reloading thresholds …")
                    old_thresholds = {rs.config.name: rs.config.threshold for rs in self.relays}
                    self.cfg = self._load_config(self.config_path)
                    new_relays = self._parse_relays()
                    for rs, new_cfg in zip(self.relays, new_relays):
                        if rs.config.threshold != new_cfg.threshold:
                            self.logger.info(
                                f"  [{rs.config.name}] threshold: "
                                f"{old_thresholds[rs.config.name]:.1f} → {new_cfg.threshold:.1f} c/kWh"
                            )
                        rs.config = new_cfg
                    self._config_mtime = current_mtime
                    poll_secs = self.cfg.get("poll_interval", 60)
                self.run_once()
            except Exception as exc:
                self.logger.error(f"Unexpected error during evaluation: {exc}", exc_info=True)
            time.sleep(poll_secs)

    def shutdown(self):
        self.logger.info("Shutting down — restoring all relays to ON …")
        if self.gpio:
            for rs in self.relays:
                self.gpio.set_relay(rs.config.gpio_pin, True, rs.config.normally_open)
            self.gpio.cleanup()

    def _load_overrides(self) -> dict:
        import json
        override_path = Path(self.config_path).parent / "overrides.json"
        try:
            return json.loads(override_path.read_text())
        except Exception:
            return {}

    def _save_overrides(self, data: dict):
        import json
        override_path = Path(self.config_path).parent / "overrides.json"
        override_path.write_text(json.dumps(data, indent=2))

    def _active_hours_set(self, cfg_dict: dict, todays_prices: list) -> set:
        """Compute the set of hours an appliance should run, via compute_active_hours()."""
        return compute_active_hours(cfg_dict, todays_prices)

    def _evaluate_relay(self, rs: RelayState, price: float, todays_prices: list = None):
        cfg = rs.config
        rs.last_price_seen = price

        # ── Killswitch: highest priority, applies to ALL relay types ──
        # Engaged from the dashboard for maintenance/repair. Forces the relay OFF
        # and overrides threshold, manual, and cheapest-hours logic entirely.
        overrides = self._load_overrides()
        if overrides.get(cfg.name, {}).get("killed"):
            if rs.is_on is not False:
                self.logger.info(f"  [{cfg.name}] → OFF  (KILLSWITCH engaged — huoltotila)")
                if not self.dry_run:
                    self.gpio.set_relay(cfg.gpio_pin, False, cfg.normally_open)
                rs.is_on = False
                rs.last_change = datetime.now()
            return

        # ── Cheapest-hours mode: no threshold, no manual override ──
        if cfg.mode == "cheapest_hours":
            todays_prices = todays_prices or []
            active = self._active_hours_set(cfg.raw, todays_prices)
            current_hour = datetime.now().hour
            should_be_on = current_hour in active if active else rs.is_on
            if should_be_on != rs.is_on:
                action = "ON" if should_be_on else "OFF"
                self.logger.info(
                    f"  [{cfg.name}] → {action}  "
                    f"(cheapest-hours: {current_hour:02d}:00 "
                    f"{'active' if should_be_on else 'inactive'}, "
                    f"{len(active)} h/day selected)"
                )
                if not self.dry_run:
                    self.gpio.set_relay(cfg.gpio_pin, should_be_on, cfg.normally_open)
                rs.is_on = should_be_on
                rs.last_change = datetime.now()
            return

        # Check manual override
        overrides = self._load_overrides()
        ov = overrides.get(cfg.name, {})
        mode = ov.get("mode", "auto")
        expires = ov.get("expires", 0)
        now = datetime.now().timestamp()

        # Expire timed overrides
        if mode != "auto" and expires > 0 and now > expires:
            mode = "auto"
            overrides[cfg.name] = {"mode": "auto", "expires": 0}
            self._save_overrides(overrides)
            self.logger.info(f"  [{cfg.name}] manual override expired — returning to AUTO")

        if mode == "manual_on":
            should_be_on = True
            reason = "manual override ON"
        elif mode == "manual_off":
            should_be_on = False
            reason = "manual override OFF"
        else:
            should_be_on = price <= cfg.threshold
            reason = (
                f"{price:.2f} c/kWh ≤ {cfg.threshold} c/kWh threshold"
                if should_be_on
                else f"{price:.2f} c/kWh > {cfg.threshold} c/kWh threshold"
            )

        if should_be_on == rs.is_on:
            return

        action = "ON" if should_be_on else "OFF"
        self.logger.info(f"  [{cfg.name}] → {action}  ({reason})")

        if not self.dry_run:
            self.gpio.set_relay(cfg.gpio_pin, should_be_on, cfg.normally_open)

        rs.is_on = should_be_on
        rs.last_change = datetime.now()

    def _log_status_table(self, price: float, avg: Optional[float]):
        lines = ["", "─" * 60]
        lines.append(f"  {'Appliance':<22} {'Threshold':>10}  {'Status':>6}")
        lines.append("─" * 60)
        for rs in self.relays:
            status = "✓ ON " if rs.is_on else "✗ OFF"
            lines.append(
                f"  {rs.config.name:<22} {rs.config.threshold:>8.1f} c/kWh  {status}"
            )
        lines.append("─" * 60)
        self.logger.info("\n".join(lines))

    def _load_config(self, path: str) -> dict:
        cfg_path = Path(path)
        if not cfg_path.exists():
            sys.exit(f"Config file not found: {path}")
        with open(cfg_path) as f:
            return yaml.safe_load(f)

    def _parse_relays(self) -> list[RelayConfig]:
        raw = self.cfg.get("relays", [])
        if not raw:
            sys.exit("No relays defined in config.yaml")
        relays = []
        for r in raw:
            relays.append(RelayConfig(
                name=r["name"],
                gpio_pin=r["gpio_pin"],
                threshold=float(r.get("threshold", 0.0)),
                normally_open=r.get("normally_open", True),
                priority=r.get("priority", 99),
                mode=r.get("mode", "threshold"),
                cheapest_hours=int(r.get("cheapest_hours", 0)),
                raw=dict(r),
            ))
        return sorted(relays, key=lambda r: r.priority)

    def _setup_logging(self):
        log_file = self.cfg.get("log_file", "logs/controller.log")
        log_level = getattr(logging, self.cfg.get("log_level", "INFO").upper(), logging.INFO)
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        root = logging.getLogger()
        root.setLevel(log_level)

        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)

        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    def _handle_signal(self, signum, frame):
        self.logger.info(f"Signal {signum} received — stopping.")
        self._running = False
        self.shutdown()
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Spot-price electricity relay controller")
    parser.add_argument("--config",       default="config.yaml",  help="Path to config.yaml")
    parser.add_argument("--dry-run",      action="store_true",     help="Log only, don't touch GPIO")
    parser.add_argument("--gpio-backend", default="auto",          help="auto | rpigpio | gpiozero | mock")
    parser.add_argument("--once",         action="store_true",     help="Run one cycle and exit")
    args = parser.parse_args()

    ctrl = SpotPriceController(
        config_path=args.config,
        dry_run=args.dry_run,
        gpio_backend=args.gpio_backend,
    )

    if args.once:
        ctrl.run_once()
        ctrl.shutdown()
    else:
        ctrl.run_loop()


if __name__ == "__main__":
    main()
