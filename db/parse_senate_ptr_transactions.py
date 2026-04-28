from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mirror_fetch import _parse_senate_ptr_transactions_page


PTR_RE = re.compile(r"/search/view/ptr/[a-f0-9\\-]+/?", flags=re.I)


def _read_json(path: str) -> Any:
    return json.loads(open(path, "r", encoding="utf-8").read())


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _is_ptr_url(u: str) -> bool:
    u = (u or "").strip()
    if not u:
        return False
    return "/search/view/ptr/" in u.lower()


@dataclass(frozen=True)
class CensusReport:
    url: str
    external_id: str | None
    filer: str | None
    submitted_date: str | None


def _load_census_reports(path: str) -> list[CensusReport]:
    blob = _read_json(path)
    if isinstance(blob, dict):
        items = blob.get("reports") or []
    elif isinstance(blob, list):
        items = blob
    else:
        items = []

    out: list[CensusReport] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get("report_url") or "").strip()
        if not _is_ptr_url(url):
            continue
        out.append(
            CensusReport(
                url=url,
                external_id=str(it.get("external_id") or "").strip() or None,
                filer=str(it.get("filer") or "").strip() or None,
                submitted_date=str(it.get("submitted_date") or "").strip() or None,
            )
        )

    # de-dup by url preserving order
    seen: set[str] = set()
    uniq: list[CensusReport] = []
    for r in out:
        if r.url in seen:
            continue
        seen.add(r.url)
        uniq.append(r)
    return uniq


def _try_import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "Playwright non disponibile. Installa con:\n"
            "  pip install playwright\n"
            "  python -m playwright install --with-deps chromium\n"
        ) from e


