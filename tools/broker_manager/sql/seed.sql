-- brokers DB — canonical seed (v2025-11-09)

-- 1) Brokers
INSERT INTO public.brokers (broker_id, display_name, api_base, kind) VALUES
  ('alpaca','Alpaca','https://api.alpaca.markets','retail'),
  ('ib','Interactive Brokers','http://localhost:7497','pro'),
  ('schwab','Charles Schwab','https://api.schwab.com','retail'),
  ('webull','Webull','https://quotes-gw.webullfintech.com','retail'),
  ('lightspeed','Light Speed','https://rest.lightspeed.com','retail'),
  ('sim','Simulator','http://127.0.0.1:7002','sim')
ON CONFLICT (broker_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    api_base     = EXCLUDED.api_base,
    kind         = EXCLUDED.kind;

-- 2) Accounts (booleans + flags JSONB)
--    Per-account URL override goes in flags->'base_url'
INSERT INTO public.broker_accounts
  (account_id, broker_id, account_code, label, active, margin, paper, flags)
VALUES
  -- Alpaca: paper + live (paper uses its own base URL)
  ('alpaca:paper','alpaca','PAPER-ACCT','Alpaca Paper', TRUE, TRUE,  TRUE,
     '{"base_url":"https://paper-api.alpaca.markets","pdt_applies":true,"pdt_restricted":false,"allow_short":true}'::jsonb),
  ('alpaca:live' ,'alpaca','LIVE-ACCT' ,'Alpaca Live',  TRUE, TRUE,  FALSE,
     '{"pdt_applies":true,"pdt_restricted":false,"allow_short":true}'::jsonb),

  -- Simulator: cash + margin
  ('sim:cash'   ,'sim','SIM-CASH'  ,'Simulator Cash',   TRUE, FALSE, TRUE,
     '{"starting_cash":30000,"allow_short":false,"pdt_applies":false,"pdt_restricted":false}'::jsonb),
  ('sim:margin' ,'sim','SIM-MARGIN','Simulator Margin', TRUE, TRUE,  TRUE,
     '{"starting_cash":30000,"allow_short":true,"pdt_applies":false,"pdt_restricted":false}'::jsonb)
ON CONFLICT (account_id) DO UPDATE
SET broker_id    = EXCLUDED.broker_id,
    account_code = EXCLUDED.account_code,
    label        = EXCLUDED.label,
    active       = EXCLUDED.active,
    margin       = EXCLUDED.margin,
    paper        = EXCLUDED.paper,
    flags        = EXCLUDED.flags;

-- 3) Example: wipe old Alpaca creds (idempotent)
DELETE FROM public.credentials
WHERE scope_type='account' AND scope_id IN ('alpaca:paper','alpaca:live');

-- 4) Insert encrypted creds (replace placeholders with your Fernet ciphertexts)
--    Use:  python - <<'PY'\nfrom cryptography.fernet import Fernet; import base64,sys; key=sys.argv[1].encode(); f=Fernet(key); print(f.encrypt(b'VALUE').decode())\nPY  <FERNET_KEY>
--    Then wrap with decode('<base64>','base64')

-- Insert encrypted creds (Fernet tokens as bytea)
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  ('account','alpaca:paper','api_key_id', decode('gAAAAABpEN5Qe2I4YUa4oPuftYeHEjkS3UfycppzhlSby+6K/j9Y0rmCKRxbtm3ZpxI+U7OBfViGsE0tGlmUpCv+3rVCgQidC4jd7xlD7ShZ8AcLke41OYc=','base64')),
  ('account','alpaca:paper','api_secret', decode('gAAAAABpEN5Q5gnSHJY/Ko0R/2qmlNi7RsToDedmxrMLw40c/xBdJdOnYCZKqrM7MgHy6O2rlNQc0fB7Wzagr6Ao9TtGPRJdTB/NuAkE+t/OqmSncS+SOriIaefS8tHuBIU7commMNAE','base64')),
  ('account','alpaca:live','api_key_id',  decode('gAAAAABpEN5QdyIZf2iIDwjrROC4OvzmLLYXGfuugAdRX2sswrrk0rmE2ywEE5dBMlbXsFIMsFoBIFc7ckWIItCnK0fAany0FTd8Nujtjn26ykGq3jc7IWk=','base64')),
  ('account','alpaca:live','api_secret',  decode('gAAAAABpEN5QCByAkmjgOsVUE16ogsIZ7/ISNOKsK84s408RaAsSNYCzvioJsfhP93bMrdEGfURzAMDW8pwy0xeFW40tCjusb3EOPVk+2l81JH9z9PoV0GM8j7bJFUiBK8WOEjJF3ZFA','base64'));


-- Show effective URL per account (override via settings->base_url else broker.api_base)
SELECT a.account_id,
       a.label,
       a.broker_id,
       COALESCE(a.settings->>'base_url', b.api_base) AS effective_url,
       a.margin, a.paper, a.active
FROM public.broker_accounts a
JOIN public.brokers b USING (broker_id)
ORDER BY a.account_id;

-- Confirm Alpaca creds are present
SELECT scope_id,
       bool_or(key='api_key_id') AS has_key_id,
       bool_or(key='api_secret') AS has_secret,
       COUNT(*) AS total_items
FROM public.credentials
WHERE scope_type='account' AND scope_id IN ('alpaca:paper','alpaca:live')
GROUP BY scope_id
ORDER BY scope_id;

-- Peek at simulator starting cash knob
SELECT account_id, settings->>'starting_cash' AS starting_cash
FROM public.broker_accounts
WHERE account_id IN ('sim:cash','sim:margin')
ORDER BY account_id;
