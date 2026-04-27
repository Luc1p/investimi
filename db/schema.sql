-- Investimi DB schema (MVP)
-- Postgres 16+

create schema if not exists core;
create schema if not exists ops;

-- --------
-- core
-- --------

create table if not exists core.sources (
  id bigserial primary key,
  key text not null unique,
  description text,
  created_at timestamptz not null default now()
);

create table if not exists core.actors (
  id bigserial primary key,
  actor_type text not null, -- politician | insider | other
  name text not null,
  chamber text, -- house | senate | null
  state text,
  party text,
  bio_guide_id text,
  created_at timestamptz not null default now(),
  unique (actor_type, name, chamber)
);

create index if not exists actors_name_idx on core.actors (name);

create table if not exists core.instruments (
  id bigserial primary key,
  ticker text not null,
  asset_class text not null default 'unknown', -- equity|etf|option|fund|unknown
  cik text,
  name text,
  exchange text,
  created_at timestamptz not null default now(),
  unique (ticker)
);

create index if not exists instruments_cik_idx on core.instruments (cik);

create table if not exists core.filings (
  id bigserial primary key,
  source_id bigint not null references core.sources(id) on delete restrict,
  external_id text not null,
  filing_url text,
  filed_at timestamptz,
  received_at timestamptz,
  raw_hash text,
  ingested_at timestamptz not null default now(),
  unique (source_id, external_id)
);

create index if not exists filings_ingested_at_idx on core.filings (ingested_at);

create table if not exists core.trade_events (
  id bigserial primary key,
  source_id bigint not null references core.sources(id) on delete restrict,
  filing_id bigint references core.filings(id) on delete set null,
  actor_id bigint references core.actors(id) on delete set null,
  instrument_id bigint references core.instruments(id) on delete set null,

  -- raw/canonical fields
  ticker_raw text,
  asset_description text,
  asset_type_raw text,

  side text, -- buy|sell|unknown
  transaction_type text,
  owner text,
  actor_role_title text,

  shares numeric,
  price numeric,
  amount_raw text,
  amount_min_usd numeric,
  amount_max_usd numeric,

  transaction_date date,
  disclosure_date date,
  disclosed_at timestamptz,

  ptr_link text,

  event_fingerprint text not null,
  raw jsonb,

  ingested_at timestamptz not null default now(),

  unique (source_id, event_fingerprint)
);

create index if not exists trade_events_disclosed_at_idx on core.trade_events (disclosed_at);
create index if not exists trade_events_tx_date_idx on core.trade_events (transaction_date);
create index if not exists trade_events_actor_idx on core.trade_events (actor_id, disclosed_at);
create index if not exists trade_events_instr_idx on core.trade_events (instrument_id, disclosed_at);

-- --------
-- ops
-- --------

create table if not exists ops.ingestion_runs (
  id bigserial primary key,
  job_key text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running', -- running|ok|error
  meta jsonb
);

