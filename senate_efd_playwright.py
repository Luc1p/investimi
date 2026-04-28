from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from mirror_fetch import _parse_senate_ptr_transactions_page


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _parse_mmddyyyy(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _collect_ptr_links_via_browser(*, start: date, end: date) -> tuple[list[str], list[list[Any]] | None]:
    base = "https://efdsearch.senate.gov"
    url = f"{base}/search/"

    ptr_links: list[str] = []
    dt_rows: list[list[Any]] | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        debug: dict[str, Any] = {
            "disclaimer_clicked": False,
            "datatable_resp": None,
            "page_url": None,
            "page_title": None,
            "page_text_head": None,
        }
        try:
            debug["page_url"] = page.url
            debug["page_title"] = page.title()
            debug["page_text_head"] = page.inner_text("body")[:6000]
        except Exception:
            pass

        # Capture DataTables response directly (more reliable than DOM inspection).
        def on_response(resp):  # type: ignore[no-untyped-def]
            try:
                if "/search/report/data/" in (resp.url or ""):
                    debug["datatable_resp"] = {"url": resp.url, "status": resp.status}
                    if resp.status == 200:
                        debug["datatable_json"] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)

        def _accept_disclaimer() -> bool:
            # Primary: the home gate uses a checkbox that auto-submits.
            try:
                cb = page.locator("#agree_statement")
                if cb.count() > 0:
                    cb.check(timeout=3000)
                    return True
            except Exception:
                pass

            # Try obvious buttons by text
            for label in ("I Agree", "Agree", "Accept", "Continue", "OK"):
                try:
                    page.get_by_role("button", name=label).click(timeout=2000)
                    return True
                except Exception:
                    pass
            # Try submit inputs/buttons
            try:
                page.locator('input[type="submit"]').first.click(timeout=2000)
                return True
            except Exception:
                pass
            try:
                page.locator('button[type="submit"]').first.click(timeout=2000)
                return True
            except Exception:
                pass
            # Try checking any checkbox then submitting
            try:
                cb = page.locator('input[type="checkbox"]').first
                if cb.count() > 0:
                    cb.check(timeout=2000)
                    try:
                        page.locator('button[type="submit"]').first.click(timeout=2000)
                    except Exception:
                        page.locator('input[type="submit"]').first.click(timeout=2000)
                    return True
            except Exception:
                pass
            # As a last resort, submit the first form
            try:
                page.locator("form").first.evaluate("f => f.submit()")
                return True
            except Exception:
                return False

        clicked = _accept_disclaimer()
        debug["disclaimer_clicked"] = bool(clicked)
        try:
            page.wait_for_timeout(2500)
            debug["page_url_after_disclaimer"] = page.url
        except Exception:
            pass

        # Fill submitted date range if fields exist
        for field_name in ("submitted_start_date", "submitted_start_date__gte"):
            try:
                page.locator(f'input[name="{field_name}"]').fill(start.strftime("%m/%d/%Y"), timeout=2000)
            except Exception:
                pass
        for field_name in ("submitted_end_date", "submitted_end_date__lte", "submitted_end_date"):
            try:
                page.locator(f'input[name="{field_name}"]').fill(end.strftime("%m/%d/%Y"), timeout=2000)
            except Exception:
                pass

        # Try to select PTR + Senator if selects exist
        for sel, val in (('select[name="report_types[]"]', "11"), ('select[name="report_types"]', "11")):
            try:
                page.locator(sel).select_option(val, timeout=2000)
            except Exception:
                pass
        for sel, val in (('select[name="filer_types[]"]', "1"), ('select[name="filer_types"]', "1")):
            try:
                page.locator(sel).select_option(val, timeout=2000)
            except Exception:
                pass

        # Submit search (button text varies)
        submitted = False
        for btn in ("Search", "Submit", "Find", "Filter"):
            try:
                page.get_by_role("button", name=btn).click(timeout=2000)
                submitted = True
                break
            except Exception:
                pass
        if not submitted:
            # fallback: try submit button or submit first form
            try:
                page.locator('button[type="submit"]').first.click(timeout=2000)
                submitted = True
            except Exception:
                pass
        if not submitted:
            try:
                page.locator("form").first.evaluate("f => f.submit()")
            except Exception:
                pass

        # Prefer extracting PTR links from the DataTables JSON response, if we got it.
        try:
            page.wait_for_timeout(6000)
        except Exception:
            pass

        dt = debug.get("datatable_json")
        if isinstance(dt, dict) and isinstance(dt.get("data"), list):
            dt_rows = []
            for row in dt.get("data") or []:
                if not isinstance(row, list):
                    continue
                dt_rows.append(row)
                for cell in row:
                    if isinstance(cell, str) and "/search/view/ptr/" in cell:
                        m = re.search(r'href\\s*=\\s*["\\\']([^"\\\']+)["\\\']', cell, flags=re.I)
                        if m:
                            href = m.group(1)
                            if href.startswith("http"):
                                ptr_links.append(href)
                            else:
                                ptr_links.append(base + href)
                        break

        # Fallback: DOM inspection (if JSON wasn't captured)
        if not ptr_links:
            try:
                page.wait_for_selector("table#filedReports", timeout=60000)
                page.wait_for_selector('table#filedReports a[href*="/search/view/ptr/"]', timeout=30000)
                hrefs = page.eval_on_selector_all(
                    'table#filedReports a[href*="/search/view/ptr/"]',
                    "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
                )
                for h in hrefs:
                    if not isinstance(h, str):
                        continue
                    if h.startswith("http"):
                        ptr_links.append(h)
                    else:
                        ptr_links.append(base + h)
            except PlaywrightTimeoutError:
                pass

        # Write lightweight debug artifacts
        try:
            os.makedirs("data/senate/debug", exist_ok=True)
            with open("data/senate/debug/playwright_state.json", "w", encoding="utf-8") as f:
                json.dump(debug, f, ensure_ascii=False)
            with open("data/senate/debug/playwright_page.html", "w", encoding="utf-8") as f:
                f.write(page.content()[:500000])
            page.screenshot(path="data/senate/debug/playwright.png", full_page=True)
        except Exception:
            pass
        browser.close()

    # de-dup preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in ptr_links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return (uniq, dt_rows)


