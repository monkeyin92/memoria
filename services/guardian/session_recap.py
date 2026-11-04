"""Session-end child recap: constrained fields first, then optional prose.

Built from the in-process transcript at session end, then the transcript is
discarded. Never persists raw utterances. Product rules (authoritative):
generate once per session; skip short or story-only sessions; parent voice via
LLM; reject raw-transcript echoes and name/health leaks; archive only on
separate consent. Ordinary activity feedback stays separate from crisis notify.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from services.common.crisis_policy import route_crisis
from services.guardian.child_memory import (
    SUMMARY_MAX_CHARS,
    ArchiveFact,
    DialogueSummary,
    quotes_speech,
)
from services.guardian.preference_extract import TOPIC_ROWS, extract_interest_facts

INTEREST_TOPICS: Final[tuple[tuple[str, str], ...]] = TOPIC_ROWS

Role = Literal["user", "assistant"]
InfoState = Literal[
    "ready",
    "degraded",
    "not_generated",
    "crisis_skipped",
    "failed",
    "skipped_short",
    "skipped_story",
]
SessionState = Literal["ordinary", "crisis", "too_short", "story_only", "model_failed"]
DetectorId = Literal[
    "names",
    "precise_location",
    "health",
    "victimization",
    "verbatim",
    "psych_inference",
]
Certainty = Literal["hit", "uncertain"]

# Conservative allowed themes for ordinary parent activity feedback (A07 draft).
ALLOWED_THEMES: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("stories", "听故事", ("故事", "童话", "绘本")),
    ("animals", "动物", ("恐龙", "霸王龙", "小狗", "小猫", "动物")),
    ("vehicles", "交通工具", ("赛车", "火车", "火箭", "汽车")),
    ("drawing", "画画", ("画画", "画了", "涂色")),
    ("blocks", "积木", ("积木",)),
    ("outdoor_play", "户外玩耍", ("公园", "户外", "玩耍")),
    ("space", "太空", ("太空", "星星", "月亮")),
    ("school_routine", "学校日常", ("上学", "放学", "作业", "课堂")),
    ("weekend_plans", "周末安排", ("周末", "星期六", "星期天")),
    ("games", "游戏", ("游戏", "玩法", "玩具")),
    ("music", "唱歌", ("唱歌", "儿歌", "音乐")),
    ("other_safe", "日常聊天", ()),
)

_STORY_ONLY_USER = re.compile(r"^(再)?(讲|说|读)(一)?(个|则|段)?(故事|童话|绘本).{0,8}$")
# Named people only: a short name attached to the role word. Skip plurals like 同学们.
_NAMED_ROLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[\u4e00-\u9fff]{1,3}(?:老师|同学|班主任|校长)(?!们)"
    r"|(?:老师|同学|班主任|校长)(?!们)[\u4e00-\u9fff]{1,3})"
)
# Friend / peer nicknames such as 小明 / 小红. Animals, 小学, play themes are not names.
_FRIEND_NICK_RE: Final[re.Pattern[str]] = re.compile(
    r"小(?!学|时|心|朋友|孩|狗|猫|鸟|兔|熊|鸡|鸭|鱼|马|龙|恐|火|车|花|草|树|河|山)"
    r"[\u4e00-\u9fff]{1,2}"
)
# Bare given names introduced as people (张伟 / 李雷), not theme words.
# Surname scan is person-cued only so 周末 / 霸王龙 do not false-hit.
_INTRODUCED_PERSON_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:和|跟|与|找|叫|认识了)([\u4e00-\u9fff]{2,3})(?:聊|玩|说|一起|还|,|，|$)"
)
# 和/跟/与 stay on _INTRODUCED_PERSON_RE so 「和周末」 is not a surname hit.
_CUED_SURNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:叫|认识了|朋友|同学)"
    r"(?:张|王|李|赵|刘|陈|杨|黄|周|吴|徐|孙|马|朱|胡|郭|何|高|林|罗|郑|梁|谢|宋|"
    r"唐|许|韩|冯|邓|曹|彭|曾|肖|田|董|袁|潘|蒋|蔡|余|杜|叶|程|苏|魏|吕|丁|任|"
    r"沈|姚|卢|姜|崔|钟|谭|陆|汪|范|金|石|廖|贾|夏|韦|方|白|邹|孟|熊|秦|邱|"
    r"江|尹|薛|闫|段|雷|侯|史|陶|黎|贺|顾|毛|郝|龚|邵|万|钱|严|覃|武|戴|莫|"
    r"孔|向|汤)([\u4e00-\u9fff]{1,2})"
)
_HEALTH_TERMS: Final[tuple[str, ...]] = (
    "生病",
    "发烧",
    "感冒",
    "医院",
    "吃药",
    "打针",
    "咳嗽",
    "诊所",
    "输液",
    "过敏",
    "不舒服",
    "肚子疼",
    "头疼",
    "头痛",
)
_HEALTH_UNCERTAIN: Final[tuple[str, ...]] = (
    "看病",
    "挂号",
    "体温",
    "药店",
)
_VICTIM_DETAIL_TERMS: Final[tuple[str, ...]] = (
    "被打",
    "被性侵",
    "性侵",
    "强奸",
    "被下毒",
    "中毒",
    "正在打我",
    "对我实施暴力",
    "要杀我",
    "救命",
)
_VICTIM_UNCERTAIN: Final[tuple[str, ...]] = (
    "欺负",
    "打人",
    "受伤",
    "流血",
)
_PSYCH_INFERENCE_TERMS: Final[tuple[str, ...]] = (
    "性格",
    "心理",
    "内向",
    "外向",
    "情商",
    "成长阶段",
    "成长很快",
    "焦虑",
    "抑郁",
    "依恋",
    "人格",
    "有进步",
    "更成熟",
)
#: Same window as guardian daily recap: this many consecutive chars is a quote.
VERBATIM_WINDOW: Final = 10

SESSION_SUMMARY_SYSTEM_PROMPT: Final = (
    "你是严谨的中文助手。只输出家长视角的会话摘要正文，不要 Markdown、标题或引号。"
)

SESSION_SUMMARY_USER_PROMPT_PREFIX: Final = """
请用家长的视角，用 1 到 3 句中文概括孩子和陪伴机器人这一次聊天。
硬性规则：
- 用“孩子”做主语或隐含主语，例如“聊了公园里的黄色小狗”。
- 不得引用或复述孩子的原话，不得截取原文片段。
- 不要写任何人的姓名或昵称（包括小明、同学名、老师名），只写“孩子”或泛称。
- 不要写学校全称或具体住址门牌。
- 不要写具体健康细节（生病、医院、吃药、不舒服等）。
- 不要写受害细节（被打、被性侵、下毒、暴力等），也不要用「危机」一词。
- 不要推断性格、心理、情商或成长结论；只写本次聊过的活动主题。
- 只写聊天里出现过的话题，不编造。
- 全文不超过 200 字。

