from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.parser import (
    normalize_queue,
    normalize_yes_no,
    parse_report_datetime,
    parse_reports_html,
)
from app.storage import Storage, dedupe_consecutive_observations

FIXTURE = Path(__file__).parent / "fixtures" / "reports_page.html"


def test_parse_reports_html_returns_stations():
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    assert len(stations) == 24
    first = stations[0]
    assert first.brand == "TEBOIL"
    assert first.fuel_92 == "yes"
    assert first.queue == "60_plus"
    assert first.last_report_at == "2026-07-11T16:15:25+03:00"


def test_normalization():
    assert normalize_yes_no("Да") == "yes"
    assert normalize_yes_no("Нет") == "no"
    assert normalize_yes_no("-") is None
    assert normalize_queue("до 30") == "up_to_30"
    assert normalize_queue("60+") == "60_plus"
    assert parse_report_datetime("2026-07-11 16:15:25") == "2026-07-11T16:15:25+03:00"


def test_dedupe_consecutive_observations():
    base = {
        "is_working": "yes",
        "fuel_92": "yes",
        "fuel_95": "yes",
        "fuel_diesel": "yes",
        "queue": "none",
        "reason": None,
        "expected_working_at": None,
        "last_report_at": "2026-07-11T16:00:00+03:00",
    }
    rows = [
        {"station_id": "a", "collected_at": "t1", **base},
        {"station_id": "a", "collected_at": "t2", **base},
        {"station_id": "a", "collected_at": "t3", **{**base, "fuel_92": "no"}},
    ]
    deduped = dedupe_consecutive_observations(rows)
    assert len(deduped) == 2
    assert deduped[0]["collected_at"] == "t1"
    assert deduped[1]["collected_at"] == "t3"


def test_skip_unchanged_snapshot_index(tmp_path):
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    first_at = datetime(2026, 7, 11, 17, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    second_at = datetime(2026, 7, 11, 17, 5, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    storage.save_snapshot(stations, "https://example.test", first_at)
    storage.save_snapshot(stations, "https://example.test", second_at)

    meta = storage.get_meta()
    assert meta["snapshot_count"] == 1


def test_save_snapshot_and_index(tmp_path):
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    collected_at = datetime(2026, 7, 11, 17, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    path = storage.save_snapshot(stations, "https://example.test", collected_at)
    assert path.exists()

    meta = storage.get_meta()
    assert meta["snapshot_count"] == 1
    assert len(meta["stations"]) == 24

    latest = storage.get_latest()
    assert latest is not None
    assert latest["station_count"] == 24

    # Re-import same snapshot time should not duplicate
    storage.save_snapshot(stations, "https://example.test", collected_at)
    meta_after = storage.get_meta()
    assert meta_after["snapshot_count"] == 1


def test_save_snapshot_without_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SAVE_SNAPSHOT_FILES", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    collected_at = datetime(2026, 7, 11, 17, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    path = storage.save_snapshot(stations, "https://example.test", collected_at)

    assert not path.exists()
    assert (tmp_path / "history.db").exists()
    meta = storage.get_meta()
    assert meta["snapshot_count"] == 1
    assert not any(tmp_path.glob("snapshots/**/*.json"))


def test_api_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)
    storage.save_snapshot(stations, "https://example.test")

    from app import web

    web.storage = storage
    client = TestClient(web.app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    meta = client.get("/api/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["snapshot_count"] == 1
    assert body["stations"][0]["district"]
    assert body["region_groups"]

    timeseries = client.get("/api/timeseries")
    assert timeseries.status_code == 200
    assert len(timeseries.json()) == 24

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    assert summary.json()[0]["total"] == 24

    fuel_since = client.get("/api/fuel-since")
    assert fuel_since.status_code == 200
    assert len(fuel_since.json()) == 24
    first = fuel_since.json()[0]
    assert "fuel_92_since" in first
    assert "fuel_95_since" in first
    assert "fuel_diesel_since" in first

    index = client.get("/")
    assert index.status_code == 200
