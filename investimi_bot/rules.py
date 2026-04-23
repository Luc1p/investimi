from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from investimi_bot.config import (
    AndCondition,
    Condition,
    FredThreshold,
    FmpEconomicEventToday,
    FmpEconomicEventSurpriseToday,
    FmpEconomicEventWithin,
    FmpEarningsToday,
    PriceThreshold,
    TwelveDataPriceThreshold,
    TwelveDataWatchlistMovers,
    TwelveDataAtrSpike,
    TwelveDataGapPercent,
    TwelveDataPercentMove,
    TwelveDataSmaCross,
)
from investimi_bot.providers.fred import FredClient
from investimi_bot.providers.fmp import FmpClient
from investimi_bot.providers.prices import StooqClient
from investimi_bot.providers.sec_edgar import SecEdgarClient
from investimi_bot.providers.congress_trades_free import CongressTradesFreeClient
from investimi_bot.providers.twelve_data import TwelveDataClient


def _cmp(op: str, left: float, right: float) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"Unsupported op: {op!r}")


@dataclass(frozen=True)
class EvalResult:
    ok: bool
    context: dict[str, Any]
    fingerprint: str


class RuleEngine:
    def __init__(
        self,
        fred: FredClient,
        prices: StooqClient,
        fmp: FmpClient | None,
        twelve_data: TwelveDataClient | None,
        sec: SecEdgarClient | None,
        congress: CongressTradesFreeClient | None,
    ) -> None:
        self._fred = fred
        self._prices = prices
        self._fmp = fmp
        self._twelve_data = twelve_data
        self._sec = sec
        self._congress = congress

    def eval(self, cond: Condition) -> EvalResult:
        ctx: dict[str, Any] = {"evaluated_at": datetime.utcnow().isoformat() + "Z"}

        if isinstance(cond, FredThreshold):
            obs = self._fred.latest(cond.series)
            ctx.update({"series": cond.series, "value": obs.value, "as_of": obs.as_of.isoformat()})
            ok = _cmp(cond.op, obs.value, cond.value)
            fp = self._fingerprint({"t": "fred", "s": cond.series, "op": cond.op, "thr": cond.value, "v": obs.value, "d": obs.as_of.isoformat()})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, PriceThreshold):
            q = self._prices.latest_close(cond.symbol)
            ctx.update({"symbol": cond.symbol, "value": q.close, "as_of": q.as_of.isoformat()})
            ok = _cmp(cond.op, q.close, cond.value)
            fp = self._fingerprint({"t": "price", "sym": cond.symbol, "op": cond.op, "thr": cond.value, "v": q.close, "d": q.as_of.isoformat()})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataPriceThreshold):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            q = self._twelve_data.price(cond.symbol)
            ctx.update({"symbol": cond.symbol, "value": q.price, "as_of": q.fetched_at, "provider": "twelvedata"})
            ok = _cmp(cond.op, q.price, cond.value)
            fp = self._fingerprint({"t": "twelvedata_price", "sym": cond.symbol, "op": cond.op, "thr": cond.value, "v": q.price})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataWatchlistMovers):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            quotes = self._twelve_data.quotes(cond.symbols, exchange=cond.exchange)
            movers = []
            for q in quotes:
                pct = q.percent_change
                if pct is None:
                    continue
                if abs(pct) >= cond.min_abs_pct_change:
                    movers.append(
                        {
                            "symbol": q.symbol,
                            "price": q.price,
                            "pct_change": pct,
                            "change": q.change,
                            "previous_close": q.previous_close,
                        }
                    )
            movers_sorted = sorted(movers, key=lambda x: abs(float(x["pct_change"])), reverse=True)
            top = movers_sorted[: cond.top_n]
            ok = len(top) > 0
            ctx.update(
                {
                    "exchange": cond.exchange,
                    "min_abs_pct_change": cond.min_abs_pct_change,
                    "count": len(top),
                    "movers": top,
                }
            )
            fp = self._fingerprint(
                {
                    "t": "twelvedata_watchlist_movers",
                    "ex": cond.exchange,
                    "min": cond.min_abs_pct_change,
                    "top": [(m["symbol"], round(float(m["pct_change"]), 3), round(float(m["price"]), 4)) for m in top],
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataPercentMove):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            bars = self._twelve_data.time_series(
                cond.symbol,
                interval=cond.interval,
                outputsize=max(cond.lookback_bars + 2, 5),
                exchange=cond.exchange,
            )
            # values: lista di dict con close/open/high/low/datetime; ordine tipico: più recente prima
            if len(bars) < cond.lookback_bars + 1:
                raise RuntimeError(f"Not enough bars for {cond.symbol} interval={cond.interval}")
            close_now = float(bars[0]["close"])
            close_prev = float(bars[cond.lookback_bars]["close"])
            pct = ((close_now - close_prev) / close_prev) * 100.0 if close_prev != 0 else 0.0
            ok = abs(pct) >= cond.min_abs_pct_change
            ctx.update(
                {
                    "symbol": cond.symbol,
                    "interval": cond.interval,
                    "lookback_bars": cond.lookback_bars,
                    "value": close_now,
                    "prev": close_prev,
                    "pct_change": pct,
                }
            )
            fp = self._fingerprint(
                {"t": "twelvedata_percent_move", "sym": cond.symbol, "int": cond.interval, "lb": cond.lookback_bars, "pct": round(pct, 3)}
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataGapPercent):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            bars = self._twelve_data.time_series(cond.symbol, interval=cond.interval, outputsize=3, exchange=cond.exchange)
            if len(bars) < 2:
                raise RuntimeError(f"Not enough bars for {cond.symbol} interval={cond.interval}")
            open_now = float(bars[0]["open"])
            close_prev = float(bars[1]["close"])
            gap_pct = ((open_now - close_prev) / close_prev) * 100.0 if close_prev != 0 else 0.0
            ok = abs(gap_pct) >= cond.min_abs_gap_pct
            ctx.update(
                {
                    "symbol": cond.symbol,
                    "interval": cond.interval,
                    "open": open_now,
                    "prev_close": close_prev,
                    "gap_pct": gap_pct,
                }
            )
            fp = self._fingerprint({"t": "twelvedata_gap", "sym": cond.symbol, "int": cond.interval, "gap": round(gap_pct, 3)})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataSmaCross):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            if cond.fast >= cond.slow:
                raise RuntimeError("SMA fast must be < slow")
            need = cond.slow + 5
            bars = self._twelve_data.time_series(cond.symbol, interval=cond.interval, outputsize=need, exchange=cond.exchange)
            closes = [float(b["close"]) for b in reversed(bars)]  # cronologico
            if len(closes) < cond.slow + 1:
                raise RuntimeError(f"Not enough bars for SMA cross {cond.symbol}")

            def sma(n: int, end_idx: int) -> float:
                window = closes[end_idx - n + 1 : end_idx + 1]
                return sum(window) / float(n)

            i = len(closes) - 1
            fast_now = sma(cond.fast, i)
            slow_now = sma(cond.slow, i)
            fast_prev = sma(cond.fast, i - 1)
            slow_prev = sma(cond.slow, i - 1)

            bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
            bearish_cross = fast_prev >= slow_prev and fast_now < slow_now
            ok = bullish_cross if cond.direction == "bullish" else bearish_cross
            ctx.update(
                {
                    "symbol": cond.symbol,
                    "interval": cond.interval,
                    "fast": cond.fast,
                    "slow": cond.slow,
                    "direction": cond.direction,
                    "fast_now": fast_now,
                    "slow_now": slow_now,
                    "fast_prev": fast_prev,
                    "slow_prev": slow_prev,
                }
            )
            fp = self._fingerprint(
                {
                    "t": "twelvedata_sma_cross",
                    "sym": cond.symbol,
                    "int": cond.interval,
                    "fast": cond.fast,
                    "slow": cond.slow,
                    "dir": cond.direction,
                    "fast_now": round(fast_now, 6),
                    "slow_now": round(slow_now, 6),
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, TwelveDataAtrSpike):
            if not self._twelve_data:
                raise RuntimeError("Twelve Data not configured (missing TWELVE_DATA_API_KEY)")
            bars = self._twelve_data.time_series(
                cond.symbol,
                interval=cond.interval,
                outputsize=max(cond.period + 5, 25),
                exchange=cond.exchange,
            )
            # cronologico
            b = list(reversed(bars))
            if len(b) < cond.period + 2:
                raise RuntimeError(f"Not enough bars for ATR {cond.symbol}")

            # True Range per bar i (>=1): max(high-low, abs(high-prev_close), abs(low-prev_close))
            trs: list[float] = []
            for i in range(1, len(b)):
                high = float(b[i]["high"])
                low = float(b[i]["low"])
                prev_close = float(b[i - 1]["close"])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            # ATR semplice sugli ultimi 'period' TR (prendiamo l'ultimo valore)
            if len(trs) < cond.period:
                raise RuntimeError(f"Not enough TRs for ATR {cond.symbol}")
            atr = sum(trs[-cond.period :]) / float(cond.period)
            close_now = float(b[-1]["close"])
            atr_pct = (atr / close_now) * 100.0 if close_now != 0 else 0.0
            ok = atr_pct >= cond.min_atr_pct
            ctx.update(
                {
                    "symbol": cond.symbol,
                    "interval": cond.interval,
                    "period": cond.period,
                    "close": close_now,
                    "atr": atr,
                    "atr_pct": atr_pct,
                }
            )
            fp = self._fingerprint({"t": "twelvedata_atr", "sym": cond.symbol, "int": cond.interval, "atr_pct": round(atr_pct, 3)})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, FmpEconomicEventToday):
            if not self._fmp:
                raise RuntimeError("FMP not configured (missing FMP_API_KEY)")
            # Eventi di oggi (timezone lato API: spesso UTC; qui ci basta 'giorno' per alert quotidiani)
            from datetime import date as _date

            today = _date.today()
            events = self._fmp.economic_calendar(today, today)
            countries_upper = {c.upper() for c in cond.countries} if cond.countries else None

            def _match(e: Any) -> bool:
                if countries_upper:
                    if not e.country or e.country.upper() not in countries_upper:
                        return False
                if cond.impacts:
                    if not e.impact or e.impact not in cond.impacts:
                        return False
                if cond.keywords:
                    ev = (e.event or "").lower()
                    if not any(k.lower() in ev for k in cond.keywords):
                        return False
                return True

            matched = [e for e in events if _match(e)]
            ok = len(matched) > 0
            ctx.update(
                {
                    "count": len(matched),
                    "events": [
                        {
                            "event": e.event,
                            "country": e.country,
                            "date_time": e.date_time,
                            "impact": e.impact,
                            "actual": e.actual,
                            "estimate": e.estimate,
                            "previous": e.previous,
                        }
                        for e in matched[:10]
                    ],
                }
            )
            # fingerprint stabile per "oggi+filtri": cambia se cambia la lista eventi (idempotente)
            fp = self._fingerprint(
                {
                    "t": "fmp_econ_today",
                    "d": today.isoformat(),
                    "countries": cond.countries,
                    "impacts": cond.impacts,
                    "keywords": cond.keywords,
                    "top": [(e.event, e.country, e.date_time, e.impact) for e in matched[:20]],
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, FmpEconomicEventWithin):
            if not self._fmp:
                raise RuntimeError("FMP not configured (missing FMP_API_KEY)")
            from datetime import date as _date, timedelta

            now = datetime.utcnow()
            until = now + timedelta(minutes=cond.window_minutes)
            # prendiamo oggi + domani per beccare eventi vicini a mezzanotte UTC
            today = _date.today()
            tomorrow = today + timedelta(days=1)
            events = self._fmp.economic_calendar(today, tomorrow)
            countries_upper = {c.upper() for c in cond.countries} if cond.countries else None

            def _parse_dt(s: str) -> datetime | None:
                s = (s or "").strip()
                if not s:
                    return None
                try:
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    # accetta "YYYY-MM-DD HH:MM:SS" o ISO
                    if " " in s and "T" not in s:
                        s = s.replace(" ", "T")
                    return datetime.fromisoformat(s).astimezone(tz=None).replace(tzinfo=None)
                except Exception:
                    return None

            def _match(e: Any) -> bool:
                if countries_upper:
                    if not e.country or e.country.upper() not in countries_upper:
                        return False
                if cond.impacts:
                    if not e.impact or e.impact not in cond.impacts:
                        return False
                if cond.keywords:
                    ev = (e.event or "").lower()
                    if not any(k.lower() in ev for k in cond.keywords):
                        return False
                dt = _parse_dt(e.date_time)
                if not dt:
                    return False
                return now <= dt <= until

            matched = [e for e in events if _match(e)]
            ok = len(matched) > 0
            ctx.update(
                {
                    "window_minutes": cond.window_minutes,
                    "count": len(matched),
                    "events": [
                        {
                            "event": e.event,
                            "country": e.country,
                            "date_time": e.date_time,
                            "impact": e.impact,
                            "actual": e.actual,
                            "estimate": e.estimate,
                            "previous": e.previous,
                        }
                        for e in matched[:10]
                    ],
                }
            )
            fp = self._fingerprint(
                {
                    "t": "fmp_econ_within",
                    "now": now.replace(second=0, microsecond=0).isoformat(),
                    "win": cond.window_minutes,
                    "countries": cond.countries,
                    "impacts": cond.impacts,
                    "keywords": cond.keywords,
                    "top": [(e.event, e.country, e.date_time, e.impact) for e in matched[:20]],
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, FmpEconomicEventSurpriseToday):
            if not self._fmp:
                raise RuntimeError("FMP not configured (missing FMP_API_KEY)")
            from datetime import date as _date

            today = _date.today()
            events = self._fmp.economic_calendar(today, today)
            countries_upper = {c.upper() for c in cond.countries} if cond.countries else None

            def _match(e: Any) -> tuple[bool, float | None, float | None]:
                if countries_upper:
                    if not e.country or e.country.upper() not in countries_upper:
                        return (False, None, None)
                if cond.impacts:
                    if not e.impact or e.impact not in cond.impacts:
                        return (False, None, None)
                if cond.keywords:
                    ev = (e.event or "").lower()
                    if not any(k.lower() in ev for k in cond.keywords):
                        return (False, None, None)
                if e.actual is None or e.estimate is None:
                    return (False, None, None)
                abs_s = abs(e.actual - e.estimate)
                pct_s = abs_s / abs(e.estimate) if e.estimate != 0 else None
                if cond.min_abs_surprise is not None and abs_s < cond.min_abs_surprise:
                    return (False, abs_s, pct_s)
                if cond.min_pct_surprise is not None and (pct_s is None or pct_s < cond.min_pct_surprise):
                    return (False, abs_s, pct_s)
                return (True, abs_s, pct_s)

            matched: list[dict[str, Any]] = []
            top_for_fp: list[tuple[Any, ...]] = []
            for e in events:
                ok_e, abs_s, pct_s = _match(e)
                if not ok_e:
                    continue
                item = {
                    "event": e.event,
                    "country": e.country,
                    "date_time": e.date_time,
                    "impact": e.impact,
                    "actual": e.actual,
                    "estimate": e.estimate,
                    "previous": e.previous,
                    "abs_surprise": abs_s,
                    "pct_surprise": pct_s,
                }
                matched.append(item)
                top_for_fp.append((e.event, e.country, e.date_time, e.actual, e.estimate, abs_s))

            ok = len(matched) > 0
            ctx.update({"count": len(matched), "events": matched[:10]})
            fp = self._fingerprint(
                {
                    "t": "fmp_econ_surprise_today",
                    "d": today.isoformat(),
                    "countries": cond.countries,
                    "impacts": cond.impacts,
                    "keywords": cond.keywords,
                    "min_abs": cond.min_abs_surprise,
                    "min_pct": cond.min_pct_surprise,
                    "top": top_for_fp[:30],
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, FmpEarningsToday):
            if not self._fmp:
                raise RuntimeError("FMP not configured (missing FMP_API_KEY)")
            from datetime import date as _date

            today = _date.today()
            events = self._fmp.earnings_calendar(today, today)
            symbols_set = {s.upper() for s in cond.symbols} if cond.symbols else None
            matched = [e for e in events if (not symbols_set or e.symbol.upper() in symbols_set)]
            ok = len(matched) > 0
            ctx.update(
                {
                    "count": len(matched),
                    "earnings": [
                        {
                            "symbol": e.symbol,
                            "date": e.date_str,
                            "time": e.time,
                            "eps_estimated": e.eps_estimated,
                            "eps": e.eps,
                            "revenue_estimated": e.revenue_estimated,
                            "revenue": e.revenue,
                        }
                        for e in matched[:15]
                    ],
                }
            )
            fp = self._fingerprint(
                {
                    "t": "fmp_earnings_today",
                    "d": today.isoformat(),
                    "symbols": sorted(list(symbols_set)) if symbols_set else [],
                    "top": [(e.symbol, e.date_str, e.time) for e in matched[:50]],
                }
            )
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        # Insider trading (SEC Form 4)
        from investimi_bot.config import SecForm4Significant

        if isinstance(cond, SecForm4Significant):
            if not self._sec:
                raise RuntimeError("SEC EDGAR not configured (missing SEC_USER_AGENT)")
            all_hits: list[dict[str, Any]] = []
            fp_items: list[tuple[str, str, float | None]] = []
            for t in cond.tickers:
                txs = self._sec.form4_transactions(t, within_days=cond.within_days)
                for tx in txs:
                    if tx.side and tx.side not in cond.sides:
                        continue
                    if cond.codes is not None:
                        if tx.code is None or tx.code not in cond.codes:
                            continue
                    if tx.value_usd is not None and tx.value_usd < cond.min_value_usd:
                        continue
                    all_hits.append(
                        {
                            "ticker": tx.ticker,
                            "filed_at": tx.filed_at,
                            "owner": tx.reporting_owner,
                            "title": tx.owner_title,
                            "code": tx.code,
                            "side": tx.side,
                            "shares": tx.shares,
                            "price": tx.price,
                            "value_usd": tx.value_usd,
                            "accession": tx.accession_no,
                        }
                    )
                    fp_items.append((tx.ticker, tx.accession_no, tx.value_usd))

            ok = len(all_hits) > 0
            # ordina per value desc (se presente)
            all_hits_sorted = sorted(all_hits, key=lambda x: float(x["value_usd"] or 0.0), reverse=True)
            ctx.update(
                {
                    "count": len(all_hits_sorted),
                    "hits": all_hits_sorted[:10],
                    "min_value_usd": cond.min_value_usd,
                    "within_days": cond.within_days,
                    "sides": cond.sides,
                    "codes": cond.codes or [],
                }
            )
            fp = self._fingerprint({"t": "sec_form4", "items": sorted(fp_items)[:50]})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        # US Congress trades (Senate + House) via provider
        from investimi_bot.config import UsCongressTradesSignificant

        if isinstance(cond, UsCongressTradesSignificant):
            if not self._congress:
                raise RuntimeError("US Congress trades not configured (missing CONGRESS_TRADES_USER_AGENT)")
            hits: list[dict[str, Any]] = []
            fp_items: list[tuple[str, str | None, str | None, float | None]] = []
            needles = [n.lower() for n in cond.politicians_contains]
            symbols = {s.upper() for s in cond.symbols} if cond.symbols else None

            for chamber in cond.chambers:
                if chamber == "house" and not self._congress._house_url and cond.politicians_contains:  # noqa: SLF001
                    trades = self._congress.fetch_house_by_names(cond.politicians_contains)
                else:
                    trades = self._congress.fetch(chamber)
                for t in trades:
                    if t.side not in cond.sides:
                        continue
                    if symbols and (t.ticker or "").upper() not in symbols:
                        continue
                    # Opzione A: soglia applicata su MAX range
                    amt = t.amount_max_usd if t.amount_max_usd is not None else t.amount_min_usd
                    if amt is not None and amt < cond.min_amount_usd:
                        continue
                    if needles:
                        who = (t.politician or "").lower()
                        if not any(n in who for n in needles):
                            continue
                    hits.append(
                        {
                            "chamber": t.chamber,
                            "politician": t.politician,
                            "ticker": t.ticker,
                            "asset_type": t.asset_type,
                            "asset_description": t.asset_description,
                            "side": t.side,
                            "transaction_type": t.transaction_type,
                            "transaction_date": t.transaction_date,
                            "disclosure_date": t.disclosure_date,
                            "amount_raw": t.amount_raw,
                            "amount_min_usd": t.amount_min_usd,
                            "amount_max_usd": t.amount_max_usd,
                            "source": t.source,
                        }
                    )
                    fp_items.append((t.chamber, t.politician, t.ticker, t.amount_max_usd))

            # sort by disclosure_date desc (best effort string) and amount desc
            hits_sorted = sorted(hits, key=lambda x: float(x["amount_max_usd"] or x["amount_min_usd"] or 0.0), reverse=True)
            ok = len(hits_sorted) > 0
            ctx.update(
                {
                    "count": len(hits_sorted),
                    "hits": hits_sorted[:10],
                    "min_amount_usd": cond.min_amount_usd,
                    "chambers": cond.chambers,
                    "sides": cond.sides,
                    "symbols": cond.symbols,
                }
            )
            fp = self._fingerprint({"t": "us_congress", "items": sorted(fp_items)[:100]})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        if isinstance(cond, AndCondition):
            results = [self.eval(c) for c in cond.all]
            ok = all(r.ok for r in results)
            # Nomi comodi per template: left/right quando sono 2 condizioni
            if len(results) == 2:
                ctx["left"] = results[0].context
                ctx["right"] = results[1].context
            ctx["all"] = [r.context for r in results]
            fp = self._fingerprint({"t": "and", "fps": [r.fingerprint for r in results], "ok": ok})
            return EvalResult(ok=ok, context=ctx, fingerprint=fp)

        raise TypeError(f"Unsupported condition type: {type(cond)}")

    @staticmethod
    def _fingerprint(obj: Any) -> str:
        raw = repr(obj).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

