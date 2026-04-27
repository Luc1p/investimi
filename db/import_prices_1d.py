from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
import requests
from bs4 import BeautifulSoup
from io import StringIO
import pyarrow.parquet as pq
import csv

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore[assignment]


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _normalize_universe_name(name: str) -> str:
    n = (name or "").strip().lower()
    n = n.replace(" ", "").replace("-", "").replace("_", "")
    if n in {"sp500", "sandp500", "s&p500", "s&p"}:
        return "sp500"
    if n in {"nasdaq100", "ndx100", "nasdaq"}:
        return "nasdaq100"
    if n in {"ftsemib", "ftsemib40", "mib", "ftsemib100", "mib100"}:
        return "ftsemib"
    return n


def _fetch_wikipedia_html(url: str) -> str:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.getenv("WIKI_USER_AGENT", "InvestimiBot/0.1 (youremail@example.com)"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    r = s.get(url, timeout=25)
    r.raise_for_status()
    return r.text


def _tickers_sp500() -> list[str]:
    html = _fetch_wikipedia_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.wikitable")
    out: list[str] = []
    if not table:
        return out
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        sym = tds[0].get_text(strip=True)
        if sym:
            out.append(sym.upper().replace(".", "-"))
    return out


def _tickers_nasdaq100() -> list[str]:
    html = _fetch_wikipedia_html("https://en.wikipedia.org/wiki/Nasdaq-100")
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.wikitable"):
        ths = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        if not ths:
            ths = [th.get_text(strip=True).lower() for th in table.select("tr th")]
        if "ticker" not in ths and "symbol" not in ths:
            continue
        out: list[str] = []
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            sym = tds[0].get_text(strip=True)
            if sym and 1 <= len(sym) <= 10:
                out.append(sym.upper().replace(".", "-"))
        if len(out) >= 50:
            return out
    return []


def _tickers_ftsemib() -> list[str]:
    # Per i ticker italiani, teniamo il formato "XXX.MI" standard.
    html = _fetch_wikipedia_html("https://en.wikipedia.org/wiki/FTSE_MIB")
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.wikitable"):
        ths = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        if not ths:
            ths = [th.get_text(strip=True).lower() for th in table.select("tr th")]
        if "ticker" not in ths and "symbol" not in ths:
            continue
        out: list[str] = []
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            sym = tds[0].get_text(strip=True)
            if sym:
                s = sym.upper().strip()
                # Wikipedia può mostrare "ENEL" invece di "ENEL.MI"
                if "." not in s:
                    s = f"{s}.MI"
                out.append(s)
        if len(out) >= 20:
            return out
    return []


def _tickers_from_universes(universes: list[str]) -> list[str]:
    out: list[str] = []
    for u in universes:
        uu = _normalize_universe_name(u)
        if uu == "sp500":
            out.extend(_tickers_sp500())
        elif uu == "nasdaq100":
            out.extend(_tickers_nasdaq100())
        elif uu == "ftsemib":
            out.extend(_tickers_ftsemib())

    # Always include benchmark for US alpha computations
    if "SPY" not in out:
        out.append("SPY")

    seen: set[str] = set()
    dedup: list[str] = []
    for t in out:
        tt = (t or "").strip().upper()
        if not tt or tt in seen:
            continue
        seen.add(tt)
        dedup.append(tt)
    return dedup


def _to_stooq_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    # Stooq: US spesso ".US", Italia spesso ".IT". In più class shares: BRK-B -> BRK.B
    t = t.replace("-", ".")
    if t.endswith(".MI"):
        t = t[:-3] + ".IT"
    if "." not in t:
        t = t + ".US"
    return t.lower()


def _download_stooq_1d(ticker: str, *, start: date) -> pd.DataFrame:
    apikey = os.getenv("STOOQ_APIKEY", "").strip()
    if not apikey:
        raise RuntimeError(
            "Missing STOOQ_APIKEY. Get a free apikey once via "
            "https://stooq.com/q/d/?s=aapl.us&get_apikey (captcha) and set STOOQ_APIKEY env var."
        )
    stooq = _to_stooq_symbol(ticker)
    url = "https://stooq.com/q/d/l/"
    r = requests.get(url, params={"s": stooq, "i": "d", "apikey": apikey}, timeout=25)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    # Stooq columns: Date,Open,High,Low,Close,Volume
    if "Date" not in df.columns:
        raise RuntimeError(f"Unexpected Stooq format for {ticker} ({stooq})")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce", utc=True)
    df = df.dropna(subset=["Date"]).sort_values("Date")
    df = df[df["Date"].dt.date >= start]
    df = df.rename(
        columns={
            "Date": "ts",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df["ticker"] = ticker.strip().upper()
    df["source"] = "stooq"
    df["freq"] = "1d"
    df["asof"] = _utc_now()
    # normalize dtypes
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df = df.dropna(subset=["close"])
    return df[["ts", "ticker", "open", "high", "low", "close", "volume", "source", "freq", "asof"]]


def _download_yahoo_1d(ticker: str, *, start: date, end: date | None = None) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("Missing yfinance dependency (pip install yfinance)")
    end_dt = None
    if end is not None:
        # yfinance end is exclusive; add one day via pandas Timestamp
        end_dt = (pd.Timestamp(end) + pd.Timedelta(days=1)).to_pydatetime()
    df = yf.download(
        ticker,
        start=pd.Timestamp(start).to_pydatetime(),
        end=end_dt,
        interval="1d",
        auto_adjust=False,
        progress=False,
        actions=False,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", "ticker", "open", "high", "low", "close", "volume", "source", "freq", "asof"])
    # yfinance can return MultiIndex columns even for a single ticker
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]) for c in df.columns]
    df = df.reset_index()
    # yfinance uses column 'Date' or 'Datetime'
    ts_col = "Date" if "Date" in df.columns else "Datetime" if "Datetime" in df.columns else None
    if not ts_col:
        raise RuntimeError(f"Unexpected yfinance format for {ticker}")
    df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    df = df.dropna(subset=[ts_col]).sort_values(ts_col)
    df = df.rename(
        columns={
            ts_col: "ts",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df["ticker"] = ticker.strip().upper()
    df["source"] = "yahoo"
    df["freq"] = "1d"
    df["asof"] = _utc_now()
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df = df.dropna(subset=["close"])
    return df[["ts", "ticker", "open", "high", "low", "close", "volume", "source", "freq", "asof"]]


def _yyyymmdd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


def _download_stooq_1d_range(ticker: str, *, d1: date, d2: date) -> pd.DataFrame:
    """
    Scarica solo un range (incrementale) via Stooq d1/d2.
    d1/d2 format: YYYYMMDD.
    """
    apikey = os.getenv("STOOQ_APIKEY", "").strip()
    if not apikey:
        raise RuntimeError(
            "Missing STOOQ_APIKEY. Get a free apikey once via "
            "https://stooq.com/q/d/?s=aapl.us&get_apikey (captcha) and set STOOQ_APIKEY env var."
        )
    stooq = _to_stooq_symbol(ticker)
    url = "https://stooq.com/q/d/l/"
    r = requests.get(
        url,
        params={"s": stooq, "i": "d", "d1": _yyyymmdd(d1), "d2": _yyyymmdd(d2), "apikey": apikey},
        timeout=25,
    )
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    if "Date" not in df.columns:
        raise RuntimeError(f"Unexpected Stooq format for {ticker} ({stooq})")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce", utc=True)
    df = df.dropna(subset=["Date"]).sort_values("Date")
    df = df.rename(
        columns={
            "Date": "ts",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df["ticker"] = ticker.strip().upper()
    df["source"] = "stooq"
    df["freq"] = "1d"
    df["asof"] = _utc_now()
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df = df.dropna(subset=["close"])
    return df[["ts", "ticker", "open", "high", "low", "close", "volume", "source", "freq", "asof"]]


def _read_existing_max_ts(parquet_path: Path) -> pd.Timestamp | None:
    if not parquet_path.exists():
        return None
    # leggero: carico solo colonna ts
    try:
        tbl = pq.read_table(parquet_path, columns=["ts"])
        if tbl.num_rows == 0:
            return None
        s = tbl.column("ts").to_pandas()
        if s.empty:
            return None
        return pd.to_datetime(s, utc=True, errors="coerce").max()
    except Exception:
        return None


def _merge_and_write(existing_path: Path, new_df: pd.DataFrame) -> pd.DataFrame:
    if not existing_path.exists():
        new_df = new_df.sort_values("ts")
        new_df.to_parquet(existing_path, index=False)
        return new_df
    # leggi esistente (per ticker ~6-7k righe => ok in RAM)
    old = pd.read_parquet(existing_path)
    merged = pd.concat([old, new_df], ignore_index=True)
    merged["ts"] = pd.to_datetime(merged["ts"], utc=True, errors="coerce")
    merged = merged.dropna(subset=["ts"]).drop_duplicates(subset=["ts"], keep="last").sort_values("ts")
    merged.to_parquet(existing_path, index=False)
    return merged


def _parquet_path(lake_root: Path, *, source: str, freq: str, ticker: str) -> Path:
    return lake_root / "prices" / "v1" / f"source={source}" / f"freq={freq}" / f"ticker={ticker.upper()}" / "bars.parquet"


def _ensure_parent_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _upsert_instrument(cur: psycopg.Cursor[Any], ticker: str) -> int:
    cur.execute(
        """
        insert into core.instruments (ticker, asset_class)
        values (%s, %s)
        on conflict (ticker) do update set ticker = excluded.ticker
        returning id
        """,
        (ticker, "equity"),
    )
    return int(cur.fetchone()[0])


def _upsert_manifest(
    cur: psycopg.Cursor[Any],
    *,
    instrument_id: int,
    source_key: str,
    freq: str,
    parquet_path: str,
    start_date: date | None,
    end_date: date | None,
    row_count: int,
) -> None:
    cur.execute(
        """
        insert into core.price_bars_manifest (
          instrument_id, source_key, freq, parquet_path, start_date, end_date, row_count, updated_at
        ) values (%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (instrument_id, source_key, freq)
        do update set
          parquet_path = excluded.parquet_path,
          start_date = excluded.start_date,
          end_date = excluded.end_date,
          row_count = excluded.row_count,
          updated_at = now()
        """,
        (instrument_id, source_key, freq, parquet_path, start_date, end_date, row_count),
    )


@dataclass(frozen=True)
class ImportCfg:
    dsn: str
    lake_root: Path
    start_date: date
    tickers_per_second: float
    max_tickers: int | None


def main() -> int:
    if load_dotenv:
        try:
            load_dotenv(".env")
        except Exception:
            load_dotenv()
    dsn = os.getenv("INVESTIMI_DB_DSN", "postgresql://investimi:investimi@localhost:5433/investimi")
    lake_root = Path(os.getenv("LAKE_ROOT", "lake"))
    universes = [u.strip() for u in os.getenv("PRICE_UNIVERSES", "sp500,nasdaq100,ftsemib").split(",") if u.strip()]
    start_s = os.getenv("PRICE_START_DATE", "2000-01-01")
    y, m, d = (int(x) for x in start_s.split("-"))
    start_date = date(y, m, d)
    tickers_per_second = float(os.getenv("PRICE_TICKERS_PER_SECOND", "2.0"))
    incremental = os.getenv("PRICE_INCREMENTAL", "1").strip().lower() not in {"0", "false", "no"}
    export_manifest_csv = os.getenv("PRICE_EXPORT_MANIFEST_CSV", "").strip()
    provider = os.getenv("PRICE_PROVIDER", "auto").strip().lower()
    max_tickers = os.getenv("PRICE_MAX_TICKERS")
    max_tickers_i = int(max_tickers) if max_tickers else None

    tickers = _tickers_from_universes(universes)
    if max_tickers_i is not None and max_tickers_i > 0:
        tickers = tickers[: max_tickers_i]
    if not tickers:
        raise SystemExit("No tickers resolved from PRICE_UNIVERSES")

    cfg = ImportCfg(
        dsn=dsn,
        lake_root=lake_root,
        start_date=start_date,
        tickers_per_second=tickers_per_second,
        max_tickers=max_tickers_i,
    )

    with psycopg.connect(cfg.dsn) as conn:
        conn.execute("set timezone to 'UTC'")
        with conn.cursor() as cur:
            cur.execute(
                "insert into ops.ingestion_runs (job_key, status, meta) values (%s,%s,%s) returning id",
                (
                    "import_prices_1d",
                    "running",
                    json.dumps(
                        {
                            "universes": universes,
                            "start_date": start_s,
                            "tickers_count": len(tickers),
                            "max_tickers": cfg.max_tickers,
                            "incremental": incremental,
                        }
                    ),
                ),
            )
            run_id = int(cur.fetchone()[0])

            ok = 0
            skipped = 0
            errors: list[dict[str, Any]] = []
            touched: list[str] = []

            try:
                for t in tickers:
                    t0 = time.time()
                    try:
                        # provider selection:
                        # - auto: stooq for US-like tickers, yahoo for .MI (FTSE MIB)
                        # - stooq|yahoo: forced
                        prov = provider
                        if prov == "auto":
                            prov = "yahoo" if t.upper().endswith(".MI") else "stooq"

                        source_key = prov
                        p = _parquet_path(cfg.lake_root, source=source_key, freq="1d", ticker=t)
                        _ensure_parent_dir(p)

                        df: pd.DataFrame
                        if incremental and p.exists():
                            last_ts = _read_existing_max_ts(p)
                            if last_ts is None:
                                if prov == "stooq":
                                    df = _download_stooq_1d(t, start=cfg.start_date)
                                else:
                                    df = _download_yahoo_1d(t, start=cfg.start_date)
                                if df.empty:
                                    skipped += 1
                                    continue
                                df = _merge_and_write(p, df)
                            else:
                                last_date = last_ts.date()
                                today = date.today()
                                if last_date >= today:
                                    # già aggiornato
                                    skipped += 1
                                    continue
                                # scarica solo dal giorno successivo
                                d1 = date.fromordinal(last_date.toordinal() + 1)
                                if prov == "stooq":
                                    df_new = _download_stooq_1d_range(t, d1=d1, d2=today)
                                else:
                                    df_new = _download_yahoo_1d(t, start=d1, end=today)
                                if df_new.empty:
                                    skipped += 1
                                    continue
                                df = _merge_and_write(p, df_new)
                        else:
                            if prov == "stooq":
                                df = _download_stooq_1d(t, start=cfg.start_date)
                            else:
                                df = _download_yahoo_1d(t, start=cfg.start_date)
                            if df.empty:
                                skipped += 1
                                continue
                            df = df.sort_values("ts")
                            df.to_parquet(p, index=False)

                        inst_id = _upsert_instrument(cur, t)
                        sd = df["ts"].min().date() if not df.empty else None
                        ed = df["ts"].max().date() if not df.empty else None
                        _upsert_manifest(
                            cur,
                            instrument_id=inst_id,
                            source_key=source_key,
                            freq="1d",
                            parquet_path=str(p),
                            start_date=sd,
                            end_date=ed,
                            row_count=int(len(df)),
                        )
                        ok += 1
                        touched.append(t)
                    except Exception as e:
                        errors.append({"ticker": t, "error": str(e)[:250]})
                    finally:
                        if cfg.tickers_per_second > 0:
                            min_dt = 1.0 / cfg.tickers_per_second
                            dt = time.time() - t0
                            if dt < min_dt:
                                time.sleep(min_dt - dt)

                if export_manifest_csv:
                    # export manifest rows for tickers touched in this run
                    outp = Path(export_manifest_csv)
                    outp.parent.mkdir(parents=True, exist_ok=True)
                    cur.execute(
                        """
                        select i.ticker, m.source_key, m.freq, m.start_date, m.end_date, m.row_count, m.parquet_path, m.updated_at
                        from core.price_bars_manifest m
                        join core.instruments i on i.id = m.instrument_id
                        where m.source_key='stooq' and m.freq='1d' and i.ticker = any(%s)
                        order by i.ticker
                        """,
                        (touched,),
                    )
                    rows = cur.fetchall()
                    with outp.open("w", encoding="utf-8", newline="") as f:
                        w = csv.writer(f)
                        w.writerow(["ticker", "source_key", "freq", "start_date", "end_date", "row_count", "parquet_path", "updated_at"])
                        w.writerows(rows)

                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    (
                        "ok",
                        json.dumps(
                            {
                                "universes": universes,
                                "start_date": start_s,
                                "tickers_count": len(tickers),
                                "ok": ok,
                                "skipped": skipped,
                                "errors_count": len(errors),
                                "errors_sample": errors[:10],
                                "export_manifest_csv": export_manifest_csv or None,
                            }
                        ),
                        run_id,
                    ),
                )
            except Exception as e:
                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    ("error", json.dumps({"error": str(e)[:500]}), run_id),
                )
                raise

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

