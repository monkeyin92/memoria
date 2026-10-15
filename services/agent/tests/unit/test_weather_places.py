from __future__ import annotations

import pytest
from services.agent.src.providers.weather_places import _DISTRICTS, City, find_city


def test_every_district_parent_is_one_city() -> None:
    names = {city.standard_name for city in City}
    assert {parent for _alias, parent in _DISTRICTS} <= names


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("南京明天天气怎么样", "南京"),
        ("我是小明，我家在海淀区，现在天气怎么样", "北京"),
        ("浦东天气怎么样", "上海"),
        ("江苏省南京市未来三天天气", "南京"),
        ("从今天起未来三天南京天气", "南京"),
        ("未来三天六盘水天气", "六盘水"),
        ("乌鲁木齐天气", "乌鲁木齐"),
        ("大理天气怎么样", "大理州"),
        ("朝阳天气怎么样", "朝阳"),
        ("吉林省吉林市天气", "吉林"),
        ("海口天气怎么样", "海口"),
        ("香港天气怎么样", "香港"),
    ],
)
def test_find_city_maps_one_allowlisted_place(query: str, expected: str) -> None:
    assert find_city(query) is City[expected]


@pytest.mark.parametrize(
    "query",
    [
        "天气南京怎么样",
        "东京天气怎么样",
        "今天天气怎么样",
        "北京和上海哪里热天气",
        "南京路天气怎么样",
        "朝阳区天气怎么样",
        "鼓楼区天气",
        "通州区天气",
        "海南天气怎么样",
        "吉林省天气怎么样",
        "东方明珠天气怎么样",
    ],
)
def test_find_city_asks_when_the_place_is_missing_or_ambiguous(query: str) -> None:
    assert find_city(query) is None


def test_wire_name_is_the_qweather_standard_name() -> None:
    nanjing = City["南京"]
    assert (nanjing.standard_name, nanjing.province) == ("南京", "江苏省")
    assert City["北京"].province == "北京市"
    assert City["香港"].province == "香港特别行政区"
    assert len(list(City)) == 339
