"""Annotated + holdout preference-extract fixtures for A02.

Wrong subject or wrong polarity is a hard fail. Miss rate is reported only;
do not treat miss as pass, and do not invent a miss-rate threshold here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

HardKind = Literal["wrong_subject", "wrong_polarity", "ok_positive", "ok_empty"]
SetKind = Literal["annotated", "holdout"]


@dataclass(frozen=True, slots=True)
class PreferenceExtractCase:
    case_id: str
    set_kind: SetKind
    utterance: str
    # Labels that must appear (positive current self preference).
    must_include: tuple[str, ...]
    # Labels that must never appear (wrong subject, wrong polarity, withdrawn).
    must_exclude: tuple[str, ...]
    # When True, the extract must be exactly must_include (order ignored).
    exact: bool
    note: str


ANNOTATED_CASES: Final[tuple[PreferenceExtractCase, ...]] = (
    PreferenceExtractCase(
        case_id="e1-mom-also-likes",
        set_kind="annotated",
        utterance="我喜欢画画，妈妈也喜欢恐龙",
        must_include=("喜欢画画",),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E1: mom's topic must not attach to child",
    ),
    PreferenceExtractCase(
        case_id="e2-negated-then-positive",
        set_kind="annotated",
        utterance="我不喜欢恐龙，我喜欢小狗",
        must_include=("喜欢小狗",),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E2: negated clause must not yield positive label",
    ),
    PreferenceExtractCase(
        case_id="e3-but-now-dislike",
        set_kind="annotated",
        utterance="我喜欢恐龙但现在不喜欢了",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E3: current withdraw cancels earlier like",
    ),
    PreferenceExtractCase(
        case_id="e3-comma-now-dislike",
        set_kind="annotated",
        utterance="我喜欢恐龙，现在不喜欢了",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E3 comma form",
    ),
    PreferenceExtractCase(
        case_id="e3-past-now",
        set_kind="annotated",
        utterance="以前我喜欢恐龙现在不喜欢",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E3 past/now form",
    ),
    PreferenceExtractCase(
        case_id="e4-mom-only",
        set_kind="annotated",
        utterance="我妈妈喜欢恐龙",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="E4 control: other-person only",
    ),
    PreferenceExtractCase(
        case_id="multi-same-subject",
        set_kind="annotated",
        utterance="我喜欢小狗，也喜欢小猫",
        must_include=("喜欢小狗", "喜欢小猫"),
        must_exclude=(),
        exact=True,
        note="same-subject multi-topic must remain",
    ),
    PreferenceExtractCase(
        case_id="negation-alone",
        set_kind="annotated",
        utterance="我不喜欢恐龙",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="bare dislike writes nothing",
    ),
    PreferenceExtractCase(
        case_id="request-not-pref",
        set_kind="annotated",
        utterance="给我讲个恐龙故事",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="request is not preference",
    ),
    PreferenceExtractCase(
        case_id="fear-not-pref",
        set_kind="annotated",
        utterance="我怕小狗",
        must_include=(),
        must_exclude=("喜欢小狗",),
        exact=True,
        note="fear is not preference",
    ),
    PreferenceExtractCase(
        case_id="robot-question",
        set_kind="annotated",
        utterance="你喜欢恐龙吗",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="question to robot",
    ),
    PreferenceExtractCase(
        case_id="reported-says",
        set_kind="annotated",
        utterance="老师说我喜欢恐龙",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="reported/hearsay → no auto positive (pending or empty)",
    ),
    PreferenceExtractCase(
        case_id="hypothetical",
        set_kind="annotated",
        utterance="如果我喜欢恐龙会怎样",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="hypothetical → no auto positive",
    ),
    PreferenceExtractCase(
        case_id="correction",
        set_kind="annotated",
        utterance="我说错了，我不喜欢恐龙，我喜欢小狗",
        must_include=("喜欢小狗",),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="correction drops the rejected topic",
    ),
    PreferenceExtractCase(
        case_id="self-positive",
        set_kind="annotated",
        utterance="我最喜欢恐龙",
        must_include=("喜欢恐龙",),
        must_exclude=(),
        exact=True,
        note="plain self positive",
    ),
)

HOLDOUT_CASES: Final[tuple[PreferenceExtractCase, ...]] = (
    PreferenceExtractCase(
        case_id="holdout-mom-synonym",
        set_kind="holdout",
        utterance="我喜欢积木，我妈妈特别中意恐龙",
        must_include=("喜欢积木",),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="holdout: 中意 + 我妈妈",
    ),
    PreferenceExtractCase(
        case_id="holdout-neg-synonym",
        set_kind="holdout",
        utterance="我不太喜欢恐龙，不过我喜欢火车",
        must_include=("喜欢火车",),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="holdout: 不太喜欢 negation",
    ),
    PreferenceExtractCase(
        case_id="holdout-temporal-synonym",
        set_kind="holdout",
        utterance="从前我超喜欢恐龙，可是现在不喜欢了",
        must_include=(),
        must_exclude=("喜欢恐龙",),
        exact=True,
        note="holdout: 从前/可是现在",
    ),
    PreferenceExtractCase(
        case_id="holdout-also-self",
        set_kind="holdout",
        utterance="我喜欢火箭，还喜欢太空",
        must_include=("喜欢火箭", "喜欢太空"),
        must_exclude=(),
        exact=True,
        note="holdout: 还喜欢 same subject",
    ),
)

ALL_CASES: Final[tuple[PreferenceExtractCase, ...]] = ANNOTATED_CASES + HOLDOUT_CASES
