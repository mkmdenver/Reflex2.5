-- brokers DB — canonical schema (v2025-11-09)

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1) brokers (master)
DROP TABLE IF EXISTS public.brokers CASCADE;
CREATE TABLE public.brokers (
  broker_id    text PRIMARY KEY,
  display_name text        NOT NULL,
  api_base     text        NOT NULL,
  kind         text        NOT NULL
);

COMMENT ON TABLE  public.brokers IS 'Broker registry';
COMMENT ON COLUMN public.brokers.api_base IS 'Default REST base URL (per-account override via broker_accounts.flags->base_url)';

-- 2) broker_accounts (per-account knobs + flags JSONB)
DROP TABLE IF EXISTS public.broker_accounts CASCADE;
CREATE TABLE public.broker_accounts (
  account_id    text PRIMARY KEY,
  broker_id     text        NOT NULL REFERENCES public.brokers(broker_id) ON DELETE CASCADE,
  account_code  text        NOT NULL,        -- e.g. PAPER-ACCT, LIVE-ACCT, SIM-CASH, SIM-MARGIN
  label         text        NOT NULL,

  -- simple booleans that many UIs like to filter on
  active        boolean     NOT NULL DEFAULT TRUE,
  margin        boolean     NOT NULL DEFAULT FALSE,
  paper         boolean     NOT NULL DEFAULT FALSE,

  -- canonical place for everything else the Trader reads
  -- expected keys used by Trader today:
  --   base_url        : string (per-account REST base URL override)
  --   pdt_applies     : bool
  --   pdt_restricted  : bool
  --   allow_short     : bool
  --   starting_cash   : number/string for sim
  flags         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_broker_accounts_broker
  ON public.broker_accounts(broker_id);

COMMENT ON COLUMN public.broker_accounts.flags IS
  'Free-form config used by runtime. Known keys: base_url, pdt_applies, pdt_restricted, allow_short, starting_cash';

-- 3) credentials (encrypted values; scope can be 'broker' or 'account')
DROP TABLE IF EXISTS public.credentials CASCADE;
CREATE TABLE public.credentials (
  cred_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type    text        NOT NULL CHECK (scope_type IN ('broker','account')),
  scope_id      text        NOT NULL,  -- brokers.broker_id OR broker_accounts.account_id
  key           text        NOT NULL,  -- eg: api_key_id, api_secret, oauth_token, base_url (if you choose to store it encrypted)
  value_cipher  bytea       NOT NULL,  -- output of Fernet (stored as raw bytes via decode(...,'base64'))
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_type, scope_id, key)
);

CREATE INDEX IF NOT EXISTS ix_credentials_scope
  ON public.credentials(scope_type, scope_id);

-- 4) helper view: resolves effective base URL per account
DROP VIEW IF EXISTS public.v_accounts_effective_url;
CREATE VIEW public.v_accounts_effective_url AS
SELECT
  a.account_id,
  a.label,
  a.broker_id,
  COALESCE(a.flags->>'base_url', b.api_base) AS effective_url,
  a.active, a.margin, a.paper,
  a.flags
FROM public.broker_accounts a
JOIN public.brokers b USING (broker_id);

-- sanity: minimal privileges for local dev (adjust to your role model)
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO PUBLIC;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO PUBLIC;
