from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import requests
from bs4 import BeautifulSoup


Chamber = Literal["house", "senate"]
Side = Literal["buy", "sell", "exchange", "unknown"]


@dataclass(frozen=True)
class CongressTrade:
    chamber: Chamber
    politician: str | None
    ticker: str | None
    asset_description: str | None
    asset_type: str | None
    transaction_type: str | None
    transaction_date: str | None
    disclosure_date: str | None
    amount_raw: str | None
    amount_min_usd: float | None
    amount_max_usd: float | None
    side: Side
    source: str


class CongressTradesFreeClient:
    """
    Free sources:
    - Senate: GitHub mirror (timothycarambat/senate-stock-watcher-data)
    - House: optional URL (some endpoints return 403 in certain environments)
    """

    def __init__(
        self,
        *,
        user_agent: str,
        senate_url: str = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions_for_senators.json",
        house_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not user_agent:
            raise ValueError("user_agent is required")
        self._senate_url = senate_url
        self._house_url = house_url
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"})

    def fetch(self, chamber: Chamber) -> list[CongressTrade]:
        if chamber == "senate":
            return self._fetch_senate()
        if chamber == "house":
            if self._house_url:
                return self._fetch_house(self._house_url)
            return []
        raise ValueError(f"Unsupported chamber: {chamber!r}")

    def fetch_house_by_names(self, names: list[str], *, max_filings_per_last_name: int = 5) -> list[CongressTrade]:
        """
        Scrape House PTR filings for a small set of known names.
        This is intentionally limited to avoid heavy scraping.
        """
        last_names = _unique_last_names(names)
        if not last_names:
            return []
        client = _HouseDisclosureClient(self._session)
        if not client.init_session():
            return []
        out: list[CongressTrade] = []
        for last in last_names:
            filings = client.search_last_name(last)[:max_filings_per_last_name]
            for f in filings:
                html = client.fetch_page(f["url"])
                if not html:
                    continue
                txs = _parse_house_ptr_trades(html, ptr_url=f["url"])
                for tx in txs:
                    tx.setdefault("representative", f.get("name"))
                    out.append(_to_trade("house", tx, source="house_scrape"))
        return out

    def _fetch_house(self, url: str) -> list[CongressTrade]:
        data = self._get_json(url)
        # expected: list[dict]
        out: list[CongressTrade] = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            out.append(_to_trade("house", item, source="house_free"))
        return out

    def _fetch_senate(self) -> list[CongressTrade]:
        data = self._get_json(self._senate_url)
        # GitHub mirror format (common): list of senators, each with "transactions": [...]
        out: list[CongressTrade] = []
        if isinstance(data, list):
            for senator in data:
                if not isinstance(senator, dict):
                    continue
                who = _s(senator.get("office") or senator.get("name"))
                txs = senator.get("transactions")
                if not isinstance(txs, list):
                    continue
                for tx in txs:
                    if not isinstance(tx, dict):
                        continue
                    # merge politician name into transaction item for uniform mapping
                    merged = dict(tx)
                    merged.setdefault("senator", who)
                    out.append(_to_trade("senate", merged, source="senate_free"))
            return out
        # fallback: attempt to interpret as already-flat list/dict
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for _, arr in data.items():
                if isinstance(arr, list):
                    for x in arr:
                        if isinstance(x, dict):
                            items.append(x)
        elif isinstance(data, list):
            items = [x for x in data if isinstance(x, dict)]
        for item in items:
            out.append(_to_trade("senate", item, source="senate_free"))
        return out

    def _get_json(self, url: str) -> Any:
        r = self._session.get(url, timeout=35)
        r.raise_for_status()
        return r.json()


class _HouseDisclosureClient:
    """
    Minimal ASP.NET WebForms client for:
    https://clerk.house.gov/public_disc/financial-search

    We keep it small + best-effort, since forms can change.
    """

    SEARCH_URL = "https://clerk.house.gov/public_disc/financial-search"

    def __init__(self, session: requests.Session) -> None:
        self._s = session
        self._viewstate = ""
        self._viewstate_gen = ""
        self._event_validation = ""

    def init_session(self) -> bool:
        r = self._s.get(self.SEARCH_URL, timeout=35)
        if r.status_code != 200:
            return False
        self._extract_hidden(r.text)
        return True

    def _extract_hidden(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        self._viewstate = (soup.find("input", {"name": "__VIEWSTATE"}) or {}).get("value", "")  # type: ignore[union-attr]
        self._viewstate_gen = (soup.find("input", {"name": "__VIEWSTATEGENERATOR"}) or {}).get("value", "")  # type: ignore[union-attr]
        self._event_validation = (soup.find("input", {"name": "__EVENTVALIDATION"}) or {}).get("value", "")  # type: ignore[union-attr]

    def search_last_name(self, last_name: str) -> list[dict[str, str]]:
        payload = {
            "__VIEWSTATE": self._viewstate,
            "__VIEWSTATEGENERATOR": self._viewstate_gen,
            "__EVENTVALIDATION": self._event_validation,
            "LastName": last_name,
            "FilingYear": "",
            "State": "",
            "District": "",
        }
        r = self._s.post(self.SEARCH_URL, data=payload, timeout=35, headers={"Referer": self.SEARCH_URL})
        if r.status_code != 200:
            return []
        self._extract_hidden(r.text)
        return _parse_house_search_results(r.text)

    def fetch_page(self, url: str) -> str | None:
        r = self._s.get(url, timeout=35)
        if r.status_code != 200:
            return None
        return r.text


def _parse_house_search_results(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        text = a.get_text(strip=True)
        # PTR pages are typically not direct PDFs; skip obvious pdfs
        if href.lower().endswith(".pdf"):
            continue
        if "ptr" not in (href.lower() + " " + text.lower()) and "transaction" not in text.lower():
            continue
        if href.startswith("/"):
            url = "https://clerk.house.gov" + href
        else:
            url = href
        if not url.startswith("http"):
            continue
        out.append({"name": text, "url": url})
    # de-dup
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for x in out:
        u = x["url"]
        if u in seen:
            continue
        seen.add(u)
        uniq.append(x)
    return uniq


def _parse_house_ptr_trades(html: str, *, ptr_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        header_text = " ".join(th.get_text(strip=True).lower() for th in t.find_all("th"))
        if "transaction" in header_text and ("ticker" in header_text or "asset" in header_text or "amount" in header_text):
            table = t
            break
    if not table:
        return []

    header_map = {
        "transaction date": "transaction_date",
        "owner": "owner",
        "ticker": "ticker",
        "asset": "asset_description",
        "description": "asset_description",
        "type": "type",
        "transaction type": "type",
        "amount": "amount",
    }

    col_map: list[str | None] = []
    for th in table.find_all("th"):
        h = th.get_text(strip=True).lower()
        field = None
        for k, v in header_map.items():
            if k in h:
                field = v
                break
        col_map.append(field)

    tbody = table.find("tbody") or table
    out: list[dict[str, Any]] = []
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        trade: dict[str, Any] = {
            "transaction_date": "",
            "owner": "--",
            "ticker": "--",
            "asset_description": "",
            "asset_type": "",
            "type": "",
            "amount": "",
            "ptr_link": ptr_url,
        }
        for i, cell in enumerate(cells):
            if i >= len(col_map) or col_map[i] is None:
                continue
            field = col_map[i]
            if field == "asset_description":
                trade[field] = cell.decode_contents().strip()
            else:
                trade[field] = cell.get_text(strip=True) or "--"
        if trade.get("transaction_date") or trade.get("asset_description"):
            out.append(trade)
    return out


def _unique_last_names(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        parts = [p for p in str(n).strip().split() if p]
        if not parts:
            continue
        last = parts[-1].strip().strip(",").strip()
        last_u = last.upper()
        if not last_u or last_u in seen:
            continue
        seen.add(last_u)
        out.append(last)
    return out


def _to_trade(chamber: Chamber, item: dict[str, Any], *, source: str) -> CongressTrade:
    politician = _s(item.get("senator") or item.get("representative") or item.get("politician") or item.get("name"))
    ticker = _clean_ticker(_s(item.get("ticker") or item.get("symbol")))
    asset_desc = _s(item.get("asset_description") or item.get("assetDescription") or item.get("asset"))
    if (not ticker) or ticker == "--":
        ticker = _extract_ticker_from_description(asset_desc) or ticker
    asset_type = _s(item.get("asset_type") or item.get("assetType"))
    tx_type = _s(item.get("type") or item.get("transaction_type") or item.get("transactionType"))
    tx_date = _s(item.get("transaction_date") or item.get("transactionDate"))
    disc_date = _s(item.get("disclosure_date") or item.get("disclosureDate"))
    amount_raw = _s(item.get("amount") or item.get("amount_range") or item.get("amountRange"))
    amt_min, amt_max = _parse_amount_range(amount_raw)
    side = _infer_side(tx_type)
    return CongressTrade(
        chamber=chamber,
        politician=politician,
        ticker=ticker,
        asset_description=asset_desc,
        asset_type=asset_type,
        transaction_type=tx_type,
        transaction_date=tx_date,
        disclosure_date=disc_date,
        amount_raw=amount_raw,
        amount_min_usd=amt_min,
        amount_max_usd=amt_max,
        side=side,
        source=source,
    )


def _infer_side(tx_type: str | None) -> Side:
    s = (tx_type or "").lower()
    if "purchase" in s or "buy" in s:
        return "buy"
    if "sale" in s or "sell" in s:
        return "sell"
    if "exchange" in s:
        return "exchange"
    return "unknown"


def _parse_amount_range(amount: str | None) -> tuple[float | None, float | None]:
    """
    Parses common disclosure ranges like:
    - "$1,001 - $15,000"
    - "$50,001 - $100,000"
    - ">$1,000,000"
    Returns (min,max) in USD.
    """
    if not amount:
        return (None, None)
    a = amount.replace(",", "").replace("$", "").strip()
    if a.startswith(">"):
        v = _to_float(a[1:].strip())
        return (v, None)
    # split on hyphen
    parts = [p.strip() for p in a.replace("–", "-").split("-") if p.strip()]
    if len(parts) == 2:
        return (_to_float(parts[0]), _to_float(parts[1]))
    v = _to_float(a)
    return (v, v)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _clean_ticker(t: str | None) -> str | None:
    if not t:
        return None
    s = t.strip()
    # Senate dataset sometimes embeds Yahoo Finance HTML links.
    if "<a" in s and "</a>" in s:
        try:
            # take inner text of last </a>
            inner = s.split(">")[-1].split("</a>")[0].strip()
            return inner or None
        except Exception:
            return s
    return s


def _extract_ticker_from_description(desc: str | None) -> str | None:
    if not desc:
        return None
    s = desc.strip()
    # common pattern: "AAPL - Apple Inc."
    if " - " in s:
        head = s.split(" - ", 1)[0].strip()
        head = _strip_html(head).strip()
        if _looks_like_ticker(head):
            return head
    return None


def _strip_html(s: str) -> str:
    # minimal tag stripper for dataset fields
    out: list[str] = []
    in_tag = False
    for ch in s:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return "".join(out)


def _looks_like_ticker(s: str) -> bool:
    if not s:
        return False
    if len(s) > 10:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
    return all(c in allowed for c in s) and any(c.isalpha() for c in s)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None

