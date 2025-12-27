-- =====================================================================
-- brokers DB: canonical schema aligned with trader/db_brokers.py usage
-- =====================================================================

-- Safe reset for dev
DROP TRIGGER IF EXISTS trg_credentials_touch ON public.credentials;
DROP TRIGGER IF EXISTS trg_accounts_touch    ON public.broker_accounts;
DROP TRIGGER IF EXISTS trg_brokers_touch     ON public.brokers;

DROP FUNCTION IF EXISTS public.t_touch_updated_at();

DROP TABLE IF EXISTS public.credentials;
DROP TABLE IF EXISTS public.broker_accounts;
DROP TABLE IF EXISTS public.brokers;

-- ---------------------------------------------------------------------
-- brokers
--   Matches list_brokers(): SELECT broker_id, display_name, kind, api_base
-- ---------------------------------------------------------------------
CREATE TABLE public.brokers(
  broker_id   text PRIMARY KEY,
  display_name text NOT NULL,
  kind         text NOT NULL DEFAULT 'rest',   -- e.g. 'rest','ib','sim'
  api_base     text,                           -- e.g. https://paper-api.alpaca.markets
  enabled      boolean NOT NULL DEFAULT true,
  flags        jsonb    NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- broker_accounts
--   Matches list_accounts(): selects account_id, broker_id, label,
--   account_code, margin (used to derive class), flags JSON.
-- ---------------------------------------------------------------------
CREATE TABLE public.broker_accounts(
  account_id   text PRIMARY KEY,                -- store real ID like 'alpaca:paper'
  broker_id    text NOT NULL REFERENCES public.brokers(broker_id) ON DELETE CASCADE,
  label        text NOT NULL,
  account_code text,                            -- optional broker-native code
  margin       boolean NOT NULL DEFAULT true,   -- CASE WHEN margin THEN 'margin' ELSE 'cash'
  flags        jsonb   NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- credentials
--   Used by get_account_creds()/get_broker_creds() to fetch:
--     keys: api_key, api_secret, base_url  (all lowercase)
--   value_cipher = Fernet ciphertext (base64->bytea via decode()).
-- ---------------------------------------------------------------------
CREATE TABLE public.credentials(
  id           bigserial PRIMARY KEY,
  scope_type   text NOT NULL,     -- 'account' or 'broker'
  scope_id     text NOT NULL,     -- e.g. 'alpaca' (broker) or 'alpaca:paper' (account)
  key          text NOT NULL,     -- 'api_key' | 'api_secret' | 'base_url'
  value_cipher bytea NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_type, scope_id, key)
);

-- ---------------------------------------------------------------------
-- touch triggers
-- ---------------------------------------------------------------------
CREATE FUNCTION public.t_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE TRIGGER trg_brokers_touch
BEFORE UPDATE ON public.brokers
FOR EACH ROW EXECUTE FUNCTION public.t_touch_updated_at();

CREATE TRIGGER trg_accounts_touch
BEFORE UPDATE ON public.broker_accounts
FOR EACH ROW EXECUTE FUNCTION public.t_touch_updated_at();

CREATE TRIGGER trg_credentials_touch
BEFORE UPDATE ON public.credentials
FOR EACH ROW EXECUTE FUNCTION public.t_touch_updated_at();

-- ---------------------------------------------------------------------
-- seeds
-- ---------------------------------------------------------------------

-- Brokers (include api_base for Alpaca so adapters can fall back to it)
INSERT INTO public.brokers (broker_id, display_name, kind, api_base) VALUES
  ('alpaca', 'Alpaca Markets',    'rest',   'https://paper-api.alpaca.markets'),
  ('ib',     'Interactive Brokers','ib',    NULL),
  ('schwab', 'Charles Schwab',     'rest',  NULL),
  ('webull', 'Webull',             'rest',  NULL),
  ('sim',    'Internal Simulator', 'sim',   NULL),
  ('generic','Generic REST Broker','rest',  NULL)
ON CONFLICT (broker_id) DO UPDATE
  SET display_name = EXCLUDED.display_name,
      kind         = EXCLUDED.kind,
      api_base     = EXCLUDED.api_base;

-- Accounts your Trader expects to see
INSERT INTO public.broker_accounts (account_id, broker_id, label, account_code, margin, flags) VALUES
  ('alpaca:paper', 'alpaca', 'Alpaca Paper', NULL, true,  '{}'::jsonb),
  ('alpaca:live',  'alpaca', 'Alpaca Live',  NULL, true,  '{}'::jsonb),
  ('sim:cash',     'sim',    'Simulator Cash',  NULL, false, '{}'::jsonb),
  ('sim:margin',   'sim',    'Simulator Margin',NULL, true,  '{}'::jsonb)
ON CONFLICT (account_id) DO UPDATE
  SET broker_id = EXCLUDED.broker_id,
      label     = EXCLUDED.label,
      account_code = EXCLUDED.account_code,
      margin    = EXCLUDED.margin;

-- Clean prior Alpaca creds so we don't end up with mixed key names
DELETE FROM public.credentials
WHERE (scope_type, scope_id) IN (('account','alpaca:paper'), ('account','alpaca:live'), ('broker','alpaca'));

-- ---------------------------------------------------------------------
-- Insert encrypted credentials
--   Use tools/crypto/fernet_util.py with REFLEX_FERNET_KEY to make base64
--   ciphertext, then put those base64 strings into decode('...', 'base64')
--   EXACTLY as shown below. Keys MUST be: api_key, api_secret, base_url.
-- ---------------------------------------------------------------------

BEGIN;

-- Alpaca (broker-scope defaults are optional; account-scope preferred)
-- Example: set broker-scope base_url only (optional)
-- INSERT INTO public.credentials (scope_type, scope_id, key, value_cipher)
-- VALUES ('broker','alpaca','base_url', decode('<ENC_BASEURL_B64>'::text, 'base64'))
-- ON CONFLICT (scope_type, scope_id, key) DO UPDATE SET value_cipher = EXCLUDED.value_cipher;


-------------


BEGIN;

/* Alpaca PAPER */
INSERT INTO public.credentials (scope_type, scope_id, key, value_cipher)
VALUES
  ('account','alpaca:paper','api_key',    decode('Z0FBQUFBQnBFZmk1aGo2ZlcxdjZlUnE0YlZabFBrU0Z4WnJBOUlHMzl1N0tUcTd0S21MZ2taUF9DcU15aWZCbDNXY2hmLUxQTENaSk1MUzJOeXVtX0hiSFAwNERvVm9oaU5KRDJ4cHhfYV91UjdKUXRibTQzZ0U9'::text, 'base64')),
  ('account','alpaca:paper','api_secret', decode('Z0FBQUFBQnBFZmk1QndUUlQxTjRPano3cGVTU04xR0lGUkhobW8tSW53Z2t0Q2hvUnBjTW44WVFCLVJ2TkhjRzNtWG9vbUJJZWFEdU5Bbkc4dE9CcmJkLTJVSzZzOGdaRzc5ZmY2cklPSEthdWtZM1VMWDcxY1Z6bzVOS1hLb09Rc3FJaE85bV9OUlo='::text, 'base64')),
  ('account','alpaca:paper','base_url',   decode('Z0FBQUFBQnBFZmk1dktwU3FyNmt3ay1NakVZVE5fX1VkSkI0ZEhZWVNtZzZDclQ2VFhZWVd0TWhYWWpDaFpCb1RLcVFxWl9DZWJCSGpJRE9MUy16WHVGb2pUUTJ4M1hiM3M0Y2JRX1kxa3FNOVd5cjhUc3Q3SVlNd2p2TjRpS3dvcFBGQXlBTnJ5WVI='::text, 'base64'))
ON CONFLICT (scope_type, scope_id, key)
DO UPDATE SET value_cipher = EXCLUDED.value_cipher;

/* Alpaca LIVE */
INSERT INTO public.credentials (scope_type, scope_id, key, value_cipher)
VALUES
  ('account','alpaca:live','api_key',     decode('Z0FBQUFBQnBFZmk1Ml9OQzhkLXpDT1FRN29BTWdmYndzRWw2VFlCNjd5Rms0WVVWbW5yRXlhMnZwR1BkbXIyR3N0cGpKdVpiNzJlWjR2TVZ3ZWRSNjk2UHlRNzFoY0lRWjktUmY3U2hUTHlFcEFNcUU0UnRDUG89'::text, 'base64')),
  ('account','alpaca:live','api_secret',  decode('Z0FBQUFBQnBFZmk1TWFvV1hudTF1NkRCbFc4aFU0V05BYTJvclJ1T3ZEOVh1Ql9JLU1XaW9WM0VHNEJxQjhFLTBVVnAtYkR4WFdRTGFlTDhyd1BTcVkyVGZmcjFybnVKazk2QjRRQktMYW5lN1NIdEZlbzFJMTctMW5aa0Y3d1lMV1ZDSEJoVl9CTjY='::text, 'base64')),
  ('account','alpaca:live','base_url',    decode('Z0FBQUFBQnBFZmk1RVZxSXJQakpOTGQxYUNWNjJ5ZWUtalZwSmJaOHJsdFFMYnhSbWdWZ1VRanBHeFduaWpfbkw3dC10Xy1PeHZONFFEM3VuRmtfWVJKelJsVVBTdXZVNV9Pd2hYdV9BaTd3UGRyMDZHWlpMR3M9'::text, 'base64'))
ON CONFLICT (scope_type, scope_id, key)
DO UPDATE SET value_cipher = EXCLUDED.value_cipher;

COMMIT;


COMMIT;

-- ---------------------------------------------------------------------
-- Quick sanity checks
-- ---------------------------------------------------------------------
-- Brokers shape (must have these columns)
-- SELECT broker_id, display_name, kind, api_base FROM public.brokers ORDER BY 1;

-- Accounts shape (must have these columns; class is derived in code)
-- SELECT account_id, broker_id, label, account_code, margin, flags FROM public.broker_accounts ORDER BY 1;

-- Creds keys should be exactly: api_key, api_secret, base_url
-- SELECT scope_type, scope_id, key, octet_length(value_cipher) FROM public.credentials ORDER BY 1,2,3;
