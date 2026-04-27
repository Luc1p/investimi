# Investimi — Roadmap Dataset (2020→oggi)

Questa cartella descrive **cosa stiamo costruendo** e **in che ordine**: un dataset locale (Postgres + Parquet) per backtest/feature engineering su:
- trade **politici** (House + Senate, STOCK Act)
- trade **insider privati** (SEC Form 4)
- **prezzi 1D** (OHLCV) per collegare eventi → performance

## Obiettivo
Arrivare ad avere dal **2020 a oggi** una base dati “quasi totale” per:
- misurare chi “batte il mercato” (alpha vs benchmark)
- costruire feature/event study per un modello di algotrading

## Storage & compute (linee guida)
- **Parquet (lake)**: dati voluminosi (prezzi, feature, label).
- **Postgres**: eventi, metadata, dedup, manifest/parquet index, ingestion runs.
- **SSD esterna** (consigliata): spostare `LAKE_ROOT` su SSD per non riempire il disco interno.
- **RAM 16GB**: ok; sempre batch/chunk, niente carichi “tutto in RAM”.

## Step 1 — Census/Index House + Senate (solo link + metadata) ✅ prossimo
**Cosa**: indicizzare tutte le disclosure report (PTR) disponibili dal 2020, salvando:
- link al report (PDF/URL)
- metadata minime (chamber, filer/politician, filing date / submitted date, year)

**Perché**: ottenere un “inventario” totale e misurabile (totale disponibile) prima del parsing completo.

**Dove gira**: GitHub Actions (leggero).

**Output**:
- JSON/CSV con tutti i report indicizzati (artifact)
- righe in Postgres (tabella dedicata) con dedup + resume

## Step 2 — Backfill House + Senate (parse transazioni dal 2020)
**Cosa**: per ogni report indicizzato, parsare le transazioni e popolare `core.trade_events`.

**House**: parsing PDF (CPU medium, rete alta).

**Senate**: pagine eFD (503/403 possibili → retry/backoff + Playwright quando serve).

**Output**:
- `core.trade_events` “quasi totale” dal 2020
- `ops.ingestion_runs` per resume/monitor

## Step 3 — Prezzi 1D (2000→oggi) + incrementale giornaliero
**Cosa**: OHLCV 1D in Parquet + `core.price_bars_manifest`.

**Provider**:
- US: Stooq (con `STOOQ_APIKEY`)
- IT (FTSE MIB): Yahoo (yfinance) per `*.MI`

**Output**:
- Parquet in `lake/prices/...`
- manifest in Postgres

## Step 4 — SEC Form 4 “totale utile” (2020→oggi)
**Cosa**: importare Form 4 dal 2020 per un universo definito e “utile”.

**Universi utili tipici**:
- tickers presenti nei trade politici (Step 2)
- + universi market (SP500/NDX)

**Output**:
- `core.trade_events` (source `sec_edgar_form4`)
- dedup tramite fingerprint

## Automazione (GitHub Actions)
- prezzi: `.github/workflows/prices.yml` (artifact Parquet + manifest CSV)
- census/index: (aggiungeremo `census.yml`)
- backfill: (aggiungeremo `backfill.yml` a tranche)

