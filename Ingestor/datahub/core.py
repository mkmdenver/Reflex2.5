# datahub/core.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Dict, Any, Optional

from common.config import load_cfg
from common.logging import get_logger
from datahub.ingest_router import Router
from datahub.subscriptions import SubManager
from datahub.flag_listener import FlagListener
from datahub.tier_listener import TierListener
from datahub.adapters.live_adapter import LIVEAdapter
from datahub.adapters.replay_adapter import ReplayAdapter

Mode = Literal["LIVE", "REPLAY"]
LOG = get_logger("datahub.core")


# ---------------------------------------------------------------------------
# Thin wrappers so SubManager can stay “tier-centric” and not care about
# LIVE vs REPLAY adapter details.
# ---------------------------------------------------------------------------

class LiveFeedWrapper:
    """
    Adapter wrapper used by SubManager in LIVE mode.

    Translates tier operations into LIVEAdapter.subscribe() calls.
    """
    def __init__(self, adapter: LIVEAdapter):
        self.adapter = adapter

    def sub_trades(self, symbol: str) -> None:
        self.adapter.subscribe(trades=[symbol])

    def sub_daily(self, symbol: str) -> None:
        # For now, minute/daily are handled via other machinery.
        # Placeholder so SUBSCRIPTION_POLICY can stay expressive.
        pass

    def sub_minute(self, symbol: str) -> None:
        # Same note as sub_daily.
        pass

    def sub_quotes_tob(self, symbol: str) -> None:
        self.adapter.subscribe(quotes=[symbol])

    def sub_quotes_l2(self, symbol: str) -> None:
        # L2 not yet wired; placeholder for future.
        pass


class ReplayFeedWrapper:
    """
    Adapter wrapper used by SubManager in REPLAY mode.

    Same surface as LiveFeedWrapper, but forwards to ReplayAdapter.subscribe().
    """
    def __init__(self, adapter: ReplayAdapter):
        self.adapter = adapter

    def sub_trades(self, symbol: str) -> None:
        self.adapter.subscribe(trades=[symbol])

    def sub_daily(self, symbol: str) -> None:
        pass

    def sub_minute(self, symbol: str) -> None:
        pass

    def sub_quotes_tob(self, symbol: str) -> None:
        self.adapter.subscribe(quotes=[symbol])

    def sub_quotes_l2(self, symbol: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Core hub
# ---------------------------------------------------------------------------

@dataclass
class HubCore:
    mode: Mode
    adapter: object          # LIVEAdapter | ReplayAdapter
    router: Router
    subman: SubManager
    flag_listener: FlagListener
    tier_listener: TierListener

    async def run(self) -> None:
        LOG.info("hub.start mode=%s", self.mode)

        # Start router (sets up Redis publisher)
        await self.router.start()

        # Kick off concurrent tasks
        tasks = [
            asyncio.create_task(
                _stream_loop(self.adapter, self.router),
                name="hub.stream",
            ),
            asyncio.create_task(
                self.flag_listener.run(),
                name="hub.flags",
            ),
            asyncio.create_task(
                self.tier_listener.run(),
                name="hub.tiers",
            ),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            LOG.warn("hub.run: cancelled")
        finally:
            LOG.info("hub.stop")


# ---------------------------------------------------------------------------
# Streaming loop: shared for LIVE + REPLAY
# ---------------------------------------------------------------------------

async def _stream_loop(adapter: object, router: Router) -> None:
    """
    Run the adapter's blocking stream() in an executor, and route
    Polygon/replay events into Router.on_tick / on_quote_tob.

    This is mode-agnostic: we just interpret "T"/"Q" style events.
    """
    loop = asyncio.get_running_loop()
    LOG.info("stream.loop.start")

    def _run_blocking() -> None:
        try:
            # LIVEAdapter has open(); ReplayAdapter usually has start().
            if hasattr(adapter, "open"):
                adapter.open()  # type: ignore[attr-defined]
            elif hasattr(adapter, "start"):
                adapter.start()  # type: ignore[attr-defined]

            for ev in adapter.stream():  # type: ignore[attr-defined]
                if not isinstance(ev, dict):
                    continue

                # Polygon native events typically use ev="T"/"Q" and sym="XYZ"
                k = ev.get("ev") or ev.get("event_type") or ev.get("type")
                sym = ev.get("sym") or ev.get("symbol")
                if not sym:
                    continue

                if k in ("T", "trade"):
                    asyncio.run_coroutine_threadsafe(
                        router.on_tick(sym, ev), loop
                    )
                elif k in ("Q", "quote"):
                    asyncio.run_coroutine_threadsafe(
                        router.on_quote_tob(sym, ev), loop
                    )
        except Exception as exc:  # noqa: BLE001
            LOG.error("stream.loop.error %r", exc)

    await loop.run_in_executor(None, _run_blocking)


# ---------------------------------------------------------------------------
# Builders for LIVE and REPLAY faces
# ---------------------------------------------------------------------------

def _build_live_core() -> HubCore:
    cfg = load_cfg()
    live_cfg = cfg.live

    adapter = LIVEAdapter(
        api_key=live_cfg.polygon_ws_key or live_cfg.polygon_rest_key or "",
        ws_url=None,
    )
    feed = LiveFeedWrapper(adapter)

    router = Router()
    subman = SubManager(feed)
    flag_listener = FlagListener()
    tier_listener = TierListener(subman)

    return HubCore(
        mode="LIVE",
        adapter=adapter,
        router=router,
        subman=subman,
        flag_listener=flag_listener,
        tier_listener=tier_listener,
    )


def _build_replay_core() -> HubCore:
    cfg = load_cfg()
    rep = cfg.replay

    adapter = ReplayAdapter(
        session=rep.session,
        speed=rep.speed,
        clock=rep.clock,
        start=rep.start,
        end=rep.end,
    )
    feed = ReplayFeedWrapper(adapter)

    router = Router()
    subman = SubManager(feed)
    flag_listener = FlagListener()
    tier_listener = TierListener(subman)

    return HubCore(
        mode="REPLAY",
        adapter=adapter,
        router=router,
        subman=subman,
        flag_listener=flag_listener,
        tier_listener=tier_listener,
    )


async def run_core(mode: Optional[Mode] = None) -> None:
    """
    Main async entry point.

    If mode is None, REFLEX__HUB_MODE (via common.config) decides.
    """
    cfg = load_cfg()
    hub_mode: Mode = (mode or cfg.hub.mode or "LIVE").upper()  # type: ignore[arg-type]

    if hub_mode == "LIVE":
        core = _build_live_core()
    elif hub_mode == "REPLAY":
        core = _build_replay_core()
    else:
        raise RuntimeError(f"Unknown hub mode: {hub_mode!r}")

    await core.run()
