from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal
from xml.etree import ElementTree as ET

import requests


TransactionCode = Literal["P", "S"]
Side = Literal["acquire", "dispose"]


@dataclass(frozen=True)
class Form4Transaction:
    ticker: str
    cik: int
    accession_no: str
    filed_at: str  # YYYY-MM-DD
    reporting_owner: str | None
    owner_title: str | None
    code: TransactionCode | None  # P (buy) / S (sell) – se riconoscibile
    side: Side | None  # acquire/dispose (da acquiredDisposedCode A/D) – più robusto di transactionCode
    shares: float | None
    price: float | None
    value_usd: float | None


class SecEdgarClient:
    """
    SEC EDGAR helper (Form 4).
    Richiede User-Agent conforme policy SEC: es. "InvestimiBot/0.1 (email@example.com)".
    """

    def __init__(self, user_agent: str, session: requests.Session | None = None) -> None:
        if not user_agent:
            raise ValueError("SEC user_agent is required (set SEC_USER_AGENT)")
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json,text/plain,*/*",
            }
        )
        self._ticker_to_cik: dict[str, int] | None = None

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = self._session.get(url, params=params, timeout=25)
        r.raise_for_status()
        return r.json()

    def load_ticker_map(self) -> dict[str, int]:
        if self._ticker_to_cik is not None:
            return self._ticker_to_cik
        # Official SEC file
        data = self._get_json("https://www.sec.gov/files/company_tickers.json")
        out: dict[str, int] = {}
        # It's a dict keyed by index
        for _, item in (data or {}).items():
            t = str(item.get("ticker") or "").upper().strip()
            cik = item.get("cik_str")
            if t and isinstance(cik, int):
                out[t] = cik
        self._ticker_to_cik = out
        return out

    def cik_for_ticker(self, ticker: str) -> int:
        m = self.load_ticker_map()
        t = ticker.upper().strip()
        if t not in m:
            raise RuntimeError(f"Ticker {ticker!r} not found in SEC ticker map")
        return int(m[t])

    def recent_filings(self, cik: int) -> list[dict[str, Any]]:
        cik10 = str(int(cik)).zfill(10)
        sub = self._get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json")
        recent = (sub.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accession = recent.get("accessionNumber") or []
        filing_date = recent.get("filingDate") or []
        primary_doc = recent.get("primaryDocument") or []
        out: list[dict[str, Any]] = []
        for i in range(min(len(forms), len(accession), len(filing_date), len(primary_doc))):
            out.append(
                {
                    "form": forms[i],
                    "accessionNumber": accession[i],
                    "filingDate": filing_date[i],
                    "primaryDocument": primary_doc[i],
                }
            )
        return out

    def form4_transactions(
        self,
        ticker: str,
        *,
        within_days: int = 7,
        max_filings: int = 20,
    ) -> list[Form4Transaction]:
        cik = self.cik_for_ticker(ticker)
        filings = [f for f in self.recent_filings(cik) if str(f.get("form")) == "4"][:max_filings]
        cutoff = date.today() - timedelta(days=within_days)

        txs: list[Form4Transaction] = []
        for f in filings:
            filed = str(f.get("filingDate") or "")
            try:
                y, m, d = (int(x) for x in filed.split("-"))
                if date(y, m, d) < cutoff:
                    continue
            except Exception:
                continue

            accession_no = str(f.get("accessionNumber") or "")
            if not accession_no:
                continue
            xml_url = self._guess_form4_xml_url(cik, accession_no, str(f.get("primaryDocument") or ""))
            if not xml_url:
                continue
            try:
                txs.extend(self._parse_form4_xml(ticker, cik, accession_no, filed, xml_url))
            except Exception:
                # se un filing è strano, non bloccare tutto
                continue
        return txs

    def _guess_form4_xml_url(self, cik: int, accession_no: str, primary_document: str) -> str | None:
        cik_nolead = str(int(cik))
        acc_nodash = accession_no.replace("-", "")
        # spesso primaryDocument è già .xml; altrimenti proviamo a trovare ownership.xml dall'index.json
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_nolead}/{acc_nodash}"
        try:
            idx = self._get_json(f"{base}/index.json")
            items = (idx.get("directory") or {}).get("item") or []
            # preferisci sempre il documento XML "pulito" in root (spesso form4.xml)
            for it in items:
                name = str(it.get("name") or "")
                if name.lower() == "form4.xml":
                    return f"{base}/{name}"
            # se primaryDocument punta a una versione XSL/HTML, evitiamola
            if primary_document.lower().endswith(".xml") and "/" not in primary_document:
                return f"{base}/{primary_document}"
            # ownership.xml è il più comune
            for it in items:
                name = str(it.get("name") or "")
                if name.lower().endswith(".xml") and ("ownership" in name.lower() or "form4" in name.lower()):
                    return f"{base}/{name}"
            # fallback: primo xml
            for it in items:
                name = str(it.get("name") or "")
                if name.lower().endswith(".xml"):
                    return f"{base}/{name}"
        except Exception:
            return None
        return None

    def _parse_form4_xml(
        self,
        ticker: str,
        cik: int,
        accession_no: str,
        filed_at: str,
        xml_url: str,
    ) -> list[Form4Transaction]:
        r = self._session.get(xml_url, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.text)

        # reportingOwnerName (best effort)
        owner_name = _find_text(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
        owner_title = _find_text(root, ".//reportingOwner/reportingOwnerRelationship/officerTitle")

        txs: list[Form4Transaction] = []
        # Non-derivative transactions (Table I)
        for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
            code = _find_text(tx, ".//transactionCoding/transactionCode")
            ad = _find_text(tx, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
            shares = _to_float(_find_text(tx, ".//transactionAmounts/transactionShares/value"))
            price = _to_float(_find_text(tx, ".//transactionAmounts/transactionPricePerShare/value"))
            val = (shares * price) if (shares is not None and price is not None) else None
            code_norm: TransactionCode | None = None
            if code in ("P", "S"):
                code_norm = code  # type: ignore[assignment]
            side: Side | None = None
            if ad == "A":
                side = "acquire"
            elif ad == "D":
                side = "dispose"
            txs.append(
                Form4Transaction(
                    ticker=ticker.upper(),
                    cik=int(cik),
                    accession_no=accession_no,
                    filed_at=filed_at,
                    reporting_owner=owner_name,
                    owner_title=owner_title,
                    code=code_norm,
                    side=side,
                    shares=shares,
                    price=price,
                    value_usd=val,
                )
            )
        return txs


def _find_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _to_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None

