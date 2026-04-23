from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import requests


@dataclass(frozen=True)
class FredObservation:
    series: str
    value: float
    as_of: date


class FredClient:
    def __init__(self, api_key: str | None, session: requests.Session | None = None) -> None:
        self._api_key = api_key
        self._session = session or requests.Session()

    def latest(self, series: str) -> FredObservation:
        params: dict[str, Any] = {
            "series_id": series,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,  # qualche osservazione, poi prendiamo la prima valida
        }
        if self._api_key:
            params["api_key"] = self._api_key
        r = self._session.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        obs = payload.get("observations") or []
        for item in obs:
            v = item.get("value")
            d = item.get("date")
            if not v or v == "." or not d:
                continue
            try:
                fv = float(v)
                y, m, dd = (int(x) for x in d.split("-"))
                return FredObservation(series=series, value=fv, as_of=date(y, m, dd))
            except Exception:
                continue
        raise RuntimeError(f"No valid latest observation for FRED series {series!r}")

