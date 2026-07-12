from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

MOSCOW = ZoneInfo("Europe/Moscow")

YES_VALUES = {"да", "yes", "true", "1"}
NO_VALUES = {"нет", "no", "false", "0"}
QUEUE_MAP = {
    "нет": "none",
    "до 30": "up_to_30",
    "30-60": "30_60",
    "60+": "60_plus",
}


def normalize_yes_no(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text or text == "-":
        return None
    if text in YES_VALUES:
        return "yes"
    if text in NO_VALUES:
        return "no"
    return None


def normalize_queue(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text or text == "-":
        return None
    return QUEUE_MAP.get(text, text)


def parse_report_datetime(value: str | None) -> str | None:
    if not value or value.strip() in {"", "-"}:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=MOSCOW)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def make_station_id(brand: str, address: str, name: str) -> str:
    key = f"{brand}|{address}|{name}".lower().strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def cell_text(cell) -> str:
    return cell.get_text(" ", strip=True)


@dataclass
class StationRecord:
    id: str
    brand: str
    name: str
    district: str
    address: str
    is_working: str | None
    fuel_92: str | None
    fuel_95: str | None
    fuel_diesel: str | None
    queue: str | None
    reason: str | None
    expected_working_at: str | None
    last_report_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_reports_html(html: str) -> list[StationRecord]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("Table not found in HTML")

    rows = table.find("tbody")
    if rows is None:
        return []

    stations: list[StationRecord] = []
    for row in rows.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 11:
            continue

        brand_el = cells[0].find("strong")
        brand = brand_el.get_text(strip=True) if brand_el else cell_text(cells[0])
        name = cell_text(cells[0])
        district = cell_text(cells[1])
        address = cell_text(cells[2])
        station_id = make_station_id(brand, address, name)

        reason = cell_text(cells[8])
        expected = cell_text(cells[9])

        stations.append(
            StationRecord(
                id=station_id,
                brand=brand,
                name=name,
                district=district,
                address=address,
                is_working=normalize_yes_no(cell_text(cells[3])),
                fuel_92=normalize_yes_no(cell_text(cells[4])),
                fuel_95=normalize_yes_no(cell_text(cells[5])),
                fuel_diesel=normalize_yes_no(cell_text(cells[6])),
                queue=normalize_queue(cell_text(cells[7])),
                reason=None if reason == "-" else reason,
                expected_working_at=None if expected == "-" else expected,
                last_report_at=parse_report_datetime(cell_text(cells[10])),
            )
        )

    return stations
