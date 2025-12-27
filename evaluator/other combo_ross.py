# evaluator/loop_combo_ross.py
from __future__ import annotations

import asyncio

from common.bus import subscribe, CHANNELS, unpack
from common.logging import info, error

from evaluator.model_combo_ross_bullflag import ComboRossBullFlagModel
from evaluator.filters import filter1_float_watch, filter2_cameron_from_ross


COMPONENT = "eval.combo_ross"


async def _run_filter1_once() -> None:
    """
    Run Filter1 (float band → WATCH) one time at startup.

    This just reuses the existing script entrypoint in
    evaluator/filters/filter1_float_watch.py, but runs it in a thread so
    we don't block the asyncio loop.
    """
    info(COMPONENT, "filter1.start", script="filter1_float_watch")
    try:
        # filter1_float_watch.main() is synchronous and may do DB + HTTP work.
        await asyncio.to_thread(filter1_float_watch.main)
        info(COMPONENT, "filter1.done", script="filter1_float_watch")
    except SystemExit as e:
        error(
            COMPONENT,
            "filter1.exit",
            script="filter1_float_watch",
            code=getattr(e, "code", None),
        )
    except Exception as e:
        error(
            COMPONENT,
            "filter1.exception",
            script="filter1_float_watch",
            error=str(e),
        )


async def _filter2_loop(interval_seconds: int = 60) -> None:
    """
    Periodically run Filter2 (Ross-style pillars).

    This uses the existing script entrypoint in
    evaluator/filters/filter2_cameron_from_ross.py, again via a thread so
    the tick/quote pumps stay responsive.

    Each run:
      - Scans the float-band universe
      - Computes today's price + volume from minute_bars
      - Raises service tier to WARM for passers
      - Publishes coarse Ross events onto eval.ross_pillars
    """
    info(
        COMPONENT,
        "filter2.loop.start",
        script="filter2_cameron_from_ross",
        interval_seconds=interval_seconds,
    )

    while True:
        try:
            await asyncio.to_thread(filter2_cameron_from_ross.main)
        except SystemExit as e:
            error(
                COMPONENT,
                "filter2.exit",
                script="filter2_cameron_from_ross",
                code=getattr(e, "code", None),
            )
        except Exception as e:
            error(
                COMPONENT,
                "filter2.exception",
                script="filter2_cameron_from_ross",
                error=str(e),
            )

        await asyncio.sleep(interval_seconds)


async def run() -> None:
    # Log startup configuration
    info(
        COMPONENT,
        "startup.begin",
        instance="default",
        mode="LIVE",
        channels={"ticks": CHANNELS["ticks"], "quotes": CHANNELS["quotes"]},
    )

    # 1) Run Filter1 once to seed WATCH tier from fundamentals
    await _run_filter1_once()

    # 2) Subscribe to ticks and quotes from DataHub
    ps_ticks = await subscribe(CHANNELS["ticks"])
    ps_quotes = await subscribe(CHANNELS["quotes"])

    model = ComboRossBullFlagModel(name="combo_ross_bullflag_v1")

    async def pump_ticks() -> None:
        info(COMPONENT, "pump_ticks.start", channel=CHANNELS["ticks"])
        async for msg in ps_ticks.listen():
            if msg.get("type") != "message":
                continue
            ev = unpack(msg["data"])
            await model.on_trade(ev)

    async def pump_quotes() -> None:
        info(COMPONENT, "pump_quotes.start", channel=CHANNELS["quotes"])
        async for msg in ps_quotes.listen():
            if msg.get("type") != "message":
                continue
            ev = unpack(msg["data"])
            await model.on_quote_tob(ev)

    # 3) Start everything together: ticks, quotes, and Filter2 loop
    info(COMPONENT, "startup.ready")
    await asyncio.gather(
        pump_ticks(),
        pump_quotes(),
        _filter2_loop(interval_seconds=60),
    )


if __name__ == "__main__":
    asyncio.run(run())