def _ensure_gate_accepted(page: Any) -> None:
    """
    Senate EFD uses a disclaimer gate at /search/home/ with checkbox #agree_statement.
    Keep it best-effort: if already accepted, nothing happens.
    """
    try:
        cb = page.locator("#agree_statement")
        if cb.count() > 0:
            cb.check(timeout=8000)
    except Exception:
        pass
    # Wait until we actually leave the home gate.
    try:
        page.wait_for_url(re.compile(r".*/search/(?!home/).*"), timeout=20000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass
    # If still on home, try /search/ which often triggers redirect post-acceptance.
    try:
        if "/search/home/" in (page.url or ""):
            page.goto("https://efdsearch.senate.gov/search/", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass


def main() -> int:
    census_path = (os.getenv("SENATE_CENSUS_JSON") or "artifacts/census/senate_reports.json").strip()
    out_dir = (os.getenv("SENATE_PTR_OUT_DIR") or "artifacts/senate_ptr").strip() or "artifacts/senate_ptr"
    limit = int(os.getenv("SENATE_PTR_LIMIT", "0"))  # 0 = all
    resume = (os.getenv("SENATE_PTR_RESUME") or "").strip() == "1"

    out_txs_path = os.path.join(out_dir, "senate_ptr_transactions.json")
    out_err_path = os.path.join(out_dir, "senate_ptr_errors.json")
    out_state_path = os.path.join(out_dir, "senate_ptr_state.json")

    reports = _load_census_reports(census_path)
    if limit > 0:
        reports = reports[:limit]

    # Local runs sometimes get Akamai 403 in headless mode.
    # Allow opting into a headed browser to look more like a normal user session.
    headless = (os.getenv("SENATE_PTR_HEADLESS") or "1").strip() != "0"
    user_agent = (os.getenv("MIRROR_UA") or "").strip() or None
    delay_ms = int((os.getenv("SENATE_PTR_DELAY_MS") or "250").strip() or "250")
    relaunch_every = int((os.getenv("SENATE_PTR_RELAUNCH_EVERY") or "120").strip() or "120")

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
        # Backward-compatible: older versions marked failures as done.
        try:
            err_urls = {str(e.get("url") or "").strip() for e in errors if isinstance(e, dict)}
            err_urls = {u for u in err_urls if u}
            if err_urls:
                failed_urls |= err_urls
                done_urls -= err_urls
        except Exception:
            pass

    sync_playwright = _try_import_playwright()

    started = datetime.utcnow().isoformat() + "Z"
    with sync_playwright() as p:
        def _new_page() -> tuple[Any, Any, Any]:
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx_kwargs: dict[str, Any] = {}
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.goto("https://efdsearch.senate.gov/search/home/", wait_until="domcontentloaded", timeout=60000)
            _ensure_gate_accepted(page)
            return (browser, context, page)

        browser, context, page = _new_page()

        processed = 0
        ok_since_relaunch = 0
        consecutive_403 = 0
        for rep in reports:
            if rep.url in done_urls:
                continue

            processed += 1
            ok = False
            last: dict[str, Any] = {
                "url": rep.url,
                "external_id": rep.external_id,
                "census_filer": rep.filer,
                "census_submitted_date": rep.submitted_date,
            }

            for attempt in range(3):
                try:
                    resp = page.goto(rep.url, wait_until="domcontentloaded", timeout=60000)
                    status = (resp.status if resp else None) or None
                    last["http_status"] = status
                    # If redirected back to gate, accept and retry.
                    if "/search/home/" in (page.url or ""):
                        _ensure_gate_accepted(page)
                        continue
                    if status not in (200, 302, 303):
                        last["error"] = f"http_{status}"
                        if status == 403:
                            consecutive_403 += 1
                        break

                    # Let the page finish loading; the table is often rendered after JS runs.
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass

                    html = page.content()
                    who, rows = _parse_senate_ptr_transactions_page(html)
                    # Add context to each row (schema compatible with import_trades.py)
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        row["senator"] = who or rep.filer
                        row["ptr_link"] = rep.url
                        row["disclosure_date"] = rep.submitted_date
                        row["_census_external_id"] = rep.external_id
                        tx_out.append(row)
                    ok = True
                    consecutive_403 = 0
                    break
                except Exception as e:
                    last["error"] = (str(e) or type(e).__name__)[:180]
                    # Some transient failures resolve by re-accepting gate
                    try:
                        if "/search/home/" in (page.url or ""):
                            _ensure_gate_accepted(page)
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(800 + attempt * 400)
                    except Exception:
                        pass
                    continue

            if ok:
                done_urls.add(rep.url)
                failed_urls.discard(rep.url)
                ok_since_relaunch += 1
            else:
                failed_urls.add(rep.url)
                errors.append(last)

            # Gentle pacing reduces upstream blocks.
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

            # If we hit repeated 403s, relaunch the browser/context (new fingerprint/session).
            if consecutive_403 >= 3:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                browser, context, page = _new_page()
                ok_since_relaunch = 0
                consecutive_403 = 0

            # Periodic relaunch to avoid long-lived session degradation.
            if relaunch_every > 0 and ok_since_relaunch >= relaunch_every:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                browser, context, page = _new_page()
                ok_since_relaunch = 0

            # periodic checkpoint
            if processed % 25 == 0:
                _write_json(out_txs_path, tx_out)
                _write_json(out_err_path, errors)
                _write_json(
                    out_state_path,
                    {
                        "started_utc": started,
                        "updated_utc": datetime.utcnow().isoformat() + "Z",
                        "census_path": census_path,
                        "total_ptr_reports": len(reports),
                        "done": len(done_urls),
                        "errors": len(errors),
                        "failed": len(failed_urls),
                        "done_urls": sorted(done_urls),
                        "failed_urls": sorted(failed_urls),
                    },
                )

        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    _write_json(out_txs_path, tx_out)
    _write_json(out_err_path, errors)
    _write_json(
        out_state_path,
        {
            "started_utc": started,
            "finished_utc": datetime.utcnow().isoformat() + "Z",
            "census_path": census_path,
            "total_ptr_reports": len(reports),
            "done": len(done_urls),
            "errors": len(errors),
            "done_urls": sorted(done_urls),
            "failed_urls": sorted(failed_urls),
        },
    )
    # Friendly hint when everything is blocked.
    if reports and len(errors) == len(reports):
        if all(str(e.get("http_status")) == "403" for e in errors if isinstance(e, dict)):
            print("hint: got 403 for all PTR pages. Try SENATE_PTR_HEADLESS=0 to run headed browser.")
    print(f"ok ptr_reports={len(reports)} done={len(done_urls)} tx_rows={len(tx_out)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

