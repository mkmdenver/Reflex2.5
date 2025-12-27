# evaluator/core/drivers.py

import asyncio
from typing import Awaitable, Callable

from common.bus import subscribe, unpack
from common.log import info, error
from .state_publisher import EvalStatePublisher

COMPONENT = "evaluator.driver"


class LiveStreamDriver:
    """
    Drives a model from live DataHub pub/sub channels.
    """

    def __init__(
        self,
        name: str,
        ticks_channel: str,
        quotes_channel: str | None,
        on_trade: Callable[[dict], Awaitable[None]],
        on_quote: Callable[[dict], Awaitable[None]] | None = None,
        state_publisher: EvalStatePublisher | None = None,
    ) -> None:
        self.name = name
        self.ticks_channel = ticks_channel
        self.quotes_channel = quotes_channel
        self.on_trade = on_trade
        self.on_quote = on_quote
        self.state_publisher = state_publisher
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        info(COMPONENT, "live_driver.start", name=self.name)

        ps_ticks = await subscribe(self.ticks_channel)
        ps_quotes = None
        if self.quotes_channel and self.on_quote:
            ps_quotes = await subscribe(self.quotes_channel)

        async def pump_ticks() -> None:
            info(COMPONENT, "pump_ticks.start", channel=self.ticks_channel)
            async for msg in ps_ticks.listen():
                if msg.get("type") != "message":
                    continue
                ev = unpack(msg["data"])
                await self.on_trade(ev)

        async def pump_quotes() -> None:
            if not ps_quotes:
                return
            info(COMPONENT, "pump_quotes.start", channel=self.quotes_channel)
            async for msg in ps_quotes.listen():
                if msg.get("type") != "message":
                    continue
                ev = unpack(msg["data"])
                await self.on_quote(ev)

        try:
            await asyncio.gather(
                pump_ticks(),
                pump_quotes() if ps_quotes else asyncio.sleep(0),
            )
        except asyncio.CancelledError:
            info(COMPONENT, "live_driver.cancelled", name=self.name)
            raise
        except Exception as e:
            error(
                COMPONENT,
                "live_driver.exception",
                name=self.name,
                error=str(e),
            )
        finally:
            info(COMPONENT, "live_driver.stop", name=self.name)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
