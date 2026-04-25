from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

import requests
from bs4 import BeautifulSoup


UA = os.getenv("MIRROR_UA", "CongressTradesMirror/0.2 (public-interest)")

Chamber = Literal["house", "senate"]


@dataclass(frozen=True)
class Trade:
    chamber: Chamber
    transaction_date: str | None
    disclosure_date: str | None
    owner: str | None
    ticker: str | None
    asset_description: str | None
    asset_type: str | None
    type: str | None
    amount: str | None
    comment: str | None
    politician: str | None
    ptr_link: str | None
    source: str


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def fetch_house_from_s3(s: requests.Session) -> list[dict[str, Any]] | None:
    url = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
    r = s.get(url, timeout=45)
    if r.status_code != 200:
        return None
    return r.json()


def _pdftotext_available() -> bool:
    try:
        r = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True)
        return r.returncode == 0 or "pdftotext" in (r.stderr or r.stdout or "")
    except Exception:
        return False


def _download_bytes(s: requests.Session, url: str) -> bytes | None:
    r = s.get(url, timeout=60)
    if r.status_code != 200:
        return None
    return r.content


def _extract_house_ptr_links(
    s: requests.Session,
    last_name: str,
    year: int,
    *,
    state: str | None = None,
    district: str | None = None,
) -> list[str]:
    """
    Uses the official House Clerk search result HTML to find PTR PDF links.
    """
    search_url = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
    payload = {
        "LastName": last_name,
        "FilingYear": str(year),
        "Office": "",
        "State": (state or ""),
        "District": (district or ""),
    }
    r = s.post(
        search_url,
        data=payload,
        timeout=45,
        headers={"Referer": "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewSearch"},
    )
    if r.status_code != 200:
        return []
    hrefs = re.findall(r'href="([^"]+)"', r.text)
    out: list[str] = []
    for h in hrefs:
        if "ptr-pdfs" not in h:
            continue
        if h.startswith("http"):
            out.append(h)
        else:
            out.append("https://disclosures-clerk.house.gov/" + h.lstrip("/"))
    # de-dup
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def fetch_house_members(s: requests.Session) -> list[dict[str, str]]:
    """
    Fetch the official House member list from the Clerk and return:
      { "name": "...", "state": "TX", "district": "2", "last_name": "Crenshaw" }

    District is normalized to:
      - digits as string (e.g. "12")
      - "At Large" kept as "At Large"
    """
    url = "https://clerk.house.gov/Members/ViewMemberList"
    r = s.get(url, timeout=45, headers={"Accept": "text/html,*/*"})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        # Fallback: try parse markdown-ish table from text (rare)
        return []

    out: list[dict[str, str]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        rep_cell, _party, state_cell, dist_cell = tds[0], tds[1], tds[2], tds[3]
        a = rep_cell.find("a")
        name = (a.get_text(" ", strip=True) if a else rep_cell.get_text(" ", strip=True)).strip()
        name = re.sub(r"\s+", " ", name).strip()
        # Some rows include a trailing uppercase alias like "ADAMS,ALMA" — strip it.
        name = re.sub(r"\s+[A-Z.\-']+,[A-Z.\-']+$", "", name).strip()
        state_txt = state_cell.get_text(" ", strip=True).strip()
        # extract state abbreviation from "Texas (TX)"
        m = re.search(r"\(([A-Z]{2})\)", state_txt)
        state = m.group(1) if m else state_txt[:2].upper()
        dist_txt = dist_cell.get_text(" ", strip=True).strip()
        # Keep the district *as shown* for search filtering (e.g. "12th", "At Large")
        district = dist_txt.strip()
        # last name: Clerk list is "Last, First ..." so split on comma first
        last = name.split(",", 1)[0].strip() if "," in name else name.split()[-1].strip()
        if not name or not last or not state:
            continue
        out.append({"name": name, "last_name": last, "state": state, "district": district})
    return out


def _parse_house_ptr_pdf_text(text: str, *, representative: str) -> list[dict[str, Any]]:
    """
    Best-effort PTR PDF parser for the House Clerk format.
    Extracts per-transaction rows by tracking asset lines and a subsequent
    line containing transaction type + dates + amount range.
    """
    t = text.replace("\r", "\n")
    raw_lines = [ln.rstrip() for ln in t.split("\n")]
    # collapse excessive whitespace but keep structure
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_lines if re.sub(r"\s+", " ", ln).strip()]

    # examples:
    # - "$250,001 - $500,000"
    # - split across lines: "$250,001 -" then next line "$500,000"
    amt_full_re = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+", re.IGNORECASE)
    amt_start_re = re.compile(r"(\$[\d,]+)\s*-\s*$", re.IGNORECASE)
    amt_end_re = re.compile(r"^\s*(\$[\d,]+)\s*$", re.IGNORECASE)
    date_re = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
    # ticker appears in parentheses: "(AAPL)"
    ticker_re = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,7})\)")
    # transaction type column commonly: "P" or "S (partial)"
    # Keep this strict: we only want standalone tokens, not random letters in prose.
    tx_type_re = re.compile(r"\b(P|S(?:\s*\([^)]*\))?)\b", re.IGNORECASE)

    # disclosure/signature date: "Digitally Signed: ... , 01/17/2025"
    disclosure_date: str | None = None
    for ln in reversed(lines[-60:]):
        if "digitally signed" in ln.lower():
            m = date_re.search(ln)
            if m:
                disclosure_date = m.group(0)
            break

    txs: list[dict[str, Any]] = []

    cur_asset_lines: list[str] = []
    cur_ticker: str | None = None
    pending_amount_start: str | None = None
    pending_type_line: str | None = None

    def flush_if_complete(type_line: str) -> None:
        nonlocal cur_asset_lines, cur_ticker
        # extract tx type + date + amount from the "type line"
        m_type = tx_type_re.search(type_line)
        dates = date_re.findall(type_line)
        m_date = dates[0] if dates else None
        m_amt = amt_full_re.search(type_line)
        # House PTR rows normally include both transaction date and notification date.
        # Requiring 2 dates avoids accidentally picking up option expiration dates in description text.
        if not (m_type and m_date and m_amt and len(dates) >= 2):
            return
        tx_type = m_type.group(1).strip()
        tx_date = m_date
        amount = m_amt.group(0)
        asset_desc = " ".join(cur_asset_lines).strip() if cur_asset_lines else None
        # derive buy/sell words for compatibility with existing side inference
        side_word = "Purchase" if tx_type.upper().startswith("P") else "Sale" if tx_type.upper().startswith("S") else None
        txs.append(
            {
                "transaction_date": tx_date,
                "disclosure_date": disclosure_date,
                "owner": None,
                "ticker": cur_ticker or "--",
                "asset_description": asset_desc,
                "asset_type": None,
                "type": side_word or tx_type,
                "transaction_type": tx_type,
                "amount": amount,
                "comment": None,
                "representative": representative,
            }
        )
        cur_asset_lines = []
        cur_ticker = None

    for ln in lines:
        # capture asset/ticker lines (these often start with "SP" owner column then description)
        mt = ticker_re.search(ln)
        if mt:
            cur_ticker = mt.group(1)
        # Heuristic: asset lines contain "Stock" or have "(TICKER)".
        if (" stock" in ln.lower()) or ("common" in ln.lower()) or ("(" in ln and ")" in ln):
            # Avoid grabbing the header row.
            if "transaction date" not in ln.lower() and "amount" not in ln.lower():
                # Keep the asset line only until we see a transaction type line
                if not (amt_full_re.search(ln) and date_re.search(ln) and tx_type_re.search(ln)):
                    cur_asset_lines.append(ln)
                    continue

        # Amount can be split across lines. If we saw a type+date line with "$X -", complete on next "$Y".
        if pending_amount_start and pending_type_line:
            mend = amt_end_re.match(ln)
            if mend:
                full = f"{pending_amount_start} - {mend.group(1)}"
                flush_if_complete(pending_type_line + " " + full)
                pending_amount_start = None
                pending_type_line = None
                continue

        # When we see a line with tx type + date + full amount, close a record.
        if amt_full_re.search(ln) and len(date_re.findall(ln)) >= 2 and tx_type_re.search(ln):
            flush_if_complete(ln)
            continue

        # When we see a line with tx type + date + amount START (split case), remember and wait for next line.
        if len(date_re.findall(ln)) >= 2 and tx_type_re.search(ln):
            mstart = amt_start_re.search(ln)
            if mstart:
                pending_amount_start = mstart.group(1)
                pending_type_line = ln
                continue

    return txs


