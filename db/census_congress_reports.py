from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
import requests

from mirror_fetch import _extract_house_ptr_links, _session, fetch_house_members, fetch_senate_efd_range

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _parse_any_date(v: Any) -> date | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


@dataclass(frozen=True)
class SenateReport:
    submitted: date | None
    filer: str | None
    url: str
    external_id: str
    raw: dict[str, Any]


def _extract_first_href(html_cell: str) -> str | None:
    # very small helper: the Senate rows include an <a href="...">
    import re

    m = re.search(r'href="([^"]+)"', html_cell or "")
    return m.group(1) if m else None


def _normalize_senate_report_rows(rows: list[dict[str, Any]]) -> list[SenateReport]:
    out: list[SenateReport] = []
    base = "https://efdsearch.senate.gov"
    for r in rows:
        raw = r.get("raw") if isinstance(r, dict) else None
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        submitted = _parse_any_date(raw[4])
        filer = str(raw[0]).strip() if raw[0] is not None else None
        href = _extract_first_href(str(raw[3]))
        if not href:
            continue
        url = href
        if url.startswith("/"):
            url = base + url
        # external_id: uuid segment if present, else hash url
        ext = url.rstrip("/").split("/")[-1]
        if len(ext) < 8:
            ext = _sha1(url)[:16]
        out.append(SenateReport(submitted=submitted, filer=filer, url=url, external_id=ext, raw={"raw": raw, **{k: v for k, v in r.items() if k != "raw"}}))
    return out


def _upsert_report(
    cur: psycopg.Cursor[Any],
    *,
    chamber: str,
    report_type: str,
    filer: str | None,
    submitted_date: date | None,
    filing_year: int | None,
    external_id: str,
    report_url: str,
    source_key: str,
    raw: dict[str, Any],
) -> bool:
    cur.execute(
        """
        insert into core.disclosure_reports (
          chamber, report_type, filer, submitted_date, filing_year,
          external_id, report_url, source_key, raw
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (source_key, external_id) do update set
          report_url = excluded.report_url,
          filer = coalesce(excluded.filer, core.disclosure_reports.filer),
          submitted_date = coalesce(excluded.submitted_date, core.disclosure_reports.submitted_date),
          filing_year = coalesce(excluded.filing_year, core.disclosure_reports.filing_year),
          raw = excluded.raw,
          indexed_at = now()
        """,
        (
            chamber,
            report_type,
            filer,
            submitted_date,
            filing_year,
            external_id,
            report_url,
            source_key,
            json.dumps(raw),
        ),
    )
    return cur.rowcount == 1


