from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

class Settings:
    API_TITLE = "BrokerView API"
    API_VERSION = "0.5.1"

    BROKERVIEW_PORT = int(os.getenv("BROKERVIEW_PORT", "7010"))
    TRADER_API_PORT = int(os.getenv("TRADER_API_PORT", "7002"))

    ALPACA_PAPER_ACCOUNT_ID = "alpaca:paper"
    ALPACA_LIVE_ACCOUNT_ID  = "alpaca:live"
    SIM_MARGIN_ACCOUNT_ID   = "sim:margin"

    ALPACA_PAPER_BASE = os.getenv("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")
    ALPACA_LIVE_BASE  = os.getenv("ALPACA_LIVE_BASE", "https://api.alpaca.markets")
    ALPACA_PAPER_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "")
    ALPACA_PAPER_SECRET = os.getenv("ALPACA_API_SECRET_KEY", "")
    ALPACA_LIVE_KEY_ID  = os.getenv("ALPACA_LIVE_API_KEY_ID", "")
    ALPACA_LIVE_SECRET  = os.getenv("ALPACA_LIVE_API_SECRET_KEY", "")

    PG_DSN = (
        os.getenv("BROKER_DATABASE_URL") or
        os.getenv("REFLEX_BROKER_DSN")   or
        os.getenv("REFLEX_PG_DSN")       or
        "postgresql://postgres:postgres@localhost:5432/brokers"
    )

    INSTANCE = os.getenv("INSTANCE", "liveA")
    GARNET_URL = os.getenv("GARNET_URL", "redis://127.0.0.1:6379/0")

settings = Settings()
