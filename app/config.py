from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SOURCE_URL = (
    "https://gas-monitoring.admlr.lipetsk.ru/reports/"
    "?scope=recent_reports&search=&brand=&district="
    "&is_working=&fuel_92_status=&fuel_95_status=&fuel_diesel_status="
    "&queue=&reported="
)


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    source_url: str = os.getenv("SOURCE_URL", DEFAULT_SOURCE_URL)
    collect_interval_seconds: int = _int("COLLECT_INTERVAL_SECONDS", 300)
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    listen_port: int = _int("LISTEN_PORT", 8000)
    api_cache_seconds: int = _int("API_CACHE_SECONDS", 300)
    timezone: str = os.getenv("TIMEZONE", "Europe/Moscow")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    request_retries: int = _int("REQUEST_RETRIES", 3)

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.db"


settings = Settings()


def save_snapshot_files_enabled() -> bool:
    return _bool("SAVE_SNAPSHOT_FILES", True)