孩子说过的话（只供你理解，勿照搬）：
""".strip()

_ADDRESS_OR_SCHOOL_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"[\u4e00-\u9fff]{0,12}幼儿园"
    r"|[\u4e00-\u9fff]{2,12}(?:小学|中学|大学)"
    r"|[\u4e00-\u9fff]{1,12}(?:大道|路|街|巷|弄)\d{1,4}号"
    r"|\d{1,3}栋\d{1,4}室"
    r"|[\u4e00-\u9fff]{2,8}市[\u4e00-\u9fff]{1,8}区"
    r")"
)
_LOCATION_UNCERTAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[\u4e00-\u9fff]{2,8}(?:大道|路|街|巷|弄)|门口|小区|几号楼|\d{1,3}栋)"
)


@dataclass(frozen=True, slots=True)
class SessionTurn:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class DetectorFinding:
    detector: DetectorId
    certainty: Certainty


@dataclass(frozen=True, slots=True)
class ParentActivityFeedback:
    """Constrained parent activity feedback before any free prose is kept."""

    info_state: InfoState
    themes: tuple[str, ...]
    activities: tuple[str, ...]
    session_state: SessionState
    readable_summary: str | None
    findings: tuple[DetectorFinding, ...]


@dataclass(frozen=True, slots=True)
class SessionRecapDraft:
    summary: DialogueSummary | None
    archive_facts: tuple[ArchiveFact, ...]
    skipped_reason: str | None
    user_turn_count: int
    feedback: ParentActivityFeedback | None = None


def user_turns(turns: Sequence[SessionTurn]) -> list[SessionTurn]:
    return [turn for turn in turns if turn.role == "user" and turn.text.strip()]


def is_story_playback_only(turns: Sequence[SessionTurn]) -> bool:
    """True when every user turn is only a story-request phrase."""

    users = user_turns(turns)
    if not users:
        return True
    return all(_STORY_ONLY_USER.match(turn.text.strip()) is not None for turn in users)


def skip_session_recap(turns: Sequence[SessionTurn]) -> str | None:
    """Return a skip reason, or None when a recap should be produced."""

    users = user_turns(turns)
    if len(users) < 2:
        return "fewer_than_two_turns"
    if is_story_playback_only(turns):
        return "story_playback_only"
    return None


def session_summary_prompt(turns: Sequence[SessionTurn]) -> str:
    """User prompt for the summary model. Includes child lines only for context."""

    lines: list[str] = []
    for turn in user_turns(turns)[:40]:
        lines.append("- " + " ".join(turn.text.split())[:200])
    return SESSION_SUMMARY_USER_PROMPT_PREFIX + "\n" + "\n".join(lines)


def _compact(text: str) -> str:
    return "".join(text.split())


def copied_transcript_fragments(summary: str, turns: Sequence[SessionTurn]) -> list[str]:
    """Runs of the child's words that the summary repeats in VERBATIM_WINDOW pieces."""

    haystack = _compact(summary)
    fragments: list[str] = []
    for turn in user_turns(turns):
        words = _compact(turn.text)
        if len(words) < VERBATIM_WINDOW:
            continue
        covered = [False] * len(words)
        for start in range(len(words) - VERBATIM_WINDOW + 1):
            if words[start : start + VERBATIM_WINDOW] in haystack:
                covered[start : start + VERBATIM_WINDOW] = [True] * VERBATIM_WINDOW
        run = ""
        for char, hit in zip(words, covered, strict=True):
            if hit:
                run += char
            elif run:
                fragments.append(run)
                run = ""
        if run:
            fragments.append(run)
    return fragments


