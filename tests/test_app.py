from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.parser import (
    StationRecord,
    normalize_queue,
    normalize_yes_no,
    parse_report_datetime,
    parse_reports_html,
)
from app.storage import (
    Storage,
    apply_carry_forward,
    carry_forward_station_fuels,
    dedupe_consecutive_observations,
    fuel_yes_since,
)

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


def test_dedupe_consecutive_observations() -> None:
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


def test_apply_carry_forward_preserves_fuel_during_outage() -> None:
    rows = [
        {
            "station_id": "a",
            "collected_at": "2026-07-12T08:00:00+03:00",
            "is_working": "yes",
            "fuel_95": "yes",
            "fuel_92": "yes",
            "fuel_diesel": "no",
        },
        {
            "station_id": "a",
            "collected_at": "2026-07-12T12:59:00+03:00",
            "is_working": "no",
            "fuel_95": None,
            "fuel_92": None,
            "fuel_diesel": None,
        },
    ]
    enriched = apply_carry_forward(rows)
    assert enriched[1]["fuel_95"] == "yes"
    assert enriched[1]["fuel_92"] == "yes"
    assert enriched[1]["fuel_diesel"] == "no"


def test_apply_carry_forward_preserves_queue_during_outage() -> None:
    rows = [
        {
            "station_id": "a",
            "collected_at": "2026-07-12T08:00:00+03:00",
            "is_working": "yes",
            "fuel_95": "yes",
            "queue": "up_to_30",
        },
        {
            "station_id": "a",
            "collected_at": "2026-07-12T12:59:00+03:00",
            "is_working": "no",
            "fuel_95": None,
            "queue": None,
        },
    ]
    enriched = apply_carry_forward(rows)
    assert enriched[1]["fuel_95"] == "yes"
    assert enriched[1]["queue"] == "up_to_30"


def test_carry_forward_station_fuels_on_save() -> None:
    station = {
        "id": "a",
        "is_working": "no",
        "fuel_95": None,
        "fuel_92": "yes",
        "fuel_diesel": None,
    }
    carry_forward_station_fuels(
        station,
        {"fuel_95": "yes", "fuel_92": "yes", "fuel_diesel": "no"},
    )
    assert station["fuel_95"] == "yes"
    assert station["fuel_92"] == "yes"
    assert station["fuel_diesel"] == "no"


