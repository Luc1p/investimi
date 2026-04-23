from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import requests


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    close: float
    as_of: date


class StooqClient:
    """
    Provider prezzi gratuito (no key) via stooq.com.
    Nota: i simboli US spesso richiedono suffisso ".us".
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def latest_close(self, symbol: str) -> PriceQuote:
        stooq_symbol = symbol.lower()
        if "." not in stooq_symbol:
            stooq_symbol = f"{stooq_symbol}.us"
        url = "https://stooq.com/q/l/"
        params = {"s": stooq_symbol, "f": "sd2c", "h": "", "e": "csv"}
        r = self._session.get(url, params=params, timeout=15)
        r.raise_for_status()
        # CSV: Symbol,Date,Close
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise RuntimeError(f"Unexpected Stooq response for {symbol!r}")
        row = lines[1].split(",")
        if len(row) < 3 or row[1] == "N/A" or row[2] == "N/A":
            raise RuntimeError(f"No quote for {symbol!r} (mapped to {stooq_symbol!r})")
        y, m, d = (int(x) for x in row[1].split("-"))
        return PriceQuote(symbol=symbol, close=float(row[2]), as_of=date(y, m, d))

