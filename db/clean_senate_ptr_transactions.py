from __future__ import annotations

import html as _html
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _read_json(path: str) -> Any:
    return json.loads(open(path, "r", encoding="utf-8").read())


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _strip_html(s: str) -> str:
    s = _html.unescape(s or "")
    s = TAG_RE.sub(" ", s)
    s = WS_RE.sub(" ", s).strip()
    return s


def _parse_date_any(s: Any) -> date | None:
    if s is None:
        return None
    st = str(s).strip()
    if not st:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(st, fmt).date()
        except Exception:
            pass
    return None


@dataclass(frozen=True)
class ExtractedMeta:
    coupon_rate: float | None
    matures_on: date | None


COUPON_RE = re.compile(r"rate/coupon:\s*([0-9]+(?:\.[0-9]+)?)\s*%", flags=re.I)
MATURES_RE = re.compile(r"matures:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", flags=re.I)


def _extract_coupon_and_maturity(raw: str) -> ExtractedMeta:
    """
    Pulls 'Rate/Coupon: 5.0%' and 'Matures: 06/01/2041' out of the HTML-ish tail.
    Works on the raw string before stripping tags.
    """
    if not raw:
        return ExtractedMeta(coupon_rate=None, matures_on=None)
    txt = _strip_html(raw).lower()
    coupon = None
    mat = None
    m1 = COUPON_RE.search(txt)
    if m1:
        try:
            coupon = float(m1.group(1))
        except Exception:
            coupon = None
    m2 = MATURES_RE.search(txt)
    if m2:
        mat = _parse_date_any(m2.group(1))
    return ExtractedMeta(coupon_rate=coupon, matures_on=mat)


def _classify_asset(row: dict[str, Any], *, desc_text: str) -> str:
    """
    Coarse but useful taxonomy.
    """
    ticker = str(row.get("ticker") or "").strip().upper()
    tx_type = str(row.get("type") or row.get("transaction_type") or "").strip().upper()
    desc_u = desc_text.upper()

    if ticker and ticker not in ("--", "N/A"):
        # Equity-like (includes ETFs); we can refine later
        if "ETF" in desc_u or "EXCHANGE TRADED FUND" in desc_u:
            return "etf"
        return "equity"

    # Non-ticker assets
    if "TREASURY" in desc_u and ("BILL" in desc_u or "NOTE" in desc_u or "BOND" in desc_u):
        return "treasury"
    if "BOND" in desc_u or "REVENUE BOND" in desc_u or "OBLIGATION BOND" in desc_u:
        return "bond"

    # Derivatives / futures / options
    if any(k in tx_type for k in ("CALL", "PUT", "OPTION", "FUTURE", "FUTURES")):
        return "derivative"
    if any(k in desc_u for k in ("CALL ", " PUT ", "CME", "CBT", "FUTURE", "FUTURES", "SOYBEAN", "CATTLE", "CORN", "WHEAT")):
        return "derivative"

    return "other"


def main() -> int:
    in_path = (os.getenv("SENATE_PTR_IN_JSON") or "artifacts/senate_ptr/senate_ptr_transactions.json").strip()
    out_dir = (os.getenv("SENATE_PTR_CLEAN_OUT_DIR") or "artifacts/senate_ptr_clean").strip() or "artifacts/senate_ptr_clean"
    out_path = os.path.join(out_dir, "senate_ptr_transactions_clean.json")
    summary_path = os.path.join(out_dir, "senate_ptr_clean_summary.json")

    data = _read_json(in_path)
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON list at {in_path!r}")

    cleaned: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    html_desc = 0
    coupon_hits = 0
    maturity_hits = 0

    for it in data:
        if not isinstance(it, dict):
            continue
        raw_desc = str(it.get("asset_description") or "")
        if "<" in raw_desc and ">" in raw_desc:
            html_desc += 1
        desc_text = _strip_html(raw_desc)
        meta = _extract_coupon_and_maturity(raw_desc)
        if meta.coupon_rate is not None:
            coupon_hits += 1
        if meta.matures_on is not None:
            maturity_hits += 1

        asset_class = _classify_asset(it, desc_text=desc_text)
        class_counts[asset_class] += 1

        out = dict(it)
        out["asset_description"] = desc_text or None
        out["asset_description_raw"] = raw_desc or None
        out["coupon_rate"] = meta.coupon_rate
        out["matures_on"] = meta.matures_on.isoformat() if meta.matures_on else None
        out["asset_class"] = asset_class
        cleaned.append(out)

    _write_json(out_path, cleaned)
    _write_json(
        summary_path,
        {
            "input_path": in_path,
            "output_path": out_path,
            "rows_in": len(data),
            "rows_out": len(cleaned),
            "asset_description_had_html": html_desc,
            "coupon_rate_extracted": coupon_hits,
            "matures_on_extracted": maturity_hits,
            "asset_class_counts": dict(class_counts),
        },
    )
    print(f"ok rows_out={len(cleaned)} html_desc={html_desc} classes={dict(class_counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

