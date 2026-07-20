from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings, save_snapshot_files_enabled
from app.parser import StationRecord
from app.regions import build_region_meta, districts_for_regions, region_group_for_district

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo(settings.timezone)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL UNIQUE,
    filepath TEXT NOT NULL,
    station_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    station_id TEXT NOT NULL,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,
    district TEXT NOT NULL,
    address TEXT NOT NULL,
    is_working TEXT,
    fuel_92 TEXT,
    fuel_95 TEXT,
    fuel_diesel TEXT,
    queue TEXT,
    reason TEXT,
    expected_working_at TEXT,
    last_report_at TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);

CREATE TABLE IF NOT EXISTS station_state (
    station_id TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,
    district TEXT NOT NULL,
    address TEXT NOT NULL,
    is_working TEXT,
    fuel_92 TEXT,
    fuel_95 TEXT,
    fuel_diesel TEXT,
    queue TEXT,
    reason TEXT,
    expected_working_at TEXT,
    last_report_at TEXT,
    fuel_92_since TEXT,
    fuel_95_since TEXT,
    fuel_diesel_since TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    value TEXT
);

CREATE TABLE IF NOT EXISTS summary_points (
    collected_at TEXT NOT NULL,
    region_id TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL,
    fuel_92_yes INTEGER NOT NULL,
    fuel_95_yes INTEGER NOT NULL,
    fuel_diesel_yes INTEGER NOT NULL,
    working_yes INTEGER NOT NULL,
    PRIMARY KEY (collected_at, region_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_collected_at ON observations(collected_at);
CREATE INDEX IF NOT EXISTS idx_obs_station_id ON observations(station_id);
CREATE INDEX IF NOT EXISTS idx_obs_brand ON observations(brand);
CREATE INDEX IF NOT EXISTS idx_obs_station_collected
    ON observations(station_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_segments_range
    ON timeline_segments(metric, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_segments_station
    ON timeline_segments(station_id, metric, start_at);
CREATE INDEX IF NOT EXISTS idx_summary_collected
    ON summary_points(collected_at);
CREATE INDEX IF NOT EXISTS idx_summary_region_collected
    ON summary_points(region_id, collected_at);
"""

COMPARE_FIELDS = (
    "is_working",
    "fuel_92",
    "fuel_95",
    "fuel_diesel",
    "queue",
    "reason",
    "expected_working_at",
    "last_report_at",
)

SEGMENT_METRICS = ("is_working", "fuel_92", "fuel_95", "fuel_diesel", "queue")
FUEL_KEYS = ("fuel_92", "fuel_95", "fuel_diesel")
OUTAGE_CARRY_KEYS = (*FUEL_KEYS, "queue")
GLOBAL_REGION_ID = ""


def observation_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in COMPARE_FIELDS)


def dedupe_consecutive_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_signature: dict[str, tuple[Any, ...]] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        station_id = row["station_id"]
        signature = observation_signature(row)
        if last_signature.get(station_id) == signature:
            continue
        last_signature[station_id] = signature
        result.append(row)
    return result


def dedupe_consecutive_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_fields = ("total", "fuel_92_yes", "fuel_95_yes", "fuel_diesel_yes", "working_yes")
    last_signature: tuple[Any, ...] | None = None
    result: list[dict[str, Any]] = []
    for row in rows:
        signature = tuple(row[field] for field in summary_fields)
        if signature == last_signature:
            continue
        last_signature = signature
        result.append(row)
    return result


def apply_carry_forward(
    rows: list[dict[str, Any]],
    *,
    already_sorted: bool = False,
) -> list[dict[str, Any]]:
    if not rows:
        return rows

    carry_by_station: dict[str, dict[str, str | None]] = {}
    enriched_by_index: dict[int, dict[str, Any]] = {}
    ordered = (
        enumerate(rows)
        if already_sorted
        else sorted(enumerate(rows), key=lambda item: item[1]["collected_at"])
    )
    for idx, row in ordered:
        carry = carry_by_station.setdefault(row["station_id"], {})
        enriched = dict(row)
        for key in OUTAGE_CARRY_KEYS:
            value = row.get(key)
            if value is not None:
                carry[key] = value
                enriched[key] = value
            elif row.get("is_working") == "no" and carry.get(key) is not None:
                enriched[key] = carry[key]
            else:
                enriched[key] = value
        enriched_by_index[idx] = enriched
    return [enriched_by_index[index] for index in range(len(rows))]


def carry_forward_station_fuels(
    station: dict[str, Any],
    previous: dict[str, str | None] | None,
) -> None:
    if station.get("is_working") != "no" or not previous:
        return
    for key in OUTAGE_CARRY_KEYS:
        if station.get(key) is None and previous.get(key) is not None:
            station[key] = previous[key]


def _needs_outage_carry(station: dict[str, Any]) -> bool:
    return station.get("is_working") == "no" and any(
        station.get(key) is None for key in OUTAGE_CARRY_KEYS
    )


def fuel_yes_since(rows: list[dict[str, Any]], fuel_key: str) -> str | None:
    """Start of the most recent yes spell after a no within the selected period."""
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: row["collected_at"])
    since: str | None = None
    last_spell_start: str | None = None
    for row in ordered:
        value = row.get(fuel_key)
        if value == "no":
            since = None
        elif value == "yes" and since is None:
            since = row["collected_at"]
            last_spell_start = since
    return last_spell_start


def next_fuel_since(
    previous_fuel: str | None,
    previous_since: str | None,
    new_fuel: str | None,
    collected_at: str,
) -> str | None:
    if new_fuel == "yes":
        if previous_fuel != "yes":
            return collected_at
        return previous_since or collected_at
    return previous_since


def segment_value_for_metric(metric: str, state: dict[str, Any]) -> str | None:
    if metric in FUEL_KEYS:
        if state.get("is_working") == "no":
            return "offline"
        value = state.get(metric)
        return value if value is not None else None
    if metric == "queue":
        return state.get("queue")
    if metric == "is_working":
        return state.get("is_working")
    return None


def summarize_stations(stations: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(stations),
        "fuel_92_yes": sum(
            1
            for station in stations
            if station.get("is_working") == "yes" and station.get("fuel_92") == "yes"
        ),
        "fuel_95_yes": sum(
            1
            for station in stations
            if station.get("is_working") == "yes" and station.get("fuel_95") == "yes"
        ),
        "fuel_diesel_yes": sum(
            1
            for station in stations
            if station.get("is_working") == "yes" and station.get("fuel_diesel") == "yes"
        ),
        "working_yes": sum(
            1 for station in stations if station.get("is_working") == "yes"
        ),
    }


class Storage:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or settings.data_dir
        self.snapshots_dir = self.data_dir / "snapshots"
        self.db_path = self.data_dir / "history.db"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            needs_rebuild = self._read_models_need_rebuild(conn)
        if needs_rebuild:
            logger.info("Rebuilding read models from observations")
            self.rebuild_read_models()

    def _read_models_need_rebuild(self, conn: sqlite3.Connection) -> bool:
        observations = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        if not observations:
            return False
        state_count = conn.execute("SELECT COUNT(*) FROM station_state").fetchone()[0]
        return state_count == 0

    def snapshot_path(self, collected_at: datetime) -> Path:
        local = collected_at.astimezone(MOSCOW)
        filename = local.strftime("%Y-%m-%dT%H-%M-%S+03-00.json")
        return (
            self.snapshots_dir
            / str(local.year)
            / f"{local.month:02d}"
            / f"{local.day:02d}"
            / filename
        )

    def save_snapshot(
        self,
        stations: list[StationRecord],
        source_url: str,
        collected_at: datetime | None = None,
    ) -> Path:
        collected_at = collected_at or datetime.now(MOSCOW)
        stations_data = [station.to_dict() for station in stations]
        with self._connect() as conn:
            self._carry_forward_fuels_on_save(conn, stations_data)
        payload = {
            "collected_at": collected_at.isoformat(),
            "source_url": source_url,
            "station_count": len(stations_data),
            "stations": stations_data,
        }

        path = self.snapshot_path(collected_at)
        if save_snapshot_files_enabled():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
                suffix=".tmp",
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        index_status = self._index_snapshot(path, payload)
        if index_status == "full":
            logger.info("Saved snapshot %s (%s stations)", path, len(stations_data))
        elif index_status == "snapshot_only":
            logger.info(
                "Saved unchanged snapshot %s (%s stations, observations reused)",
                path,
                len(stations_data),
            )
        elif index_status == "duplicate":
            logger.info("Snapshot already indexed: %s", payload["collected_at"])
        elif save_snapshot_files_enabled():
            logger.info("Saved snapshot file only (DB unavailable): %s", path)
        else:
            logger.info("Snapshot unchanged, DB index skipped")
        return path

    def _load_station_state_map(
        self, conn: sqlite3.Connection
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute("SELECT * FROM station_state").fetchall()
        return {row["station_id"]: dict(row) for row in rows}

    def _get_last_signatures(self, conn: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
        state = self._load_station_state_map(conn)
        if state:
            return {
                station_id: observation_signature(row)
                for station_id, row in state.items()
            }
        rows = conn.execute(
            """
            SELECT o.station_id, o.is_working, o.fuel_92, o.fuel_95, o.fuel_diesel, o.queue,
                   o.reason, o.expected_working_at, o.last_report_at
            FROM observations o
            INNER JOIN (
                SELECT station_id, MAX(collected_at) AS max_at
                FROM observations
                GROUP BY station_id
            ) latest
              ON latest.station_id = o.station_id
             AND latest.max_at = o.collected_at
            """
        ).fetchall()
        return {row["station_id"]: observation_signature(dict(row)) for row in rows}

    def _carry_forward_fuels_on_save(
        self, conn: sqlite3.Connection, stations: list[dict[str, Any]]
    ) -> None:
        need = [station for station in stations if _needs_outage_carry(station)]
        if not need:
            return
        state = self._load_station_state_map(conn)
        for station in need:
            previous = state.get(station["id"])
            if previous:
                carry_forward_station_fuels(station, previous)
                continue
            history = conn.execute(
                """
                SELECT station_id, collected_at, is_working, fuel_92, fuel_95, fuel_diesel, queue
                FROM observations
                WHERE station_id = ?
                ORDER BY collected_at ASC
                """,
                (station["id"],),
            ).fetchall()
            if not history:
                continue
            enriched = apply_carry_forward(
                [dict(row) for row in history], already_sorted=True
            )
            carry_forward_station_fuels(station, enriched[-1])

    def _close_open_segment(
        self,
        conn: sqlite3.Connection,
        station_id: str,
        metric: str,
        end_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE timeline_segments
            SET end_at = ?
            WHERE station_id = ?
              AND metric = ?
              AND end_at IS NULL
            """,
            (end_at, station_id, metric),
        )

    def _open_segment(
        self,
        conn: sqlite3.Connection,
        station_id: str,
        metric: str,
        start_at: str,
        value: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO timeline_segments (station_id, metric, start_at, end_at, value)
            VALUES (?, ?, ?, NULL, ?)
            """,
            (station_id, metric, start_at, value),
        )

    def _upsert_station_state(
        self,
        conn: sqlite3.Connection,
        state: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO station_state (
                station_id, brand, name, district, address, is_working,
                fuel_92, fuel_95, fuel_diesel, queue, reason,
                expected_working_at, last_report_at,
                fuel_92_since, fuel_95_since, fuel_diesel_since, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                brand = excluded.brand,
                name = excluded.name,
                district = excluded.district,
                address = excluded.address,
                is_working = excluded.is_working,
                fuel_92 = excluded.fuel_92,
                fuel_95 = excluded.fuel_95,
                fuel_diesel = excluded.fuel_diesel,
                queue = excluded.queue,
                reason = excluded.reason,
                expected_working_at = excluded.expected_working_at,
                last_report_at = excluded.last_report_at,
                fuel_92_since = excluded.fuel_92_since,
                fuel_95_since = excluded.fuel_95_since,
                fuel_diesel_since = excluded.fuel_diesel_since,
                updated_at = excluded.updated_at
            """,
            (
                state["station_id"],
                state["brand"],
                state["name"],
                state["district"],
                state["address"],
                state.get("is_working"),
                state.get("fuel_92"),
                state.get("fuel_95"),
                state.get("fuel_diesel"),
                state.get("queue"),
                state.get("reason"),
                state.get("expected_working_at"),
                state.get("last_report_at"),
                state.get("fuel_92_since"),
                state.get("fuel_95_since"),
                state.get("fuel_diesel_since"),
                state["updated_at"],
            ),
        )

    def _write_summary_points(
        self,
        conn: sqlite3.Connection,
        collected_at: str,
        states: list[dict[str, Any]],
    ) -> None:
        by_region: dict[str, list[dict[str, Any]]] = {GLOBAL_REGION_ID: states}
        for station in states:
            region_id = region_group_for_district(station.get("district")) or GLOBAL_REGION_ID
            if region_id == GLOBAL_REGION_ID:
                continue
            by_region.setdefault(region_id, []).append(station)

        for region_id, subset in by_region.items():
            summary = summarize_stations(subset)
            conn.execute(
                """
                INSERT INTO summary_points (
                    collected_at, region_id, total, fuel_92_yes, fuel_95_yes,
                    fuel_diesel_yes, working_yes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collected_at, region_id) DO UPDATE SET
                    total = excluded.total,
                    fuel_92_yes = excluded.fuel_92_yes,
                    fuel_95_yes = excluded.fuel_95_yes,
                    fuel_diesel_yes = excluded.fuel_diesel_yes,
                    working_yes = excluded.working_yes
                """,
                (
                    collected_at,
                    region_id,
                    summary["total"],
                    summary["fuel_92_yes"],
                    summary["fuel_95_yes"],
                    summary["fuel_diesel_yes"],
                    summary["working_yes"],
                ),
            )

    def _apply_read_model(
        self,
        conn: sqlite3.Connection,
        collected_at: str,
        stations: list[dict[str, Any]],
        previous_state: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        previous_state = (
            previous_state
            if previous_state is not None
            else self._load_station_state_map(conn)
        )
        next_state = dict(previous_state)

        for raw in stations:
            station_id = raw.get("station_id") or raw["id"]
            previous = previous_state.get(station_id)
            current = {
                "station_id": station_id,
                "brand": raw["brand"],
                "name": raw["name"],
                "district": raw["district"],
                "address": raw["address"],
                "is_working": raw.get("is_working"),
                "fuel_92": raw.get("fuel_92"),
                "fuel_95": raw.get("fuel_95"),
                "fuel_diesel": raw.get("fuel_diesel"),
                "queue": raw.get("queue"),
                "reason": raw.get("reason"),
                "expected_working_at": raw.get("expected_working_at"),
                "last_report_at": raw.get("last_report_at"),
                "updated_at": collected_at,
            }
            carry_forward_station_fuels(current, previous)

            for fuel_key in FUEL_KEYS:
                current[f"{fuel_key}_since"] = next_fuel_since(
                    previous.get(fuel_key) if previous else None,
                    previous.get(f"{fuel_key}_since") if previous else None,
                    current.get(fuel_key),
                    collected_at,
                )

            for metric in SEGMENT_METRICS:
                new_value = segment_value_for_metric(metric, current)
                old_value = (
                    segment_value_for_metric(metric, previous)
                    if previous
                    else object()
                )
                if previous is None or new_value != old_value:
                    if previous is not None:
                        self._close_open_segment(conn, station_id, metric, collected_at)
                    self._open_segment(
                        conn, station_id, metric, collected_at, new_value
                    )

            self._upsert_station_state(conn, current)
            next_state[station_id] = current

        self._write_summary_points(conn, collected_at, list(next_state.values()))
        return next_state

    def rebuild_read_models(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM station_state")
            conn.execute("DELETE FROM timeline_segments")
            conn.execute("DELETE FROM summary_points")

            timestamps = conn.execute(
                """
                SELECT DISTINCT collected_at
                FROM observations
                ORDER BY collected_at ASC
                """
            ).fetchall()
            state: dict[str, dict[str, Any]] = {}
            for (collected_at,) in timestamps:
                rows = conn.execute(
                    """
                    SELECT station_id, brand, name, district, address, is_working,
                           fuel_92, fuel_95, fuel_diesel, queue, reason,
                           expected_working_at, last_report_at
                    FROM observations
                    WHERE collected_at = ?
                    ORDER BY brand, name
                    """,
                    (collected_at,),
                ).fetchall()
                stations = [dict(row) for row in rows]
                state = self._apply_read_model(conn, collected_at, stations, state)
            conn.commit()
            logger.info(
                "Read models rebuilt: %s stations, %s segments, %s summary points",
                conn.execute("SELECT COUNT(*) FROM station_state").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM timeline_segments").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM summary_points").fetchone()[0],
            )

    def _index_snapshot(self, path: Path, payload: dict[str, Any]) -> str:
        collected_at = payload["collected_at"]
        stations = payload["stations"]

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM snapshots WHERE collected_at = ?",
                (collected_at,),
            ).fetchone()
            if existing:
                return "duplicate"

            last_signatures = self._get_last_signatures(conn)
            unchanged = bool(last_signatures) and all(
                last_signatures.get(station["id"]) == observation_signature(station)
                for station in stations
            )

            cursor = conn.execute(
                """
                INSERT INTO snapshots (collected_at, filepath, station_count)
                VALUES (?, ?, ?)
                """,
                (collected_at, str(path), len(stations)),
            )
            snapshot_id = cursor.lastrowid

            if not unchanged:
                conn.executemany(
                    """
                    INSERT INTO observations (
                        snapshot_id, collected_at, station_id, brand, name, district,
                        address, is_working, fuel_92, fuel_95, fuel_diesel, queue,
                        reason, expected_working_at, last_report_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snapshot_id,
                            collected_at,
                            s["id"],
                            s["brand"],
                            s["name"],
                            s["district"],
                            s["address"],
                            s.get("is_working"),
                            s.get("fuel_92"),
                            s.get("fuel_95"),
                            s.get("fuel_diesel"),
                            s.get("queue"),
                            s.get("reason"),
                            s.get("expected_working_at"),
                            s.get("last_report_at"),
                        )
                        for s in stations
                    ],
                )
                self._apply_read_model(conn, collected_at, stations)

            conn.commit()
            return "snapshot_only" if unchanged else "full"

    def _stations_from_payload(
        self, payload: dict[str, Any], collected_at: str | None = None
    ) -> list[dict[str, Any]]:
        at = collected_at or payload["collected_at"]
        return [
            {
                "station_id": station["id"],
                "brand": station["brand"],
                "name": station["name"],
                "district": station["district"],
                "address": station["address"],
                "is_working": station.get("is_working"),
                "fuel_92": station.get("fuel_92"),
                "fuel_95": station.get("fuel_95"),
                "fuel_diesel": station.get("fuel_diesel"),
                "queue": station.get("queue"),
                "reason": station.get("reason"),
                "expected_working_at": station.get("expected_working_at"),
                "last_report_at": station.get("last_report_at"),
                "collected_at": at,
            }
            for station in payload["stations"]
        ]

    def _load_snapshot_payload(self, filepath: str) -> dict[str, Any] | None:
        path = Path(filepath)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read snapshot file %s: %s", path, exc)
            return None

    def get_meta(self) -> dict[str, Any]:
        with self._connect() as conn:
            range_row = conn.execute(
                """
                SELECT MIN(collected_at) AS min_at, MAX(collected_at) AS max_at,
                       COUNT(*) AS snapshot_count
                FROM snapshots
                """
            ).fetchone()
            stations = conn.execute(
                """
                SELECT station_id, brand, name, address, district
                FROM station_state
                ORDER BY brand, name
                """
            ).fetchall()

        station_rows = [dict(row) for row in stations]
        if not station_rows:
            latest = self.get_latest()
            if latest and latest.get("stations"):
                station_rows = [
                    {
                        "station_id": station["station_id"],
                        "brand": station["brand"],
                        "name": station["name"],
                        "address": station["address"],
                        "district": station.get("district"),
                    }
                    for station in latest["stations"]
                ]

        for station in station_rows:
            station["region_group"] = region_group_for_district(station.get("district"))

        brands = sorted({station["brand"] for station in station_rows})
        region_meta = build_region_meta(station_rows)

        return {
            "snapshot_count": range_row["snapshot_count"] or 0,
            "from": range_row["min_at"],
            "to": range_row["max_at"],
            "station_count": len(station_rows),
            "brands": brands,
            "stations": station_rows,
            **region_meta,
        }

    def get_latest(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT collected_at, filepath, station_count
                FROM snapshots
                ORDER BY collected_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None

            # Only stations seen in the latest scrape — station_state keeps
            # historical rows that would inflate yes/total past 100%.
            state_rows = conn.execute(
                """
                SELECT station_id, brand, name, district, address, is_working,
                       fuel_92, fuel_95, fuel_diesel, queue, reason,
                       expected_working_at, last_report_at, updated_at AS collected_at
                FROM station_state
                WHERE updated_at = ?
                ORDER BY brand, name
                """,
                (row["collected_at"],),
            ).fetchall()

        if state_rows:
            stations = [dict(station) for station in state_rows]
            return {
                "collected_at": row["collected_at"],
                "filepath": row["filepath"],
                "station_count": len(stations),
                "stations": stations,
            }

        with self._connect() as conn:
            observations = conn.execute(
                """
                SELECT station_id, brand, name, district, address, is_working,
                       fuel_92, fuel_95, fuel_diesel, queue, reason,
                       expected_working_at, last_report_at, collected_at
                FROM observations
                WHERE collected_at = ?
                ORDER BY brand, name
                """,
                (row["collected_at"],),
            ).fetchall()

        if observations:
            stations = [dict(o) for o in observations]
        else:
            payload = self._load_snapshot_payload(row["filepath"])
            if payload:
                stations = self._stations_from_payload(payload, row["collected_at"])
            else:
                stations = []

        return {
            "collected_at": row["collected_at"],
            "filepath": row["filepath"],
            "station_count": len(stations),
            "stations": stations,
        }

    def query_timeseries(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return timeline segments overlapping the requested range."""
        clauses = ["1=1"]
        params: list[Any] = []

        if date_from:
            clauses.append("(s.end_at IS NULL OR s.end_at > ?)")
            params.append(date_from)
        if date_to:
            clauses.append("s.start_at < ?")
            params.append(date_to)
        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            clauses.append(f"s.station_id IN ({placeholders})")
            params.extend(station_ids)
        if brands:
            placeholders = ",".join("?" for _ in brands)
            clauses.append(f"st.brand IN ({placeholders})")
            params.extend(brands)

        query = f"""
            SELECT s.station_id, st.brand, st.name, st.address,
                   s.metric, s.start_at, s.end_at, s.value
            FROM timeline_segments s
            JOIN station_state st ON st.station_id = s.station_id
            WHERE {' AND '.join(clauses)}
            ORDER BY st.brand ASC, st.name ASC, s.metric ASC, s.start_at ASC
        """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def query_summary(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        _ = station_ids, brands

        clauses = ["1=1"]
        params: list[Any] = []

        if date_from:
            clauses.append("collected_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("collected_at <= ?")
            params.append(date_to)

        if regions:
            valid = [region for region in regions if region]
            if not valid:
                return []
            districts = districts_for_regions(valid)
            if not districts:
                return []
            placeholders = ",".join("?" for _ in valid)
            clauses.append(f"region_id IN ({placeholders})")
            params.extend(valid)
            query = f"""
                SELECT collected_at,
                       SUM(total) AS total,
                       SUM(fuel_92_yes) AS fuel_92_yes,
                       SUM(fuel_95_yes) AS fuel_95_yes,
                       SUM(fuel_diesel_yes) AS fuel_diesel_yes,
                       SUM(working_yes) AS working_yes
                FROM summary_points
                WHERE {' AND '.join(clauses)}
                GROUP BY collected_at
                ORDER BY collected_at ASC
            """
        else:
            clauses.append("region_id = ?")
            params.append(GLOBAL_REGION_ID)
            query = f"""
                SELECT collected_at, total, fuel_92_yes, fuel_95_yes,
                       fuel_diesel_yes, working_yes
                FROM summary_points
                WHERE {' AND '.join(clauses)}
                ORDER BY collected_at ASC
            """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def query_fuel_since(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        _ = date_from, date_to
        clauses = ["1=1"]
        params: list[Any] = []

        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            clauses.append(f"station_id IN ({placeholders})")
            params.extend(station_ids)
        if brands:
            placeholders = ",".join("?" for _ in brands)
            clauses.append(f"brand IN ({placeholders})")
            params.extend(brands)

        query = f"""
            SELECT station_id, brand, name, address,
                   fuel_92_since, fuel_95_since, fuel_diesel_since,
                   fuel_92, fuel_95, fuel_diesel, is_working, queue
            FROM station_state
            WHERE {' AND '.join(clauses)}
            ORDER BY brand ASC, name ASC
        """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
