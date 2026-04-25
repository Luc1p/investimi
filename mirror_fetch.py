from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


def _extract_house_ptr_links(s: requests.Session, last_name: str, year: int) -> list[str]:
    """
    Uses the official House Clerk search result HTML to find PTR PDF links.
    """
    search_url = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
    payload = {"LastName": last_name, "FilingYear": str(year), "Office": "", "State": "", "District": ""}
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


def _parse_house_ptr_pdf_text(text: str, *, representative: str) -> list[dict[str, Any]]:
    """
    Very best-effort PTR PDF parser.
    Extracts lines containing a date + an amount range, and guesses ticker/type.
    """
    # normalize
    t = text.replace("\r", "\n")
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]

    # common amount range patterns
    amt_re = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\s*to\s*\$[\d,]+", re.IGNORECASE)
    date_re = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")

    txs: list[dict[str, Any]] = []
    for ln in lines:
        if not amt_re.search(ln):
            continue
        d = date_re.search(ln)
        if not d:
            continue
        amount = amt_re.search(ln).group(0)
        # guess ticker: first uppercase token 1-6 chars not common words
        tokens = re.findall(r"\b[A-Z][A-Z0-9.\-]{0,7}\b", ln)
        ticker = None
        for tok in tokens:
            if tok in {"USD", "LLC", "INC", "ETF"}:
                continue
            if 1 <= len(tok) <= 6:
                ticker = tok
                break
        typ = None
        lnl = ln.lower()
        if "purchase" in lnl or "buy" in lnl:
            typ = "Purchase"
        if "sale" in lnl or "sell" in lnl:
            typ = "Sale"
        txs.append(
            {
                "transaction_date": d.group(0),
                "owner": None,
                "ticker": ticker or "--",
                "asset_description": None,
                "asset_type": None,
                "type": typ,
                "amount": amount,
                "comment": None,
                "representative": representative,
            }
        )
    return txs


def fetch_house_ptr_transactions(
    s: requests.Session, *, names: list[str], years: list[int] | None = None, max_pdfs_per_name: int = 15
) -> list[dict[str, Any]]:
    """
    Scrape House PTR PDFs for a short list of known names.
    """
    if not _pdftotext_available():
        return []
    years = years or [date.today().year, date.today().year - 1]
    out: list[dict[str, Any]] = []
    for full in names:
        last = full.strip().split()[-1]
        rep = full.strip()
        links: list[str] = []
        for y in years:
            links.extend(_extract_house_ptr_links(s, last, y))
        for url in links[:max_pdfs_per_name]:
            b = _download_bytes(s, url)
            if not b:
                continue
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
            txs = _parse_house_ptr_pdf_text(text, representative=rep)
            for t in txs:
                t["ptr_link"] = url
                t["disclosure_date"] = None
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
    payload = {
        "draw": "1",
        "start": "0",
        "length": "100",
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
    }
    # Retry on transient upstream errors (503 etc.)
    last_err: str | None = None
    for attempt in range(5):
        r2 = s.post(data_url, data=payload, headers=headers, timeout=45)
        if r2.status_code == 200:
            break
        last_err = f"senate_efd_data_http_{r2.status_code}:{r2.text[:120]}"
        if r2.status_code in (429, 500, 502, 503, 504):
            # exponential-ish backoff
            import time

            time.sleep(2 ** attempt)
            continue
        raise requests.HTTPError(last_err)
    else:
        raise requests.HTTPError(last_err or "senate_efd_data_http_unknown")
    data = r2.json()
    rows = data.get("data") or []
    out: list[dict[str, Any]] = []

    for row in rows:
        # row is typically a list of columns, some may contain HTML <a href="...">
        if isinstance(row, dict):
            out.append(row)
            continue
        if not isinstance(row, list):
            out.append({"raw": row})
            continue
        out.append({"raw": row})
    return out


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
        # fallback: scrape a small set of known names from official House search + PTR PDFs
        names = [x.strip() for x in os.getenv("HOUSE_KNOWN_NAMES", "Nancy Pelosi").split(",") if x.strip()]
        try:
            txs = fetch_house_ptr_transactions(s, names=names)
            if txs:
                status += _status_line("house_status", "ok_scrape")
                status += _status_line("house_scrape_transactions", str(len(txs)))
                _write_json("data/house/all_transactions.json", txs)
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
            txs = build_senate_transactions_from_efd(s, days=60, max_reports=80)
            status += _status_line("senate_efd_status", "ok")
            status += _status_line("senate_efd_transactions", str(len(txs)))
            # Only overwrite if we actually got something.
            if txs:
                _write_json("data/senate/all_transactions.json", txs)
        except Exception as e:
            status += _status_line("senate_efd_status", f"error:{type(e).__name__}")
            status += _status_line("senate_efd_error", (str(e) or "")[:180])
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

