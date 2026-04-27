from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests
import streamlit as st


@dataclass(frozen=True)
class Source:
    name: str
    url: str


def _load_json(url: str) -> list[dict[str, Any]]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _parse_date_any(s: Any) -> dt.date | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _to_df(items: list[dict[str, Any]], *, chamber: str) -> pd.DataFrame:
    df = pd.DataFrame(items)
    if df.empty:
        return df

    # Normalize common fields across House/Senate formats
    if "representative" in df.columns and "politician" not in df.columns:
        df["politician"] = df["representative"]
    if "senator" in df.columns and "politician" not in df.columns:
        df["politician"] = df["senator"]
    if "transaction_date" not in df.columns and "date" in df.columns:
        df["transaction_date"] = df["date"]

    df["chamber"] = chamber
    df["transaction_date_parsed"] = df.get("transaction_date", "").apply(_parse_date_any)
    df["disclosure_date_parsed"] = df.get("disclosure_date", "").apply(_parse_date_any)

    # Amount parsing (best-effort)
    if "amount_raw" not in df.columns and "amount" in df.columns:
        df["amount_raw"] = df["amount"]

    return df


def main() -> None:
    st.set_page_config(page_title="Investimi — Congress Trades", layout="wide")
    st.title("Investimi — Congress Trades (House + Senate)")

    st.caption("Dashboard locale per filtrare i dati normalizzati dal mirror GitHub.")

    with st.sidebar:
        st.header("Sorgenti")
        default_owner_repo = st.text_input("GitHub repo (owner/repo)", value="Luc1p/investimi")
        base = f"https://raw.githubusercontent.com/{default_owner_repo}/main"
        house_url = st.text_input("House JSON URL", value=f"{base}/data/house/all_transactions.json")
        senate_url = st.text_input("Senate JSON URL", value=f"{base}/data/senate/all_transactions.json")
        reload_btn = st.button("Ricarica dati", type="primary")

        st.header("Filtri")
        days = st.slider("Ultimi N giorni (0 = tutto)", min_value=0, max_value=365, value=45, step=1)
        only_buys = st.checkbox("Solo acquisti (buy)", value=False)
        min_amount = st.number_input("Min amount USD (best-effort)", min_value=0, value=50_000, step=5_000)
        ticker_q = st.text_input("Ticker contiene", value="")
        name_q = st.text_input("Nome politico contiene", value="")

    @st.cache_data(show_spinner=False, ttl=300)
    def load_all(h_url: str, s_url: str, nonce: str) -> pd.DataFrame:
        house = _load_json(h_url)
        senate = _load_json(s_url)
        df = pd.concat(
            [
                _to_df(house, chamber="house"),
                _to_df(senate, chamber="senate"),
            ],
            ignore_index=True,
        )
        return df

    nonce = dt.datetime.utcnow().isoformat() if reload_btn else "cached"
    with st.spinner("Carico JSON dal mirror…"):
        df = load_all(house_url, senate_url, nonce)

    if df.empty:
        st.error("Nessun dato caricato (JSON vuoti o URL errati).")
        return

    # Apply filters
    dff = df.copy()
    if days and days > 0:
        cutoff = dt.datetime.utcnow().date() - dt.timedelta(days=int(days))
        dff = dff[dff["transaction_date_parsed"].notna()]
        dff = dff[dff["transaction_date_parsed"] >= cutoff]

    if ticker_q.strip():
        q = ticker_q.strip().upper()
        dff = dff[dff.get("ticker", "").fillna("").astype(str).str.upper().str.contains(q, na=False)]

    if name_q.strip():
        qn = name_q.strip().lower()
        dff = dff[dff.get("politician", "").fillna("").astype(str).str.lower().str.contains(qn, na=False)]

    if only_buys:
        dff = dff[dff.get("side", "").fillna("").astype(str).str.lower().isin(["buy", "purchase", "acquire"])]

    # Best-effort numeric amount: use amount_max_usd if present, else amount_min_usd
    amt_col = None
    if "amount_max_usd" in dff.columns:
        amt_col = "amount_max_usd"
    elif "amount_min_usd" in dff.columns:
        amt_col = "amount_min_usd"
    if amt_col is not None and min_amount > 0:
        dff[amt_col] = pd.to_numeric(dff[amt_col], errors="coerce")
        dff = dff[dff[amt_col].fillna(0) >= float(min_amount)]

    st.subheader("Risultati")
    c1, c2, c3 = st.columns(3)
    c1.metric("Righe", f"{len(dff):,}")
    c2.metric("House", f"{(dff['chamber']=='house').sum():,}")
    c3.metric("Senate", f"{(dff['chamber']=='senate').sum():,}")

    # Choose columns to display
    cols = [
        "transaction_date",
        "disclosure_date",
        "chamber",
        "politician",
        "ticker",
        "side",
        "transaction_type",
        "amount_raw",
        "amount_min_usd",
        "amount_max_usd",
        "ptr_link",
    ]
    cols = [c for c in cols if c in dff.columns]
    st.dataframe(dff.sort_values(by=["transaction_date_parsed"], ascending=False)[cols], use_container_width=True)

    with st.expander("Esporta JSON filtrato"):
        payload = json.dumps(dff[cols].to_dict(orient="records"), ensure_ascii=False, indent=2)
        st.download_button("Scarica JSON", data=payload, file_name="congress_filtered.json", mime="application/json")


if __name__ == "__main__":
    main()

