"""
price_fetcher.py — Fetches current Nord Pool spot price for a given area.

Uses the `nordpool` PyPI library (pip install nordpool).
Prices are cached for the current hour to avoid hammering the API.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class PriceFetcher:
    """
    Retrieves the current hourly spot price (c/kWh) from Nord Pool.

    Prices from the nordpool library come as EUR/MWh; we convert to c/kWh
    (÷ 10) which is what Finnish consumers are used to seeing.
    """

    def __init__(self, area: str = "FI", currency: str = "EUR"):
        self.area = area
        self.currency = currency

        self._cached_prices: list[dict] = []   # today's hourly entries
        self._cache_date: Optional[str] = None  # YYYY-MM-DD the cache covers

        try:
            from nordpool import elspot
            self._elspot = elspot.Prices(currency=currency)
            logger.info(f"Nord Pool price fetcher initialised (area={area}, currency={currency})")
        except ImportError:
            raise RuntimeError(
                "The 'nordpool' package is not installed.\n"
                "Run:  pip install nordpool"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_current_price(self) -> Optional[float]:
        """
        Return the current spot price in c/kWh, or None if unavailable.
        Refreshes the hourly price list once per calendar day.
        """
        self._refresh_if_needed()
        return self._price_for_now()

    def get_todays_prices(self) -> list[dict]:
        """
        Return today's full hourly price list.
        Each entry: {'hour': int, 'price_cKwh': float, 'start': datetime}
        """
        self._refresh_if_needed()
        return self._cached_prices

    def get_daily_average(self) -> Optional[float]:
        """Return today's average price in c/kWh."""
        prices = [e["price_cKwh"] for e in self.get_todays_prices()
                  if e["price_cKwh"] is not None]
        if not prices:
            return None
        return round(sum(prices) / len(prices), 4)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _refresh_if_needed(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._cache_date == today_str and self._cached_prices:
            return  # still valid

        logger.info("Fetching today's spot prices from Nord Pool …")
        try:
            # nordpool library fetches "tomorrow" by default; fetch today explicitly
            from nordpool import elspot
            fetcher = elspot.Prices(currency=self.currency)
            data = fetcher.fetch(areas=[self.area], end_date=datetime.now())

            area_values = data["areas"][self.area]["values"]
            entries = []
            for entry in area_values:
                start_utc: datetime = entry["start"]
                # Convert UTC → local wall-clock hour
                start_local = start_utc.astimezone()
                price_eur_mwh = entry["value"]
                price_c_kwh = None if price_eur_mwh is None else round(price_eur_mwh / 10, 4)
                entries.append({
                    "hour": start_local.hour,
                    "start": start_local,
                    "price_cKwh": price_c_kwh,
                })

            self._cached_prices = entries
            self._cache_date = today_str
            logger.info(
                f"Loaded {len(entries)} hourly prices. "
                f"Daily avg: {self.get_daily_average()} c/kWh"
            )
        except Exception as exc:
            logger.error(f"Failed to fetch prices: {exc}")
            # Keep stale cache if we have one; controller will use last known prices

    def _price_for_now(self) -> Optional[float]:
        now_hour = datetime.now().hour
        for entry in self._cached_prices:
            if entry["hour"] == now_hour:
                return entry["price_cKwh"]
        logger.warning(f"No price entry found for hour {now_hour}")
        return None
