-- createandseed.sql
-- create tables and seed initial data for broker manager

### does not run this is a copilation of notes.

SELECT current_database() AS db, current_schema() AS schema;
-- if schema isn’t public:
SET search_path = public;

-- brokers
CREATE TABLE IF NOT EXISTS public.brokers (
  broker_id       text PRIMARY KEY,
  display_name    text NOT NULL,
  api_base        text,
  kind            text,
  characteristics jsonb DEFAULT '{}'::jsonb,
  flags           jsonb DEFAULT '{}'::jsonb,
  notes           text,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);

-- broker_accounts
CREATE TABLE IF NOT EXISTS public.broker_accounts (
  account_id      text PRIMARY KEY,
  broker_id       text NOT NULL REFERENCES public.brokers(broker_id) ON DELETE CASCADE,
  account_code    text,
  label           text,
  class           text,
  pdt_applies     boolean DEFAULT true,
  pdt_restricted  boolean DEFAULT false,
  allow_short     boolean DEFAULT true,
  margin_rate_bps integer,
  commissions     jsonb DEFAULT '{}'::jsonb,
  flags           jsonb DEFAULT '{}'::jsonb,
  notes           text,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);

-- credentials (encrypted key/values for broker or account scope)
CREATE TABLE IF NOT EXISTS public.credentials (
  cred_id      bigserial PRIMARY KEY,
  scope_type   text NOT NULL CHECK (scope_type IN ('broker','account')),
  scope_id     text NOT NULL,
  key          text NOT NULL,
  value_cipher bytea NOT NULL,
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now()
);




-- Brokers
INSERT INTO brokers (broker_id, display_name, kind, api_base, characteristics) VALUES
  ('alpaca','Alpaca','retail','https://api.alpaca.markets','{"rate_limits":{"orders_per_min":200}}'),
  ('ib','Interactive Brokers','pro',NULL,'{"notes":"TWS/Gateway"}'),
  ('schwab','Charles Schwab','retail',NULL,'{}'),
  ('webull','Webull','retail',NULL,'{}'),
  ('lightspeed','Light Speed','retail',NULL,'{}'),
  ('sim','Simulator','sim',NULL,'{}')
ON CONFLICT (broker_id) DO NOTHING;

-- Accounts
INSERT INTO broker_accounts (account_id, broker_id, account_code, label, class,
                             pdt_applies, pdt_restricted, allow_short, flags, notes)
VALUES
  ('alpaca:paper','alpaca','PAPER-ACCT','Alpaca Paper','margin', TRUE,  FALSE, TRUE, '{}'::jsonb, 'paper trading'),
  ('alpaca:live' ,'alpaca','LIVE-ACCT' ,'Alpaca Live' ,'margin', TRUE,  FALSE, TRUE, '{}'::jsonb, 'live trading'),
  ('sim:paper'   ,'sim'   ,'SIM'       ,'Simulator Paper','cash', FALSE, FALSE, TRUE, '{"starting_cash":30000}'::jsonb,
   'seeded starting cash for simulated account')
ON CONFLICT (account_id) DO NOTHING;


REFLEX__FERNET_KEY=WtfyYYj5R4UxVA50uxELXxGRhpiC4NwTK2WpcOVU2p8=


YOUR_ALPACA_PAPER_SECRET=4ZYWtNSUgzWdw7b9CkhujxwYCMHsuyTzpkHLSgvrtfgS
YOUR_ALPACA_PAPER_KEY=PKVZ6RI32JSZ775Q52CPLMRFSE    
YOUR_ALPACA_PAPER_BASEURL=https://paper-api.alpaca.markets
YOUR_ALPACA_LIVE_SECRET=DzqiRaPsyZRtJmuGMNhrJDWXcUcTUaADMFUEvLYe9fLa
YOUR_ALPACA_LIVE_KEY=AKOC266JG5KTVKOMLNVN7LZPSM
YOUR_ALPACA_LIVE_BASEURL=https://api.alpaca.markets

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_KEY"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
$FERNET_TOKEN_PAPER_KEY = $PLAINTEXT


$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_LIVE_KEY  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_KEY=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_PAPER_SECRET  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_SECRET"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_PAPER_BASEURL  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_LIVE_BASEURL  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_LIVE_KEY  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_KEY=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_PAPER_SECRET  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_LIVE_SECRET  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT =YOUR_ALPACA_PAPER_BASEURL  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = YOUR_ALPACA_LIVE_BASEURL  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_BASEURL=$PLAINTEXT


-- Credentials (Fernet tokens pasted; converted to std Base64 for decode)
-- PAPER
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  ('account','alpaca:paper','api_key',
     decode(translate('FERNET_TOKEN_PAPER_KEY','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_PAPER_KEY') % 4) % 4),'base64')),
  ('account','alpaca:paper','api_secret',
     decode(translate('FERNET_TOKEN_PAPER_SECRET','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_PAPER_SECRET') % 4) % 4),'base64')),
  ('account','alpaca:paper','base_url',
     decode(translate('FERNET_TOKEN_PAPER_BASEURL','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_PAPER_BASEURL') % 4) % 4),'base64'))
ON CONFLICT DO NOTHING;

-- LIVE
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  ('account','alpaca:live','api_key',
     decode(translate('FERNET_TOKEN_LIVE_KEY','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_LIVE_KEY') % 4) % 4),'base64')),
  ('account','alpaca:live','api_secret',
     decode(translate('FERNET_TOKEN_LIVE_SECRET','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_LIVE_SECRET') % 4) % 4),'base64')),
  ('account','alpaca:live','base_url',
     decode(translate('FERNET_TOKEN_LIVE_BASEURL','-_','+/') || repeat('=', (4 - length('FERNET_TOKEN_LIVE_BASEURL') % 4) % 4),'base64'))
ON CONFLICT DO NOTHING;
