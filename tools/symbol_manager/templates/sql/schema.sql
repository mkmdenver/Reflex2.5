-- ============================================================================
-- REFLEX2 — Timescale "Bedrock" Schema (Tickless runtime; ticks/quotes in Parquet)
-- Concrete pour date: 2025-10-21
-- ============================================================================

SET search_path = public, pg_catalog;

-- Timescale
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================================
-- HOUSEKEEPING: remove legacy tick/quote tables if present
-- ============================================================================
DROP TABLE IF EXISTS tick_data CASCADE;
DROP TABLE IF EXISTS quote_data CASCADE;

-- Drop old KPIs that referenced ticks/quotes
DROP MATERIALIZED VIEW IF EXISTS kpi_ingest_1m CASCADE;
DROP MATERIALIZED VIEW IF EXISTS kpi_ingest_1m_ticks CASCADE;
DROP MATERIALIZED VIEW IF EXISTS kpi_ingest_1m_quotes CASCADE;
DROP MATERIALIZED VIEW IF EXISTS kpi_ingest_1m_minbars CASCADE;

-- ============================================================================
-- CORE TABLES (minute & daily bars are authoritative)
-- ============================================================================

-- trade_triggers (optional scratchpad for replay/evaluator hooks)
DROP TABLE IF EXISTS trade_triggers CASCADE;
CREATE TABLE trade_triggers (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    trigger_type TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
    metadata JSONB
);

-- minute_bar_audit (ingest integrity summaries)
DROP TABLE IF EXISTS minute_bar_audit CASCADE;
CREATE TABLE minute_bar_audit (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    ingested_volume BIGINT,
    expected_volume BIGINT,
    integrity_passed BOOLEAN
);

