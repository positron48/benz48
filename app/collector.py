from __future__ import annotations

import logging
import sys
import time

import httpx

from app.bootstrap import bootstrap_on_startup
from app.config import settings
from app.parser import parse_reports_html
from app.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def fetch_html(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, settings.request_retries + 1):
        try:
            with httpx.Client(
                timeout=settings.request_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "lipetsk-gas-monitor/1.0"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            last_error = exc
            logger.warning("Fetch attempt %s failed: %s", attempt, exc)
            if attempt < settings.request_retries:
                time.sleep(min(attempt * 2, 10))

    raise RuntimeError(f"Failed to fetch {url}") from last_error


def collect_once(storage: Storage | None = None) -> None:
    if not settings.collection_enabled:
        raise RuntimeError("Collection is disabled (COLLECTION_ENABLED=false)")
    storage = storage or Storage()
    html = fetch_html(settings.source_url)
    stations = parse_reports_html(html)
    if not stations:
        raise RuntimeError("Parsed zero stations from source page")
    storage.save_snapshot(stations, settings.source_url)


def run_loop() -> None:
    if not settings.collection_enabled:
        logger.warning(
            "Collection disabled (source archived after %s); idle forever",
            settings.archive_to,
        )
        while True:
            time.sleep(3600)

    storage = Storage()
    logger.info(
        "Collector started: interval=%ss url=%s",
        settings.collect_interval_seconds,
        settings.source_url,
    )

    while True:
        started = time.monotonic()
        try:
            collect_once(storage)
        except Exception:
            logger.exception("Collection failed")
        elapsed = time.monotonic() - started
        sleep_for = max(settings.collect_interval_seconds - elapsed, 1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        bootstrap_on_startup()
        collect_once()
    else:
        run_loop()