def fetch_house_ptr_transactions(
    s: requests.Session,
    *,
    names: list[str],
    years: list[int] | None = None,
    max_pdfs_per_name: int = 15,
    cutoff_date: date | None = None,
    already_seen_ptr_links: set[str] | None = None,
    pdf_budget: int | None = None,
) -> list[dict[str, Any]]:
    """
    Scrape House PTR PDFs for a short list of known names.
    """
    if not _pdftotext_available():
        return []
    years = years or [date.today().year, date.today().year - 1]
    out: list[dict[str, Any]] = []
    already_seen_ptr_links = already_seen_ptr_links or set()
    pdf_budget = int(pdf_budget) if pdf_budget is not None else None
    cutoff_date = cutoff_date or date(2026, 3, 1)
    for full in names:
        last = full.strip().split()[-1]
        rep = full.strip()
        links: list[str] = []
        for y in years:
            links.extend(_extract_house_ptr_links(s, last, y))
        for url in links[:max_pdfs_per_name]:
            if url in already_seen_ptr_links:
                continue
            if pdf_budget is not None and pdf_budget <= 0:
                return out
            b = _download_bytes(s, url)
            if not b:
                continue
            already_seen_ptr_links.add(url)
            if pdf_budget is not None:
                pdf_budget -= 1
            # write temp file
            os.makedirs(".tmp", exist_ok=True)
            pdf_path = os.path.join(".tmp", "ptr.pdf")
            txt_path = os.path.join(".tmp", "ptr.txt")
            with open(pdf_path, "wb") as f:
                f.write(b)
            try:
                subprocess.run(["pdftotext", "-layout", pdf_path, txt_path], check=False)
                text = open(txt_path, "r", encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            # save debug text excerpt (artifact-friendly)
            try:
                os.makedirs("data/house/debug", exist_ok=True)
                docid = url.rstrip("/").split("/")[-1].replace(".pdf", "")
                dbg_path = os.path.join("data/house/debug", f"{rep.replace(' ', '_')}_{docid}.txt")
                with open(dbg_path, "w", encoding="utf-8") as df:
                    df.write(text[:20000])
            except Exception:
                pass
            txs = _parse_house_ptr_pdf_text(text, representative=rep)
            for t in txs:
                t["ptr_link"] = url
                # keep parser's disclosure_date if present
                # Filter by transaction_date cutoff (best-effort)
                try:
                    dt = datetime.strptime(str(t.get("transaction_date") or ""), "%m/%d/%Y").date()
                except Exception:
                    dt = None
                if dt and dt < cutoff_date:
                    continue
                out.append(t)
    return out


def _parse_house_ptr_pdf_url(
    s: requests.Session,
    *,
    url: str,
    representative: str,
    cutoff_date: date,
) -> list[dict[str, Any]]:
    if not _pdftotext_available():
        return []
    b = _download_bytes(s, url)
    if not b:
        return []
    os.makedirs(".tmp", exist_ok=True)
    docid = url.rstrip("/").split("/")[-1].replace(".pdf", "")
    pdf_path = os.path.join(".tmp", f"ptr_{docid}.pdf")
    txt_path = os.path.join(".tmp", f"ptr_{docid}.txt")
    with open(pdf_path, "wb") as f:
        f.write(b)
    try:
        subprocess.run(["pdftotext", "-layout", pdf_path, txt_path], check=False)
        text = open(txt_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    # artifact-friendly debug excerpt
    try:
        os.makedirs("data/house/debug", exist_ok=True)
        dbg_path = os.path.join("data/house/debug", f"{representative.replace(' ', '_')}_{docid}.txt")
        with open(dbg_path, "w", encoding="utf-8") as df:
            df.write(text[:20000])
    except Exception:
        pass
    txs = _parse_house_ptr_pdf_text(text, representative=representative)
    out: list[dict[str, Any]] = []
    for t in txs:
        t["ptr_link"] = url
        try:
            dt = datetime.strptime(str(t.get("transaction_date") or ""), "%m/%d/%Y").date()
        except Exception:
            dt = None
        if dt and dt < cutoff_date:
            continue
        out.append(t)
    return out


def fetch_senate_from_s3(s: requests.Session) -> list[dict[str, Any]] | None:
    url = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
    r = s.get(url, timeout=45)
    if r.status_code != 200:
        return None
    return r.json()


def fetch_senate_efd(s: requests.Session, *, days: int = 60) -> list[dict[str, Any]]:
    """
    Scrape the Senate EFD search endpoint (data tables JSON).
    Returns filings metadata; PTR detail parsing is not included (linking only).
    """
    base = "https://efdsearch.senate.gov"
    home = f"{base}/search/"
    r = s.get(home, timeout=45, headers={"Accept": "text/html,*/*"})
    if r.status_code != 200:
        raise requests.HTTPError(f"senate_efd_home_http_{r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    csrf = soup.find("input", {"name": "csrfmiddlewaretoken"})
    token = csrf.get("value") if csrf else None
    # Sometimes token is only in cookie.
    if not token:
        token = s.cookies.get("csrftoken")
    if not token:
        raise RuntimeError("Missing csrf token on senate efd home")

    # DataTables endpoint used by the site
    data_url = f"{base}/search/report/data/"
    # last N days by submission date
    end = date.today()
    start = end.fromordinal(end.toordinal() - days)
    base_payload = {
        "draw": "1",
        "length": os.getenv("SENATE_EFD_PAGE_SIZE", "100"),
        "report_types[]": ["11"],  # PTR
        "filer_types[]": ["1"],  # senator
        "submitted_start_date": start.strftime("%m/%d/%Y"),
        "submitted_end_date": end.strftime("%m/%d/%Y"),
    }
    headers = {
        "Referer": home,
        "X-CSRFToken": token,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # DataTables pagination: keep fetching until empty or SENATE_EFD_MAX_PAGES reached.
    out: list[dict[str, Any]] = []
    max_pages = int(os.getenv("SENATE_EFD_MAX_PAGES", "6"))
    start_idx = 0
    page = 0
    page_size = int(str(base_payload["length"]))

    while page < max_pages:
        payload = dict(base_payload)
        payload["start"] = str(start_idx)

        # Retry on transient upstream errors (503 etc.) with jitter.
        last_err: str | None = None
        for attempt in range(7):
            r2 = s.post(data_url, data=payload, headers=headers, timeout=45)
            if r2.status_code == 200:
                break
            last_err = f"senate_efd_data_http_{r2.status_code}:{r2.text[:120]}"
            if r2.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(30, (2 ** attempt) + random.random()))
                continue
            raise requests.HTTPError(last_err)
        else:
            raise requests.HTTPError(last_err or "senate_efd_data_http_unknown")

        data = r2.json()
        rows = data.get("data") or []
        if not rows:
            break
        for row in rows:
            # row is typically a list of columns, some may contain HTML <a href="...">
            if isinstance(row, dict):
                out.append(row)
                continue
            if not isinstance(row, list):
                out.append({"raw": row})
                continue
            out.append({"raw": row})

        page += 1
        start_idx += page_size

    return out


def fetch_senate_from_mirrors(s: requests.Session) -> tuple[str | None, list[dict[str, Any]] | None]:
    """
    Fallback: pull Senate transactions from one of several public JSON mirrors.
    Provide SENATE_MIRROR_URLS as comma-separated URLs (raw GitHub recommended).
    """
    # Only accept mirrors that are "recent enough", otherwise they are noise.
    # Default: require latest date >= 2026-01-01.
    min_date_s = os.getenv("SENATE_MIN_DATE", "2026-01-01").strip()
    try:
        min_date = datetime.strptime(min_date_s, "%Y-%m-%d").date()
    except Exception:
        min_date = date(2026, 1, 1)

    default_urls = [
        # NOTE: Some public mirrors are stale (historical-only). We validate freshness before using.
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
    ]
    urls = [u.strip() for u in os.getenv("SENATE_MIRROR_URLS", "").split(",") if u.strip()] or default_urls
    if not urls:
        return (None, None)

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

    def _latest_date(items: list[dict[str, Any]]) -> date | None:
        latest: date | None = None
        for it in items:
            if not isinstance(it, dict):
                continue
            dt = (
                _parse_any_date(it.get("transaction_date"))
                or _parse_any_date(it.get("date"))
                or _parse_any_date(it.get("disclosure_date"))
            )
            if dt and (latest is None or dt > latest):
                latest = dt
        return latest

    for u in urls:
        try:
            r = s.get(u, timeout=60)
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]
                latest = _latest_date(items)
                if latest is None or latest < min_date:
                    # stale mirror: skip it
                    continue
                return (u, items)
        except Exception:
            continue
    return (None, None)


def _extract_first_href(html: str) -> str | None:
    m = re.search(r'href\\s*=\\s*["\\\']([^"\\\']+)["\\\']', html, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1)


def _parse_senate_ptr_transactions_page(html: str) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Best-effort parsing of a Senate PTR page to extract:
    - politician name
    - transactions table rows
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try to find a name on page
    title = soup.find(["h1", "h2", "h3"])
    who = _s(title.get_text(" ", strip=True) if title else None)

    # Find transactions table by headers
    table = None
    for t in soup.find_all("table"):
        header = " ".join(th.get_text(" ", strip=True).lower() for th in t.find_all("th"))
        if "transaction date" in header and ("ticker" in header or "asset" in header or "amount" in header):
            table = t
            break
    if not table:
        return (who, [])

    headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    # map columns
    def col_idx(keys: list[str]) -> int | None:
        for i, h in enumerate(headers):
            for k in keys:
                if k in h:
                    return i
        return None

    idx_date = col_idx(["transaction date"])
    idx_owner = col_idx(["owner"])
    idx_ticker = col_idx(["ticker"])
    idx_asset = col_idx(["asset", "description"])
    idx_type = col_idx(["type"])
    idx_amount = col_idx(["amount"])
    idx_comment = col_idx(["comment"])

    out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        def get(i: int | None, *, html_ok: bool = False) -> str | None:
            if i is None or i >= len(tds):
                return None
            return (tds[i].decode_contents().strip() if html_ok else tds[i].get_text(" ", strip=True)) or None

        out.append(
            {
                "transaction_date": get(idx_date),
                "owner": get(idx_owner),
                "ticker": get(idx_ticker),
                "asset_description": get(idx_asset, html_ok=True),
                "asset_type": None,
                "type": get(idx_type),
                "amount": get(idx_amount),
                "comment": get(idx_comment),
            }
        )
    return (who, out)


def build_senate_transactions_from_efd(s: requests.Session, *, days: int = 60, max_reports: int = 60) -> list[dict[str, Any]]:
    """
    Uses Senate EFD search to get recent PTR report links, then scrapes each report page
    to extract transaction rows into a stock-watcher-compatible schema.
    """
    filings = fetch_senate_efd(s, days=days)
    links: list[str] = []

    for f in filings:
        raw = f.get("raw")
        # Find any href in any raw cell
        if isinstance(raw, list):
            for cell in raw:
                if isinstance(cell, str) and "href" in cell.lower():
                    href = _extract_first_href(cell)
                    if href:
                        links.append(href)
                        break
        elif isinstance(raw, str) and "href" in raw.lower():
            href = _extract_first_href(raw)
            if href:
                links.append(href)

    # Normalize absolute URLs
    abs_links: list[str] = []
    for href in links:
        if href.startswith("http"):
            abs_links.append(href)
        else:
            abs_links.append("https://efdsearch.senate.gov" + href)

    # de-dup, keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in abs_links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    tx_out: list[dict[str, Any]] = []
    for url in uniq[:max_reports]:
        try:
            r = s.get(url, timeout=45, headers={"Referer": "https://efdsearch.senate.gov/search/"})
            if r.status_code != 200:
                continue
            who, rows = _parse_senate_ptr_transactions_page(r.text)
            for row in rows:
                row["senator"] = who
                row["ptr_link"] = url
                # EFD pages usually include "date received" elsewhere; we leave disclosure_date None.
                row["disclosure_date"] = None
                tx_out.append(row)
        except Exception:
            continue
    return tx_out


def _status_line(key: str, val: str) -> str:
    return f"{key}={val}\n"


def main() -> int:
    s = _session()
    os.makedirs("data/house", exist_ok=True)
    os.makedirs("data/senate", exist_ok=True)

    status = ""
    status += _status_line("updated_utc", datetime.utcnow().isoformat() + "Z")

    house = fetch_house_from_s3(s)
    if house is None:
        # fallback: enumerate the full House member list and pull *new* PTR PDFs incrementally
        # Defaults aim to capture recent useful data without scraping the full historical backlog.
        start_date_s = os.getenv("HOUSE_START_DATE", "2026-03-01").strip()
        try:
            cutoff_date = datetime.strptime(start_date_s, "%Y-%m-%d").date()
        except Exception:
            cutoff_date = date(2026, 3, 1)
        pdf_budget = int(os.getenv("HOUSE_PDF_BUDGET", "150"))
        years_s = os.getenv("HOUSE_YEARS", str(date.today().year)).strip()
        years: list[int] = []
        for part in [p.strip() for p in years_s.split(",") if p.strip()]:
            try:
                years.append(int(part))
            except Exception:
                continue
        if not years:
            years = [date.today().year]

        # Load last known good House transactions to avoid re-downloading PDFs we already parsed.
        existing_house: list[dict[str, Any]] = []
        seen_ptr: set[str] = set()
        try:
            if os.path.exists("data/house/all_transactions.json"):
                existing_house = json.loads(open("data/house/all_transactions.json", "r", encoding="utf-8").read()) or []
                for it in existing_house:
                    u = str((it or {}).get("ptr_link") or "").strip()
                    if u:
                        seen_ptr.add(u)
        except Exception:
            existing_house = []
            seen_ptr = set()

        members = fetch_house_members(s)
        status += _status_line("house_members", str(len(members)))
        # Use a deterministic order but allow spreading load via offset
        offset = int(os.getenv("HOUSE_MEMBER_OFFSET", "0"))
        if members and offset:
            offset = offset % len(members)
            members = members[offset:] + members[:offset]
        member_limit = int(os.getenv("HOUSE_MEMBER_LIMIT", "60"))
        if member_limit > 0 and len(members) > member_limit:
            members = members[:member_limit]
        status += _status_line("house_member_limit", str(len(members)))

        # Discover PTR links per member with State/District filter to reduce last-name collisions.
        new_links: list[tuple[str, str]] = []  # (member_name, pdf_url)
        for m in members:
            last = m.get("last_name") or ""
            st = m.get("state") or ""
            dist = m.get("district") or ""
            name = m.get("name") or last
            for y in years:
                links = _extract_house_ptr_links(s, last, y, state=st, district=dist)
                # If the District/State format doesn't match the search backend, fall back to last-name only.
                if not links:
                    links = _extract_house_ptr_links(s, last, y)
                for u in links:
                    if u in seen_ptr:
                        continue
                    new_links.append((name, u))
                # stop discovery early once we have enough candidates for this run
                if len(new_links) >= max(50, pdf_budget * 3):
                    break
            if len(new_links) >= max(50, pdf_budget * 3):
                break

        # de-dup new links
        uniq_links: list[tuple[str, str]] = []
        seen_u: set[str] = set()
        for name, u in new_links:
            if u in seen_u:
                continue
            seen_u.add(u)
            uniq_links.append((name, u))
        status += _status_line("house_new_ptr_links", str(len(uniq_links)))

        # Download+parse up to budget, filtering by cutoff_date.
        try:
            txs: list[dict[str, Any]] = []
            downloaded = 0
            for rep_name, u in uniq_links:
                if downloaded >= pdf_budget:
                    break
                if u in seen_ptr:
                    continue
                seen_ptr.add(u)
                downloaded += 1
                txs.extend(_parse_house_ptr_pdf_url(s, url=u, representative=rep_name, cutoff_date=cutoff_date))
            status += _status_line("house_pdf_budget", str(pdf_budget))
            status += _status_line("house_pdfs_downloaded", str(downloaded))
            if txs or existing_house:
                status += _status_line("house_status", "ok_scrape")
                status += _status_line("house_scrape_new_transactions", str(len(txs)))
                # merge + de-dup (by ptr_link + transaction_date + ticker + amount)
                merged = list(existing_house) + txs
                seen_sig: set[str] = set()
                uniq: list[dict[str, Any]] = []
                for it in merged:
                    if not isinstance(it, dict):
                        continue
                    sig = "|".join(
                        [
                            str(it.get("ptr_link") or ""),
                            str(it.get("transaction_date") or ""),
                            str(it.get("ticker") or ""),
                            str(it.get("amount") or ""),
                            str(it.get("transaction_type") or ""),
                        ]
                    )
                    if sig in seen_sig:
                        continue
                    seen_sig.add(sig)
                    uniq.append(it)
                _write_json("data/house/all_transactions.json", uniq)
            else:
                status += _status_line("house_status", "missing")
                if not os.path.exists("data/house/all_transactions.json"):
                    _write_json("data/house/all_transactions.json", [])
        except Exception as e:
            status += _status_line("house_status", "missing")
            status += _status_line("house_scrape_error", (str(e) or "")[:180])
            if not os.path.exists("data/house/all_transactions.json"):
                _write_json("data/house/all_transactions.json", [])
    else:
        status += _status_line("house_status", "ok")
        _write_json("data/house/all_transactions.json", house)

    senate = fetch_senate_from_s3(s)
    if senate is None:
        status += _status_line("senate_status", "missing_s3_try_efd")
        try:
            efd_days = int(os.getenv("SENATE_EFD_DAYS", "60"))
            max_reports = int(os.getenv("SENATE_EFD_MAX_REPORTS", "120"))
            txs = build_senate_transactions_from_efd(s, days=efd_days, max_reports=max_reports)
            status += _status_line("senate_efd_status", "ok")
            status += _status_line("senate_efd_transactions", str(len(txs)))
            # Only overwrite if we actually got something.
            if txs:
                _write_json("data/senate/all_transactions.json", txs)
        except Exception as e:
            status += _status_line("senate_efd_status", f"error:{type(e).__name__}")
            status += _status_line("senate_efd_error", (str(e) or "")[:180])
            # Fallback: try mirrors, then keep last known good file.
            src, mirrored = fetch_senate_from_mirrors(s)
            if mirrored:
                status += _status_line("senate_mirror_status", "ok")
                status += _status_line("senate_mirror_url", (src or "")[:180])
                status += _status_line("senate_mirror_transactions", str(len(mirrored)))
                _write_json("data/senate/all_transactions.json", mirrored)
            else:
                status += _status_line("senate_mirror_status", "missing")
                # Keep last known good file if present; otherwise write empty list.
                if not os.path.exists("data/senate/all_transactions.json"):
                    _write_json("data/senate/all_transactions.json", [])
    else:
        status += _status_line("senate_status", "ok")
        _write_json("data/senate/all_transactions.json", senate)

    with open("data/STATUS.txt", "w", encoding="utf-8") as f:
        f.write(status)

    # sanity json
    for p in [
        "data/house/all_transactions.json",
        "data/senate/all_transactions.json",
    ]:
        try:
            json.loads(open(p, "r", encoding="utf-8").read())
        except Exception as e:
            print(f"Invalid JSON {p}: {e}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

