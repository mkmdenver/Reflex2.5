-- brokers DB schema aligned with your Python (no triggers, no DO $$, no BEGIN/COMMIT)
SET search_path = public;

-- Reset (safe in dev)
DROP TABLE IF EXISTS credentials;
DROP TABLE IF EXISTS broker_accounts;
DROP TABLE IF EXISTS brokers;

-- brokers: required columns in code: broker_id, display_name, kind, api_base
CREATE TABLE brokers(
  broker_id    text PRIMARY KEY,
  display_name text NOT NULL,
  kind         text NOT NULL DEFAULT 'rest',
  api_base     text,
  enabled      boolean NOT NULL DEFAULT true,
  flags        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- broker_accounts: required by list_accounts()
CREATE TABLE broker_accounts(
  account_id   text PRIMARY KEY,
  broker_id    text NOT NULL REFERENCES brokers(broker_id) ON DELETE CASCADE,
  label        text NOT NULL,
  account_code text,
  margin       boolean NOT NULL DEFAULT true,
  flags        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- credentials: keys MUST be exactly: api_key, api_secret, base_url
CREATE TABLE credentials(
  id           bigserial PRIMARY KEY,
  scope_type   text NOT NULL,     -- 'broker' or 'account'
  scope_id     text NOT NULL,     -- e.g. 'alpaca' or 'alpaca:paper'
  key          text NOT NULL,     -- 'api_key' | 'api_secret' | 'base_url'
  value_cipher bytea NOT NULL,    -- Fernet ciphertext (base64->bytea)
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_type, scope_id, key)
);

-- seed brokers
INSERT INTO brokers (broker_id, display_name, kind, api_base) VALUES
  ('alpaca','Alpaca Markets',     'rest','https://paper-api.alpaca.markets'),
  ('ib',    'Interactive Brokers','ib',  NULL),
  ('schwab','Charles Schwab',     'rest',NULL),
  ('webull','Webull',             'rest',NULL),
  ('sim',   'Internal Simulator', 'sim', NULL),
  ('generic','Generic REST Broker','rest',NULL)
ON CONFLICT (broker_id) DO UPDATE
  SET display_name = EXCLUDED.display_name,
      kind         = EXCLUDED.kind,
      api_base     = EXCLUDED.api_base;

-- seed accounts
INSERT INTO broker_accounts (account_id, broker_id, label, account_code, margin, flags) VALUES
  ('alpaca:paper','alpaca','Alpaca Paper',   NULL, true,  '{}'::jsonb),
  ('alpaca:live', 'alpaca','Alpaca Live',    NULL, true,  '{}'::jsonb),
  ('sim:cash',    'sim',   'Simulator Cash', NULL, false, '{}'::jsonb),
  ('sim:margin',  'sim',   'Simulator Margin',NULL, true, '{}'::jsonb)
ON CONFLICT (account_id) DO UPDATE
  SET broker_id = EXCLUDED.broker_id,
      label     = EXCLUDED.label,
      account_code = EXCLUDED.account_code,
      margin    = EXCLUDED.margin;

-- wipe any prior mismatched creds for these scopes
DELETE FROM credentials
WHERE (scope_type, scope_id) IN (('account','alpaca:paper'), ('account','alpaca:live'), ('broker','alpaca'));

-- INSERT: one row per unique (scope_type, scope_id, key), no placeholders
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  -- Alpaca PAPER
  ('account','alpaca:paper','api_key',    decode('Z0FBQUFBQnBFZmk1aGo2ZlcxdjZlUnE0YlZabFBrU0Z4WnJBOUlHMzl1N0tUcTd0S21MZ2taUF9DcU15aWZCbDNXY2hmLUxQTENaSk1MUzJOeXVtX0hiSFAwNERvVm9oaU5KRDJ4cHhfYV91UjdKUXRibTQzZ0U9', 'base64')),
  ('account','alpaca:paper','api_secret', decode('Z0FBQUFBQnBFZmk1QndUUlQxTjRPano3cGVTU04xR0lGUkhobW8tSW53Z2t0Q2hvUnBjTW44WVFCLVJ2TkhjRzNtWG9vbUJJZWFEdU5Bbkc4dE9CcmJkLTJVSzZzOGdaRzc5ZmY2cklPSEthdWtZM1VMWDcxY1Z6bzVOS1hLb09Rc3FJaE85bV9OUlo=', 'base64')),
  ('account','alpaca:paper','base_url',   decode('Z0FBQUFBQnBFZmk1dktwU3FyNmt3ay1NakVZVE5fX1VkSkI0ZEhZWVNtZzZDclQ2VFhZWVd0TWhYWWpDaFpCb1RLcVFxWl9DZWJCSGpJRE9MUy16WHVGb2pUUTJ4M1hiM3M0Y2JRX1kxa3FNOVd5cjhUc3Q3SVlNd2p2TjRpS3dvcFBGQXlBTnJ5WVI=', 'base64')),

  -- Alpaca LIVE
  ('account','alpaca:live','api_key',     decode('Z0FBQUFBQnBFZmk1Ml9OQzhkLXpDT1FRN29BTWdmYndzRWw2VFlCNjd5Rms0WVVWbW5yRXlhMnZwR1BkbXIyR3N0cGpKdVpiNzJlWjR2TVZ3ZWRSNjk2UHlRNzFoY0lRWjktUmY3U2hUTHlFcEFNcUU0UnRDUG89', 'base64')),
  ('account','alpaca:live','api_secret',  decode('Z0FBQUFBQnBFZmk1TWFvV1hudTF1NkRCbFc4aFU0V05BYTJvclJ1T3ZEOVh1Ql9JLU1XaW9WM0VHNEJxQjhFLTBVVnAtYkR4WFdRTGFlTDhyd1BTcVkyVGZmcjFybnVKazk2QjRRQktMYW5lN1NIdEZlbzFJMTctMW5aa0Y3d1lMV1ZDSEJoVl9CTjY=', 'base64')),
  ('account','alpaca:live','base_url',    decode('Z0FBQUFBQnBFZmk1RVZxSXJQakpOTGQxYUNWNjJ5ZWUtalZwSmJaOHJsdFFMYnhSbWdWZ1VRanBHeFduaWpfbkw3dC10Xy1PeHZONFFEM3VuRmtfWVJKelJsVVBTdXZVNV9Pd2hYdV9BaTd3UGRyMDZHWlpMR3M9', 'base64'))
ON CONFLICT (scope_type, scope_id, key)
DO UPDATE SET value_cipher = EXCLUDED.value_cipher;

-- quick checks
SELECT broker_id, display_name, kind, api_base FROM brokers ORDER BY 1;
SELECT account_id, broker_id, label, account_code, margin, flags FROM broker_accounts ORDER BY 1;
SELECT scope_type, scope_id, key, octet_length(value_cipher) AS bytes FROM credentials ORDER BY 1,2,3;
