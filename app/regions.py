from __future__ import annotations

from typing import Any

# Кластеры для дашборда: без одиночных МО в фильтре, но все сырые district сохраняются в БД.
REGION_GROUPS: list[dict[str, Any]] = [
    {
        "id": "lipetsk_city",
        "label": "Липецк",
        "districts": ["г.о. город Липецк"],
    },
    {
        "id": "yelets",
        "label": "Елец",
        "districts": ["г.о. город Елец"],
    },
    {
        "id": "lipetsk_suburbs",
        "label": "Пригород Липецка",
        "districts": ["Липецкий муниципальный округ"],
    },
    {
        "id": "south",
        "label": "Лебедянь и юг",
        "districts": [
            "Лебедянский муниципальный округ",
            "Задонский муниципальный округ",
            "Данковский муниципальный округ",
            "Добринский муниципальный округ",
            "Долгоруковский муниципальный округ",
        ],
    },
    {
        "id": "center",
        "label": "Грязи и центр",
        "districts": [
            "Грязинский муниципальный округ",
            "Добровский муниципальный округ",
            "Усманский муниципальный округ",
        ],
    },
    {
        "id": "east",
        "label": "Чаплыгин и восток",
        "districts": [
            "Чаплыгинский муниципальный округ",
            "Становлянский муниципальный округ",
            "Измалковский муниципальный округ",
            "Тербунский муниципальный округ",
        ],
    },
    {
        "id": "north_west",
        "label": "Север и запад",
        "districts": [
            "Хлевенский муниципальный округ",
            "Краснинский муниципальный округ",
            "Левтолстовский муниципальный район",
            "Воловский муниципальный округ",
        ],
    },
]

DISTRICT_TO_REGION: dict[str, str] = {
    district: group["id"]
    for group in REGION_GROUPS
    for district in group["districts"]
}

REGION_LABELS: dict[str, str] = {group["id"]: group["label"] for group in REGION_GROUPS}


def region_group_for_district(district: str | None) -> str | None:
    if not district:
        return None
    return DISTRICT_TO_REGION.get(district)


def districts_for_regions(region_ids: list[str]) -> list[str]:
    wanted = set(region_ids)
    districts: list[str] = []
    for group in REGION_GROUPS:
        if group["id"] in wanted:
            districts.extend(group["districts"])
    return districts


def build_region_meta(stations: list[dict[str, Any]]) -> dict[str, Any]:
    district_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {group["id"]: 0 for group in REGION_GROUPS}
    unknown_districts: dict[str, int] = {}

    for station in stations:
        district = station.get("district") or ""
        district_counts[district] = district_counts.get(district, 0) + 1
        region_id = region_group_for_district(district)
        if region_id:
            region_counts[region_id] += 1
        elif district:
            unknown_districts[district] = unknown_districts.get(district, 0) + 1

    region_groups = [
        {
            "id": group["id"],
            "label": group["label"],
            "count": region_counts[group["id"]],
            "districts": list(group["districts"]),
        }
        for group in REGION_GROUPS
        if region_counts[group["id"]] > 0
    ]

    districts = [
        {"name": name, "count": count, "region_group": region_group_for_district(name)}
        for name, count in sorted(district_counts.items(), key=lambda item: (-item[1], item[0]))
        if name
    ]

    return {
        "region_groups": region_groups,
        "districts": districts,
        "unknown_districts": [
            {"name": name, "count": count}
            for name, count in sorted(unknown_districts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }
