"""Keep deployment smoke aligned with fixed-safety source of truth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.assert_response_plan_safety import (
    OLD_DRAFT_MARKERS,
    SAFE_UNKNOWN_REPLY,
    SMOKE_SAFETY_CASES,
    assert_response_plan_safety_payloads,
    expected_routes,
)
from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    SUPPORT_RELAY_REPLY,
    SUPPORT_VICTIM_REPLY,
)

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "smoke_server_deployment.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_smoke_utterances_map_to_approved_templates_and_push_flags() -> None:
    routes = expected_routes()
    assert set(routes) == {"crisis", "third_party", "victim", "prevention"}
    assert routes["crisis"]["reply_template_id"] == "crisis-self-v1"
    assert routes["crisis"]["attempt_push"] is True
    assert routes["crisis"]["direct_text"] == CRISIS_SUPPORT_REPLY
    assert routes["victim"]["reply_template_id"] == "victim-support-v1"
    assert routes["victim"]["attempt_push"] is True
    assert routes["victim"]["direct_text"] == SUPPORT_VICTIM_REPLY
    assert routes["third_party"]["reply_template_id"] == "support-other-v1"
    assert routes["third_party"]["attempt_push"] is False
    assert routes["third_party"]["direct_text"] == SUPPORT_RELAY_REPLY
    assert routes["prevention"]["reply_template_id"] == "support-other-v1"
    assert routes["prevention"]["attempt_push"] is False
    assert routes["prevention"]["direct_text"] == SUPPORT_RELAY_REPLY


def test_checker_accepts_canonical_payloads_and_rejects_old_draft() -> None:
    good = {
        name: {"direct_text": reply}
        for name, _query, reply, _template, _push in SMOKE_SAFETY_CASES
    }
    assert_response_plan_safety_payloads(
        crisis=good["crisis"],
        third_party=good["third_party"],
        victim=good["victim"],
        prevention=good["prevention"],
        prohibited={"direct_text": SAFE_UNKNOWN_REPLY},
    )
    stale = {
        "direct_text": (
            "请立即联系当地急救或报警。你现在是否正准备伤害自己？"
        )
    }
    with pytest.raises(AssertionError):
        assert_response_plan_safety_payloads(
            crisis=stale,
            third_party=good["third_party"],
            victim=good["victim"],
            prevention=good["prevention"],
            prohibited={"direct_text": SAFE_UNKNOWN_REPLY},
        )


def test_smoke_script_imports_checker_and_drops_old_draft_copy() -> None:
    body = SMOKE.read_text(encoding="utf-8")
    assert "assert_response_plan_safety.py" in body
    assert "PYTHONPATH" in body
    for marker in OLD_DRAFT_MARKERS:
        assert marker not in body, f"smoke still embeds stale marker {marker!r}"
    for phrase in (
        'assert "安全" in crisis["direct_text"]',
        'assert planned["direct_text"] is None',
        "危机支持 > 语言学习 > 引导式学习 > 普通陪伴",
    ):
        assert phrase not in body


def test_ci_python_gates_run_this_drift_check() -> None:
    gates = CI.read_text(encoding="utf-8")
    assert "scripts/tests/test_smoke_response_plan_safety.py" in gates


def test_checker_round_trips_json_files(tmp_path: Path) -> None:
    routes = expected_routes()
    paths = []
    for name in ("crisis", "third_party", "victim", "prevention"):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps({"direct_text": routes[name]["direct_text"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        paths.append(path)
    prohibited = tmp_path / "prohibited.json"
    prohibited.write_text(
        json.dumps({"direct_text": SAFE_UNKNOWN_REPLY}, ensure_ascii=False),
        encoding="utf-8",
    )
    paths.append(prohibited)
    from scripts.assert_response_plan_safety import main

    assert main(["assert_response_plan_safety.py", *[str(p) for p in paths]]) == 0
