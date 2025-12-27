# finviz_fetch.py
import os
import sys
import time
import re
from typing import List, Optional, Dict, Tuple, Literal, Union


from finvizfinance.screener.overview import Overview
from finvizfinance.screener.ownership import Ownership
import pandas as pd

# Set up logging
import logging
log = logging.getLogger("finviz_fetch")
log.setLevel(logging.INFO)

# ---- BOOTSTRAP (works from batch or `python -m`) --------------------------------
_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
_COMMON_DIR = os.path.join(_PROJECT_ROOT, "common")
for path in [_PROJECT_ROOT, _COMMON_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)
# ----------------------------------------------------------------------------------

from common.dbLayer.db import ExecuteSQLBlock

# ------------------ Constants / Validation ------------------
VALID_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
DEFAULT_MARKET_CAP = "Small ($300mln to $2bln)"
DEFAULT_PRICE = "Over $1"

TIER_MAP = {"cold": 0, "watch": 1, "warm": 2, "hot": 3}


def _validate_exchanges(exchanges: List[str]) -> List[str]:
    bad = [e for e in exchanges if e not in VALID_EXCHANGES]
    if bad:
        log.warning("Unknown exchanges ignored: %s", bad)
    return [e for e in exchanges if e in VALID_EXCHANGES]


def _norm_tier(val: Union[str, int, None], *, default: int = 0) -> int:
    """
    Normalize a tier value to an int in [0,3].
    Accepts 'cold'/'watch'/'warm'/'hot' or integers 0..3. None -> default.
    """
    if val is None:
        return default
    if isinstance(val, int):
        if 0 <= val <= 3:
            return val
        log.warning("Tier int out of range (0..3). Using default=%s; got=%s", default, val)
        return default
    if isinstance(val, str):
        m = TIER_MAP.get(val.strip().lower())
        if m is not None:
            return m
        # If string looks like an int
        s = val.strip()
        if s.isdigit():
            try:
                iv = int(s)
                if 0 <= iv <= 3:
                    return iv
            except Exception:
                pass
        log.warning("Unknown tier string '%s'. Using default=%s", val, default)
        return default
    log.warning("Unsupported tier type (%s). Using default=%s", type(val), default)
    return default


# ------------------ Fetch (finvizfinance) ------------------
def _fetch_view(cls, filters: Dict[str, str], view_name: str, sleep_s: float) -> pd.DataFrame:
    """
    Call finvizfinance view and return a DataFrame (or empty DF).
    """
    try:
        view = cls()
        view.set_filter(filters_dict=filters)
        df = view.screener_view()
        if df is None:
            log.warning("[%s] returned None for filters=%s", view_name, filters)
            return pd.DataFrame()
        df = pd.DataFrame(df).drop_duplicates()
        return df
    except Exception as e:
        log.error("[%s] failed (filters=%s): %s", view_name, filters, e)
        return pd.DataFrame()
    finally:
        if sleep_s > 0:
            time.sleep(sleep_s)


