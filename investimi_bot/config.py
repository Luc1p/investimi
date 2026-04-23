from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class NotifyConfig(BaseModel):
    text: str


class FredThreshold(BaseModel):
    type: Literal["fred_threshold"]
    series: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float


class PriceThreshold(BaseModel):
    type: Literal["price_threshold"]
    symbol: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float


class TwelveDataPriceThreshold(BaseModel):
    type: Literal["twelvedata_price_threshold"]
    symbol: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float


class TwelveDataWatchlistMovers(BaseModel):
    type: Literal["twelvedata_watchlist_movers"]
    symbols: list[str] = Field(min_length=1)
    exchange: str | None = None
    top_n: int = Field(default=8, ge=1, le=40)
    min_abs_pct_change: float = Field(default=1.0, ge=0.0)  # es. 1.0 = 1%


class TwelveDataPercentMove(BaseModel):
    type: Literal["twelvedata_percent_move"]
    symbol: str
    interval: str = "1day"  # 1day, 1h, 15min, ecc.
    lookback_bars: int = Field(default=1, ge=1, le=30)
    min_abs_pct_change: float = Field(default=2.0, ge=0.0)
    exchange: str | None = None


class TwelveDataGapPercent(BaseModel):
    type: Literal["twelvedata_gap_percent"]
    symbol: str
    interval: str = "1day"
    min_abs_gap_pct: float = Field(default=1.0, ge=0.0)
    exchange: str | None = None


class TwelveDataSmaCross(BaseModel):
    type: Literal["twelvedata_sma_cross"]
    symbol: str
    interval: str = "1day"
    fast: int = Field(default=50, ge=2, le=400)
    slow: int = Field(default=200, ge=3, le=600)
    direction: Literal["bullish", "bearish"] = "bullish"
    exchange: str | None = None


class TwelveDataAtrSpike(BaseModel):
    type: Literal["twelvedata_atr_spike"]
    symbol: str
    interval: str = "1day"
    period: int = Field(default=14, ge=2, le=100)
    min_atr_pct: float = Field(default=2.0, ge=0.0)  # ATR / close * 100
    exchange: str | None = None


class FmpEconomicEventToday(BaseModel):
    type: Literal["fmp_economic_event_today"]
    # Filtri semplici (tutti opzionali). Se non passi nulla: prende tutti gli eventi di oggi.
    countries: list[str] = Field(default_factory=list)
    impacts: list[Literal["Low", "Medium", "High"]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # match case-insensitive su "event"


class FmpEconomicEventWithin(BaseModel):
    type: Literal["fmp_economic_event_within"]
    window_minutes: int = Field(ge=1, le=24 * 60)
    countries: list[str] = Field(default_factory=list)
    impacts: list[Literal["Low", "Medium", "High"]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class FmpEconomicEventSurpriseToday(BaseModel):
    type: Literal["fmp_economic_event_surprise_today"]
    countries: list[str] = Field(default_factory=list)
    impacts: list[Literal["Low", "Medium", "High"]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    min_abs_surprise: float | None = None  # abs(actual - estimate) >= this
    min_pct_surprise: float | None = None  # abs(actual-estimate)/abs(estimate) >= this


class FmpEarningsToday(BaseModel):
    type: Literal["fmp_earnings_today"]
    symbols: list[str] = Field(default_factory=list)  # watchlist; vuoto = tutto


class SecForm4Significant(BaseModel):
    type: Literal["sec_form4_significant"]
    tickers: list[str] = Field(min_length=1)
    within_days: int = Field(default=7, ge=1, le=30)
    sides: list[Literal["acquire", "dispose"]] = Field(default_factory=lambda: ["acquire"])
    codes: list[Literal["P", "S"]] | None = None  # opzionale: filtra solo se vuoi proprio P/S
    min_value_usd: float = Field(default=50000.0, ge=0.0)


class UsCongressTradesSignificant(BaseModel):
    type: Literal["us_congress_trades_significant"]
    symbols: list[str] = Field(default_factory=list)  # vuoto = tutti
    chambers: list[Literal["house", "senate"]] = Field(default_factory=lambda: ["house", "senate"])
    sides: list[Literal["buy", "sell"]] = Field(default_factory=lambda: ["buy", "sell"])
    min_amount_usd: float = Field(default=50000.0, ge=0.0)  # soglia applicata su MAX range (opzione A)
    politicians_contains: list[str] = Field(default_factory=list)  # match case-insensitive sul nome


class AndCondition(BaseModel):
    type: Literal["and"]
    all: list["Condition"]


Condition = (
    FredThreshold
    | PriceThreshold
    | TwelveDataPriceThreshold
    | TwelveDataWatchlistMovers
    | TwelveDataPercentMove
    | TwelveDataGapPercent
    | TwelveDataSmaCross
    | TwelveDataAtrSpike
    | FmpEconomicEventToday
    | FmpEconomicEventWithin
    | FmpEconomicEventSurpriseToday
    | FmpEarningsToday
    | SecForm4Significant
    | UsCongressTradesSignificant
    | AndCondition
)


class RuleConfig(BaseModel):
    id: str
    every_seconds: int = Field(ge=10, le=86400)
    when: Condition
    notify: NotifyConfig


class AlertsConfig(BaseModel):
    timezone: str = "Europe/Rome"
    rules: list[RuleConfig] = Field(default_factory=list)


def load_alerts_config(path: str | Path) -> AlertsConfig:
    p = Path(path)
    data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AlertsConfig.model_validate(data)

