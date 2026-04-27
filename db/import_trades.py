from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_mmddyyyy(s: Any) -> date | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _norm_ticker(s: Any) -> str | None:
    if s is None:
        return None
    t = str(s).strip().upper()
    if not t or t in ("--", "N/A"):
        return None
    # strip anything that looks like "(AAPL)" or HTML remnants
    t = re.sub(r"<[^>]+>", "", t).strip().upper()
    t = t.replace("(", "").replace(")", "").strip()
    # keep simple tickers only
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
        return None
    return t


def _infer_side(row: dict[str, Any]) -> str:
    s = str(row.get("side") or "").strip().lower()
    if s in ("buy", "purchase", "acquire"):
        return "buy"
    if s in ("sell", "sale", "dispose"):
        return "sell"
    tx = str(row.get("transaction_type") or row.get("type") or "").strip().upper()
    if tx.startswith("P"):
        return "buy"
    if tx.startswith("S"):
        return "sell"
    return "unknown"


def _amount_range(raw: Any) -> tuple[str | None, float | None, float | None]:
    if raw is None:
        return (None, None, None)
    s = str(raw).strip()
    if not s or s == "--":
        return (s or None, None, None)
    # "$15,001 - $50,000"
    m = re.search(r"\$([\d,]+)\s*-\s*\$([\d,]+)", s)
    if not m:
        return (s, None, None)
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", ""))
    return (s, lo, hi)