def fetch_finviz(
    exchanges: List[str] = ["NASDAQ", "NYSE", "AMEX"],
    *,
    market_cap: str = DEFAULT_MARKET_CAP,
    price: str = DEFAULT_PRICE,
    extra_filters: Optional[Dict[str, str]] = None,
    sleep_between_calls_s: float = 0.25,
    join_how: Literal["outer", "inner", "left", "right", "cross", "left_anti", "right_anti"] = "outer",
) -> pd.DataFrame:
    """
    Fetch Overview + Ownership frames per exchange and merge on Ticker.
    Emits a wide DataFrame with these normalized columns added:
      - symbol (upper), name (Company), source_exchange
      - plus original prefixed columns: overview_*, ownership_*
    """
    exchanges = _validate_exchanges(exchanges)
    if not exchanges:
        log.error("No valid exchanges provided.")
        return pd.DataFrame()

    all_dfs: List[pd.DataFrame] = []

    for exch in exchanges:
        filters = {"Exchange": exch, "Market Cap.": market_cap, "Price": price}
        if extra_filters:
            filters.update(extra_filters)

        log.info("[Finviz] Fetching Overview + Ownership for %s ...", exch)
        df_over = _fetch_view(Overview, filters, "Overview", sleep_between_calls_s)
        if df_over.empty:
            log.warning("[Finviz] Overview returned 0 rows for %s", exch)
            continue
        if "Ticker" not in df_over.columns:
            log.warning("[Finviz] Overview missing 'Ticker' for %s (columns=%s)", exch, list(df_over.columns))
            continue
        df_over = df_over.rename(columns=lambda c: f"overview_{c}")

        df_own = _fetch_view(Ownership, filters, "Ownership", sleep_between_calls_s)
        if not df_own.empty:
            if "Ticker" not in df_own.columns:
                log.warning("[Finviz] Ownership missing 'Ticker' for %s", exch)
                df_own = pd.DataFrame()
            else:
                df_own = df_own.rename(columns=lambda c: f"ownership_{c}")

        # Merge (outer by default keeps more)
        if df_own.empty:
            df = df_over.copy()
        else:
            df = pd.merge(
                df_over,
                df_own,
                left_on="overview_Ticker",
                right_on="ownership_Ticker",
                how=str(join_how),
            )
            df.drop(columns=["ownership_Ticker"], inplace=True, errors="ignore")

        df.rename(columns={"overview_Ticker": "symbol"}, inplace=True)
        df["symbol"] = df["symbol"].astype(str).str.upper()
        # Best-effort company name
        df["name"] = df.get("overview_Company")
        # Best-effort exchange preference: Overview.Exchange > provided 'exch'
        df["source_exchange"] = (df.get("overview_Exchange") if "overview_Exchange" in df.columns else exch)
        all_dfs.append(df)
        log.info("[Finviz] Merged %d rows for %s", len(df), exch)

    if not all_dfs:
        log.error("[Finviz] No data returned from any exchange.")
        return pd.DataFrame()

    out = pd.concat(all_dfs, ignore_index=True)
    # Deduplicate by symbol+source_exchange
    out = out.sort_values(["symbol"]).drop_duplicates(subset=["symbol", "source_exchange"], keep="first")
    return out


# ------------------ Parsing helpers (string -> typed) ------------------
_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}


def _parse_num_with_suffix(s: Optional[str]) -> Optional[float]:
    """
    Parse '1.2M' => 1200000.0, '950K' => 950000, '123' => 123.0, None/'' => None
    """
    if s is None:
        return None
    txt = str(s).strip().replace(",", "")
    if txt == "" or txt.upper() in {"N/A", "-"}:
        return None
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)([KMBT])?", txt, flags=re.IGNORECASE)
    if not m:
        try:
            return float(txt)
        except Exception:
            return None
    val = float(m.group(1))
    suf = m.group(2)
    if suf:
        val *= _SUFFIX[suf.upper()]
    return val


def _parse_percent(s: Optional[str]) -> Optional[float]:
    """
    '12.3%' => 12.3   (not 0.123)
    """
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t == "" or t.upper() in {"N/A", "-"}:
        return None
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t)
    except Exception:
        return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    val = _parse_num_with_suffix(s)
    if val is None:
        return None
    try:
        return int(round(val))
    except Exception:
        return None


def _parse_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t == "" or t.upper() in {"N/A", "-"}:
        return None
    try:
        return float(t)
    except Exception:
        return None


# ------------------ Hydration (meta-centric) ------------------


