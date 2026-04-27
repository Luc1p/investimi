from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


@dataclass(frozen=True)
class EventRow:
    event_id: int
    actor: str | None
    actor_type: str | None
    chamber: str | None
    side: str | None
    ticker: str | None
    disclosed_date: date | None
    instrument_id: int | None


def _pick_price_path(cur: psycopg.Cursor[Any], instrument_id: int) -> str | None:
    # Prefer stooq if present; else yahoo.
    cur.execute(
        """
        select parquet_path
        from core.price_bars_manifest
        where instrument_id=%s and freq='1d'
        order by case source_key when 'stooq' then 0 when 'yahoo' then 1 else 2 end, updated_at desc
        limit 1
        """,
        (instrument_id,),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _load_close_series(parquet_path: str) -> pd.Series:
    df = pd.read_parquet(parquet_path, columns=["ts", "close"]).sort_values("ts")
    s = pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["ts"], utc=True, errors="coerce"))
    s = s.dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _price_on_or_after(series: pd.Series, d: pd.Timestamp) -> tuple[pd.Timestamp | None, float | None]:
    if series.empty:
        return None, None
    idx = series.index
    pos = idx.searchsorted(d, side="left")
    if pos >= len(idx):
        return None, None
    ts = idx[pos]
    return ts, float(series.iloc[pos])


def _return_over(series: pd.Series, d0: pd.Timestamp, horizon_days: int) -> float | None:
    t0, p0 = _price_on_or_after(series, d0)
    if p0 is None or t0 is None:
        return None
    target = d0 + pd.Timedelta(days=horizon_days)
    t1, p1 = _price_on_or_after(series, target)
    if p1 is None or t1 is None:
        return None
    if p0 == 0:
        return None
    return (p1 / p0) - 1.0


def main() -> int:
    if load_dotenv:
        try:
            load_dotenv(".env")
        except Exception:
            load_dotenv()

    dsn = os.getenv("INVESTIMI_DB_DSN", "postgresql://investimi:investimi@localhost:5433/investimi")
    benchmark_ticker = os.getenv("ALPHA_BENCHMARK", "SPY").strip().upper()
    horizons = [int(x) for x in os.getenv("ALPHA_HORIZONS", "1,5,20,60").split(",") if x.strip()]
    min_trades = int(os.getenv("ALPHA_MIN_TRADES", "5"))
    actor_type = os.getenv("ALPHA_ACTOR_TYPE", "politician").strip().lower()  # politician|insider|all
    since = os.getenv("ALPHA_SINCE", "2024-01-01").strip()
    out_csv = os.getenv("ALPHA_OUT_CSV", "").strip()

    since_date = pd.to_datetime(since, utc=True, errors="coerce")
    if pd.isna(since_date):
        raise SystemExit("Invalid ALPHA_SINCE (expected YYYY-MM-DD)")

    with psycopg.connect(dsn) as conn:
        conn.execute("set timezone to 'UTC'")
        with conn.cursor() as cur:
            # Load benchmark close series (SPY)
            cur.execute(
                """
                insert into core.instruments (ticker, asset_class)
                values (%s, %s)
                on conflict (ticker) do update set ticker=excluded.ticker
                returning id
                """,
                (benchmark_ticker, "etf"),
            )
            bench_inst_id = int(cur.fetchone()[0])
            bench_path = _pick_price_path(cur, bench_inst_id)
            if not bench_path:
                raise SystemExit(
                    f"Benchmark {benchmark_ticker} has no price_bars_manifest. "
                    f"Import prices for {benchmark_ticker} first (e.g. include it in an universe run)."
                )
            bench = _load_close_series(bench_path)

            where_actor = ""
            params: list[Any] = [since_date.date()]
            if actor_type in {"politician", "insider"}:
                where_actor = "and a.actor_type=%s"
                params.append(actor_type)

            cur.execute(
                f"""
                select
                  e.id,
                  a.name,
                  a.actor_type,
                  a.chamber,
                  e.side,
                  i.ticker,
                  (e.disclosed_at at time zone 'UTC')::date as disclosed_date,
                  e.instrument_id
                from core.trade_events e
                left join core.actors a on a.id=e.actor_id
                left join core.instruments i on i.id=e.instrument_id
                where e.disclosed_at is not null
                  and (e.disclosed_at at time zone 'UTC')::date >= %s
                  {where_actor}
                order by e.disclosed_at desc
                """,
                tuple(params),
            )
            rows = [
                EventRow(
                    event_id=int(x[0]),
                    actor=x[1],
                    actor_type=x[2],
                    chamber=x[3],
                    side=x[4],
                    ticker=x[5],
                    disclosed_date=x[6],
                    instrument_id=int(x[7]) if x[7] is not None else None,
                )
                for x in cur.fetchall()
            ]

            if not rows:
                print("No events found")
                return 0

            price_cache: dict[int, pd.Series] = {bench_inst_id: bench}
            recs: list[dict[str, Any]] = []

            for ev in rows:
                if not ev.instrument_id or not ev.disclosed_date:
                    continue
                # US-only for now (benchmark SPY)
                if ev.ticker and ev.ticker.upper().endswith(".MI"):
                    continue

                if ev.instrument_id not in price_cache:
                    path = _pick_price_path(cur, ev.instrument_id)
                    if not path:
                        continue
                    try:
                        price_cache[ev.instrument_id] = _load_close_series(path)
                    except Exception:
                        continue

                series = price_cache[ev.instrument_id]
                d0 = pd.Timestamp(ev.disclosed_date, tz="UTC")
                bench_d0 = d0

                out: dict[str, Any] = {
                    "event_id": ev.event_id,
                    "actor": ev.actor,
                    "actor_type": ev.actor_type,
                    "chamber": ev.chamber,
                    "side": ev.side,
                    "ticker": ev.ticker,
                    "disclosed_date": str(ev.disclosed_date),
                }

                for h in horizons:
                    r_asset = _return_over(series, d0, h)
                    r_bench = _return_over(bench, bench_d0, h)
                    out[f"ret_{h}d"] = r_asset
                    out[f"bench_{h}d"] = r_bench
                    out[f"alpha_{h}d"] = (r_asset - r_bench) if (r_asset is not None and r_bench is not None) else None
                recs.append(out)

    df = pd.DataFrame(recs)
    if df.empty:
        print("No matched events with prices")
        return 0

    # Aggregate by actor for 60d alpha (and all horizons)
    agg_cols = {}
    for h in horizons:
        agg_cols[f"alpha_{h}d"] = "mean"
        agg_cols[f"ret_{h}d"] = "mean"
        agg_cols[f"bench_{h}d"] = "mean"

    g = (
        df.dropna(subset=[f"alpha_{horizons[-1]}d"])
        .groupby(["actor"], dropna=False)
        .agg(
            trades=("event_id", "count"),
            **{k: (k, v) for k, v in agg_cols.items()},
        )
        .reset_index()
    )
    g = g[g["trades"] >= min_trades].sort_values(by=f"alpha_{horizons[-1]}d", ascending=False)

    print(f"Benchmark: {benchmark_ticker} | since: {since} | min_trades: {min_trades}")
    print("Top 20 by mean alpha (last horizon):")
    cols = ["actor", "trades"] + [f"alpha_{h}d" for h in horizons]
    print(g[cols].head(20).to_string(index=False))

    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        g.to_csv(out_csv, index=False)
        df.to_csv(Path(out_csv).with_name("alpha_events.csv"), index=False)
        print(f"Wrote: {out_csv} and alpha_events.csv")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

