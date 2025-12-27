# evaluator/loop_spy_momo.py
from __future__ import annotations

import asyncio
import os

from common.bus import subscribe, CHANNELS, unpack
from common.logging import info, error

from evaluator.bots.other_simple_bars1 import SimpleBars1Model

COMPONENT = "eval.spy_momo"


async def run() -> None:
    """
    SPY Momo harness around SimpleBars1Model.

    Responsibilities:

      • Boot a SimpleBars1Model instance for a single symbol (SPY by default).
      • Run Filter1 + Filter2 once at startup (COLD → WATCH → WARM).
      • Subscribe to the shared tick channel and forward only this symbol's
        ticks into the model's on_tick() method.
      • Keep the tick pump running forever.

    The *pattern* and *trigger* stages live entirely inside SimpleBars1Model.
    """
    instance = os.getenv("EVAL_INSTANCE", "default")
    mode = os.getenv("REFLEX_MODE", os.getenv("MODE", "LIVE"))
    symbol = os.getenv("SPY_MOMO_SYMBOL", "SPY").upper()
    # Offset is absolute dollars; override via env if desired.
    offset = float(os.getenv("SPY_MOMO_OFFSET", os.getenv("SPY_MOMO_OFFSET_DOLLARS", "0.05")))

    info(
        COMPONENT,
        "startup.begin",
        extra={
            "instance": instance,
            "mode": mode,
            "symbol": symbol,
            "channels": {"ticks": CHANNELS["ticks"]},
            "params": {"offset": offset},
        },
    )

    model = SimpleBars1Model(
        symbol=symbol,
        offset=offset,
        instance=instance,
        mode=mode,
        source="spy_momo",
    )

    # 1) Run Filter1 + Filter2 immediately to raise tiers in DataHub.
    model.run_startup_filters()

    # 2) Subscribe to ticks and pump them into the model.
    ps_ticks = await subscribe(CHANNELS["ticks"])

    async def pump_ticks() -> None:
        info(
            COMPONENT,
            "pump_ticks.start",
            extra={"instance": instance, "mode": mode, "channel": CHANNELS["ticks"], "symbol": symbol},
        )
        async for msg in ps_ticks.listen():
            if msg.get("type") != "message":
                continue
            try:
                tick = unpack(msg["data"])
            except Exception as exc:  # noqa: BLE001
                error(
                    COMPONENT,
                    "pump_ticks.unpack_error",
                    extra={"err": str(exc)},
                )
                continue

            sym = tick.get("sym") or tick.get("symbol")
            if sym and sym != symbol:
                continue

            try:
                model.on_tick(tick)
            except Exception as exc:  # noqa: BLE001
                error(
                    COMPONENT,
                    "pump_ticks.model_error",
                    extra={"symbol": symbol, "err": str(exc)},
                )

    info(
        COMPONENT,
        "startup.ready",
        extra={"instance": instance, "mode": mode, "model_tier": model.tier},
    )

    await pump_ticks()


if __name__ == "__main__":
    asyncio.run(run())