def hydrate_fundamental_metadata(df: pd.DataFrame) -> int:
    """
    Upsert into fundamental_data:
      symbol (PK), company, sector, industry, country, exchange,
      market_cap, pe_ratio, shares_float, float_percent (nullable),
      short_float, average_true_range (nullable), last_updated = NOW()
    """
    if df.empty:
        return 0

    d = df.copy()
    d.columns = [c.lower() for c in d.columns]

    payload: List[Tuple] = []
    for _, row in d.iterrows():
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue

        company   = row.get("name") or row.get("overview_company")
        sector    = row.get("overview_sector")
        industry  = row.get("overview_industry")
        country   = row.get("overview_country")
        exchange  = row.get("overview_exchange") or row.get("source_exchange")

        # Finviz keys after lowercasing: 'overview_market cap', 'overview_p/e', 'overview_atr',
        # 'ownership_float', 'ownership_short float'
        market_cap         = _parse_num_with_suffix(row.get("overview_market cap"))
        pe_ratio           = _parse_float(row.get("overview_p/e"))
        shares_float       = _parse_int(row.get("ownership_float"))
        short_float        = _parse_percent(row.get("ownership_short float"))
        float_percent      = None  # Finviz doesn't expose this directly here
        average_true_range = _parse_float(row.get("overview_atr"))

        payload.append((
            symbol, company, sector, industry, country, exchange,
            market_cap, pe_ratio, shares_float, float_percent, short_float, average_true_range
        ))

    if not payload:
        return 0

    stmt = """
        INSERT INTO fundamental_data
          (symbol, company, sector, industry, country, exchange,
           market_cap, pe_ratio, shares_float, float_percent, short_float, average_true_range, last_updated)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (symbol) DO UPDATE SET
          company             = COALESCE(EXCLUDED.company,             fundamental_data.company),
          sector              = COALESCE(EXCLUDED.sector,              fundamental_data.sector),
          industry            = COALESCE(EXCLUDED.industry,            fundamental_data.industry),
          country             = COALESCE(EXCLUDED.country,             fundamental_data.country),
          exchange            = COALESCE(EXCLUDED.exchange,            fundamental_data.exchange),
          market_cap          = COALESCE(EXCLUDED.market_cap,          fundamental_data.market_cap),
          pe_ratio            = COALESCE(EXCLUDED.pe_ratio,            fundamental_data.pe_ratio),
          shares_float        = COALESCE(EXCLUDED.shares_float,        fundamental_data.shares_float),
          float_percent       = COALESCE(EXCLUDED.float_percent,       fundamental_data.float_percent),
          short_float         = COALESCE(EXCLUDED.short_float,         fundamental_data.short_float),
          average_true_range  = COALESCE(EXCLUDED.average_true_range,  fundamental_data.average_true_range),
          last_updated        = NOW()
    """
    affected = ExecuteSQLBlock(stmt, payload)
    log.info("[Finviz] Upserted %s rows into 'fundamental_data'.",
             affected if (affected and affected >= 0) else len(payload))
    return len(payload)


def hydrate_symbol_metadata(
    df: pd.DataFrame,
    *,
    default_db_tier: Union[int, str] = 0,
    default_rt_tier: Union[int, str] = 0,
) -> int:
    """
    Upsert into symbol_metadata with (db_tier, rt_tier).
    - Tiers accept ints 0..3 or strings 'cold'|'watch'|'warm'|'hot'
    - Defaults apply when inputs are missing/invalid
    """
    if df.empty:
        return 0

    d = df.copy()
    d.columns = [c.lower() for c in d.columns]

    db_default = _norm_tier(default_db_tier, default=0)
    rt_default = _norm_tier(default_rt_tier, default=0)

    payload: List[Tuple[str, int, int]] = []
    seen = set()

    for _, row in d.iterrows():
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        # Allow caller to pass columns 'db_tier'/'rt_tier' in the frame; otherwise use defaults
        row_db = row.get("db_tier")
        row_rt = row.get("rt_tier")
        db_tier = _norm_tier(row_db, default=db_default)
        rt_tier = _norm_tier(row_rt, default=rt_default)
        payload.append((symbol, db_tier, rt_tier))

    if not payload:
        return 0

    stmt = """
        INSERT INTO symbol_metadata (symbol, db_tier, rt_tier, last_updated)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (symbol) DO UPDATE SET
          db_tier      = COALESCE(EXCLUDED.db_tier, symbol_metadata.db_tier),
          rt_tier      = COALESCE(EXCLUDED.rt_tier, symbol_metadata.rt_tier),
          last_updated = NOW()
    """
    affected = ExecuteSQLBlock(stmt, payload)
    log.info(
        "[Finviz] Upserted %s rows into 'symbol_metadata' (db_tier/rt_tier).",
        affected if (affected and affected >= 0) else len(payload),
    )
    return len(payload)

