from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import requests
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


def _collect_ptr_links_via_browser(*, days: int) -> list[str]:
    base = "https://efdsearch.senate.gov"
    url = f"{base}/search/"
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)

    ptr_links: list[str] = []
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
            page.wait_for_timeout(1500)
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
            for row in dt.get("data") or []:
                if not isinstance(row, list):
                    continue
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
    return uniq


def main() -> int:
    days = int(os.getenv("SENATE_EFD_DAYS", "7"))
    max_reports = int(os.getenv("SENATE_EFD_MAX_REPORTS", "40"))
    links = _collect_ptr_links_via_browser(days=days)[:max_reports]

    # Parse each report page via requests (faster than keeping browser open)
    s = requests.Session()
    s.headers.update({"User-Agent": os.getenv("MIRROR_UA", "CongressTradesMirror/0.2 (public-interest)")})

    tx_out: list[dict[str, Any]] = []
    ok_pages = 0
    for url in links:
        try:
            r = s.get(url, timeout=60, headers={"Referer": "https://efdsearch.senate.gov/search/"})
            if r.status_code != 200:
                continue
            ok_pages += 1
            who, rows = _parse_senate_ptr_transactions_page(r.text)
            for row in rows:
                row["senator"] = who
                row["ptr_link"] = url
                row["disclosure_date"] = None
                tx_out.append(row)
        except Exception:
            continue

    _write_json("data/senate/all_transactions.json", tx_out)
    _write_json("data/senate/ptr_links.json", {"count": len(links), "ok_pages": ok_pages, "links": links})
    # Write a small status file so the main workflow can surface Senate stats.
    os.makedirs("data/senate", exist_ok=True)
    with open("data/senate/STATUS.txt", "w", encoding="utf-8") as f:
        f.write(f"updated_utc={datetime.utcnow().isoformat()}Z\n")
        f.write(f"senate_pw_days={days}\n")
        f.write(f"senate_pw_max_reports={max_reports}\n")
        f.write(f"senate_pw_ptr_links={len(links)}\n")
        f.write(f"senate_pw_ok_pages={ok_pages}\n")
        f.write(f"senate_pw_transactions={len(tx_out)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

