# put your key in the environment for this shell
$env:REFLEX__FERNET_KEY = 'WtfyYYj5R4UxVA50uxELXxGRhpiC4NwTK2WpcOVU2p8='
Helper to encrypt any plaintext:

powershell
Copy code
function Encrypt-Fernet([string]$plaintext) {
  $code = @"
import os, sys
from cryptography.fernet import Fernet
key = os.environ['REFLEX__FERNET_KEY']
print(Fernet(key).encrypt(sys.argv[1].encode()).decode())
"@
  python -c $code --% $plaintext
}



-- paper
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  ('account','alpaca:paper','api_key',
     decode(translate('gAAAAABpDf2vq5tK5b6HyrY3QINdOcq9vpU9l2u9Eew1DksNMQyyFrrHZFA73JLBd5EqECrgQhcDM1cfWU5JYR8xQ8-9WzJgTw==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2vq5tK5b6HyrY3QINdOcq9vpU9l2u9Eew1DksNMQyyFrrHZFA73JLBd5EqECrgQhcDM1cfWU5JYR8xQ8-9WzJgTw==') % 4) % 4),'base64')),
  ('account','alpaca:paper','api_secret',
     decode(translate('gAAAAABpDf2wVMOx_J5exYLh1zo89vlhLv8gYl7YrO7iwEz_xaQhxFwKZH3pueinWHsDuDTpYl4YO12k7roKQku47iF4sOFaaQ==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2wVMOx_J5exYLh1zo89vlhLv8gYl7YrO7iwEz_xaQhxFwKZH3pueinWHsDuDTpYl4YO12k7roKQku47iF4sOFaaQ==') % 4) % 4),'base64')),
  ('account','alpaca:paper','base_url',
     decode(translate('gAAAAABpDf2wm_l0ddCaVri1YKQ_qhxQBz_uKvb6f_XTGhDmoeirPDKLbyP4rtmVDetDHFSBVbpeiDl1WRdQiOOUyRQ4WXU8nQ==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2wm_l0ddCaVri1YKQ_qhxQBz_uKvb6f_XTGhDmoeirPDKLbyP4rtmVDetDHFSBVbpeiDl1WRdQiOOUyRQ4WXU8nQ==') % 4) % 4),'base64'))
ON CONFLICT DO NOTHING;

-- LIVE
INSERT INTO credentials (scope_type, scope_id, key, value_cipher) VALUES
  ('account','alpaca:live','api_key',
     decode(translate('gAAAAABpDf2wpNkfLIBUf2BKs_S978PfxflvppJL8_0AbW5l9RkTb3fUIGBuWtsXC1OArnwMjoMSuPDdf4Pc9_aROpb_Zn2-Hw==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2wpNkfLIBUf2BKs_S978PfxflvppJL8_0AbW5l9RkTb3fUIGBuWtsXC1OArnwMjoMSuPDdf4Pc9_aROpb_Zn2-Hw==') % 4) % 4),'base64')),
  ('account','alpaca:live','api_secret',
     decode(translate('gAAAAABpDf2wD_F-Qsv3Hm2AHKAbw0WeFWDwlDQyV8-AVNeJh_luBtNNLx6TPjscKDLAPI-nIxk9EzZgJl_847pGLzGU79DeKw==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2wD_F-Qsv3Hm2AHKAbw0WeFWDwlDQyV8-AVNeJh_luBtNNLx6TPjscKDLAPI-nIxk9EzZgJl_847pGLzGU79DeKw==') % 4) % 4),'base64')),
  ('account','alpaca:live','base_url',
     decode(translate('gAAAAABpDf2weHUYtSg32S0d8L5gmkO_jPv2-gJfAcQVPQOdkSdy_9Dp67WQiQKLc6ggCKh6VxOMW-dE3F_4o71RaIaAs_rZGQ==','-_','+/') ||
            repeat('=', (4 - length('gAAAAABpDf2weHUYtSg32S0d8L5gmkO_jPv2-gJfAcQVPQOdkSdy_9Dp67WQiQKLc6ggCKh6VxOMW-dE3F_4o71RaIaAs_rZGQ==') % 4) % 4),'base64'))
ON CONFLICT DO NOTHING;
















E) Quick verification (still in pgAdmin)
SELECT broker_id, display_name, kind FROM brokers ORDER BY broker_id;

SELECT account_id, broker_id, label, class, account_code, flags
FROM broker_accounts
ORDER BY broker_id, account_id;

SELECT scope_type, scope_id, key, octet_length(value_cipher) AS bytes
FROM credentials
WHERE scope_id IN ('alpaca:paper','alpaca:live')
ORDER BY 1,2,3;