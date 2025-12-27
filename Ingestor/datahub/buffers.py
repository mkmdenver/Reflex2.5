from collections import deque
from typing import Any, Deque, Dict

class DoubleRingBuffer:
    def __init__(self, short: int = 128, long: int = 2048):
        self.short: Deque[Dict[str, Any]] = deque(maxlen=short)
        self.long: Deque[Dict[str, Any]] = deque(maxlen=long)

    def push(self, item: Dict[str, Any]):
        self.short.append(item)
        self.long.append(item)

    def stats(self) -> Dict[str, int]:
        return {"short": len(self.short), "long": len(self.long)}
