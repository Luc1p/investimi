from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
import yaml
from bs4 import BeautifulSoup

from investimi_bot.providers.sec_edgar import Form4Transaction, SecEdgarClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


def _fingerprint(*parts: Any) -> str:
    flat = "|".join("" if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(flat.encode("utf-8")).hexdigest()[:32]


def _utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _parse_yyyy_mm_dd(s: str | None) -> date | None:
    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s.strip().split("-"))
        return date(y, m, d)
    except Exception:
        return None


@dataclass(frozen=True)
class ImportConfig:
    dsn: str
    alerts_path: Path
    within_days: int
    max_filings: int


def _normalize_universe_name(name: str) -> str:
    n = (name or "").strip().lower()
    n = n.replace(" ", "").replace("-", "").replace("_", "")
    if n in {"sp500", "sandp500", "s&p500", "s&p"}:
        return "sp500"
    if n in {"nasdaq100", "ndx100", "nasdaq"}:
        return "nasdaq100"
    if n in {"ftsemib", "ftsemib40", "mib"}:
        return "ftsemib"
    if n in {"ftsemib100", "mib100", "ftseitmib100"}:
        # Non esiste un “FTSE MIB 100” ufficiale; lo tratto come FTSE MIB (principale).
        return "ftsemib"
    return n