def main() -> int:
    # Prefer explicit date range if provided (useful for census window fallback).
    start_s = (os.getenv("SENATE_EFD_START_DATE") or "").strip()
    end_s = (os.getenv("SENATE_EFD_END_DATE") or "").strip()
    if start_s and end_s:
        start_d = _parse_mmddyyyy(start_s)
        end_d = _parse_mmddyyyy(end_s)
        if not start_d or not end_d:
            raise RuntimeError("Invalid SENATE_EFD_START_DATE/END_DATE; expected YYYY-MM-DD or MM/DD/YYYY")
        start = start_d
        end = end_d
        days = (end - start).days
        if days < 0:
            raise RuntimeError("SENATE_EFD_END_DATE must be >= SENATE_EFD_START_DATE")
    else:
        days = int(os.getenv("SENATE_EFD_DAYS", "7"))
        end = datetime.utcnow().date()
        start = end - timedelta(days=days)
    max_reports = int(os.getenv("SENATE_EFD_MAX_REPORTS", "40"))
    links, dt_rows = _collect_ptr_links_via_browser(start=start, end=end)
    links = links[:max_reports]

    # Persist DataTables rows (if captured) so other scripts can reuse submitted-date metadata.
    if isinstance(dt_rows, list) and dt_rows:
        _write_json(
            "data/senate/filings_rows.json",
            {
                "submitted_start_date": start.isoformat(),
                "submitted_end_date": end.isoformat(),
                "rows": dt_rows,
            },
        )

    # Parse each report page inside the browser session to avoid Akamai 403.
    tx_out: list[dict[str, Any]] = []
    ok_pages = 0
    parsed_pages = 0
    failures: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # Start from the home gate so the agreement checkbox is present.
        page.goto("https://efdsearch.senate.gov/search/home/", wait_until="domcontentloaded", timeout=60000)

        def _ensure_gate_accepted() -> None:
            # Accept disclaimer gate (checkbox auto-submits).
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
            # If still on home, try loading /search/ (sometimes the gate posts then redirects).
            try:
                if "/search/home/" in (page.url or ""):
                    page.goto("https://efdsearch.senate.gov/search/", wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_url(re.compile(r".*/search/(?!home/).*"), timeout=20000)
            except Exception:
                pass

        _ensure_gate_accepted()

        for url in links:
            got = False
            last_err: str | None = None
            for attempt in range(2):
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    status = resp.status if resp else None
                    if "/search/home/" in (page.url or ""):
                        _ensure_gate_accepted()
                        continue
                    if status not in (200, 302, 303):
                        failures.append({"url": url, "status": status, "page_url": page.url})
                        break
                    ok_pages += 1
                    got = True
                    break
                except Exception as e:
                    last_err = str(e)[:180]
                    if "/search/home/" in (page.url or ""):
                        _ensure_gate_accepted()
                        continue
                    break

            if not got:
                failures.append({"url": url, "error": "goto_failed", "detail": last_err, "page_url": page.url})
                continue

            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            html = page.content()
            who, rows = _parse_senate_ptr_transactions_page(html)
            if rows:
                parsed_pages += 1
            else:
                # Save one sample page for debugging when parsing yields 0 rows.
                try:
                    os.makedirs("data/senate/debug", exist_ok=True)
                    with open("data/senate/debug/ptr_page_sample.html", "w", encoding="utf-8") as f:
                        f.write(html[:500000])
                except Exception:
                    pass
            for row in rows:
                row["senator"] = who
                row["ptr_link"] = url
                row["disclosure_date"] = None
                tx_out.append(row)

        browser.close()

    _write_json("data/senate/all_transactions.json", tx_out)
    _write_json(
        "data/senate/ptr_links.json",
        {"count": len(links), "ok_pages": ok_pages, "links": links, "failures": failures[:20]},
    )
    # Write a small status file so the main workflow can surface Senate stats.
    os.makedirs("data/senate", exist_ok=True)
    with open("data/senate/STATUS.txt", "w", encoding="utf-8") as f:
        f.write(f"updated_utc={datetime.utcnow().isoformat()}Z\n")
        f.write(f"senate_pw_start={start.isoformat()}\n")
        f.write(f"senate_pw_end={end.isoformat()}\n")
        f.write(f"senate_pw_days={days}\n")
        f.write(f"senate_pw_max_reports={max_reports}\n")
        f.write(f"senate_pw_ptr_links={len(links)}\n")
        f.write(f"senate_pw_ok_pages={ok_pages}\n")
        f.write(f"senate_pw_parsed_pages={parsed_pages}\n")
        f.write(f"senate_pw_transactions={len(tx_out)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

