# trader/trade_runner.py
# v2.2.0 — Soft-exit-first TradeRunner (premarket/postmarket safe)
#
# Philosophy (matches system spec):
#   - Normal operation = *soft exits* driven by ticks/NBBO (especially premarket).
#   - Broker stops may be used sparingly as an extra layer, but are NOT required.
#   - On entry fill, compute stop + target immediately (default rule: +10c / -5c).
#   - When price crosses stop/target, submit a marketable LIMIT exit.
#   - If the limit doesn't fill quickly (or gets blown past), cancel + re-place
#     at a more aggressive marketable price.
#
# This file is intentionally KISS: minimal state machine, no fancy abstractions.

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

log = logging.getLogger("trader.trade_runner")

# Runtime sanity check: ensure you are editing the file that is actually executing.
print("### TRADE_RUNNER LOADED FROM:", __file__)


# DataHub tier control (Trader -> DataHub): ensure symbols we are trading are
# subscribed for ticks/quotes so soft exits can actually trigger.
try:
    from common.bus import CHANNELS, publisher, pack
except Exception:  # pragma: no cover
    CHANNELS = {}  # type: ignore
    publisher = None  # type: ignore
    pack = None  # type: ignore


def _truthy(name: str, default: str = "0") -> bool:
    v = str(os.getenv(name, default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _now() -> float:
    return time.time()


def _ts_fields() -> Dict[str, Any]:
    # Use wall clock + ns ordering; include UTC and ET for humans.
    ts = _now()
    ts_ns = int(ts * 1_000_000_000)
    try:
        ts_ns = time.time_ns()
    except Exception:
        pass

    iso_utc = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # NY time for logs (ET). If zoneinfo missing, fall back to UTC.
    try:
        if ZoneInfo is not None:
            iso_et = datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).isoformat()
        else:
            iso_et = iso_utc
    except Exception:
        iso_et = iso_utc

    return {"ts": ts, "ts_ns": ts_ns, "iso_utc": iso_utc, "iso_et": iso_et}


def _ensure_parent_dir(path: str) -> None:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass


def _safe_round(x: float) -> float:
    # Two decimals is fine for most equities; Alpaca supports up to 2 for many.
    try:
        return float(f"{float(x):.2f}")
    except Exception:
        return float(x)


def _side_kind(side: str) -> str:
    s = (side or "").lower()
    if s in ("sell", "short"):
        return "short"
    return "long"


def _session_kind_now() -> str:
    # Best effort; caller already has session in trade dict most times.
    # We don't want dependencies here; use env flag if you want forced.
    return os.getenv("MARKET_SESSION") or "RTH"


def _exit_client_id(trade_id: str, reason: str, seq: int) -> str:
    # Stable prefix so UI can group orders for a trade
    base = (trade_id or "trade").strip()
    return f"{base}:exit:{reason}:{seq}"


def _marketable_limit_price(kind: str, side: str, lt: Optional[float], bid: Optional[float], ask: Optional[float], aggress_cents: float) -> Optional[float]:
    """Compute a marketable LIMIT price to exit quickly."""
    ag = aggress_cents / 100.0

    # For sell: make limit <= bid so it can cross. For buy: limit >= ask.
    if side == "sell":
        px = bid or lt
        if px is None:
            return None
        return max(0.01, float(px) - ag)

    # buy
    px = ask or lt
    if px is None:
        return None
    return max(0.01, float(px) + ag)

def _decision_price(lt: Optional[float], bid: Optional[float], ask: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (decision_price, mid) for logging/slippage attribution.

    decision_price prefers last trade (lt). If missing, uses mid when possible,
    else falls back to bid/ask.
    """
    mid = None
    if bid is not None and ask is not None:
        try:
            mid = (float(bid) + float(ask)) / 2.0
        except Exception:
            mid = None
    decision = lt
    if decision is None:
        decision = mid if mid is not None else (bid if bid is not None else ask)
    return decision, mid

def _median(values: list[float]) -> Optional[float]:
    try:
        vals = [float(v) for v in (values or []) if v is not None]
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        mid = n // 2
        if n % 2 == 1:
            return float(vals[mid])
        return float((vals[mid - 1] + vals[mid]) / 2.0)
    except Exception:
        return None


@dataclass
class _TSLFE:
    # Extremely simple TSLFE placeholder metrics engine:
    # We track time-to-breakeven (TBE) samples and a "temperature" that decays.
    eps_price: float = 0.01
    tbe_window: int = 25
    min_samples: int = 8
    flatten_th: float = 2.6
    profit_exit_norm_th: float = 2.0

    tbe_samples: list[float] = None  # type: ignore
    temperature: float = 0.0
    time_to_safety_s: float = 0.0
    time_underwater_s: float = 0.0
    chase_count: int = 0
    _last_ts: float = 0.0
    _last_px: float = 0.0
    _underwater_since: Optional[float] = None

    def __post_init__(self) -> None:
        if self.tbe_samples is None:
            self.tbe_samples = []

    def reset(self) -> None:
        self.tbe_samples = []
        self.temperature = 0.0
        self.time_to_safety_s = 0.0
        self.time_underwater_s = 0.0
        self.chase_count = 0
        self._last_ts = 0.0
        self._last_px = 0.0
        self._underwater_since = None

    def update(self, ts: float, px: float, entry_px: float) -> Dict[str, Any]:
        # Update timers
        if self._last_ts > 0:
            dt = max(0.0, ts - self._last_ts)
        else:
            dt = 0.0
        self._last_ts = ts
        self._last_px = px

        # Underwater detection for long trades (entry_px as baseline)
        if px < entry_px - self.eps_price:
            if self._underwater_since is None:
                self._underwater_since = ts
            self.time_underwater_s = max(self.time_underwater_s, ts - float(self._underwater_since or ts))
        else:
            self._underwater_since = None

        # Fake TBE: time since entry to get back above entry (not true without entry_ts)
        # Here we just accumulate a sample count
        if px >= entry_px + self.eps_price:
            self.tbe_samples.append(float(ts))
            if len(self.tbe_samples) > self.tbe_window:
                self.tbe_samples = self.tbe_samples[-self.tbe_window :]

        # "temperature" is just normalized sample count for now
        self.temperature = min(1.0, float(len(self.tbe_samples)) / float(max(1, self.min_samples)))

        tbe_med_s = None
        if len(self.tbe_samples) >= 1:
            # This is not real TBE; placeholder. Keep for instrumentation.
            # Caller stores in trade metrics for monitoring behavior.
            # We compute median inter-arrival as a proxy.
            diffs = []
            for i in range(1, len(self.tbe_samples)):
                diffs.append(self.tbe_samples[i] - self.tbe_samples[i - 1])
            tbe_med_s = _median(diffs)

        return {
            "tbe_n": int(len(self.tbe_samples)),
            "tbe_med_s": float(tbe_med_s) if tbe_med_s is not None else None,
            "temperature": float(self.temperature),
            "time_to_safety_s": float(self.time_to_safety_s),
            "time_underwater_s": float(self.time_underwater_s),
            "chase_count": int(self.chase_count),
        }


class TradeRunner:
    def __init__(
        self,
        *,
        store: Any,
        alerts: Any,
        adapters: Any,
        portfolio_manager: Any,
    ) -> None:
        self.store = store
        self.alerts = alerts
        self.adapters = adapters
        self.pm = portfolio_manager

        self.instance_id = os.getenv("REFLEX_INSTANCE_ID") or os.getenv("INSTANCE") or "liveA"
        self.mode = os.getenv("REFLEX_MODE") or "LIVE"

        self.exit_aggress_cents = float(os.getenv("TRADER_EXIT_AGGRESS_CENTS", "3.0") or 3.0)
        self.entry_aggress_cents = float(os.getenv("TRADER_ENTRY_AGGRESS_CENTS", "2.0") or 2.0)
        self.fill_timeout = float(os.getenv("TRADER_FILL_TIMEOUT_S", "20") or 20)
        self.exit_fill_timeout = float(os.getenv("TRADER_EXIT_FILL_TIMEOUT_S", "15") or 15)

        # Broker-side protection (optional but strongly recommended for live sanity)
        self.broker_protect_enabled = _truthy("TRADER_BROKER_PROTECT_ENABLED", "0")
        self.broker_protect_only_rth = _truthy("TRADER_BROKER_PROTECT_ONLY_RTH", "1")
        self.broker_protect_stop_type = (os.getenv("TRADER_BROKER_PROTECT_STOP_TYPE") or "stop").strip().lower()
        self.broker_protect_submit_take_profit = _truthy("TRADER_BROKER_PROTECT_TAKE_PROFIT", "0")
        self.broker_protect_tif = (os.getenv("TRADER_BROKER_PROTECT_TIF") or "day").strip().lower()

        self.enabled = _truthy("TRADER_TRADE_RUNNER_ENABLED", "1")

        # Independent, append-only order/event log (JSONL). Every state change writes a line.
        self._orders_log_path = (os.getenv("TRADER_ORDERS_LOG_PATH", "logs/orders.log") or "logs/orders.log").strip()
        _ensure_parent_dir(self._orders_log_path)

        # Auto tier raise so DataHub streams ticks/quotes for symbols in-trade.
        # Without this, md_worker won't see live ticks and soft exits won't trigger.
        self.tiers_cmd_q = os.getenv("TIERS_CMD_Q") or os.getenv("TIERS_CMD_Q") or ""
        self._tier_pub = None
        self._tier_pub_ready = False
        self._tier_raise_last: Dict[str, float] = {}  # symbol -> last ts

        # TSLFE settings
        self.tslfe_enabled = _truthy("TRADER_TSLFE_ENABLED", "0")
        self.tslfe_eps = float(os.getenv("TRADER_TSLFE_EPS_PRICE", "0.01") or 0.01)
        self.tslfe_tbe_window = int(os.getenv("TRADER_TSLFE_TBE_WINDOW", "25") or 25)
        self.tslfe_min_samples = int(os.getenv("TRADER_TSLFE_MIN_SAMPLES", "8") or 8)
        self.tslfe_flatten_th = float(os.getenv("TRADER_TSLFE_FLATTEN_TH", "2.6") or 2.6)
        self.tslfe_profit_exit_norm_th = float(os.getenv("TRADER_TSLFE_PROFIT_EXIT_NORM_TH", "2.0") or 2.0)

        self.exit_model_default = os.getenv("TRADER_EXIT_MODEL_DEFAULT") or "stop"
        self.metrics_emit_secs = float(os.getenv("TRADER_METRICS_EMIT_SECS", "2") or 2)

        # Per-trade state
        self._tslfe: Dict[str, _TSLFE] = {}  # trade_id -> engine

        # Rate-limits
        self._last_metrics_emit: Dict[str, float] = {}  # trade_id -> ts

        log.info(
            "TradeRunner init enabled=%s instance=%s mode=%s exit_aggr_cents=%.2f tslfe=%s",
            self.enabled,
            self.instance_id,
            self.mode,
            self.exit_aggress_cents,
            self.tslfe_enabled,
        )

    # -------------- Orders log / persistence ---------------------------------
    def _write_orders_log(self, t: Dict[str, Any], event_type: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Write one JSONL line per lifecycle event/state change."""
        try:
            rec: Dict[str, Any] = {}
            rec.update(_ts_fields())
            rec["event"] = event_type

            # Core identifiers
            rec["trade_id"] = t.get("trade_id")
            rec["state"] = t.get("state")
            rec["account_id"] = t.get("account_id")
            rec["symbol"] = t.get("symbol")
            rec["side_kind"] = (str(t.get("side") or "buy").lower() == "sell" and "short") or "long"
            rec["market_session"] = t.get("market_session")

            # Order identifiers (best effort)
            rec["entry_order_id"] = t.get("entry_order_id")
            rec["entry_client_order_id"] = t.get("entry_client_order_id")
            rec["exit_order_id"] = t.get("exit_order_id")
            rec["exit_client_order_id"] = t.get("exit_client_order_id")
            rec["exit_reason"] = t.get("exit_reason")
            rec["flatten_client_order_id"] = t.get("flatten_client_order_id")
            rec["entry_model"] = t.get("entry_model")
            rec["profit_model"] = t.get("profit_model")
            rec["stop_loss_model"] = t.get("stop_loss_model")
            rec["position_mgmt_model"] = t.get("position_mgmt_model")
            rec["exit_reason_hint"] = t.get("exit_reason_hint")

            # Economics / sizing
            rec["qty"] = t.get("qty")
            rec["entry_avg_price"] = t.get("entry_avg_price")
            rec["soft_stop_price"] = t.get("soft_stop_price")
            rec["soft_target_price"] = t.get("soft_target_price")
            rec["stop_price"] = t.get("stop_price")
            rec["take_profit_price"] = t.get("take_profit_price")

            # Timers
            rec["entry_submitted_ts"] = t.get("entry_submitted_ts")
            rec["entry_filled_ts"] = t.get("entry_filled_ts")
            rec["exit_submitted_ts"] = t.get("exit_submitted_ts")
            rec["entry_ack_ts"] = t.get("entry_ack_ts")
            rec["entry_first_fill_ts"] = t.get("entry_first_fill_ts")
            rec["entry_first_fill_price"] = t.get("entry_first_fill_price")
            rec["entry_fill_price"] = t.get("entry_fill_price")
            rec["entry_fill_latency_ms"] = t.get("entry_fill_latency_ms")
            rec["exit_ack_ts"] = t.get("exit_ack_ts")
            rec["exit_first_fill_ts"] = t.get("exit_first_fill_ts")
            rec["exit_first_fill_price"] = t.get("exit_first_fill_price")
            rec["exit_filled_ts"] = t.get("exit_filled_ts")
            rec["exit_fill_price"] = t.get("exit_fill_price")
            rec["exit_avg_fill_price"] = t.get("exit_avg_fill_price")
            rec["exit_filled_qty"] = t.get("exit_filled_qty")

            if extra:
                rec["extra"] = extra

            line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            with open(self._orders_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


    def _persist(self, t: Dict[str, Any], event_type: str, extra: Optional[Dict[str, Any]] = None) -> None:
        # Always write the independent log first so we capture even if downstream throws.
        self._write_orders_log(t, event_type, extra)

        try:
            self.journal_append("trade", t)
        except Exception:
            pass

        evt = {
            "type": event_type,
            "ts": _now(),
            "account_id": t.get("account_id"),
            "symbol": t.get("symbol"),
            "trade_id": t.get("trade_id"),
            "state": t.get("state"),
        }
        if extra:
            evt["extra"] = extra

        try:
            self.alerts.emit(event_type, **evt)
        except Exception:
            pass

    # -------------- Journaling / storage helpers -----------------------------

    def journal_append(self, kind: str, obj: Dict[str, Any]) -> None:
        try:
            self.store.journal_append(kind, obj)
        except Exception:
            pass

    def _snapshot_for_account(self, aid: str) -> Dict[str, Any]:
        try:
            return self.pm.get_snapshot(aid) or {}
        except Exception:
            return {}

    def _find_pos(self, snap: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        try:
            for p in (snap.get("positions") or []):
                if str(p.get("symbol")).upper() == str(symbol).upper():
                    return p
        except Exception:
            pass
        return None

    # -------------- DataHub tier raise ---------------------------------------

    async def _ensure_tier_pub(self) -> None:
        if self._tier_pub_ready:
            return
        if not self.tiers_cmd_q or not publisher or not pack:
            self._tier_pub_ready = True
            return
        try:
            self._tier_pub = publisher(self.tiers_cmd_q)
        except Exception:
            self._tier_pub = None
        self._tier_pub_ready = True

    async def _raise_symbol_tier(self, symbol: str) -> None:
        # Rate limit per symbol (1s)
        now = _now()
        last = float(self._tier_raise_last.get(symbol, 0.0))
        if (now - last) < 1.0:
            return
        self._tier_raise_last[symbol] = now

        await self._ensure_tier_pub()
        if not self._tier_pub:
            return

        try:
            msg = {"kind": "add", "symbol": str(symbol).upper(), "ts": now, "source": f"trader:{self.instance_id}"}
            self._tier_pub.publish(pack(msg))
        except Exception:
            pass

    # -------------- Market data ----------------------------------------------

    async def _read_md(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Return (lt, bid, ask) for decisioning.

        Primary: store.get_md(symbol) -> {lt,bid,ask}
        Fallback: PortfolioManager snapshots -> position.market_price (as lt)
                so soft stops/targets can still trigger even if md_worker is quiet.
        """
        try:
            md = self.store.get_md(symbol) or {}
            lt = _as_float(md.get("lt") or md.get("last") or md.get("price"))
            bid = _as_float(md.get("bid"))
            ask = _as_float(md.get("ask"))

            # If we have anything usable from md_worker, return it.
            if lt is not None or bid is not None or ask is not None:
                return lt, bid, ask
        except Exception:
            pass

        # Fallback: scan cached snapshots for a position with a market_price.
        try:
            snaps = self.pm.get_snapshots() or {}
            sym_u = str(symbol).upper()
            for _aid, snap in (snaps.items() if isinstance(snaps, dict) else []):
                try:
                    for p in (snap.get("positions") or []):
                        if str(p.get("symbol") or "").upper() != sym_u:
                            continue
                        px = _as_float(
                            p.get("market_price")
                            or p.get("current_price")
                            or p.get("last_price")
                            or p.get("price")
                        )
                        if px is not None:
                            return float(px), None, None
                except Exception:
                    continue
        except Exception:
            pass

        return None, None, None


    # -------------- Entry fill detection -------------------------------------

    async def _detect_entry_fill(self, aid: str, symbol: str, entry_cid: str, qty: float) -> Tuple[bool, Optional[float]]:
        """
        Detect entry fill by observing broker position move to expected direction.
        Returns (filled, fill_price_estimate).
        """
        try:
            snap = self._snapshot_for_account(aid)
            pos = self._find_pos(snap, symbol)
            if not pos:
                return False, None
            broker_qty = float(_as_float(pos.get("qty")) or 0.0)
            if abs(broker_qty) < 1e-9:
                return False, None

            # Use broker avg_price as fill proxy; this is best effort.
            px = _as_float(pos.get("avg_price")) or None
            return True, float(px) if px else None
        except Exception:
            return False, None


    async def _ensure_broker_protection(self, aid: str, adapter: Any, t: Dict[str, Any]) -> None:
        """Place (or confirm) broker-side protective orders after entry fill.

        Minimal behavior:
          - Protective STOP for full position qty (visible in BrokerView).
          - Optional TAKE PROFIT limit (default OFF; not true OCO).
        Never fails the trade on protection errors.
        """
        try:
            if not getattr(self, "broker_protect_enabled", False):
                return

            sess = str(t.get("market_session") or _session_kind_now())
            if getattr(self, "broker_protect_only_rth", True) and sess.upper() != "RTH":
                self._persist(t, "TRADE_PROTECT_SKIPPED", extra={"reason": "only_rth", "market_session": sess})
                return

            sym = str(t.get("symbol") or "").upper()
            if not sym:
                return

            snap = self._snapshot_for_account(aid)
            pos = self._find_pos(snap, sym)
            if not pos:
                return

            broker_qty = float(_as_float(pos.get("qty")) or 0.0)
            if abs(broker_qty) < 1e-9:
                return

            kind = _side_kind(str(t.get("side") or "buy"))
            protect_side = "sell" if kind == "long" else "buy"

            stop_px = _as_float(t.get("stop_price")) or _as_float(t.get("soft_stop_price"))
            tp_px = _as_float(t.get("take_profit_price")) or _as_float(t.get("soft_target_price"))
            if not stop_px or stop_px <= 0:
                return

            # Idempotency
            if t.get("protect_stop_order_id"):
                return

            stop_cid = f"{t.get('trade_id')}:protect:stop"
            payload = {
                "symbol": sym,
                "side": protect_side,
                "qty": abs(broker_qty),
                "type": (getattr(self, "broker_protect_stop_type", None) or "stop"),
                "time_in_force": (getattr(self, "broker_protect_tif", None) or "day"),
                "stop_price": float(_safe_round(float(stop_px))),
                "extended_hours": False,
                "note": f"protect_stop:trade_id={t.get('trade_id')}",
                "client_order_id": stop_cid,
            }

            res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
            t["protect_stop_order_id"] = res.get("id") or res.get("order_id") or stop_cid
            t["protect_stop_client_order_id"] = stop_cid
            t["protect_stop_submitted_ts"] = _now()
            t["stop_price"] = t["soft_stop_price"]
            t["take_profit_price"] = t["soft_target_price"]
            self._persist(t, "TRADE_PROTECT_STOP_SUBMITTED", extra={"stop_price": payload["stop_price"], "qty": payload["qty"], "order_id": t["protect_stop_order_id"]})

            if getattr(self, "broker_protect_submit_take_profit", False) and tp_px and tp_px > 0 and not t.get("protect_tp_order_id"):
                tp_cid = f"{t.get('trade_id')}:protect:tp"
                tp_payload = {
                    "symbol": sym,
                    "side": protect_side,
                    "qty": abs(broker_qty),
                    "type": "limit",
                    "time_in_force": (getattr(self, "broker_protect_tif", None) or "day"),
                    "limit_price": float(_safe_round(float(tp_px))),
                    "extended_hours": False,
                    "note": f"protect_tp:trade_id={t.get('trade_id')}",
                    "client_order_id": tp_cid,
                }
                tp_res = await _maybe_await(getattr(adapter, "place_order", None), **tp_payload)
                t["protect_tp_order_id"] = tp_res.get("id") or tp_res.get("order_id") or tp_cid
                t["protect_tp_client_order_id"] = tp_cid
                t["protect_tp_submitted_ts"] = _now()
                self._persist(t, "TRADE_PROTECT_TP_SUBMITTED", extra={"take_profit_price": tp_payload["limit_price"], "qty": tp_payload["qty"], "order_id": t["protect_tp_order_id"]})

        except Exception as e:
            self._persist(t, "TRADE_PROTECT_ERROR", extra={"error": str(e)})


    # -------------- Exit helpers ---------------------------------------------

    async def _submit_soft_exit(
        self,
        aid: str,
        symbol: str,
        adapter: Any,
        t: Dict[str, Any],
        kind: str,
        reason: str,
        lt: Optional[float],
        bid: Optional[float],
        ask: Optional[float],
        reason_hint: Optional[str] = None,
        current_pos_qty: float | None = None,
    ) -> None:

        # Requested trade size (what the strategy wanted)
        requested_qty = float(t.get("qty") or 0.0) or 0.0
        if requested_qty <= 0:
            requested_qty = 10.0
            t["qty"] = requested_qty

        # Current broker position size (truth). Use provided snapshot if available.
        broker_qty = float(current_pos_qty) if (current_pos_qty is not None) else None
        if broker_qty is None:
            try:
                snap_now = self._snapshot_for_account(aid)
                pos_now = self._find_pos(snap_now, symbol)
                if pos_now:
                    broker_qty = float(_as_float(pos_now.get("qty")) or 0.0)
            except Exception:
                broker_qty = None
        if broker_qty is None:
            broker_qty = 0.0

        # Reduce-only clamp: never send an exit order bigger than current exposure.
        exit_qty = min(abs(broker_qty), abs(requested_qty))
        if exit_qty <= 0:
            self._persist(
                t,
                "TRADE_EXIT_SKIPPED",
                extra={"reason": "no_position_to_exit", "broker_qty": broker_qty, "requested_qty": requested_qty},
            )
            return

        # Determine exit side from broker exposure to guarantee reduce-only semantics.
        exit_side = "sell" if broker_qty > 0 else "buy"
        seq = int(t.get("exit_seq") or 0)
        seq += 1
        t["exit_seq"] = seq
        cid = _exit_client_id(str(t.get("trade_id") or ""), reason, seq)
        t["exit_client_order_id"] = cid
        t["exit_reason"] = "SOFT_STOP" if reason == "stop" else "SOFT_TARGET"
        t["exit_reason_hint"] = reason_hint

        # Aggressiveness starts at configured cents
        t["exit_aggress_cents"] = float(self.exit_aggress_cents)

        limit_px = _marketable_limit_price(kind, exit_side, lt, bid, ask, float(t["exit_aggress_cents"]))
        if limit_px is None:
            return

        # Extended hours: use limit + day + extended_hours
        sess = str(t.get("market_session") or _session_kind_now())
        extended = sess in ("PRE", "POST")

        payload = {
            "symbol": symbol,
            "side": exit_side,
            "qty": exit_qty,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": _safe_round(float(limit_px)),
            "extended_hours": bool(extended),
            "note": f"soft_exit:{reason}:trade_id={t.get('trade_id')}",
            "client_order_id": cid,
        }

        try:
            res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
            oid = res.get("id") or res.get("order_id") or res.get("client_order_id") or cid
            t["exit_order_id"] = str(oid)
            t["exit_submitted_ts"] = _now()
            t["state"] = "EXIT_SUBMITTED"
            self._persist(
                t,
                "TRADE_EXIT_SUBMITTED",
                extra={
                    "exit_reason": t.get("exit_reason"),
                    "exit_reason_hint": t.get("exit_reason_hint"),
                    "exit_client_order_id": cid,
                    "exit_order_id": t.get("exit_order_id"),
                    "limit_price": payload["limit_price"],
                    "session": sess,
                    "requested_qty": requested_qty,
                    "broker_qty": broker_qty,
                    "exit_qty": exit_qty,
                    "exit_side": exit_side,
                    "md_lt": lt,
                    "md_bid": bid,
                    "md_ask": ask,
                    "decision_price": _decision_price(lt, bid, ask)[0],
                    "decision_mid": _decision_price(lt, bid, ask)[1],
                    "aggress_cents": float(t.get("exit_aggress_cents") or 0.0),
                    "exit_seq": int(t.get("exit_seq") or 0),
                },
            )
        except Exception as e:
            t["state"] = "ERROR"
            t["error"] = f"exit.place_order failed: {e}"
            self._persist(t, "TRADE_ERROR", extra={"reason": t["error"]})

    async def _chase_exit_if_needed(
        self,
        aid: str,
        symbol: str,
        adapter: Any,
        t: Dict[str, Any],
        kind: str,
        lt: Optional[float],
        bid: Optional[float],
        ask: Optional[float],
        current_pos_qty: float | None = None,
    ) -> None:
        # If exit has been working too long, cancel + replace more aggressively.
        try:
            if t.get("state") != "EXIT_SUBMITTED":
                return
            submitted = float(_as_float(t.get("exit_submitted_ts")) or 0.0)
            if not submitted:
                return
            if (_now() - submitted) < float(self.exit_fill_timeout):
                return
        except Exception:
            return

        # Cancel current exit order
        try:
            oid = t.get("exit_order_id")
            if oid:
                await _maybe_await(getattr(adapter, "cancel_order", None), str(oid))
                self._persist(t, "TRADE_EXIT_CANCELLED_FOR_CHASE", extra={"exit_order_id": oid})
        except Exception:
            pass

        # Compute next more aggressive limit and re-place with new client id
        requested_qty = float(t.get("qty") or 0.0) or 0.0
        if requested_qty <= 0:
            requested_qty = 10.0
            t["qty"] = requested_qty

        # broker qty truth
        broker_qty = float(current_pos_qty) if (current_pos_qty is not None) else 0.0
        if broker_qty == 0.0:
            try:
                snap_now = self._snapshot_for_account(aid)
                pos_now = self._find_pos(snap_now, symbol)
                if pos_now:
                    broker_qty = float(_as_float(pos_now.get("qty")) or 0.0)
            except Exception:
                broker_qty = broker_qty

        exit_qty = min(abs(broker_qty), abs(requested_qty))
        if exit_qty <= 0:
            return

        exit_side = "sell" if broker_qty > 0 else "buy"

        # Escalate aggressiveness
        ag = float(t.get("exit_aggress_cents") or float(self.exit_aggress_cents))
        ag = min(50.0, max(0.5, ag + 2.0))
        t["exit_aggress_cents"] = ag

        limit_px = _marketable_limit_price(kind, exit_side, lt, bid, ask, ag)
        if limit_px is None:
            return

        seq = int(t.get("exit_seq") or 0)
        seq += 1
        t["exit_seq"] = seq
        reason = "stop" if str(t.get("exit_reason") or "").upper().endswith("STOP") else "target"
        cid = _exit_client_id(str(t.get("trade_id") or ""), reason, seq)
        t["exit_client_order_id"] = cid

        sess = str(t.get("market_session") or _session_kind_now())
        extended = sess in ("PRE", "POST")

        payload = {
            "symbol": symbol,
            "side": exit_side,
            "qty": exit_qty,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": _safe_round(float(limit_px)),
            "extended_hours": bool(extended),
            "note": f"soft_exit:chase:trade_id={t.get('trade_id')}",
            "client_order_id": cid,
        }

        try:
            res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
            oid = res.get("id") or res.get("order_id") or res.get("client_order_id") or cid
            t["exit_order_id"] = str(oid)
            t["exit_submitted_ts"] = _now()
            self._persist(
                t,
                "TRADE_EXIT_REPLACED",
                extra={
                    "exit_order_id": t.get("exit_order_id"),
                    "exit_client_order_id": cid,
                    "limit_price": payload["limit_price"],
                    "aggress_cents": ag,
                    "requested_qty": requested_qty,
                    "broker_qty": broker_qty,
                    "exit_qty": exit_qty,
                    "exit_side": exit_side,
                    "md_lt": lt,
                    "md_bid": bid,
                    "md_ask": ask,
                    "decision_price": _decision_price(lt, bid, ask)[0],
                    "decision_mid": _decision_price(lt, bid, ask)[1],
                },
            )
        except Exception as e:
            t["state"] = "ERROR"
            t["error"] = f"exit.replace(place_order) failed: {e}"
            self._persist(t, "TRADE_ERROR", extra={"reason": t["error"]})

    # -------------- Metrics / TSLFE -----------------------------------------

    def _tslfe_engine(self, trade_id: str) -> _TSLFE:
        eng = self._tslfe.get(trade_id)
        if eng is None:
            eng = _TSLFE(
                eps_price=self.tslfe_eps,
                tbe_window=self.tslfe_tbe_window,
                min_samples=self.tslfe_min_samples,
                flatten_th=self.tslfe_flatten_th,
                profit_exit_norm_th=self.tslfe_profit_exit_norm_th,
            )
            self._tslfe[trade_id] = eng
        return eng

    def _metrics_tick(self, t: Dict[str, Any], kind: str, px: float) -> None:
        trade_id = str(t.get("trade_id") or "")
        entry_px = float(_as_float(t.get("entry_avg_price")) or 0.0)
        if entry_px <= 0:
            return

        eng = self._tslfe_engine(trade_id)
        metrics = eng.update(_now(), float(px), float(entry_px))

        # Determine phase (SAFE vs RISK) based on px vs entry
        phase = "SAFE" if px >= entry_px else "RISK"
        if kind == "short":
            # For shorts, invert
            phase = "SAFE" if px <= entry_px else "RISK"
        metrics["phase"] = phase

        # early MAE proxy (for RISK phase)
        if phase == "RISK":
            if kind == "long":
                metrics["early_mae_r"] = max(0.0, (entry_px - px) / max(0.01, self.tslfe_eps))
            else:
                metrics["early_mae_r"] = max(0.0, (px - entry_px) / max(0.01, self.tslfe_eps))

        t["metrics"] = metrics
        t["metrics_extreme_price"] = px

        # Emit at a fixed cadence
        last_emit = float(self._last_metrics_emit.get(trade_id, 0.0))
        if (_now() - last_emit) < float(self.metrics_emit_secs):
            return
        self._last_metrics_emit[trade_id] = _now()

        self._persist(
            t,
            "TRADE_METRICS",
            extra={"metrics": metrics},
        )

    # -------------- Main loop ------------------------------------------------

    async def run_forever(self) -> None:
        if not self.enabled:
            log.warning("TradeRunner disabled; not running loop")
            return

        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("TradeRunner loop error")
            await asyncio.sleep(0.05)

    async def run_once(self) -> None:
        # Iterate active trades
        trades = []
        try:
            trades = self.store.get_active_trades() or []
        except Exception:
            trades = []

        for t in trades:
            try:
                await self._step_trade(t)
            except Exception:
                log.exception("TradeRunner step error")
                try:
                    t["state"] = "ERROR"
                    t["error"] = "exception in step"
                    self._persist(t, "TRADE_ERROR", extra={"trace": traceback.format_exc()})
                except Exception:
                    pass

    async def _step_trade(self, t: Dict[str, Any]) -> None:
        # --- normalize ---
        aid = str(t.get("account_id") or "")
        sym = str(t.get("symbol") or "").upper()
        if not aid or not sym:
            return

        adapter = None
        try:
            adapter = self.adapters.get(aid)
        except Exception:
            adapter = None
        if adapter is None:
            return

        st = str(t.get("state") or "").upper()
        kind = _side_kind(str(t.get("side") or "buy"))

        # --- helper: adapter position fallback (PM snapshots can lag/miss) ---
        async def _pos_from_adapter(symbol: str) -> Optional[Dict[str, Any]]:
            # Try common adapter APIs: get_position(symbol) or list_positions()
            try:
                fn = getattr(adapter, "get_position", None)
                if fn is not None:
                    res = fn(symbol)
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, dict) and str(res.get("symbol") or "").upper() == symbol.upper():
                        return res
            except Exception:
                pass

            try:
                fn = getattr(adapter, "list_positions", None)
                if fn is not None:
                    res = fn()
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, list):
                        for p in res:
                            if isinstance(p, dict) and str(p.get("symbol") or "").upper() == symbol.upper():
                                return p
            except Exception:
                pass

            return None

        # Snapshot/position lookup (used for exits and reduce-only sizing)
        snap = self._snapshot_for_account(aid)
        pos = self._find_pos(snap, sym)

        # ---- NEW / ADOPTED -> ACTIVE ----
        if st in ("NEW", "ADOPTED"):
            t["state"] = "ACTIVE"
            self._persist(
                t,
                "TRADE_ACTIVE",
                extra={
                    "reason": "adopt_existing_position",
                    "entry_avg_price": t.get("entry_avg_price"),
                    "soft_stop_price": t.get("soft_stop_price"),
                    "soft_target_price": t.get("soft_target_price"),
                    "market_session": t.get("market_session"),
                },
            )
            return

        # ---- ENTRY_PENDING -> place order ----
        if st == "ENTRY_PENDING":
            sess = str(t.get("market_session") or _session_kind_now())
            extended = sess in ("PRE", "POST")

            entry_cid = str(t.get("entry_client_order_id") or f"{t.get('trade_id')}:entry")
            t["entry_client_order_id"] = entry_cid

            payload = {
                "symbol": sym,
                "side": str(t.get("side") or "buy"),
                "qty": float(t.get("qty") or 10.0),
                "time_in_force": "day",
                "client_order_id": entry_cid,
                "extended_hours": bool(extended),
                "note": f"entry:trade_id={t.get('trade_id')}",
            }

            lt = None
            bid = None
            ask = None

            try:
                if extended:
                    lt, bid, ask = await self._read_md(sym)
                    px = lt if lt is not None else (bid if bid is not None else ask)
                    if px is None:
                        self._persist(t, "TRADE_ENTRY_WAITING_MD", extra={"reason": "md_missing_for_extended_entry"})
                        await self._raise_symbol_tier(sym)

                    entry_side = payload["side"]
                    limit_px = _marketable_limit_price(kind, entry_side, lt, bid, ask, float(self.entry_aggress_cents))
                    if limit_px is None:
                        self._persist(t, "TRADE_ENTRY_WAITING_MD", extra={"reason": "cannot_compute_marketable_entry"})
                        await self._raise_symbol_tier(sym)
                        return
                    payload["type"] = "limit"
                    payload["limit_price"] = _safe_round(float(limit_px))
                else:
                    payload["type"] = "market"

                res = await _maybe_await(getattr(adapter, "place_order", None), **payload)
                t["entry_order_id"] = res.get("id") or res.get("order_id") or res.get("client_order_id") or entry_cid
                t["entry_submitted_ts"] = _now()
                t["state"] = "ENTRY_SUBMITTED"

                self._persist(
                    t,
                    "TRADE_ENTRY_SUBMITTED",
                    extra={
                        "order_id": t["entry_order_id"],
                        "entry_client_order_id": entry_cid,
                        "order_type": payload.get("type"),
                        "limit_price": payload.get("limit_price"),
                        "time_in_force": payload.get("time_in_force"),
                        "extended_hours": payload.get("extended_hours"),
                        "md_lt": lt,
                        "md_bid": bid,
                        "md_ask": ask,
                        "decision_price": _decision_price(lt, bid, ask)[0],
                        "decision_mid": _decision_price(lt, bid, ask)[1],
                        "aggress_cents": float(self.entry_aggress_cents),
                    },
                )
            except Exception as e:
                t["state"] = "ERROR"
                t["error"] = f"entry.place_order failed: {e}"
                self._persist(t, "TRADE_ERROR", extra={"reason": t["error"]})
            return

        # ---- ENTRY_SUBMITTED -> detect fill ----
        if st == "ENTRY_SUBMITTED":
            start_ts = _as_float(t.get("entry_submitted_ts")) or _now()
            if (_now() - start_ts) > self.fill_timeout:
                t["state"] = "ERROR"
                t["error"] = f"entry fill timeout after {self.fill_timeout}s"
                self._persist(t, "TRADE_ENTRY_TIMEOUT", extra={"reason": t["error"]})
                return

            # If we have telemetry fill, promote to ACTIVE immediately
            if t.get("entry_filled_ts"):
                t["entry_filled_ts"] = float(t.get("entry_filled_ts") or _now())

                px = _as_float(t.get("entry_fill_price")) or _as_float(t.get("entry_avg_price")) or None
                if px and px > 0:
                    t["entry_avg_price"] = float(px)
                    t["entry_fill_price"] = float(px)

                try:
                    t["entry_fill_latency_ms"] = int(
                        (float(t.get("entry_filled_ts") or _now()) - float(t.get("entry_submitted_ts") or _now())) * 1000.0
                    )
                except Exception:
                    t["entry_fill_latency_ms"] = None

                entry_price = float(t.get("entry_avg_price") or 0.0)
                if entry_price <= 0:
                    entry_price = float(px or 0.0) or 0.0

                if kind == "long":
                    soft_stop = max(0.01, entry_price - 0.05)
                    soft_target = max(0.01, entry_price + 0.10)
                else:
                    soft_stop = max(0.01, entry_price + 0.05)
                    soft_target = max(0.01, entry_price - 0.10)

                t["soft_stop_price"] = _safe_round(soft_stop)
                t["soft_target_price"] = _safe_round(soft_target)
                t["stop_price"] = t["soft_stop_price"]
                t["take_profit_price"] = t["soft_target_price"]

                await self._raise_symbol_tier(sym)

                sess = str(t.get("market_session") or _session_kind_now())
                t["state"] = "ACTIVE"
                self._persist(
                    t,
                    "TRADE_ACTIVE",
                    extra={
                        "entry_avg_price": t.get("entry_avg_price"),
                        "entry_fill_price": t.get("entry_fill_price"),
                        "entry_fill_latency_ms": t.get("entry_fill_latency_ms"),
                        "soft_stop_price": t.get("soft_stop_price"),
                        "soft_target_price": t.get("soft_target_price"),
                        "market_session": sess,
                        "fill_source": "broker_telemetry",
                    },
                )
                return

            entry_cid = str(t.get("entry_client_order_id") or f"{t.get('trade_id')}:entry")
            filled, fill_price = await self._detect_entry_fill(aid, sym, entry_cid, float(t.get("qty") or 0.0))
            if not filled:
                return

            t["entry_filled_ts"] = _now()
            if fill_price and fill_price > 0:
                t["entry_avg_price"] = float(fill_price)
            t["entry_fill_price"] = float(fill_price) if (fill_price and fill_price > 0) else None
            try:
                t["entry_fill_latency_ms"] = int(
                    (float(t.get("entry_filled_ts") or _now()) - float(t.get("entry_submitted_ts") or _now())) * 1000.0
                )
            except Exception:
                t["entry_fill_latency_ms"] = None

            entry_price = float(t.get("entry_avg_price") or 0.0)
            if entry_price <= 0:
                entry_price = float(fill_price or 0.0) or 0.0

            if kind == "long":
                soft_stop = max(0.01, entry_price - 0.05)
                soft_target = max(0.01, entry_price + 0.10)
            else:
                soft_stop = max(0.01, entry_price + 0.05)
                soft_target = max(0.01, entry_price - 0.10)

            t["soft_stop_price"] = _safe_round(soft_stop)
            t["soft_target_price"] = _safe_round(soft_target)
            t["stop_price"] = t["soft_stop_price"]
            t["take_profit_price"] = t["soft_target_price"]

            await self._raise_symbol_tier(sym)

            sess = str(t.get("market_session") or _session_kind_now())
            t["state"] = "ACTIVE"
            self._persist(
                t,
                "TRADE_ACTIVE",
                extra={
                    "entry_avg_price": t.get("entry_avg_price"),
                    "entry_fill_price": t.get("entry_fill_price"),
                    "entry_fill_latency_ms": t.get("entry_fill_latency_ms"),
                    "soft_stop_price": t.get("soft_stop_price"),
                    "soft_target_price": t.get("soft_target_price"),
                    "market_session": sess,
                },
            )
            return

        # ---- ACTIVE / EXIT_SUBMITTED -> manage exits ----
        if st in ("ACTIVE", "EXIT_SUBMITTED"):
            # If snapshot missing the position, try adapter fallback (this is the bug you're hitting)
            if not pos:
                pos = await _pos_from_adapter(sym)

            if pos and abs(float(_as_float(pos.get("qty")) or 0.0)) >= 1e-9:
                t["pos_seen"] = True

            # Still no position? keep waiting (same behavior), but now we tried harder.
            if (not pos) or abs(float(_as_float(pos.get("qty")) or 0.0)) < 1e-9:
                if t.get("pos_seen"):
                    if t.get("state") != "DONE":
                        t["state"] = "DONE"
                        self._persist(t, "TRADE_DONE", extra={"reason": "broker_position_closed"})
                else:
                    self._persist(t, "TRADE_WAITING_FOR_POSITION", extra={"reason": "snapshot_missing_position"})
                return

            broker_qty = float(_as_float(pos.get("qty")) or 0.0)

            # Guard mismatch
            if kind == "long" and broker_qty < -1e-9:
                t["exit_reason"] = "GUARD_COVER"
                t["exit_reason_hint"] = "BROKER_SHORT_DETECTED"
                self._persist(t, "TRADE_GUARD_SIDE_MISMATCH", extra={"broker_qty": broker_qty, "expected": "long"})
                lt_g, bid_g, ask_g = await self._read_md(sym)
                await self._submit_soft_exit(
                    aid, sym, adapter, t, kind, "cover", lt_g, bid_g, ask_g, reason_hint="BROKER_SHORT_DETECTED", current_pos_qty=broker_qty
                )
                return

            if kind == "short" and broker_qty > 1e-9:
                t["exit_reason"] = "GUARD_COVER"
                t["exit_reason_hint"] = "BROKER_LONG_DETECTED"
                self._persist(t, "TRADE_GUARD_SIDE_MISMATCH", extra={"broker_qty": broker_qty, "expected": "short"})
                lt_g, bid_g, ask_g = await self._read_md(sym)
                await self._submit_soft_exit(
                    aid, sym, adapter, t, kind, "cover", lt_g, bid_g, ask_g, reason_hint="BROKER_LONG_DETECTED", current_pos_qty=broker_qty
                )
                return

            lt, bid, ask = await self._read_md(sym)
            last_px = lt if lt is not None else (bid if bid is not None else ask)
            if last_px is None:
                px_fallback = (
                    _as_float(t.get("metrics_extreme_price"))
                    or _as_float(t.get("entry_avg_price"))
                    or _as_float(t.get("soft_target_price"))
                    or _as_float(t.get("soft_stop_price"))
                    or 0.01
                )
                self._metrics_tick(t, kind, float(px_fallback))
                await self._raise_symbol_tier(sym)
                return

            self._metrics_tick(t, kind, float(last_px))

            entry_px = float(_as_float(t.get("entry_avg_price")) or 0.0)
            stop_px = float(_as_float(t.get("soft_stop_price")) or 0.0)
            target_px = float(_as_float(t.get("soft_target_price")) or 0.0)
            if entry_px <= 0:
                return

            hit_stop = False
            hit_target = False
            if kind == "long":
                hit_stop = float(last_px) <= float(stop_px) if stop_px > 0 else False
                hit_target = float(last_px) >= float(target_px) if target_px > 0 else False
            else:
                hit_stop = float(last_px) >= float(stop_px) if stop_px > 0 else False
                hit_target = float(last_px) <= float(target_px) if target_px > 0 else False

            if t.get("state") == "ACTIVE":
                if hit_stop:
                    await self._submit_soft_exit(aid, sym, adapter, t, kind, "stop", lt, bid, ask, reason_hint=t.get("exit_reason_hint"), current_pos_qty=broker_qty)
                    return
                if hit_target:
                    await self._submit_soft_exit(aid, sym, adapter, t, kind, "target", lt, bid, ask, reason_hint="HARD_TARGET", current_pos_qty=broker_qty)
                    return

            await self._chase_exit_if_needed(aid, sym, adapter, t, kind, lt, bid, ask, current_pos_qty=broker_qty)
            await self._raise_symbol_tier(sym)
            return



# --------------------------------------------------------------------------- #
# Async helper for adapters that may return coroutine or dict
# --------------------------------------------------------------------------- #

async def _maybe_await(fn: Any, *args: Any, **kwargs: Any) -> Any:
    if fn is None:
        return {}
    try:
        res = fn(*args, **kwargs)
        if asyncio.iscoroutine(res):
            return await res
        return res
    except Exception:
        raise