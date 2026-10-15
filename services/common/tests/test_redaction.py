from __future__ import annotations

import pytest
from services.common.companion_response_safety import companion_safety_decision
from services.common.crisis_policy import (
    CrisisSemanticVerdict,
    crisis_semantic_candidate,
    route_crisis,
)
from services.common.realtime_information import requires_realtime_lookup
from services.common.redaction import redact_pii

CRISIS = (
    "我不想活了", "我想死", "我想自杀", "我要跳楼", "我割腕了", "我吞了很多药",
    "救救我", "救命", "有人正在打我", "我被性侵", "我活不下去了", "我准备自杀",
    "怎么自杀", "有人要杀我", "老师老是打我", "我真的不想活了", "我想伤害自己",
)
CRISIS_ADDRESSES = (
    "南京市中山路1号", "北京市朝阳区建国路88号", "人民路12号", "中山路1号",
    "广东省深圳市南山区科技园路3号5栋", "上海市浦东新区世纪大道100号", "长安街1号",
    "解放路5号301室", "幸福村3号", "建国门外大街1号", "杭州市西湖区文三路90号",
)
CRISIS_GLUE = ("{c}{a}", "{c}住在{a}", "{c}我家在{a}", "{c}，{a}", "{a}{c}", "我家在{a}{c}")
SEARCH = ("今天天气怎么样", "明天下雨吗", "现在路况怎么样", "最新新闻", "股价多少", "火车几点到", "汇率是多少")
SEARCH_ADDRESSES = ("南京市中山路1号", "人民路12号", "北京市朝阳区建国路88号", "长安街1号")
SEARCH_GLUE = ("{a}{s}", "{s}{a}", "去{a}{s}", "我在{a}{s}", "{a}附近{s}")

CRISIS_SAMPLES = tuple(
    glue.format(c=c, a=a) for c in CRISIS for a in CRISIS_ADDRESSES for glue in CRISIS_GLUE
)
SEARCH_SAMPLES = tuple(
    glue.format(s=s, a=a) for s in SEARCH for a in SEARCH_ADDRESSES for glue in SEARCH_GLUE
)

FULLY_REDACTED = (
    "南京市鼓楼区中山路1号", "上海市黄浦区南京东路100号", "北京市东城区王府井大街255号",
    "广州市天河区天河路208号", "成都市武侯区人民南路四段1号", "重庆市渝中区解放碑民族路166号",
    "深圳市福田区深南大道6008号", "杭州市上城区解放路55号", "武汉市江汉区解放大道690号",
    "天津市和平区南京路189号", "西安市雁塔区小寨东路1号", "长沙市岳麓区麓山南路2号",
    "内蒙古自治区呼和浩特市新城区新华大街1号", "苏州市姑苏区观前街8号", "南京市秦淮区夫子庙贡院街2号",
    "多伦路2号", "来福路5号", "幸福里小区和平路12号3栋501室", "王家村18号", "石门镇100号",
    "北京市海淀区中关村大街27号", "北京市朝阳区建国路88号2号楼301室",
)


def test_glue_corpus_keeps_every_crisis_decision() -> None:
    assert len(CRISIS_SAMPLES) == 1122
    decision_flips = [
        s for s in CRISIS_SAMPLES if companion_safety_decision(redact_pii(s)) != companion_safety_decision(s)
    ]
    candidate_flips = [
        s for s in CRISIS_SAMPLES if crisis_semantic_candidate(redact_pii(s)) != crisis_semantic_candidate(s)
    ]
    semantic_route_flips = [
        s
        for s in CRISIS_SAMPLES
        if route_crisis(redact_pii(s), semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS).action
        != route_crisis(s, semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS).action
    ]
    unredacted = [s for s in CRISIS_SAMPLES if "[地址]" not in redact_pii(s)]

    assert decision_flips == []
    assert candidate_flips == []
    assert semantic_route_flips == []
    assert unredacted == []


def test_glue_corpus_keeps_every_realtime_lookup_decision() -> None:
    assert len(SEARCH_SAMPLES) == 140
    lookup_flips = [
        s for s in SEARCH_SAMPLES if requires_realtime_lookup(redact_pii(s)) != requires_realtime_lookup(s)
    ]
    unredacted = [s for s in SEARCH_SAMPLES if "[地址]" not in redact_pii(s)]

    assert lookup_flips == []
    assert unredacted == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我不想活了住在南京市中山路1号", "我不想活了住在[地址]"),
        ("我忍不住想死中山路1号", "我忍不住想死[地址]"),
        ("中山路1号我要跳楼", "[地址]我要跳楼"),
        ("救命南京市中山路1号", "救命南京市[地址]"),
        ("提到了幸福路12号", "提到了[地址]"),
        ("地址北京市朝阳区建国路88号2号楼301室。", "[地址]。"),
        ("联系demo@example.com，住北京市朝阳区建国路88号。", "联系[邮箱]，住[地址]。"),
        ("我的手机13812345678，明天南京天气怎么样", "我的手机[手机号]，明天南京天气怎么样"),
        ("南京市鼓楼区天气怎么样", "南京市鼓楼区天气怎么样"),
    ],
)
def test_redact_pii_literal_examples(text: str, expected: str) -> None:
    assert redact_pii(text) == expected


@pytest.mark.parametrize("address", FULLY_REDACTED)
def test_standalone_addresses_are_fully_redacted(address: str) -> None:
    assert redact_pii(address) == "[地址]"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("药王庙街3号", "药[地址]"),
        ("安定门内大街10号", "安定门[地址]"),
        ("朝阳门外大街6号2单元", "朝阳门[地址]"),
        ("五道口华清嘉园路7号", "五道口华清[地址]"),
    ],
)
def test_landmark_roads_keep_only_their_head(address: str, expected: str) -> None:
    assert redact_pii(address) == expected


def test_redact_pii_is_idempotent() -> None:
    samples = (*CRISIS_SAMPLES, *SEARCH_SAMPLES, *FULLY_REDACTED)
    changed = [s for s in samples if redact_pii(redact_pii(s)) != redact_pii(s)]

    assert changed == []