-- minute_bars (authoritative 1m bars, includes premarket minutes)
DROP TABLE IF EXISTS minute_bars CASCADE;
CREATE TABLE minute_bars (
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,  -- start of minute (UTC)
    open NUMERIC(18, 6),
    high NUMERIC(18, 6),
    low  NUMERIC(18, 6),
    close NUMERIC(18, 6),
    volume BIGINT,
    PRIMARY KEY (symbol, timestamp)
);
SELECT create_hypertable('minute_bars', 'timestamp',
                         chunk_time_interval => interval '1 day',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS ix_minute_bars_sym_time ON minute_bars (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_minute_bars_time ON minute_bars (timestamp DESC);

-- daily_bars (RTH daily)
DROP TABLE IF EXISTS daily_bars CASCADE;
CREATE TABLE daily_bars (
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,  -- session day bucket at UTC midnight (or consistent rule)
    open NUMERIC(18, 6),
    high NUMERIC(18, 6),
    low  NUMERIC(18, 6),
    close NUMERIC(18, 6),
    volume BIGINT,
    PRIMARY KEY (symbol, timestamp)
);
SELECT create_hypertable('daily_bars', 'timestamp',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS ix_daily_bars_sym_time ON daily_bars (symbol, timestamp DESC);

-- fundamentals (snapshot)
DROP TABLE IF EXISTS fundamental_data CASCADE;
CREATE TABLE fundamental_data (
    symbol TEXT PRIMARY KEY,
    company TEXT,
    sector TEXT,
    industry TEXT,
    country TEXT,
    exchange TEXT,
    market_cap NUMERIC(18, 2),
    pe_ratio NUMERIC(10, 2),
    shares_float BIGINT,
    float_percent NUMERIC(10, 2),
    insider_transactions NUMERIC(10, 2),
    short_float NUMERIC(10, 2),
    average_true_range NUMERIC(10, 4),
    last_updated TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fund_symbol     ON fundamental_data (symbol);
CREATE INDEX IF NOT EXISTS idx_fund_sector     ON fundamental_data (sector);
CREATE INDEX IF NOT EXISTS idx_fund_industry   ON fundamental_data (industry);
CREATE INDEX IF NOT EXISTS idx_fund_exchange   ON fundamental_data (exchange);
CREATE INDEX IF NOT EXISTS idx_fund_float      ON fundamental_data (shares_float);

-- symbol metadata (tiers etc.)
DROP TABLE IF EXISTS symbol_metadata CASCADE;
CREATE TABLE symbol_metadata (
    symbol VARCHAR PRIMARY KEY,
    db_tier SMALLINT NOT NULL DEFAULT 0 CHECK (db_tier BETWEEN 0 AND 3),  -- 0=cold..3=hot
    rt_tier SMALLINT NOT NULL DEFAULT 0 CHECK (rt_tier BETWEEN 0 AND 3),
    list_date DATE,
    filters TEXT[] DEFAULT ARRAY[]::TEXT[],
    last_updated TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_symmeta_db_tier     ON symbol_metadata (db_tier);
CREATE INDEX IF NOT EXISTS idx_symmeta_rt_tier     ON symbol_metadata (rt_tier);
CREATE INDEX IF NOT EXISTS idx_symmeta_db_warmhot  ON symbol_metadata (symbol) WHERE db_tier >= 2;
CREATE INDEX IF NOT EXISTS idx_symmeta_rt_warmhot  ON symbol_metadata (symbol) WHERE rt_tier >= 2;

-- ingest sessions (provenance)
DROP TABLE IF EXISTS ingest_sessions CASCADE;
CREATE TABLE ingest_sessions (
    session_id UUID PRIMARY KEY,
    start_time TIMESTAMPTZ NOT NULL,
    source TEXT,
    notes TEXT
);

-- evaluator flags (optional)
DROP TABLE IF EXISTS evaluator_flags CASCADE;
CREATE TABLE evaluator_flags (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    flag_type TEXT,
    confidence NUMERIC(5,2),
    metadata JSONB
);

-- profile view (read-only convenience)
DROP VIEW IF EXISTS symbol_profile_view;
CREATE VIEW symbol_profile_view AS
SELECT
    f.symbol,
    f.company, f.sector, f.industry, f.country, f.exchange,
    f.market_cap, f.shares_float, f.float_percent, f.pe_ratio, f.average_true_range,
    f.last_updated AS fundamentals_updated,
    m.db_tier, m.rt_tier,
    CASE m.db_tier WHEN 0 THEN 'cold' WHEN 1 THEN 'watch' WHEN 2 THEN 'warm' ELSE 'hot' END AS db_label,
    CASE m.rt_tier WHEN 0 THEN 'cold' WHEN 1 THEN 'watch' WHEN 2 THEN 'warm' ELSE 'hot' END AS rt_label,
    m.list_date, m.filters, m.last_updated AS metadata_updated
FROM fundamental_data f
LEFT JOIN symbol_metadata m USING (symbol);

-- ============================================================================
-- CONTINUOUS AGGREGATES from minute_bars
-- ============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS agg_5m_bars
WITH (timescaledb.continuous) AS
SELECT
  symbol,
  time_bucket('5 minutes', timestamp) AS bucket,
  first(open, timestamp)::NUMERIC(18,6) AS open,
  max(high)::NUMERIC(18,6) AS high,
  min(low )::NUMERIC(18,6) AS low,
  last(close, timestamp)::NUMERIC(18,6) AS close,
  sum(volume) AS volume
FROM minute_bars
GROUP BY symbol, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS agg_15m_bars
WITH (timescaledb.continuous) AS
SELECT
  symbol,
  time_bucket('15 minutes', timestamp) AS bucket,
  first(open, timestamp)::NUMERIC(18,6) AS open,
  max(high)::NUMERIC(18,6) AS high,
  min(low )::NUMERIC(18,6) AS low,
  last(close, timestamp)::NUMERIC(18,6) AS close,
  sum(volume) AS volume
FROM minute_bars
GROUP BY symbol, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS agg_1h_bars
WITH (timescaledb.continuous) AS
SELECT
  symbol,
  time_bucket('1 hour', timestamp) AS bucket,
  first(open, timestamp)::NUMERIC(18,6) AS open,
  max(high)::NUMERIC(18,6) AS high,
  min(low )::NUMERIC(18,6) AS low,
  last(close, timestamp)::NUMERIC(18,6) AS close,
  sum(volume) AS volume
FROM minute_bars
GROUP BY symbol, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS agg_1d_bars
WITH (timescaledb.continuous) AS
SELECT
  symbol,
  time_bucket('1 day', timestamp) AS bucket,
  first(open, timestamp)::NUMERIC(18,6) AS open,
  max(high)::NUMERIC(18,6) AS high,
  min(low )::NUMERIC(18,6) AS low,
  last(close, timestamp)::NUMERIC(18,6) AS close,
  sum(volume) AS volume
FROM minute_bars
GROUP BY symbol, bucket
WITH NO DATA;

-- Refresh policies (tune later as you like)
SELECT add_continuous_aggregate_policy('agg_5m_bars',
    start_offset => INTERVAL '6 hours',
    end_offset   => INTERVAL '10 minutes',
    schedule_interval => INTERVAL '5 minutes');

SELECT add_continuous_aggregate_policy('agg_15m_bars',
    start_offset => INTERVAL '2 days',
    end_offset   => INTERVAL '30 minutes',
    schedule_interval => INTERVAL '15 minutes');

SELECT add_continuous_aggregate_policy('agg_1h_bars',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

SELECT add_continuous_aggregate_policy('agg_1d_bars',
    start_offset => INTERVAL '120 days',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');

-- ============================================================================
-- KPI (minute-only)
-- ============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS kpi_ingest_1m_minbars
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', timestamp) AS bucket,
       'minute_bars'::text AS dataset,
       COUNT(*)::bigint AS rows_ingested
FROM minute_bars
GROUP BY bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('kpi_ingest_1m_minbars',
    start_offset => INTERVAL '12 hours',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '1 minute');

CREATE OR REPLACE VIEW kpi_ingest_1m AS
SELECT * FROM kpi_ingest_1m_minbars;

-- ============================================================================
-- SERVICE METRICS (hypertable)
-- ============================================================================
DROP TABLE IF EXISTS service_metrics CASCADE;
CREATE TABLE service_metrics (
  id        bigserial,
  ts        timestamptz NOT NULL,
  service   text NOT NULL,
  metric    text NOT NULL,
  value     double precision NOT NULL,
  labels    jsonb,
  ingest_ts timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ts, id)
);
SELECT create_hypertable('service_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS ix_service_metrics_lookup ON service_metrics (service, metric, ts);
SELECT set_chunk_time_interval('service_metrics', INTERVAL '1 day');

-- ============================================================================
-- CALENDAR / SESSIONS (NY-ET discipline)
-- ============================================================================
CREATE TABLE IF NOT EXISTS trading_calendar (
  session_date   date PRIMARY KEY,
  is_trading_day boolean NOT NULL,
  premkt_open_et time,    -- e.g., 04:00:00
  rth_open_et    time,    -- e.g., 09:30:00
  rth_close_et   time,    -- e.g., 16:00:00
  post_close_et  time,    -- e.g., 20:00:00
  notes          text
);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_session_utc AS
SELECT
  session_date,
  is_trading_day,
  (session_date + premkt_open_et) AT TIME ZONE 'US/Eastern' AS premkt_open_utc,
  (session_date + rth_open_et   ) AT TIME ZONE 'US/Eastern' AS rth_open_utc,
  (session_date + rth_close_et  ) AT TIME ZONE 'US/Eastern' AS rth_close_utc,
  (session_date + post_close_et ) AT TIME ZONE 'US/Eastern' AS post_close_utc
FROM trading_calendar;
CREATE INDEX IF NOT EXISTS ix_mv_session_utc ON mv_session_utc (session_date);

-- ============================================================================
-- CORPORATE ACTIONS & ADJUSTED PREV-CLOSE
-- ============================================================================
CREATE TABLE IF NOT EXISTS corporate_actions (
  symbol text NOT NULL,
  action_date date NOT NULL,
  action_type text NOT NULL CHECK (action_type IN ('split','dividend')),
  split_numerator numeric(10,4),
  split_denominator numeric(10,4),
  dividend_amount numeric(12,6),
  PRIMARY KEY (symbol, action_date, action_type)
);
CREATE INDEX IF NOT EXISTS ix_corp_actions_sym_date ON corporate_actions(symbol, action_date);

-- RTH raw prev close (yesterday's close per symbol)
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_prev_close_raw AS
WITH rth AS (
  SELECT symbol, date_trunc('day', timestamp)::date AS session_date,
         close::numeric(12,4) AS rth_close
  FROM daily_bars
)
SELECT r1.symbol, r1.session_date, r2.rth_close AS prev_close_raw
FROM rth r1
JOIN rth r2
  ON r2.symbol = r1.symbol
 AND r2.session_date = r1.session_date - INTERVAL '1 day';

CREATE INDEX IF NOT EXISTS ix_mv_prev_close_raw ON mv_prev_close_raw(symbol, session_date);

-- Adjusted for splits if present (simple same-day split handling)
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_prev_close_adj AS
SELECT p.symbol, p.session_date,
       p.prev_close_raw *
       COALESCE(NULLIF(ca.split_denominator,0),1)::numeric /
       COALESCE(NULLIF(ca.split_numerator,0),1)::numeric AS prev_close_adj
FROM mv_prev_close_raw p
LEFT JOIN LATERAL (
  SELECT split_numerator, split_denominator
  FROM corporate_actions
  WHERE symbol = p.symbol
    AND action_type = 'split'
    AND action_date = p.session_date
) ca ON TRUE;

CREATE INDEX IF NOT EXISTS ix_mv_prev_close_adj ON mv_prev_close_adj(symbol, session_date);

-- ============================================================================
-- FIRST TRADE OF DAY (authoritative table + CHEAT MV now)
-- ============================================================================
CREATE TABLE IF NOT EXISTS first_trade_of_day (
  session_date  date NOT NULL,
  symbol        text NOT NULL,
  ts_first_utc  timestamptz,
  price_first   numeric(12,4),
  source        text NOT NULL DEFAULT 'unknown', -- 'cheat_1m' | 'ticks_parquet' | 'manual'
  PRIMARY KEY (session_date, symbol)
);

-- Cheat = first premarket 1m bar open >= 04:00 ET
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_first_trade_cheat AS
WITH mb AS (
  SELECT symbol,
         (timestamp AT TIME ZONE 'US/Eastern')                   AS ts_et,
         open::numeric(12,4)                                     AS first_open
  FROM minute_bars
  WHERE (timestamp AT TIME ZONE 'US/Eastern')::time >= time '04:00'
)
, firsts AS (
  SELECT symbol, date_trunc('day', ts_et)::date AS session_date, min(ts_et) AS first_minute_ts_et
  FROM mb
  GROUP BY 1,2
)
SELECT f.symbol,
       f.session_date,
       (f.first_minute_ts_et AT TIME ZONE 'US/Eastern') AS first_minute_ts_utc,
       mb2.first_open                                   AS cheat_first_open
FROM firsts f
JOIN mb mb2
  ON mb2.symbol = f.symbol
 AND mb2.ts_et   = f.first_minute_ts_et;

CREATE INDEX IF NOT EXISTS ix_mv_first_trade_cheat ON mv_first_trade_cheat(symbol, session_date);

-- ============================================================================
-- CANDIDATES VIEW (drives tick Parquet backfill batches)
-- price < $20, float < 20M, gap >= 10% using CHEAT first trade & adjusted prev-close
-- ============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_candidates AS
SELECT
  ft.symbol,
  ft.session_date,
  pca.prev_close_adj         AS prev_close,
  ft.cheat_first_open        AS first_trade_price,
  ((ft.cheat_first_open / pca.prev_close_adj) - 1.0) AS gap_pct,
  f.shares_float
FROM mv_first_trade_cheat ft
JOIN mv_prev_close_adj    pca
  ON pca.symbol = ft.symbol AND pca.session_date = ft.session_date
LEFT JOIN fundamental_data f
  ON f.symbol = ft.symbol
WHERE pca.prev_close_adj < 20.00
  AND (f.shares_float IS NULL OR f.shares_float < 20000000)
  AND ft.cheat_first_open >= pca.prev_close_adj * 1.10;

CREATE INDEX IF NOT EXISTS ix_mv_candidates ON mv_candidates(session_date, symbol);

-- ============================================================================
-- MARKET / SECTOR BREADTH (context per minute)
-- ============================================================================
CREATE TABLE IF NOT EXISTS breadth_1m (
  ts timestamptz NOT NULL,
  scope text NOT NULL,        -- 'market','sector:XLK','etf:SPY', etc.
  metric text NOT NULL,       -- 'adv_dec_ratio','trin','nyse_tick','rv_spy_5m', ...
  value double precision NOT NULL,
  labels jsonb,
  PRIMARY KEY (ts, scope, metric)
);
SELECT create_hypertable('breadth_1m', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_breadth_scope_metric_ts ON breadth_1m(scope, metric, ts);

CREATE MATERIALIZED VIEW IF NOT EXISTS breadth_5m
WITH (timescaledb.continuous) AS
SELECT scope, metric, time_bucket('5 minutes', ts) AS bucket, avg(value) AS value
FROM breadth_1m
GROUP BY scope, metric, bucket
WITH NO DATA;

-- ============================================================================
-- DATA LINEAGE / INGEST AUDIT
-- ============================================================================
CREATE TABLE IF NOT EXISTS data_lineage (
  id bigserial PRIMARY KEY,
  dataset text NOT NULL,           -- 'minute_bars','daily_bars','fundamentals'
  source  text NOT NULL,           -- 'polygon_rest','s3_parquet','manual_fix'
  session_date date,
  symbol text,
  bytes_ingested bigint,
  checksum text,
  inserted_at timestamptz NOT NULL DEFAULT now(),
  notes text
);
CREATE INDEX IF NOT EXISTS ix_lineage_dataset_date_sym ON data_lineage(dataset, session_date, symbol);

CREATE TABLE IF NOT EXISTS ingest_integrity (
  id bigserial PRIMARY KEY,
  dataset text NOT NULL,           -- 'minute_bars','daily_bars'
  ts timestamptz NOT NULL,         -- minute or day boundary
  symbol text,
  expected_rows int,
  actual_rows int,
  ok boolean,
  details text
);
CREATE INDEX IF NOT EXISTS ix_ingest_integrity ON ingest_integrity(dataset, ts, symbol);

-- ============================================================================
-- COMPATIBILITY STUBS (no DB ticks/quotes — Parquet-only)
-- ============================================================================
DROP VIEW IF EXISTS ticks CASCADE;
CREATE VIEW ticks AS
SELECT
  NULL::text        AS symbol,
  NULL::timestamptz AS timestamp,
  NULL::numeric     AS price
WHERE FALSE;

DROP VIEW IF EXISTS quotes CASCADE;
CREATE VIEW quotes AS
SELECT
  NULL::text        AS symbol,
  NULL::timestamptz AS timestamp,
  NULL::numeric     AS bid,
  NULL::int         AS bid_size,
  NULL::numeric     AS ask,
  NULL::int         AS ask_size
WHERE FALSE;

-- Convenience view
DROP VIEW IF EXISTS bars_1m CASCADE;
CREATE VIEW bars_1m AS
SELECT symbol,
       timestamp AS bar_time,
       open, high, low, close, volume
FROM minute_bars;

-- ============================================================================
-- RECOMMENDED REFRESH ORDER AFTER BULK BACKFILLS
-- ============================================================================
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_session_utc;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_prev_close_raw;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_prev_close_adj;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_first_trade_cheat;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_candidates;
-- (CAGGs refresh via policies)
-- ============================================================================
-- END
-- ============================================================================
