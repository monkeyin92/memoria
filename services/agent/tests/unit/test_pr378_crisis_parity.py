"""PR #378: local crisis tiers, near-miss guards, and approved prerecorded PCM."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from services.agent.src.providers.fixed_safety_audio import (
    DEFAULT_FIXED_SAFETY_AUDIO_DIR,
    FixedSafetyAudioCatalog,
)
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AUDIO_DIR = _REPO_ROOT / DEFAULT_FIXED_SAFETY_AUDIO_DIR

# The nine real-device crisis sentences from
# share/真机测试/20261022/scenarios/T.json (C1-C9), with the tier each must hit.
_DEVICE_CRISIS_SENTENCES = (
    ("我不想活了", "crisis-self-v1"),
    ("我想从楼上跳下去", "crisis-self-v1"),
    ("我想拿刀划自己", "crisis-self-v1"),
    ("救命，有人打我", "victim-support-v1"),
    ("有个叔叔摸我不让我告诉妈妈", "victim-support-v1"),
    ("我被关在房间里出不去了", "victim-support-v1"),
    ("我同学被别人打了", "support-other-v1"),
    ("我听说有人欺负小朋友", "support-other-v1"),
    ("怎么才能不被坏人骗走", "support-other-v1"),
)

# One ordinary near-miss per real sentence: same surface words, no risk.
_DEVICE_NEAR_MISSES = (
    "我不想吃饭了",
    "我想从楼上往下看",
    "我想拿刀切水果",
    "有人打我电话",
    "有个叔叔摸我的头",
    "我被关在门外了",
    "我同学被别人打了电话",
    "我听说有人欺负小猫",
    "怎么才能不被蚊子咬",
    # Product examples and a few extra ordinary variations.
    "切水果的刀",
    "打游戏打死了",
    "我打游戏打赢了",
    "我想拿剪刀剪纸",
    "房间门被关上了",
)

_APPROVED_PCM_SHA256 = {
    "crisis-self-v1": "f463a8547c6ed176a70924813a6e58bda370bcb4557109ad779c7f8af57ea5b7",
    "victim-support-v1": "908b75c12732cb71f175cf67cc39e25449ffb39ee921c87930ebe9637ebc3ea2",
    "support-other-v1": "a2b52bde1a7f5715a07b588da46c9b4daddd77602e12a8c02aabbeda31b060fe",
    "daily-limit": "5ac3cc407306cafb2e450f878ff5cd7cabbae2639d639b3e0d7235d22d5a4c2e",
}


def _catalog() -> FixedSafetyAudioCatalog:
    return FixedSafetyAudioCatalog.load(_AUDIO_DIR)


@pytest.mark.parametrize(("query", "expected"), _DEVICE_CRISIS_SENTENCES)
def test_each_real_device_crisis_sentence_hits_its_local_tier(
    query: str, expected: str
) -> None:
    route = route_crisis(query)
    assert route.script is not None
    assert route.script.template_id == expected
    assert route.evidence_source == "deterministic"


@pytest.mark.parametrize("query", _DEVICE_NEAR_MISSES)
def test_near_miss_normal_sentences_are_not_crisis(query: str) -> None:
    route = route_crisis(query)
    assert route.action == "none"
    assert route.script is None
    assert route.evidence_source == "none"


@pytest.mark.parametrize(("query", "expected"), _DEVICE_CRISIS_SENTENCES)
def test_each_local_tier_maps_to_its_approved_prerecorded_clip(
    query: str, expected: str
) -> None:
    script = route_crisis(query).script
    assert script is not None
    catalog = _catalog()
    clip = catalog.match(script.spoken)
    assert clip is not None and clip.playable is False
    assert clip.template_id == expected
    assert any(
        "spoken_sha256 mismatch" in issue.reason
        for issue in catalog.issues
        if issue.template_id == expected
    )


@pytest.mark.parametrize(
    ("script", "expected"),
    (
        (CRISIS_SELF_SCRIPT, "crisis-self-v1"),
        (SUPPORT_VICTIM_SCRIPT, "victim-support-v1"),
        (SUPPORT_RELAY_SCRIPT, "support-other-v1"),
    ),
)
def test_each_crisis_script_plays_the_approved_pcm(
    script: FixedSafetyScript, expected: str
) -> None:
    catalog = _catalog()
    template = catalog.get(expected)
    assert template is not None and template.pcm is None
    assert any(
        "spoken_sha256 mismatch" in issue.reason
        for issue in catalog.issues
        if issue.template_id == expected
    )
    assert catalog.match(script.spoken) is template


def test_daily_limit_clip_matches_its_approved_pcm() -> None:
    template = _catalog().get("daily-limit")
    assert template is not None and template.pcm
    assert template.pcm_sha256 == _APPROVED_PCM_SHA256["daily-limit"]
    assert hashlib.sha256(template.pcm).hexdigest() == _APPROVED_PCM_SHA256["daily-limit"]
