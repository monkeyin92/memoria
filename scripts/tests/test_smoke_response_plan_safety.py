from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import assert_response_plan_safety as checker
from scripts.assert_response_plan_safety import (
    OLD_DRAFT_MARKERS,
    PROHIBITED_SMOKE_QUERY,
    SMOKE_CASE_NAMES,
    SMOKE_SAFETY_CASES,
    assert_response_plan_safety_payloads,
    assert_smoke_route_table,
    expected_direct_texts,
    main,
)
from services.common.companion_response_safety import (
    SAFE_UNKNOWN_REPLY,
    fixed_companion_reply,
)
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
)

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "smoke_server_deployment.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_smoke_utterances_map_to_approved_templates_and_push_flags() -> None:
    routes = assert_smoke_route_table()
    assert set(routes) == set(SMOKE_CASE_NAMES)
    assert routes["crisis"]["script"].template_id == "crisis-self-v1"
    assert routes["crisis"]["attempt_push"] is True
    assert routes["crisis"]["script"] is CRISIS_SELF_SCRIPT
    assert routes["victim"]["script"].template_id == "victim-support-v1"
    assert routes["victim"]["attempt_push"] is True
    assert routes["victim"]["script"] is SUPPORT_VICTIM_SCRIPT
    assert routes["third_party"]["script"].template_id == "support-other-v1"
    assert routes["third_party"]["attempt_push"] is False
    assert routes["third_party"]["script"] is SUPPORT_RELAY_SCRIPT
    assert routes["prevention"]["script"].template_id == "support-other-v1"
    assert routes["prevention"]["attempt_push"] is False
    assert routes["prevention"]["script"] is SUPPORT_RELAY_SCRIPT
    prohibited = route_crisis(PROHIBITED_SMOKE_QUERY)
    assert prohibited.script is None
    assert prohibited.attempt_push is False
    assert (
        fixed_companion_reply(query=PROHIBITED_SMOKE_QUERY, is_companion=False)
        == SAFE_UNKNOWN_REPLY
    )


def test_smoke_locks_the_spoken_phone_readings() -> None:
    routes = assert_smoke_route_table()

    assert routes["crisis"]["tts_text"] == CRISIS_SELF_SCRIPT.display
    assert routes["crisis"]["tts_text"].startswith("听到你这么说")
    assert routes["victim"]["tts_text"].endswith("请大人帮你打幺幺零。")
    assert routes["third_party"]["tts_text"] == SUPPORT_RELAY_SCRIPT.display


def test_route_table_fails_when_spoken_copy_drifts(monkeypatch: pytest.MonkeyPatch) -> None:
    crisis, *rest = SMOKE_SAFETY_CASES
    drifted = {**crisis, "tts_text": crisis["tts_text"].replace("我很心疼", "我好难过")}
    monkeypatch.setattr(checker, "SMOKE_SAFETY_CASES", (drifted, *rest))

    with pytest.raises(AssertionError, match="crisis: spoken copy drifted"):
        assert_smoke_route_table()


def test_checker_rejects_display_copy_that_names_no_script() -> None:
    for direct_text in (
        CRISIS_SELF_SCRIPT.display + "额外文字",
        "不是任何已知文案",
    ):
        with pytest.raises(AssertionError, match="crisis: unexpected direct_text"):
            assert_response_plan_safety_payloads(
                crisis={"direct_text": direct_text},
                third_party={"direct_text": SUPPORT_RELAY_SCRIPT.display},
                victim={"direct_text": SUPPORT_VICTIM_SCRIPT.display},
                prevention={"direct_text": SUPPORT_RELAY_SCRIPT.display},
                prohibited={"direct_text": SAFE_UNKNOWN_REPLY},
            )


def test_checker_accepts_canonical_payloads_and_rejects_old_draft() -> None:
    good = {
        "crisis": {"direct_text": CRISIS_SELF_SCRIPT.display},
        "third_party": {"direct_text": SUPPORT_RELAY_SCRIPT.display},
        "victim": {"direct_text": SUPPORT_VICTIM_SCRIPT.display},
        "prevention": {"direct_text": SUPPORT_RELAY_SCRIPT.display},
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
    with pytest.raises(AssertionError, match="crisis"):
        assert_response_plan_safety_payloads(
            crisis=stale,
            third_party=good["third_party"],
            victim=good["victim"],
            prevention=good["prevention"],
            prohibited={"direct_text": SAFE_UNKNOWN_REPLY},
        )


def test_smoke_script_imports_checker_and_locks_utterances() -> None:
    body = SMOKE.read_text(encoding="utf-8")
    assert "assert_response_plan_safety.py" in body
    assert 'PYTHONPATH="$release${PYTHONPATH:+:$PYTHONPATH}"' in body
    for case in SMOKE_SAFETY_CASES:
        assert case["query"] in body
    assert PROHIBITED_SMOKE_QUERY in body
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
    texts = expected_direct_texts()
    paths = []
    for name in SMOKE_CASE_NAMES:
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps({"direct_text": texts[name]}, ensure_ascii=False),
            encoding="utf-8",
        )
        paths.append(path)
    prohibited = tmp_path / "prohibited.json"
    prohibited.write_text(
        json.dumps({"direct_text": SAFE_UNKNOWN_REPLY}, ensure_ascii=False),
        encoding="utf-8",
    )
    paths.append(prohibited)
    assert main(["assert_response_plan_safety.py", *[str(p) for p in paths]]) == 0
