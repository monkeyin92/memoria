from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx
from services.agent.src.providers.domestic_weather import (
    _OUTBOUND_FIELDS,
    DomesticWeather,
    QWeatherConfig,
)
from services.agent.src.providers.weather_places import City

_ROOT = Path(__file__).resolve().parents[4]
_DOC = _ROOT / "docs/compliance.md"
_CHILD_DOC = _ROOT / "docs/compliance.md"
_HEADER_NAMES = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "x-qw-api-key",
}


def _documented_fields(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if "## 来源：`docs/compliance/weather-lookup-data-flow.md`" in text:
        section = text.split("## 来源：`docs/compliance/weather-lookup-data-flow.md`")[1]
        text = section.split("\n---")[0]
    for line in text.splitlines():
        if not line.startswith("| `/"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        path = cells[0].strip("`")
        field = cells[2].strip("`")
        found.setdefault(path, set()).add(field)
    return found


async def test_compliance_doc_keyset_matches_the_recorded_request() -> None:
    documented = _documented_fields(_DOC.read_text(encoding="utf-8"))
    recorded: dict[str, set[str]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.setdefault(request.url.path, set()).update(request.url.params)
        assert request.content == b""
        assert {name.lower() for name in request.headers} == _HEADER_NAMES
        assert request.headers["X-QW-Api-Key"] == "test-key"
        if request.url.path == "/geo/v2/city/lookup":
            name = str(request.url.params["location"])
            body: dict[str, object] = {
                "code": "200",
                "location": [
                    {
                        "name": name,
                        "id": "101190101",
                        "adm1": City[name].province,
                        "country": "中国",
                    }
                ],
            }
        elif request.url.path == "/v7/weather/now":
            body = {"code": "200", "now": {"text": "晴", "temp": "22"}}
        else:
            body = {
                "code": "200",
                "daily": [{"textDay": "多云", "tempMin": "15", "tempMax": "24"} for _ in range(7)],
            }
        return httpx.Response(200, json=body)

    weather = DomesticWeather(
        QWeatherConfig(api_key="test-key", api_host="abcd.re.qweatherapi.com"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert await weather.resolve(query="南京明天天气怎么样") == "南京明天多云，15到24度。"
    assert await weather.resolve(query="南京现在天气怎么样") == (
        "南京现在晴，22度；今天多云，15到24度。"
    )
    assert await weather.resolve(query="未来五天南京天气") == (
        "南京未来五天：今天多云，15到24度；明天多云，15到24度；后天多云，15到24度；"
        "第四天多云，15到24度；第五天多云，15到24度。"
    )
    await weather.aclose()

    assert set(documented) == set(_OUTBOUND_FIELDS) == set(recorded)
    for path, fields in _OUTBOUND_FIELDS.items():
        assert documented[path] == set(fields) == recorded[path]
    doc = _DOC.read_text(encoding="utf-8")
    for name in ("Host", "Accept", "Accept-Encoding", "Connection", "User-Agent", "X-QW-Api-Key"):
        assert f"`{name}`" in doc
    assert "docs/compliance.md" in _CHILD_DOC.read_text(encoding="utf-8")


def test_repo_has_no_foreign_weather_vendor() -> None:
    needle = re.compile("open[-_ ]?" + "meteo", re.I)
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=_ROOT)
    hits: list[str] = []
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode()
        if rel.startswith("docs/acceptance/"):
            continue
        if rel.startswith("scripts/auto_audio/fixtures/"):
            continue
        path = _ROOT / rel
        if not path.is_file():
            continue
        text = path.read_bytes()
        if b"\0" in text[:1024]:
            continue
        if needle.search(text.decode("utf-8", errors="replace")):
            hits.append(rel)
    assert hits == []