_THEME_TOKEN_EXCLUDE: Final[frozenset[str]] = frozenset(
    needle
    for _theme_id, _label, needles in ALLOWED_THEMES
    for needle in needles
) | frozenset(topic for topic, _label in INTEREST_TOPICS) | frozenset(
    {"孩子", "爸爸", "妈妈", "爷爷", "奶奶", "故事", "游戏", "玩具"}
)


def detect_names(text: str) -> DetectorFinding | None:
    if _NAMED_ROLE_RE.search(text) is not None or _FRIEND_NICK_RE.search(text) is not None:
        return DetectorFinding("names", "hit")
    introduced = _INTRODUCED_PERSON_RE.search(text)
    if introduced is not None and introduced.group(1) not in _THEME_TOKEN_EXCLUDE:
        return DetectorFinding("names", "hit")
    cued = _CUED_SURNAME_RE.search(text)
    if cued is not None and cued.group(1) not in _THEME_TOKEN_EXCLUDE:
        return DetectorFinding("names", "hit")
    return None


def detect_precise_location(text: str) -> DetectorFinding | None:
    if _ADDRESS_OR_SCHOOL_NAME_RE.search(text) is not None:
        return DetectorFinding("precise_location", "hit")
    if _LOCATION_UNCERTAIN_RE.search(text) is not None:
        return DetectorFinding("precise_location", "uncertain")
    return None


def detect_health(text: str) -> DetectorFinding | None:
    if any(term in text for term in _HEALTH_TERMS):
        return DetectorFinding("health", "hit")
    if any(term in text for term in _HEALTH_UNCERTAIN):
        return DetectorFinding("health", "uncertain")
    return None


def detect_victimization(text: str) -> DetectorFinding | None:
    if any(term in text for term in _VICTIM_DETAIL_TERMS) or "危机" in text:
        return DetectorFinding("victimization", "hit")
    if any(term in text for term in _VICTIM_UNCERTAIN):
        return DetectorFinding("victimization", "uncertain")
    return None


def detect_verbatim(text: str, turns: Sequence[SessionTurn]) -> DetectorFinding | None:
    if copied_transcript_fragments(text, turns):
        return DetectorFinding("verbatim", "hit")
    return None


def detect_psych_inference(text: str) -> DetectorFinding | None:
    if any(term in text for term in _PSYCH_INFERENCE_TERMS):
        return DetectorFinding("psych_inference", "hit")
    return None


_DETECTORS: Final = (
    detect_names,
    detect_precise_location,
    detect_health,
    detect_victimization,
    detect_psych_inference,
)


