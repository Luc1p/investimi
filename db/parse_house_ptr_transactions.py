from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from mirror_fetch import _parse_house_ptr_pdf_url, _pdftotext_available, _session


def _read_json(path: str) -> Any:
    return json.loads(open(path, "r", encoding="utf-8").read())


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _parse_yyyy_mm_dd(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


@dataclass(frozen=True)
class HouseFiling:
    report_url: str
    filer: str | None
    filing_year: int | None
    external_id: str | None


def _load_house_filings(path: str) -> list[HouseFiling]:
    blob = _read_json(path)
    if not isinstance(blob, list):
        raise RuntimeError(f"Expected a JSON list at {path!r}")

    out: list[HouseFiling] = []
    for it in blob:
        if not isinstance(it, dict):
            continue
        url = str(it.get("report_url") or "").strip()
        if not url:
            continue
        filer = str(it.get("filer") or "").strip() or None
        fy = it.get("filing_year")
        try:
            filing_year = int(fy) if fy is not None and str(fy).strip() else None
        except Exception:
            filing_year = None
        external_id = str(it.get("external_id") or "").strip() or None
        out.append(HouseFiling(report_url=url, filer=filer, filing_year=filing_year, external_id=external_id))

    # de-dup by URL preserving order
    seen: set[str] = set()
    uniq: list[HouseFiling] = []
    for r in out:
        if r.report_url in seen:
            continue
        seen.add(r.report_url)
        uniq.append(r)
    return uniq


def main() -> int:
    census_path = (os.getenv("HOUSE_CENSUS_JSON") or "artifacts/census/house_reports.json").strip()
    out_dir = (os.getenv("HOUSE_PTR_OUT_DIR") or "artifacts/house_ptr").strip() or "artifacts/house_ptr"
    resume = (os.getenv("HOUSE_PTR_RESUME") or "").strip() == "1"
    only_failed = (os.getenv("HOUSE_PTR_ONLY_FAILED") or "").strip() == "1"
    delay_ms = int((os.getenv("HOUSE_PTR_DELAY_MS") or "200").strip() or "200")
    pdf_budget = int((os.getenv("HOUSE_PTR_PDF_BUDGET") or "0").strip() or "0")  # 0 = unlimited
    cutoff_s = (os.getenv("HOUSE_PTR_CUTOFF_DATE") or "2020-01-01").strip()
    cutoff = _parse_yyyy_mm_dd(cutoff_s) or date(2020, 1, 1)

    out_txs_path = os.path.join(out_dir, "house_ptr_transactions.json")
    out_err_path = os.path.join(out_dir, "house_ptr_errors.json")
    out_state_path = os.path.join(out_dir, "house_ptr_state.json")

    filings = _load_house_filings(census_path)

    if not _pdftotext_available():
        raise RuntimeError(
            "pdftotext non disponibile. Installa poppler (mac: brew install poppler) "
            "oppure usa GitHub Actions (linux) dove viene installato automaticamente."
        )

    done_urls: set[str] = set()
    failed_urls: set[str] = set()
    tx_out: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if resume:
        try:
            st = _read_json(out_state_path)
            if isinstance(st, dict) and isinstance(st.get("done_urls"), list):
                done_urls = {str(u) for u in st.get("done_urls") if str(u).strip()}
            if isinstance(st, dict) and isinstance(st.get("failed_urls"), list):
                failed_urls = {str(u) for u in st.get("failed_urls") if str(u).strip()}
        except Exception:
            pass
        try:
            blob = _read_json(out_txs_path)
            if isinstance(blob, list):
                tx_out = [x for x in blob if isinstance(x, dict)]
        except Exception:
            tx_out = []
        try:
            blob = _read_json(out_err_path)
            if isinstance(blob, list):
                errors = [x for x in blob if isinstance(x, dict)]
        except Exception:
            errors = []

        # Backward-compatible: if older versions ever marked failures as done.
        try:
            err_urls = {str(e.get("url") or "").strip() for e in errors if isinstance(e, dict)}
            err_urls = {u for u in err_urls if u}
            if err_urls:
                failed_urls |= err_urls
                done_urls -= err_urls
        except Exception:
            pass

    if only_failed and not resume:
        raise RuntimeError("HOUSE_PTR_ONLY_FAILED=1 requires HOUSE_PTR_RESUME=1 (needs failed_urls from state).")

    started = datetime.utcnow().isoformat() + "Z"
    s = _session()
    processed = 0
    budget_left = pdf_budget if pdf_budget > 0 else None

    for f in filings:
        if only_failed and f.report_url not in failed_urls:
            continue
        if f.report_url in done_urls:
            continue
        if budget_left is not None and budget_left <= 0:
            break

        processed += 1
        ok = False
        last: dict[str, Any] = {
            "url": f.report_url,
            "external_id": f.external_id,
            "census_filer": f.filer,
            "census_filing_year": f.filing_year,
        }

        try:
            rep = f.filer or "Unknown"
            rows = _parse_house_ptr_pdf_url(s, url=f.report_url, representative=rep, cutoff_date=cutoff)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # keep a stable join key back to the census index
                row["_census_external_id"] = f.external_id
                row["_census_filing_year"] = f.filing_year
                tx_out.append(row)
            ok = True
        except Exception as e:
            last["error"] = (str(e) or type(e).__name__)[:200]

        if ok:
            done_urls.add(f.report_url)
            failed_urls.discard(f.report_url)
            if budget_left is not None:
                budget_left -= 1
        else:
            failed_urls.add(f.report_url)
            errors.append(last)

        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        if processed % 25 == 0:
            _write_json(out_txs_path, tx_out)
            _write_json(out_err_path, errors)
            _write_json(
                out_state_path,
                {
                    "started_utc": started,
                    "updated_utc": datetime.utcnow().isoformat() + "Z",
                    "census_path": census_path,
                    "total_filings": len(filings),
                    "done": len(done_urls),
                    "failed": len(failed_urls),
                    "errors": len(errors),
                    "done_urls": sorted(done_urls),
                    "failed_urls": sorted(failed_urls),
                    "pdf_budget": pdf_budget,
                    "cutoff_date": cutoff.isoformat(),
                },
            )

    _write_json(out_txs_path, tx_out)
    _write_json(out_err_path, errors)
    _write_json(
        out_state_path,
        {
            "started_utc": started,
            "finished_utc": datetime.utcnow().isoformat() + "Z",
            "census_path": census_path,
            "total_filings": len(filings),
            "done": len(done_urls),
            "failed": len(failed_urls),
            "errors": len(errors),
            "done_urls": sorted(done_urls),
            "failed_urls": sorted(failed_urls),
            "pdf_budget": pdf_budget,
            "cutoff_date": cutoff.isoformat(),
        },
    )

    print(
        f"ok filings={len(filings)} done={len(done_urls)} failed={len(failed_urls)} "
        f"tx_rows={len(tx_out)} errors={len(errors)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

