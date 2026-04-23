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
        # row is typically a list of columns; keep raw for now
        out.append({"raw": row})
    return out


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
            senate_efd = fetch_senate_efd(s, days=60)
            status += _status_line("senate_efd_status", "ok")
            # Store as separate file; bot can be upgraded to use this richer source later.
            _write_json("data/senate/efd_ptr_filings.json", senate_efd)
            # Keep all_transactions as empty for now (schema mismatch)
            _write_json("data/senate/all_transactions.json", [])
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