def main() -> int:
    if load_dotenv:
        try:
            load_dotenv(".env")
        except Exception:
            load_dotenv()

    dsn = os.getenv("INVESTIMI_DB_DSN", "postgresql://investimi:investimi@localhost:5433/investimi")

    house_start_year = int(os.getenv("CENSUS_HOUSE_START_YEAR", "2020"))
    house_end_raw = (os.getenv("CENSUS_HOUSE_END_YEAR") or "").strip()
    house_end_year = int(house_end_raw) if house_end_raw else date.today().year
    house_member_limit = int(os.getenv("CENSUS_HOUSE_MEMBER_LIMIT", "0"))  # 0 = all

    senate_start = os.getenv("CENSUS_SENATE_START_DATE", "2020-01-01").strip()
    senate_start_d = _parse_any_date(senate_start) or date(2020, 1, 1)
    senate_end = (os.getenv("CENSUS_SENATE_END_DATE") or "").strip()
    senate_end_d = _parse_any_date(senate_end) if senate_end else None
    senate_batch_days = int(os.getenv("CENSUS_SENATE_BATCH_DAYS", "31"))

    out_dir = os.getenv("CENSUS_OUT_DIR", "artifacts/census").strip() or "artifacts/census"
    os.makedirs(out_dir, exist_ok=True)

    s = _session()

    # House: member list + PTR links by (last_name, year)
    members = fetch_house_members(s)
    if house_member_limit > 0:
        members = members[:house_member_limit]

    house_index: list[dict[str, Any]] = []
    for year in range(house_start_year, house_end_year + 1):
        for m in members:
            links = _extract_house_ptr_links(
                s,
                m["last_name"],
                year,
                state=m.get("state"),
                district=m.get("district"),
            )
            for url in links:
                house_index.append(
                    {
                        "chamber": "house",
                        "report_type": "ptr",
                        "filer": m.get("name"),
                        "filing_year": year,
                        "report_url": url,
                        "external_id": _sha1(url)[:16],
                        "source_key": "clerk_house",
                    }
                )

    # Senate: fetch in small batches to reduce 503 probability.
    senate_reports: list[SenateReport] = []
    senate_index: list[dict[str, Any]] = []
    senate_errors: list[dict[str, Any]] = []
    cur_start = senate_start_d
    today = senate_end_d or date.today()
    while cur_start <= today:
        cur_end = cur_start.fromordinal(min(today.toordinal(), cur_start.toordinal() + senate_batch_days))
        try:
            rows = fetch_senate_efd_range(s, start=cur_start, end=cur_end)
            batch_reports = _normalize_senate_report_rows(rows)
            senate_reports.extend(batch_reports)
        except Exception as e:
            senate_errors.append({"start": cur_start.isoformat(), "end": cur_end.isoformat(), "error": str(e)[:200]})
        cur_start = cur_end.fromordinal(cur_end.toordinal() + 1)

    # Build index from all batches
    for r in senate_reports:
        senate_index.append(
            {
                "chamber": "senate",
                "report_type": "ptr",
                "filer": r.filer,
                "submitted_date": str(r.submitted) if r.submitted else None,
                "report_url": r.url,
                "external_id": r.external_id,
                "source_key": "senate_efd",
            }
        )

    # Write artifacts
    with open(os.path.join(out_dir, "house_reports.json"), "w", encoding="utf-8") as f:
        json.dump(house_index, f, ensure_ascii=False)
    with open(os.path.join(out_dir, "senate_reports.json"), "w", encoding="utf-8") as f:
        json.dump({"errors": senate_errors, "reports": senate_index}, f, ensure_ascii=False)

    # Upsert into Postgres
    inserted = 0
    with psycopg.connect(dsn) as conn:
        conn.execute("set timezone to 'UTC'")
        with conn.cursor() as cur:
            cur.execute(
                "insert into ops.ingestion_runs (job_key, status, meta) values (%s,%s,%s) returning id",
                (
                    "census_congress_reports",
                    "running",
                    json.dumps(
                        {
                            "house_years": [house_start_year, house_end_year],
                            "house_members": len(members),
                            "senate_since": str(senate_start_d),
                            "senate_until": str(today),
                            "senate_batch_days": senate_batch_days,
                            "senate_errors": senate_errors[:5],
                        }
                    ),
                ),
            )
            run_id = int(cur.fetchone()[0])
            try:
                for it in house_index:
                    if _upsert_report(
                        cur,
                        chamber="house",
                        report_type="ptr",
                        filer=it.get("filer"),
                        submitted_date=None,
                        filing_year=int(it["filing_year"]),
                        external_id=str(it["external_id"]),
                        report_url=str(it["report_url"]),
                        source_key="clerk_house",
                        raw=it,
                    ):
                        inserted += 1
                for r in senate_reports:
                    if _upsert_report(
                        cur,
                        chamber="senate",
                        report_type="ptr",
                        filer=r.filer,
                        submitted_date=r.submitted,
                        filing_year=None,
                        external_id=r.external_id,
                        report_url=r.url,
                        source_key="senate_efd",
                        raw=r.raw,
                    ):
                        inserted += 1

                cur.execute(
                    "update ops.ingestion_runs set status=%s, finished_at=now(), meta=%s where id=%s",
                    (
                        "ok",
                        json.dumps(
                            {
                                "inserted": inserted,
                                "house": len(house_index),
                                "senate": len(senate_reports),
                                "senate_errors": senate_errors[:20],
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

