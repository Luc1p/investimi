from __future__ import annotations

import json
import os
import re
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
    r = s.get(home, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    csrf = soup.find("input", {"name": "csrfmiddlewaretoken"})
    token = csrf.get("value") if csrf else None
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
    }
    r2 = s.post(data_url, data=payload, headers=headers, timeout=45)
    r2.raise_for_status()
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
        status += _status_line("house_status", "missing")
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
            _write_json("data/senate/all_transactions.json", txs)
        except Exception as e:
            status += _status_line("senate_efd_status", f"error:{type(e).__name__}")
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