def test_get_latest_excludes_stale_station_state(tmp_path) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    first_at = datetime(2026, 7, 12, 8, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    second_at = datetime(2026, 7, 12, 8, 5, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    storage.save_snapshot(stations, "https://example.test", first_at)
    # Second scrape loses one station — it must not inflate latest yes/total.
    storage.save_snapshot(stations[1:], "https://example.test", second_at)

    latest = storage.get_latest()
    assert latest is not None
    assert latest["station_count"] == len(latest["stations"]) == 23
    dropped_id = stations[0].id
    assert all(s["station_id"] != dropped_id for s in latest["stations"])


def test_get_latest_enriches_fuel_during_outage(tmp_path) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    working_at = datetime(2026, 7, 12, 8, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    outage_at = datetime(2026, 7, 12, 12, 59, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    storage.save_snapshot(stations, "https://example.test", working_at)

    outage_stations = []
    for station in stations:
        data = station.to_dict()
        if data["brand"] == "ЛУКОЙЛ" and "Катукова, 49" in data["address"]:
            data["is_working"] = "no"
            data["fuel_92"] = None
            data["fuel_95"] = None
            data["fuel_diesel"] = None
            data["queue"] = None
            data["reason"] = "Технический перерыв"
        outage_stations.append(StationRecord(**data))

    storage.save_snapshot(outage_stations, "https://example.test", outage_at)

    latest = storage.get_latest()
    assert latest is not None
    target = next(
        s
        for s in latest["stations"]
        if s["brand"] == "ЛУКОЙЛ" and "Катукова, 49" in s["address"]
    )
    assert target["is_working"] == "no"
    assert target["fuel_92"] == "yes"
    assert target["queue"] == "60_plus"


def test_summary_excludes_offline_from_fuel_share(tmp_path) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    working_at = datetime(2026, 7, 12, 8, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    storage.save_snapshot(stations, "https://example.test", working_at)

    baseline = storage.query_summary()[0]
    assert baseline["total"] == 24
    assert baseline["working_yes"] >= 1
    baseline_fuel_95 = baseline["fuel_95_yes"]

    outage_at = datetime(2026, 7, 12, 12, 59, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    outage_stations = []
    for station in stations:
        data = station.to_dict()
        if data["brand"] == "ЛУКОЙЛ" and "Катукова, 49" in data["address"]:
            assert data["is_working"] == "yes"
            assert data["fuel_95"] == "yes"
            data["is_working"] = "no"
            data["fuel_92"] = None
            data["fuel_95"] = None
            data["fuel_diesel"] = None
            data["queue"] = None
        outage_stations.append(StationRecord(**data))

    storage.save_snapshot(outage_stations, "https://example.test", outage_at)
    latest = storage.query_summary()[-1]

    assert latest["total"] == 24
    assert latest["working_yes"] == baseline["working_yes"] - 1
    assert latest["fuel_95_yes"] == baseline_fuel_95 - 1


def test_fuel_yes_since_uses_latest_spell_after_no() -> None:
    rows = [
        {"station_id": "a", "collected_at": "2026-07-11T17:09:20+03:00", "fuel_95": "yes"},
        {"station_id": "a", "collected_at": "2026-07-11T17:40:00+03:00", "fuel_95": "no"},
        {"station_id": "a", "collected_at": "2026-07-12T08:01:18+03:00", "fuel_95": "yes"},
        {"station_id": "a", "collected_at": "2026-07-12T09:21:18+03:00", "fuel_95": None},
        {"station_id": "a", "collected_at": "2026-07-12T10:31:18+03:00", "fuel_95": "yes"},
    ]
    assert fuel_yes_since(rows, "fuel_95") == "2026-07-12T08:01:18+03:00"


def test_fuel_yes_since_keeps_last_spell_when_fuel_lost() -> None:
    rows = [
        {"station_id": "a", "collected_at": "2026-07-11T17:09:20+03:00", "fuel_95": "yes"},
        {"station_id": "a", "collected_at": "2026-07-11T17:40:00+03:00", "fuel_95": "no"},
    ]
    assert fuel_yes_since(rows, "fuel_95") == "2026-07-11T17:09:20+03:00"


def test_skip_unchanged_snapshot_index(tmp_path):
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    first_at = datetime(2026, 7, 11, 17, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    second_at = datetime(2026, 7, 11, 17, 5, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    storage.save_snapshot(stations, "https://example.test", first_at)
    storage.save_snapshot(stations, "https://example.test", second_at)

    meta = storage.get_meta()
    assert meta["snapshot_count"] == 2
    assert meta["to"] == second_at.isoformat()

    latest = storage.get_latest()
    assert latest is not None
    assert latest["collected_at"] == second_at.isoformat()
    assert latest["station_count"] == 24


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
    segments = timeseries.json()
    assert len(segments) >= 24
    assert {"station_id", "metric", "start_at", "value"} <= set(segments[0])
    assert {row["station_id"] for row in segments}  # non-empty station set
    assert len({row["station_id"] for row in segments}) == 24

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    assert summary.json()[0]["total"] == 24

    lipetsk = client.get("/api/summary", params={"regions": ["lipetsk_city"]})
    assert lipetsk.status_code == 200
    assert lipetsk.json()[0]["total"] == 24

    yelets = client.get("/api/summary", params={"regions": ["yelets"]})
    assert yelets.status_code == 200
    assert yelets.json() == []

    fuel_since = client.get("/api/fuel-since")
    assert fuel_since.status_code == 200
    assert len(fuel_since.json()) == 24
    first = fuel_since.json()[0]
    assert "fuel_92_since" in first
    assert "fuel_95_since" in first
    assert "fuel_diesel_since" in first

    index = client.get("/")
    assert index.status_code == 200


def test_api_cache(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("API_CACHE_SECONDS", "120")

    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)
    storage.save_snapshot(stations, "https://example.test")

    from app import web
    from app.cache import TTLCache

    web.storage = storage
    web.api_cache = TTLCache(120)
    client = TestClient(web.app)

    first = client.get("/api/meta")
    second = client.get("/api/meta")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert first.headers["Cache-Control"] == "public, max-age=120"
    assert first.json() == second.json()


def test_build_cache_key_buckets_timestamps() -> None:
    from app.cache import build_cache_key

    key_a = build_cache_key(
        "timeseries",
        date_from="2026-07-12T11:12:37.000Z",
        date_to="2026-07-14T11:14:01.500Z",
    )
    key_b = build_cache_key(
        "timeseries",
        date_from="2026-07-12T11:14:59.000Z",
        date_to="2026-07-14T11:14:40.000Z",
    )
    assert key_a == key_b
    key_c = build_cache_key(
        "timeseries",
        date_from="2026-07-12T11:20:00.000Z",
        date_to="2026-07-14T11:14:40.000Z",
    )
    assert key_c != key_a


def test_read_model_segments_and_fuel_since(tmp_path) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)

    first_at = datetime(2026, 7, 12, 8, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    second_at = datetime(2026, 7, 12, 12, 59, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    storage.save_snapshot(stations, "https://example.test", first_at)

    segments = storage.query_timeseries()
    assert any(row["metric"] == "fuel_95" for row in segments)
    assert all(row["end_at"] is None for row in segments if row["metric"] == "fuel_95")

    outage_stations = []
    for station in stations:
        data = station.to_dict()
        if data["brand"] == "ЛУКОЙЛ" and "Катукова, 49" in data["address"]:
            data["is_working"] = "no"
            data["fuel_92"] = None
            data["fuel_95"] = None
            data["fuel_diesel"] = None
            data["queue"] = None
        outage_stations.append(StationRecord(**data))
    storage.save_snapshot(outage_stations, "https://example.test", second_at)

    target = next(
        row
        for row in storage.query_fuel_since()
        if row["brand"] == "ЛУКОЙЛ" and "Катукова, 49" in row["address"]
    )
    assert target["is_working"] == "no"
    assert target["fuel_95"] == "yes"
    assert target["fuel_95_since"] == first_at.isoformat()

    fuel_segments = [
        row
        for row in storage.query_timeseries()
        if row["brand"] == "ЛУКОЙЛ"
        and "Катукова, 49" in row["address"]
        and row["metric"] == "fuel_95"
    ]
    assert len(fuel_segments) == 2
    assert fuel_segments[0]["value"] == "yes"
    assert fuel_segments[0]["end_at"] == second_at.isoformat()
    assert fuel_segments[1]["value"] == "offline"
    assert fuel_segments[1]["end_at"] is None


def test_rebuild_read_models_from_observations(tmp_path) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    stations = parse_reports_html(html)
    storage = Storage(tmp_path)
    storage.save_snapshot(
        stations,
        "https://example.test",
        datetime(2026, 7, 12, 8, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )

    with storage._connect() as conn:
        conn.execute("DELETE FROM station_state")
        conn.execute("DELETE FROM timeline_segments")
        conn.execute("DELETE FROM summary_points")
        conn.commit()

    storage.rebuild_read_models()
    assert len(storage.query_fuel_since()) == 24
    assert storage.query_summary()[0]["total"] == 24
    assert len(storage.query_timeseries()) >= 24


def test_bootstrap_from_gzip(tmp_path, monkeypatch):
    from app.bootstrap import bootstrap_database_if_needed

    bootstrap_src = Path(__file__).resolve().parents[1] / "bootstrap" / "history.db.gz"
    if not bootstrap_src.exists():
        pytest.skip("bootstrap/history.db.gz not bundled")

    monkeypatch.setenv("BOOTSTRAP_MIN_SNAPSHOTS", "10")
    storage = Storage(tmp_path)
    assert storage.get_meta()["snapshot_count"] == 0

    installed = bootstrap_database_if_needed(storage)
    assert installed is True
    assert storage.get_meta()["snapshot_count"] >= 10

    # Second run should be a no-op.
    assert bootstrap_database_if_needed(storage) is False
