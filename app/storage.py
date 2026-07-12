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

CREATE INDEX IF NOT EXISTS idx_obs_collected_at ON observations(collected_at);
CREATE INDEX IF NOT EXISTS idx_obs_station_id ON observations(station_id);
CREATE INDEX IF NOT EXISTS idx_obs_brand ON observations(brand);
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


def fuel_yes_since(rows: list[dict[str, Any]], fuel_key: str) -> str | None:
    """First yes after the latest no while fuel is currently available."""
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: row["collected_at"])
    if ordered[-1].get(fuel_key) != "yes":
        return None

    since: str | None = None
    for row in ordered:
        value = row.get(fuel_key)
        if value == "no":
            since = None
        elif value == "yes" and since is None:
            since = row["collected_at"]
    return since


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
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

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
        payload = {
            "collected_at": collected_at.isoformat(),
            "source_url": source_url,
            "station_count": len(stations),
            "stations": [station.to_dict() for station in stations],
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
            logger.info("Saved snapshot %s (%s stations)", path, len(stations))
        elif index_status == "snapshot_only":
            logger.info(
                "Saved unchanged snapshot %s (%s stations, observations reused)",
                path,
                len(stations),
            )
        elif index_status == "duplicate":
            logger.info("Snapshot already indexed: %s", payload["collected_at"])
        elif save_snapshot_files_enabled():
            logger.info("Saved snapshot file only (DB unavailable): %s", path)
        else:
            logger.info("Snapshot unchanged, DB index skipped")
        return path

    def _get_last_signatures(self, conn: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
        rows = conn.execute(
            """
            SELECT station_id, is_working, fuel_92, fuel_95, fuel_diesel, queue,
                   reason, expected_working_at, last_report_at
            FROM observations o
            WHERE collected_at = (
                SELECT MAX(collected_at) FROM observations WHERE station_id = o.station_id
            )
            """
        ).fetchall()
        return {row["station_id"]: observation_signature(dict(row)) for row in rows}

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
        else:
            with self._connect() as conn:
                stations = conn.execute(
                    """
                    SELECT station_id, brand, name, address, district
                    FROM observations
                    GROUP BY station_id
                    ORDER BY brand, name
                    """
                ).fetchall()
            station_rows = [dict(row) for row in stations]

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
                with self._connect() as conn:
                    fallback = conn.execute(
                        """
                        SELECT station_id, brand, name, district, address, is_working,
                               fuel_92, fuel_95, fuel_diesel, queue, reason,
                               expected_working_at, last_report_at, collected_at
                        FROM observations
                        WHERE collected_at = (
                            SELECT MAX(collected_at) FROM observations
                        )
                        ORDER BY brand, name
                        """
                    ).fetchall()
                stations = [
                    {**dict(o), "collected_at": row["collected_at"]} for o in fallback
                ]

        return {
            "collected_at": row["collected_at"],
            "filepath": row["filepath"],
            "station_count": row["station_count"],
            "stations": stations,
        }

    def query_timeseries(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []

        if date_from:
            clauses.append("collected_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("collected_at <= ?")
            params.append(date_to)
        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            clauses.append(f"station_id IN ({placeholders})")
            params.extend(station_ids)
        if brands:
            placeholders = ",".join("?" for _ in brands)
            clauses.append(f"brand IN ({placeholders})")
            params.extend(brands)

        query = f"""
            SELECT collected_at, station_id, brand, name, address,
                   is_working, fuel_92, fuel_95, fuel_diesel, queue,
                   reason, expected_working_at, last_report_at
            FROM observations
            WHERE {' AND '.join(clauses)}
            ORDER BY collected_at ASC, brand ASC, name ASC
        """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return dedupe_consecutive_observations([dict(row) for row in rows])

    def query_summary(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []

        if date_from:
            clauses.append("collected_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("collected_at <= ?")
            params.append(date_to)
        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            clauses.append(f"station_id IN ({placeholders})")
            params.extend(station_ids)
        if brands:
            placeholders = ",".join("?" for _ in brands)
            clauses.append(f"brand IN ({placeholders})")
            params.extend(brands)
        if regions:
            districts = districts_for_regions(regions)
            if districts:
                placeholders = ",".join("?" for _ in districts)
                clauses.append(f"district IN ({placeholders})")
                params.extend(districts)
            else:
                return []

        query = f"""
            SELECT collected_at,
                   COUNT(*) AS total,
                   SUM(CASE WHEN fuel_92 = 'yes' THEN 1 ELSE 0 END) AS fuel_92_yes,
                   SUM(CASE WHEN fuel_95 = 'yes' THEN 1 ELSE 0 END) AS fuel_95_yes,
                   SUM(CASE WHEN fuel_diesel = 'yes' THEN 1 ELSE 0 END) AS fuel_diesel_yes,
                   SUM(CASE WHEN is_working = 'yes' THEN 1 ELSE 0 END) AS working_yes
            FROM observations
            WHERE {' AND '.join(clauses)}
            GROUP BY collected_at
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
        clauses = ["1=1"]
        params: list[Any] = []

        if date_from:
            clauses.append("collected_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("collected_at <= ?")
            params.append(date_to)
        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            clauses.append(f"station_id IN ({placeholders})")
            params.extend(station_ids)
        if brands:
            placeholders = ",".join("?" for _ in brands)
            clauses.append(f"brand IN ({placeholders})")
            params.extend(brands)

        query = f"""
            SELECT collected_at, station_id, brand, name, address,
                   fuel_92, fuel_95, fuel_diesel, is_working, queue
            FROM observations
            WHERE {' AND '.join(clauses)}
            ORDER BY station_id ASC, collected_at ASC
        """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {}
        station_meta: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            station_id = item["station_id"]
            grouped.setdefault(station_id, []).append(item)
            station_meta[station_id] = {
                "station_id": station_id,
                "brand": item["brand"],
                "name": item["name"],
                "address": item["address"],
            }

        latest_by_station = self._latest_by_station(station_ids, brands)
        result: list[dict[str, Any]] = []
        for station_id in sorted(
            station_meta,
            key=lambda sid: (station_meta[sid]["brand"], station_meta[sid]["name"]),
        ):
            meta = station_meta[station_id]
            station_rows = dedupe_consecutive_observations(grouped[station_id])
            latest = latest_by_station.get(station_id, {})
            result.append(
                {
                    **meta,
                    "fuel_92_since": fuel_yes_since(station_rows, "fuel_92"),
                    "fuel_95_since": fuel_yes_since(station_rows, "fuel_95"),
                    "fuel_diesel_since": fuel_yes_since(station_rows, "fuel_diesel"),
                    "fuel_92": latest.get("fuel_92"),
                    "fuel_95": latest.get("fuel_95"),
                    "fuel_diesel": latest.get("fuel_diesel"),
                    "is_working": latest.get("is_working"),
                    "queue": latest.get("queue"),
                }
            )
        return result

    def _latest_by_station(
        self,
        station_ids: list[str] | None = None,
        brands: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
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
            SELECT station_id, fuel_92, fuel_95, fuel_diesel, is_working, queue
            FROM observations o
            WHERE {' AND '.join(clauses)}
              AND collected_at = (
                  SELECT MAX(collected_at) FROM observations WHERE station_id = o.station_id
              )
        """

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {row["station_id"]: dict(row) for row in rows}
