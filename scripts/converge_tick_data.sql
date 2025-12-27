-- scripts/converge_tick_data.sql
-- DANGER: Drops and recreates public.tick_data (data will be removed).
BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;

DROP TABLE IF EXISTS public.tick_data CASCADE;

CREATE TABLE public.tick_data (
  symbol                 text        NOT NULL,
  "timestamp"            timestamptz NOT NULL,
  sip_timestamp          bigint,
  participant_timestamp  bigint,
  trf_timestamp          bigint,
  price                  numeric(18,6) NOT NULL,
  size                   integer     NOT NULL,
  exchange               integer,
  conditions             integer[],
  tape                   integer,
  trade_id               text,
  UNIQUE (symbol, "timestamp", trade_id)
);

SELECT create_hypertable('public.tick_data', 'timestamp',
                         chunk_time_interval => interval '1 day',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS ix_tick_symbol_ts ON public.tick_data (symbol, "timestamp");

COMMIT;