def run_summary_detectors(
    text: str, turns: Sequence[SessionTurn]
) -> tuple[DetectorFinding, ...]:
    """Independent detectors. Callers treat any hit or uncertain as unsafe prose."""

    findings: list[DetectorFinding] = []
    for detector in _DETECTORS:
        found = detector(text)
        if found is not None:
            findings.append(found)
    verbatim = detect_verbatim(text, turns)
    if verbatim is not None:
        findings.append(verbatim)
    if quotes_speech(text):
        findings.append(DetectorFinding("verbatim", "hit"))
    return tuple(findings)


def extract_allowed_themes(turns: Sequence[SessionTurn]) -> tuple[str, ...]:
    """Whitelist theme ids from child turns. No psych labels."""

    blob = "".join(turn.text for turn in user_turns(turns))
    found: list[str] = []
    for theme_id, _label, needles in ALLOWED_THEMES:
        if theme_id == "other_safe":
            continue
        if any(needle in blob for needle in needles):
            found.append(theme_id)
    if not found and user_turns(turns):
        found.append("other_safe")
    return tuple(found)


def theme_activity_labels(theme_ids: Sequence[str]) -> tuple[str, ...]:
    labels = {theme_id: label for theme_id, label, _needles in ALLOWED_THEMES}
    return tuple(labels[theme_id] for theme_id in theme_ids if theme_id in labels)


def detail_free_activity_note(theme_ids: Sequence[str]) -> str | None:
    """Parent-visible note with themes only. No names, places, health, or quotes."""

    labels = theme_activity_labels(theme_ids)
    safe = tuple(label for label in labels if label != "日常聊天")
    if not safe:
        return None
    if len(safe) == 1:
        return f"孩子今天聊了{safe[0]}"
    return f"孩子今天聊了{'、'.join(safe)}"


def redact_name_and_health_terms(text: str) -> str:
    """Strip named roles, health terms, and victim details without chopping unrelated words."""

    cleaned = _NAMED_ROLE_RE.sub("", text)
    cleaned = _FRIEND_NICK_RE.sub("", cleaned)
    for term in (*_HEALTH_TERMS, *_VICTIM_DETAIL_TERMS, *_PSYCH_INFERENCE_TERMS):
        cleaned = cleaned.replace(term, "")
    cleaned = re.sub(r"[，,]{2,}", "，", cleaned)
    return cleaned.strip("，。、；; \t\n")


def accept_parent_summary(text: str, turns: Sequence[SessionTurn]) -> str | None:
    """Return cleaned prose only when every independent detector is clear."""

    original = " ".join(text.split())
    if not original:
        return None
    findings = run_summary_detectors(original, turns)
    if findings:
        return None
    cleaned = original
    if len(_compact(cleaned)) < 6:
        return None
    if len(cleaned) > SUMMARY_MAX_CHARS:
        cleaned = cleaned[: SUMMARY_MAX_CHARS - 1] + "…"
    return cleaned


def session_hits_crisis(turns: Sequence[SessionTurn]) -> bool:
    """True when any turn of the session routes to a crisis action.

    The child's own turns go through the deterministic route, as the reply
    pipeline does. The robot's scripted safety lines are added so a session
    where only the reply is a fixed safety reply still counts.
    """

    for turn in turns:
        if not turn.text.strip():
            continue
        if route_crisis(turn.text).action != "none":
            return True
        if turn.role == "assistant" and route_crisis(turn.text).script is not None:
            return True
    return False


