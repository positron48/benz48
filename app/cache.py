from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any


_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


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


def normalize_cache_value(value: Any, *, bucket_seconds: int = 300) -> Any:
    """Floor ISO timestamps to collect-interval buckets so sliding ranges share a key."""
    if isinstance(value, list):
        return [normalize_cache_value(item, bucket_seconds=bucket_seconds) for item in value]
    if not isinstance(value, str) or not _ISO_TIMESTAMP_RE.match(value):
        return value
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    if dt.tzinfo is None:
        return value
    bucket = max(1, bucket_seconds)
    floored = int(dt.timestamp()) // bucket * bucket
    return str(floored)


def build_cache_key(prefix: str, **params: Any) -> str:
    parts = [prefix]
    for name in sorted(params):
        value = params[name]
        if value is None:
            continue
        value = normalize_cache_value(value)
        if isinstance(value, list):
            if not value:
                continue
            parts.append(f"{name}={','.join(str(item) for item in sorted(value))}")
        else:
            parts.append(f"{name}={value}")
    return "|".join(parts)
