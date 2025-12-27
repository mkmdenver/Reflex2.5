REFLEX__FERNET_KEY=WtfyYYj5R4UxVA50uxELXxGRhpiC4NwTK2WpcOVU2p8=

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_KEY"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_KEY=$PLAINTEXT


$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_KEY"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_KEY=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_SECRET"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_SECRET"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_BASEURL"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_BASEURL"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_KEY"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_KEY=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_SECRET"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_SECRET"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_SECRET=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_PAPER_BASEURL"  # or SECRET or https://paper-api...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_PAPER_BASEURL=$PLAINTEXT

$env:K = $env:REFLEX__FERNET_KEY
$PLAINTEXT = "YOUR_ALPACA_LIVE_BASEURL"  # or SECRET or https://api.alpaca...
.\.venv\Scripts\python.exe -c "import os,sys; from cryptography.fernet import Fernet as F; print(F(os.environ['K']).encrypt(sys.argv[1].encode()).decode())" --% $PLAINTEXT
FERNET_TOKEN_LIVE_BASEURL=$PLAINTEXT