def _fingerprint(*parts: Any) -> str:
    flat = "|".join("" if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(flat.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ImportConfig:
    dsn: str
    house_path: Path
    senate_path: Path


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


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


def _get_or_create_actor(cur: psycopg.Cursor[Any], *, actor_type: str, name: str, chamber: str | None) -> int:
    cur.execute(
        """
        insert into core.actors (actor_type, name, chamber)
        values (%s, %s, %s)
        on conflict (actor_type, name, chamber) do update set name = excluded.name
        returning id
        """,
        (actor_type, name, chamber),
    )
    return int(cur.fetchone()[0])


def _get_or_create_instrument(cur: psycopg.Cursor[Any], *, ticker: str) -> int:
    cur.execute(
        """
        insert into core.instruments (ticker)
        values (%s)
        on conflict (ticker) do update set ticker = excluded.ticker
        returning id
        """,
        (ticker,),
    )
    return int(cur.fetchone()[0])


def _get_or_create_filing(cur: psycopg.Cursor[Any], *, source_id: int, external_id: str, filing_url: str | None) -> int:
    cur.execute(
        """
        insert into core.filings (source_id, external_id, filing_url)
        values (%s, %s, %s)
        on conflict (source_id, external_id) do update set filing_url = excluded.filing_url
        returning id
        """,
        (source_id, external_id, filing_url),
    )
    return int(cur.fetchone()[0])


def _insert_trade_event(
    cur: psycopg.Cursor[Any],
    *,
    source_id: int,
    filing_id: int | None,
    actor_id: int | None,
    instrument_id: int | None,
    row: dict[str, Any],
    chamber: str | None,
) -> bool:
    ticker_raw = row.get("ticker")
    ticker = _norm_ticker(ticker_raw)
    side = _infer_side(row)
    tx_date = _parse_mmddyyyy(row.get("transaction_date"))
    disc_date = _parse_mmddyyyy(row.get("disclosure_date")) or _parse_mmddyyyy(row.get("date"))
    disclosed_at = None
    if disc_date:
        disclosed_at = datetime(disc_date.year, disc_date.month, disc_date.day, tzinfo=timezone.utc)
    elif tx_date:
        disclosed_at = datetime(tx_date.year, tx_date.month, tx_date.day, tzinfo=timezone.utc)

    amount_raw, lo, hi = _amount_range(row.get("amount") or row.get("amount_raw"))
    shares = row.get("shares")
    price = row.get("price")
    ptr_link = row.get("ptr_link")

    fp = _fingerprint(
        row.get("ptr_link") or row.get("filing_url") or "",
        chamber or "",
        row.get("representative") or row.get("senator") or row.get("politician") or "",
        ticker or (ticker_raw or ""),
        side,
        amount_raw or "",
        row.get("transaction_type") or row.get("type") or "",
        row.get("transaction_date") or "",
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
            str(ticker_raw) if ticker_raw is not None else None,
            row.get("asset_description"),
            row.get("asset_type"),
            side,
            row.get("transaction_type") or row.get("type"),
            row.get("owner"),
            row.get("owner_title"),
            float(shares) if isinstance(shares, (int, float)) else None,
            float(price) if isinstance(price, (int, float)) else None,
            amount_raw,
            lo,
            hi,
            tx_date,
            disc_date,
            disclosed_at,
            str(ptr_link) if ptr_link else None,
            fp,
            json.dumps(row),
        ),
    )
    return cur.rowcount == 1


def run_import(cfg: ImportConfig) -> None:
    house = _read_json_list(cfg.house_path)
    senate = _read_json_list(cfg.senate_path)

    with psycopg.connect(cfg.dsn) as conn:
        conn.execute("set timezone to 'UTC'")
        with conn.cursor() as cur:
            run_meta = {"house_rows": len(house), "senate_rows": len(senate)}
            cur.execute(
                "insert into ops.ingestion_runs (job_key, status, meta) values (%s, %s, %s) returning id",
                ("import_trades", "running", json.dumps(run_meta)),
            )
            run_id = int(cur.fetchone()[0])

            try:
                src_house = _upsert_source(cur, "house_ptr_pdf", "House PTR PDF mirror")
                src_senate = _upsert_source(cur, "senate_efd_ptr", "Senate EFD PTR mirror (playwright)")

                inserted = 0
                for row in house:
                    rep = str(row.get("representative") or "").strip() or None
                    if not rep:
                        continue
                    actor_id = _get_or_create_actor(cur, actor_type="politician", name=rep, chamber="house")
                    ticker = _norm_ticker(row.get("ticker"))
                    instrument_id = _get_or_create_instrument(cur, ticker=ticker) if ticker else None
                    # filing external id: docid from ptr_link filename
                    ptr = str(row.get("ptr_link") or "").strip()
                    ext = (ptr.rstrip("/").split("/")[-1] if ptr else "") or "unknown"
                    ext = ext.replace(".pdf", "")
                    filing_id = _get_or_create_filing(cur, source_id=src_house, external_id=ext, filing_url=ptr or None)
                    if _insert_trade_event(
                        cur,
                        source_id=src_house,
                        filing_id=filing_id,
                        actor_id=actor_id,
                        instrument_id=instrument_id,
                        row=row,
                        chamber="house",
                    ):
                        inserted += 1

                for row in senate:
                    who = str(row.get("senator") or "").strip() or None
                    if not who:
                        continue
                    actor_id = _get_or_create_actor(cur, actor_type="politician", name=who, chamber="senate")
                    ticker = _norm_ticker(row.get("ticker"))
                    instrument_id = _get_or_create_instrument(cur, ticker=ticker) if ticker else None
                    ptr = str(row.get("ptr_link") or "").strip()
                    m = re.search(r"/search/view/ptr/([a-f0-9\\-]+)/", ptr, flags=re.I)
                    ext = m.group(1) if m else (ptr.rstrip("/").split("/")[-1] if ptr else "unknown")
                    filing_id = _get_or_create_filing(cur, source_id=src_senate, external_id=ext, filing_url=ptr or None)
                    if _insert_trade_event(
                        cur,
                        source_id=src_senate,
                        filing_id=filing_id,
                        actor_id=actor_id,
                        instrument_id=instrument_id,
                        row=row,
                        chamber="senate",
                    ):
                        inserted += 1

                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    ("ok", json.dumps({**run_meta, "inserted": inserted}), run_id),
                )
            except Exception as e:
                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    ("error", json.dumps({**run_meta, "error": str(e)[:500]}), run_id),
                )
                raise


def main() -> int:
    dsn = os.getenv("INVESTIMI_DB_DSN", "postgresql://investimi:investimi@localhost:5433/investimi")
    house_path = Path(os.getenv("HOUSE_JSON", "data/house/all_transactions.json"))
    senate_path = Path(os.getenv("SENATE_JSON", "data/senate/all_transactions.json"))
    cfg = ImportConfig(dsn=dsn, house_path=house_path, senate_path=senate_path)
    run_import(cfg)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

