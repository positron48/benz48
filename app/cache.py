from __future__ import annotations

import time
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self) -> None:
        self._store.clear()


def build_cache_key(prefix: str, **params: Any) -> str:
    parts = [prefix]
    for name in sorted(params):
        value = params[name]
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
            parts.append(f"{name}={','.join(str(item) for item in sorted(value))}")
        else:
            parts.append(f"{name}={value}")
    return "|".join(parts)
