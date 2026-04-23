from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import requests


EconomicImpact = Literal["Low", "Medium", "High"]


@dataclass(frozen=True)
class EconomicEvent:
    # FMP economic calendar (campi più usati)
    event: str
    country: str | None
    date_time: str  # ISO-like string da API (lo lasciamo raw)
    impact: EconomicImpact | None
    actual: float | None
    previous: float | None
    estimate: float | None


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    date_str: str  # YYYY-MM-DD
    eps_estimated: float | None
    eps: float | None
    revenue_estimated: float | None
    revenue: float | None
    time: str | None  # "bmo"/"amc"/etc se presente


class FmpClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://financialmodelingprep.com/stable",
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("FMP api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    def economic_calendar(self, from_date: date, to_date: date) -> list[EconomicEvent]:
        params = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "apikey": self._api_key,
        }
        r = self._session.get(f"{self._base_url}/economic-calendar", params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        out: list[EconomicEvent] = []
        for item in payload or []:
            out.append(
                EconomicEvent(
                    event=str(item.get("event") or ""),
                    country=item.get("country"),
                    date_time=str(item.get("date") or item.get("dateTime") or item.get("datetime") or ""),
                    impact=item.get("impact"),
                    actual=_to_float(item.get("actual")),
                    previous=_to_float(item.get("previous")),
                    estimate=_to_float(item.get("estimate") or item.get("forecast")),
                )
            )
        return out

    def earnings_calendar(self, from_date: date, to_date: date) -> list[EarningsEvent]:
        params = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "apikey": self._api_key,
        }
        r = self._session.get(f"{self._base_url}/earnings-calendar", params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        out: list[EarningsEvent] = []
        for item in payload or []:
            out.append(
                EarningsEvent(
                    symbol=str(item.get("symbol") or ""),
                    date_str=str(item.get("date") or ""),
                    eps_estimated=_to_float(item.get("epsEstimated")),
                    eps=_to_float(item.get("eps")),
                    revenue_estimated=_to_float(item.get("revenueEstimated")),
                    revenue=_to_float(item.get("revenue")),
                    time=item.get("time"),
                )
            )
        return out


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None

