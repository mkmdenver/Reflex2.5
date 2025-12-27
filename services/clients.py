from __future__ import annotations
import asyncio
import json
from typing import Any, Dict, Optional

import httpx
from redis.asyncio import Redis
from cockpit import config

class HTTPService:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=5.0)

    async def get(self, path: str, **kwargs) -> Optional[Dict[str, Any]]:
        url = f"{self.base}{path}"
        try:
            r = await self._client.get(url, **kwargs)
            r.raise_for_status()
            ct = r.headers.get("content-type","")
            if "application/json" in ct or r.text.strip().startswith("{"):
                return r.json()
            return {"raw": r.text}
        except Exception:
            return None

    async def post(self, path: str, json_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self.base}{path}"
        try:
            r = await self._client.post(url, json=json_body)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    async def aclose(self):
        await self._client.aclose()


class Bus:
    def __init__(self, url: str):
        self.url = url
        self.redis: Optional[Redis] = None

    async def connect(self):
        try:
            self.redis = Redis.from_url(self.url, decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def publish(self, channel: str, message: Dict[str, Any]) -> bool:
        if not self.redis:
            return False
        try:
            await self.redis.publish(channel, json.dumps(message))
            return True
        except Exception:
            return False

    async def subscribe(self, channel: str):
        if not self.redis:
            while True:
                await asyncio.sleep(60)
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg and msg.get("type") == "message":
                    yield msg.get("data")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
