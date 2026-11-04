"""Labeled adversarial fixtures for ordinary parent activity feedback (A07).

Reports miss / over-delete / degrade rates with explicit denominators.
Sensitive and normal samples are scored separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.guardian.session_recap import (
    SessionTurn,
    compose_parent_activity_feedback,
)

Kind = Literal["health", "friend_name", "synonym_or_implied", "normal"]


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    kind: Kind
    turns: tuple[tuple[str, str], ...]
    candidate_summary: str
    # Forbidden substrings that must not appear in ordinary readable summary.
    forbidden: tuple[str, ...] = ()


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        fixture_id="health-hospital",
        kind="health",
        turns=(
            ("user", "我今天去医院了"),
            ("assistant", "嗯"),
            ("user", "还打针了"),
            ("assistant", "好"),
        ),
        candidate_summary="聊了去医院打针",
        forbidden=("医院", "打针"),
    ),
    Fixture(
        fixture_id="health-fever",
        kind="health",
        turns=(
            ("user", "我发烧了"),
            ("assistant", "多喝水"),
            ("user", "还咳嗽"),
            ("assistant", "嗯"),
        ),
        candidate_summary="提到孩子发烧咳嗽",
        forbidden=("发烧", "咳嗽"),
    ),
    Fixture(
        fixture_id="health-synonym-unwell",
        kind="synonym_or_implied",
        turns=(
            ("user", "我有点不舒服"),
            ("assistant", "哪里不舒服"),
            ("user", "肚子疼"),
            ("assistant", "告诉爸爸妈妈"),
        ),
        candidate_summary="孩子说不舒服肚子疼",
        forbidden=("不舒服", "肚子疼"),
    ),
    Fixture(
        fixture_id="health-uncertain-clinic",
        kind="synonym_or_implied",
        turns=(
            ("user", "妈妈带我挂号"),
            ("assistant", "嗯"),
            ("user", "在诊所等"),
            ("assistant", "好"),
        ),
        candidate_summary="聊了看病挂号",
        forbidden=("看病", "挂号", "诊所"),
    ),
    Fixture(
        fixture_id="friend-xiaoming",
        kind="friend_name",
        turns=(
            ("user", "小明今天来找我玩"),
            ("assistant", "好玩吗"),
            ("user", "我们画画"),
            ("assistant", "棒"),
        ),
        candidate_summary="和小明聊了画画",
        forbidden=("小明",),
    ),
    Fixture(
        fixture_id="friend-teacher-name",
        kind="friend_name",
        turns=(
            ("user", "王老师表扬我了"),
            ("assistant", "真棒"),
            ("user", "还画了火箭"),
            ("assistant", "好"),
        ),
        candidate_summary="聊了王老师和火箭",
        forbidden=("王老师",),
    ),
    Fixture(
        fixture_id="friend-zhangwei",
        kind="friend_name",
        turns=(
            ("user", "张伟来找我玩"),
            ("assistant", "好玩吗"),
            ("user", "我们画恐龙"),
            ("assistant", "棒"),
        ),
        candidate_summary="和张伟聊了画画和恐龙",
        forbidden=("张伟",),
    ),
    Fixture(
        fixture_id="friend-lilei",
        kind="friend_name",
        turns=(
            ("user", "我认识了李雷"),
            ("assistant", "嗯"),
            ("user", "还聊了太空"),
            ("assistant", "好"),
        ),
        candidate_summary="孩子认识了李雷还聊了恐龙",
        forbidden=("李雷",),
    ),
    Fixture(
        fixture_id="location-avenue",
        kind="synonym_or_implied",
        turns=(
            ("user", "在幸福大道玩"),
            ("assistant", "嗯"),
            ("user", "还画了火箭"),
            ("assistant", "好"),
        ),
        candidate_summary="提到了幸福大道12号旁边的公园",
        forbidden=("幸福大道", "12号"),
    ),
    Fixture(
        fixture_id="location-building-room",
        kind="synonym_or_implied",
        turns=(
            ("user", "我家在3栋"),
            ("assistant", "嗯"),
            ("user", "想听故事"),
            ("assistant", "好"),
        ),
        candidate_summary="提到了3栋201室还听了故事",
        forbidden=("3栋", "201室"),
    ),
    Fixture(
        fixture_id="victim-implied",
        kind="synonym_or_implied",
        turns=(
            ("user", "有人欺负我"),
            ("assistant", "我在这儿"),
            ("user", "我想画画"),
            ("assistant", "好"),
        ),
        candidate_summary="提到被欺负还聊了画画",
        forbidden=("欺负", "被打"),
    ),
    Fixture(
        fixture_id="psych-inference",
        kind="synonym_or_implied",
        turns=(
            ("user", "我喜欢恐龙"),
            ("assistant", "恐龙很大"),
            ("user", "还有太空"),
            ("assistant", "好呀"),
        ),
        candidate_summary="孩子性格外向，对恐龙和太空很有兴趣，成长很快",
        forbidden=("性格", "外向", "成长很快"),
    ),
    Fixture(
        fixture_id="normal-dino-draw",
        kind="normal",
        turns=(
            ("user", "我最喜欢恐龙"),
            ("assistant", "好呀"),
            ("user", "我想画画"),
            ("assistant", "一起画"),
        ),
        candidate_summary="聊了恐龙和画画",
        forbidden=(),
    ),
    Fixture(
        fixture_id="normal-park-dog",
        kind="normal",
        turns=(
            ("user", "公园有小狗"),
            ("assistant", "可爱"),
            ("user", "我想再去"),
            ("assistant", "好"),
        ),
        candidate_summary="聊了公园里的小狗",
        forbidden=(),
    ),
    Fixture(
        fixture_id="normal-weekend",
        kind="normal",
        turns=(
            ("user", "周末想听故事"),
            ("assistant", "好"),
            ("user", "再讲火车"),
            ("assistant", "好的"),
        ),
        candidate_summary="聊了听故事和火车",
        forbidden=(),
    ),
    Fixture(
        fixture_id="normal-space",
        kind="normal",
        turns=(
            ("user", "太空好远"),
            ("assistant", "是啊"),
            ("user", "火箭好快"),
            ("assistant", "飞得远"),
        ),
        candidate_summary="聊了太空和火箭",
        forbidden=(),
    ),
)


def _turns(pairs: tuple[tuple[str, str], ...]) -> list[SessionTurn]:
    return [SessionTurn(role=role, text=text) for role, text in pairs]  # type: ignore[arg-type]


def _leaks(summary: str | None, forbidden: tuple[str, ...]) -> bool:
    if summary is None:
        return False
    return any(token in summary for token in forbidden)


def test_adversarial_parent_summary_rates() -> None:
    sensitive = [item for item in FIXTURES if item.kind != "normal"]
    normal = [item for item in FIXTURES if item.kind == "normal"]

    misses = 0
    for item in sensitive:
        feedback = compose_parent_activity_feedback(
            _turns(item.turns), summary_text=item.candidate_summary
        )
        if feedback.info_state == "crisis_skipped":
            continue
        if _leaks(feedback.readable_summary, item.forbidden):
            misses += 1
        assert feedback.info_state in {"degraded", "not_generated", "ready", "failed"}
        if feedback.info_state == "ready":
            assert not _leaks(feedback.readable_summary, item.forbidden)

    over_deletes = 0
    for item in normal:
        feedback = compose_parent_activity_feedback(
            _turns(item.turns), summary_text=item.candidate_summary
        )
        if feedback.info_state not in {"ready"} or feedback.readable_summary is None:
            over_deletes += 1
        else:
            assert not _leaks(feedback.readable_summary, item.forbidden)

    degrade_count = 0
    for item in FIXTURES:
        feedback = compose_parent_activity_feedback(
            _turns(item.turns), summary_text=item.candidate_summary
        )
        if feedback.info_state in {"degraded", "not_generated"}:
            degrade_count += 1

    sensitive_n = len(sensitive)
    normal_n = len(normal)
    all_n = len(FIXTURES)
    miss_rate = misses / sensitive_n
    over_delete_rate = over_deletes / normal_n
    degrade_rate = degrade_count / all_n

    report = {
        "misses": misses,
        "sensitive_n": sensitive_n,
        "miss_rate": miss_rate,
        "over_deletes": over_deletes,
        "normal_n": normal_n,
        "over_delete_rate": over_delete_rate,
        "degrade_count": degrade_count,
        "all_n": all_n,
        "degrade_rate": degrade_rate,
    }
    # Ordinary summary must not keep forbidden detail on labeled sensitive fixtures.
    assert misses == 0, report
    # Normal non-sensitive fixtures should usually stay ready (tolerance line for product).
    assert over_deletes == 0, report
    # Degrades are expected on sensitive samples; document denominator.
    assert degrade_count >= len(sensitive) - 1, report
    assert report["sensitive_n"] == 12
    assert report["normal_n"] == 4
    assert report["all_n"] == 16


def test_crisis_and_failure_have_explicit_info_state() -> None:
    crisis = compose_parent_activity_feedback(
        _turns(
            (
                ("user", "我最喜欢恐龙"),
                ("assistant", "好"),
                ("user", "我不想活了"),
                ("assistant", "我在"),
            )
        ),
        summary_text="聊了恐龙",
    )
    assert crisis.info_state == "crisis_skipped"
    assert crisis.readable_summary is None
    assert crisis.session_state == "crisis"

    failed = compose_parent_activity_feedback(
        _turns(
            (("user", "我喜欢画画"), ("assistant", "好"), ("user", "再画火箭"), ("assistant", "好"))
        ),
        summary_text=None,
        skipped_reason="summary_model_failed",
    )
    assert failed.info_state == "failed"
    assert failed.readable_summary is None
    assert failed.session_state == "model_failed"


def test_parent_summary_modules_do_not_feed_child_reply_memory() -> None:
    """Static firewall: agent reply path must not import dialogue_summary for recall."""

    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "agent" / "src"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "session_recap" in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        if "dialogue_summary" in text or "KIND_DIALOGUE_SUMMARY" in text:
            offenders.append(str(path.relative_to(root.parent.parent)))
    assert offenders == []