def _fetch_wikipedia_table(url: str) -> BeautifulSoup:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.getenv("WIKI_USER_AGENT", "InvestimiBot/0.1 (youremail@example.com)"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    r = s.get(url, timeout=25)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def _tickers_sp500() -> list[str]:
    soup = _fetch_wikipedia_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
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
    soup = _fetch_wikipedia_table("https://en.wikipedia.org/wiki/Nasdaq-100")
    # La pagina ha più tabelle: cerco la tabella componenti (prima colonna = Ticker)
    for table in soup.select("table.wikitable"):
        # heuristics: header contains 'Ticker'
        ths = [th.get_text(strip=True).lower() for th in table.select("thead th")]
        if not ths:
            ths = [th.get_text(strip=True).lower() for th in table.select("tr th")]
        if "ticker" not in ths and "ticker symbol" not in ths and "symbol" not in ths:
            continue
        out: list[str] = []
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            sym = tds[0].get_text(strip=True)
            if sym and 1 <= len(sym) <= 10:
                out.append(sym.upper().replace(".", "-"))
        # Se sembra sensata (>=50) è quella giusta
        if len(out) >= 50:
            return out
    return []


def _tickers_ftsemib() -> list[str]:
    # Nota: tickers italiani NON sono su SEC. Li includo comunque nell’universo così puoi riutilizzare la lista
    # per altri dataset (prezzi/feature), ma il loader SEC li skippa.
    soup = _fetch_wikipedia_table("https://en.wikipedia.org/wiki/FTSE_MIB")
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
                out.append(sym.upper())
        if len(out) >= 20:
            return out
    return []


def _tickers_from_universes(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        nn = _normalize_universe_name(n)
        if nn == "sp500":
            out.extend(_tickers_sp500())
        elif nn == "nasdaq100":
            out.extend(_tickers_nasdaq100())
        elif nn == "ftsemib":
            out.extend(_tickers_ftsemib())
    # dedup stable
    seen: set[str] = set()
    dedup: list[str] = []
    for t in out:
        tt = (t or "").strip().upper()
        if not tt or tt in seen:
            continue
        seen.add(tt)
        dedup.append(tt)
    return dedup


def _upsert_source(cur: psycopg.Cursor[Any], key: str, description: str) -> int:
    cur.execute(
        """
        insert into core.sources (key, description)
        values (%s, %s)
        on conflict (key) do update set description = excluded.description
        returning id
        """,
        (key, description),
    )
    return int(cur.fetchone()[0])


def _get_or_create_actor(cur: psycopg.Cursor[Any], *, name: str) -> int:
    cur.execute(
        """
        insert into core.actors (actor_type, name, chamber)
        values (%s, %s, %s)
        on conflict (actor_type, name, chamber) do update set name = excluded.name
        returning id
        """,
        ("insider", name, None),
    )
    return int(cur.fetchone()[0])


def _get_or_create_instrument(cur: psycopg.Cursor[Any], *, ticker: str, cik: int | None) -> int:
    cur.execute(
        """
        insert into core.instruments (ticker, cik, asset_class)
        values (%s, %s, %s)
        on conflict (ticker) do update set cik = coalesce(core.instruments.cik, excluded.cik)
        returning id
        """,
        (ticker, str(cik) if cik else None, "equity"),
    )
    return int(cur.fetchone()[0])


def _get_or_create_filing(cur: psycopg.Cursor[Any], *, source_id: int, accession: str, filing_url: str | None) -> int:
    cur.execute(
        """
        insert into core.filings (source_id, external_id, filing_url)
        values (%s, %s, %s)
        on conflict (source_id, external_id) do update set filing_url = excluded.filing_url
        returning id
        """,
        (source_id, accession, filing_url),
    )
    return int(cur.fetchone()[0])


def _insert_tx(
    cur: psycopg.Cursor[Any],
    *,
    source_id: int,
    filing_id: int,
    actor_id: int | None,
    instrument_id: int,
    tx: Form4Transaction,
) -> bool:
    filed_d = _parse_yyyy_mm_dd(tx.filed_at)
    disclosed_at = _utc_midnight(filed_d) if filed_d else None

    side = "buy" if tx.side == "acquire" else "sell" if tx.side == "dispose" else "unknown"
    amount_raw = None
    amount_min = None
    amount_max = None
    if tx.value_usd is not None:
        amount_raw = str(float(tx.value_usd))
        amount_min = float(tx.value_usd)
        amount_max = float(tx.value_usd)

    fp = _fingerprint(
        "sec_form4",
        tx.accession_no,
        tx.ticker,
        tx.reporting_owner,
        tx.owner_title,
        tx.code,
        tx.side,
        tx.shares,
        tx.price,
        tx.filed_at,
    )

    cur.execute(
        """
        insert into core.trade_events (
          source_id, filing_id, actor_id, instrument_id,
          ticker_raw, asset_description, asset_type_raw,
          side, transaction_type, owner, actor_role_title,
          shares, price, amount_raw, amount_min_usd, amount_max_usd,
          transaction_date, disclosure_date, disclosed_at,
          ptr_link,
          event_fingerprint, raw
        ) values (
          %s, %s, %s, %s,
          %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s,
          %s,
          %s, %s
        )
        on conflict (source_id, event_fingerprint) do nothing
        """,
        (
            source_id,
            filing_id,
            actor_id,
            instrument_id,
            tx.ticker,
            None,
            "equity",
            side,
            tx.code,
            tx.reporting_owner,
            tx.owner_title,
            tx.shares,
            tx.price,
            amount_raw,
            amount_min,
            amount_max,
            None,
            filed_d,
            disclosed_at,
            None,
            fp,
            json.dumps(
                {
                    "ticker": tx.ticker,
                    "cik": tx.cik,
                    "accession_no": tx.accession_no,
                    "filed_at": tx.filed_at,
                    "reporting_owner": tx.reporting_owner,
                    "owner_title": tx.owner_title,
                    "code": tx.code,
                    "side": tx.side,
                    "shares": tx.shares,
                    "price": tx.price,
                    "value_usd": tx.value_usd,
                }
            ),
        ),
    )
    return cur.rowcount == 1


def _tickers_from_alerts(alerts_path: Path) -> list[str]:
    cfg = yaml.safe_load(alerts_path.read_text(encoding="utf-8")) or {}
    rules = cfg.get("rules") or []
    for r in rules:
        if isinstance(r, dict) and r.get("id") == "insider_buys_watchlist":
            when = r.get("when") or {}
            tickers = when.get("tickers") or []
            out = []
            for t in tickers:
                if isinstance(t, str) and t.strip():
                    out.append(t.strip().upper())
            return out
    return []


def main() -> int:
    if load_dotenv:
        load_dotenv()
    dsn = os.getenv("INVESTIMI_DB_DSN", "postgresql://investimi:investimi@localhost:5433/investimi")
    alerts_path = Path(os.getenv("ALERTS_YAML", "config/alerts.yaml"))
    within_days = int(os.getenv("SEC_WITHIN_DAYS", "30"))
    max_filings = int(os.getenv("SEC_MAX_FILINGS", "30"))
    tickers_per_second = float(os.getenv("SEC_TICKERS_PER_SECOND", "2.0"))
    sec_ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not sec_ua:
        raise SystemExit("Missing SEC_USER_AGENT env var")

    universes = [u.strip() for u in os.getenv("SEC_UNIVERSES", "").split(",") if u.strip()]
    tickers = [t.strip().upper() for t in os.getenv("SEC_TICKERS", "").split(",") if t.strip()]
    if universes:
        tickers = tickers or _tickers_from_universes(universes)
        # per universi grandi, default più conservativo se non specificato
        if "SEC_MAX_FILINGS" not in os.environ:
            max_filings = 5
    if not tickers:
        tickers = _tickers_from_alerts(alerts_path)
    if not tickers:
        raise SystemExit(
            "No tickers configured. Set SEC_UNIVERSES (sp500,nasdaq100,ftsemib) or SEC_TICKERS or config/alerts.yaml insider_buys_watchlist tickers."
        )

    client = SecEdgarClient(sec_ua)
    # warm-up SEC ticker map once
    client.load_ticker_map()

    with psycopg.connect(dsn) as conn:
        conn.execute("set timezone to 'UTC'")
        with conn.cursor() as cur:
            cur.execute(
                "insert into ops.ingestion_runs (job_key, status, meta) values (%s, %s, %s) returning id",
                (
                    "import_sec_form4",
                    "running",
                    json.dumps(
                        {
                            "tickers": tickers[:50],
                            "tickers_count": len(tickers),
                            "universes": universes,
                            "within_days": within_days,
                            "max_filings": max_filings,
                        }
                    ),
                ),
            )
            run_id = int(cur.fetchone()[0])

            try:
                src_id = _upsert_source(cur, "sec_edgar_form4", "SEC EDGAR Form 4")
                inserted = 0
                total = 0
                skipped_unknown = 0
                errors = 0
                for t in tickers:
                    t0 = time.time()
                    try:
                        txs = client.form4_transactions(t, within_days=within_days, max_filings=max_filings)
                    except Exception:
                        # tipico: ticker non presente nella mappa SEC (es. FTSE MIB .MI)
                        skipped_unknown += 1
                        continue
                    total += len(txs)
                    for tx in txs:
                        actor_id = _get_or_create_actor(cur, name=tx.reporting_owner or "(unknown)")
                        inst_id = _get_or_create_instrument(cur, ticker=tx.ticker, cik=tx.cik)
                        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(tx.cik)}/{tx.accession_no.replace('-', '')}/"
                        filing_id = _get_or_create_filing(cur, source_id=src_id, accession=tx.accession_no, filing_url=filing_url)
                        if _insert_tx(cur, source_id=src_id, filing_id=filing_id, actor_id=actor_id, instrument_id=inst_id, tx=tx):
                            inserted += 1
                    # rate limit grossolano per non stressare SEC
                    if tickers_per_second > 0:
                        min_dt = 1.0 / tickers_per_second
                        dt = time.time() - t0
                        if dt < min_dt:
                            time.sleep(min_dt - dt)

                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    (
                        "ok",
                        json.dumps(
                            {
                                "tickers_count": len(tickers),
                                "universes": universes,
                                "within_days": within_days,
                                "max_filings": max_filings,
                                "total": total,
                                "inserted": inserted,
                                "skipped_unknown_tickers": skipped_unknown,
                                "errors": errors,
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

