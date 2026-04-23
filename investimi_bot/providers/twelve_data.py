from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests


@dataclass(frozen=True)
class TwelveDataPrice:
    symbol: str
    price: float
    fetched_at: str  # ISO timestamp


@dataclass(frozen=True)
class TwelveDataQuote:
    symbol: str
    price: float
    change: float | None
    percent_change: float | None
    previous_close: float | None
    fetched_at: str


class TwelveDataClient:
    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        if not api_key:
            raise ValueError("Twelve Data api_key is required")
        self._api_key = api_key
        self._session = session or requests.Session()

    def price(self, symbol: str, exchange: str | None = None) -> TwelveDataPrice:
        params: dict[str, Any] = {"symbol": symbol, "apikey": self._api_key}
        if exchange:
            params["exchange"] = exchange
        r = self._session.get("https://api.twelvedata.com/price", params=params, timeout=20)
        r.raise_for_status()
        payload: dict[str, Any] = r.json()
        if "price" not in payload:
            raise RuntimeError(f"Unexpected Twelve Data response: {payload}")
        return TwelveDataPrice(
            symbol=symbol,
            price=float(payload["price"]),
            fetched_at=datetime.utcnow().isoformat() + "Z",
        )

    def quotes(self, symbols: list[str], exchange: str | None = None) -> list[TwelveDataQuote]:
        if not symbols:
            return []
        params: dict[str, Any] = {"symbol": ",".join(symbols), "apikey": self._api_key}
        if exchange:
            params["exchange"] = exchange
        r = self._session.get("https://api.twelvedata.com/quote", params=params, timeout=25)
        r.raise_for_status()
        payload: Any = r.json()
        fetched_at = datetime.utcnow().isoformat() + "Z"

        def _parse_one(sym: str, obj: dict[str, Any]) -> TwelveDataQuote:
            # Alcuni piani ritornano errori per singolo simbolo (403/404) dentro la risposta batch.
            if "price" not in obj:
                code = obj.get("code")
                msg = obj.get("message")
                status = obj.get("status")
                raise RuntimeError(f"Twelve Data quote error for {sym!r}: code={code} status={status} message={msg}")
            return TwelveDataQuote(
                symbol=sym,
                price=float(obj["price"]),
                change=_to_float(obj.get("change")),
                percent_change=_to_float(obj.get("percent_change")),
                previous_close=_to_float(obj.get("previous_close")),
                fetched_at=fetched_at,
            )

        # Se chiedi 1 simbolo, spesso ritorna un dict flat; se ne chiedi N, ritorna dict per simbolo.
        if isinstance(payload, dict) and "price" in payload:
            sym = symbols[0]
            return [_parse_one(sym, payload)]

        if isinstance(payload, dict):
            out: list[TwelveDataQuote] = []
            for sym, obj in payload.items():
                if not isinstance(obj, dict):
                    continue
                try:
                    out.append(_parse_one(sym, obj))
                except RuntimeError:
                    # skip simboli non disponibili nel piano / non risolvibili
                    continue
            return out

        raise RuntimeError(f"Unexpected Twelve Data quotes response: {payload}")

    def time_series(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 120,
        exchange: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": self._api_key,
            "format": "JSON",
        }
        if exchange:
            params["exchange"] = exchange
        r = self._session.get("https://api.twelvedata.com/time_series", params=params, timeout=25)
        r.raise_for_status()
        payload: dict[str, Any] = r.json()
        if payload.get("status") == "error":
            raise RuntimeError(f"Twelve Data time_series error: {payload}")
        values = payload.get("values")
        if not isinstance(values, list):
            raise RuntimeError(f"Unexpected Twelve Data time_series response: {payload}")
        return values


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None