def compose_parent_activity_feedback(
    turns: Sequence[SessionTurn],
    *,
    summary_text: str | None,
    skipped_reason: str | None = None,
) -> ParentActivityFeedback:
    """Emit constrained themes/activities/session-state first, then safe prose."""

    themes = extract_allowed_themes(turns)
    activities = theme_activity_labels(themes)
    users = user_turns(turns)
    early = skip_session_recap(turns)
    if early == "fewer_than_two_turns":
        return ParentActivityFeedback(
            info_state="skipped_short",
            themes=(),
            activities=(),
            session_state="too_short",
            readable_summary=None,
            findings=(),
        )
    if early == "story_playback_only":
        return ParentActivityFeedback(
            info_state="skipped_story",
            themes=("stories",),
            activities=theme_activity_labels(("stories",)),
            session_state="story_only",
            readable_summary=None,
            findings=(),
        )
    if session_hits_crisis(turns):
        return ParentActivityFeedback(
            info_state="crisis_skipped",
            themes=(),
            activities=(),
            session_state="crisis",
            readable_summary=None,
            findings=(),
        )
    if skipped_reason in {"summary_model_unavailable", "summary_model_failed"} or (
        skipped_reason is None and summary_text is None
    ):
        return ParentActivityFeedback(
            info_state="failed",
            themes=themes,
            activities=activities,
            session_state="model_failed",
            readable_summary=None,
            findings=(),
        )
    if skipped_reason is not None and summary_text is None:
        return ParentActivityFeedback(
            info_state="failed",
            themes=themes,
            activities=activities,
            session_state="model_failed",
            readable_summary=None,
            findings=(),
        )

    assert summary_text is not None
    findings = run_summary_detectors(summary_text, turns)
    if not findings:
        accepted = accept_parent_summary(summary_text, turns)
        if accepted is not None:
            return ParentActivityFeedback(
                info_state="ready",
                themes=themes,
                activities=activities,
                session_state="ordinary",
                readable_summary=accepted,
                findings=(),
            )

    note = detail_free_activity_note(themes)
    if note is not None and not run_summary_detectors(note, turns):
        return ParentActivityFeedback(
            info_state="degraded",
            themes=themes,
            activities=activities,
            session_state="ordinary",
            readable_summary=note,
            findings=findings,
        )
    return ParentActivityFeedback(
        info_state="not_generated",
        themes=themes,
        activities=activities,
        session_state="ordinary",
        readable_summary=None,
        findings=findings,
    )


def _skip_reason_for_feedback(feedback: ParentActivityFeedback) -> str | None:
    if feedback.info_state == "ready":
        return None
    if feedback.info_state == "degraded":
        return None
    mapping: dict[InfoState, str] = {
        "skipped_short": "fewer_than_two_turns",
        "skipped_story": "story_playback_only",
        "crisis_skipped": "crisis_in_session",
        "failed": "summary_model_failed",
        "not_generated": "not_generated",
    }
    return mapping.get(feedback.info_state, "invalid_summary")


def draft_session_recap(
    *,
    subject_id: str,
    session_id: str,
    source_event_id: str,
    turns: Sequence[SessionTurn],
    archive_consent: bool,
    summary_text: str | None,
    skipped_reason: str | None = None,
) -> SessionRecapDraft:
    """Build the session-end draft from constrained feedback, then optional prose."""

    users = user_turns(turns)
    feedback = compose_parent_activity_feedback(
        turns, summary_text=summary_text, skipped_reason=skipped_reason
    )
    skip = _skip_reason_for_feedback(feedback)
    if feedback.readable_summary is None:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason=skip or "not_generated",
            user_turn_count=len(users),
            feedback=feedback,
        )
    try:
        summary = DialogueSummary(
            subject_id=subject_id,
            text=feedback.readable_summary,
            source_event_id=source_event_id,
            session_id=session_id,
        )
    except ValueError:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason="invalid_summary",
            user_turn_count=len(users),
            feedback=feedback,
        )
    # Preference archive facts only when ordinary ready prose was accepted.
    facts = (
        extract_interest_facts(subject_id, turns)
        if archive_consent and feedback.info_state == "ready"
        else ()
    )
    return SessionRecapDraft(
        summary=summary,
        archive_facts=facts,
        skipped_reason=None,
        user_turn_count=len(users),
        feedback=feedback,
    )


__all__ = [
    "ALLOWED_THEMES",
    "INTEREST_TOPICS",
    "SESSION_SUMMARY_SYSTEM_PROMPT",
    "VERBATIM_WINDOW",
    "DetectorFinding",
    "ParentActivityFeedback",
    "SessionRecapDraft",
    "SessionTurn",
    "accept_parent_summary",
    "compose_parent_activity_feedback",
    "copied_transcript_fragments",
    "detail_free_activity_note",
    "detect_health",
    "detect_names",
    "detect_precise_location",
    "detect_psych_inference",
    "detect_verbatim",
    "detect_victimization",
    "draft_session_recap",
    "extract_allowed_themes",
    "extract_interest_facts",
    "is_story_playback_only",
    "redact_name_and_health_terms",
    "run_summary_detectors",
    "session_hits_crisis",
    "session_summary_prompt",
    "skip_session_recap",
    "theme_activity_labels",
    "user_turns",
]
