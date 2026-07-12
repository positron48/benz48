
from app.regions import REGION_GROUPS, build_region_meta, region_group_for_district


def test_region_group_for_district():
    assert region_group_for_district("г.о. город Липецк") == "lipetsk_city"
    assert region_group_for_district("г.о. город Елец") == "yelets"
    assert region_group_for_district("Воловский муниципальный округ") == "north_west"
    assert region_group_for_district("unknown") is None


def test_build_region_meta_counts():
    stations = [
        {"district": "г.о. город Липецк"},
        {"district": "г.о. город Липецк"},
        {"district": "г.о. город Елец"},
        {"district": "Воловский муниципальный округ"},
    ]
    meta = build_region_meta(stations)
    lipetsk = next(g for g in meta["region_groups"] if g["id"] == "lipetsk_city")
    assert lipetsk["count"] == 2
    assert meta["districts"][0]["name"] == "г.о. город Липецк"


def test_all_live_districts_are_mapped():
    live_districts = [
        "г.о. город Липецк",
        "г.о. город Елец",
        "Липецкий муниципальный округ",
        "Добровский муниципальный округ",
        "Лебедянский муниципальный округ",
        "Грязинский муниципальный округ",
        "Чаплыгинский муниципальный округ",
        "Становлянский муниципальный округ",
        "Хлевенский муниципальный округ",
        "Задонский муниципальный округ",
        "Краснинский муниципальный округ",
        "Данковский муниципальный округ",
        "Левтолстовский муниципальный район",
        "Измалковский муниципальный округ",
        "Тербунский муниципальный округ",
        "Усманский муниципальный округ",
        "Добринский муниципальный округ",
        "Воловский муниципальный округ",
        "Долгоруковский муниципальный округ",
    ]
    mapped = {
        district
        for group in REGION_GROUPS
        for district in group["districts"]
    }
    assert set(live_districts) == mapped
