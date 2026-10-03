"""What a spoken "remember this" request means, read the same way by the archive and the agent.

The archive's write policy (``services/archive/memory_write_policy.py``) decides whether such a
request becomes a confirmed memory.  The agent must not promise more than that policy delivers, so
the checks that depend on the sentence alone live here and both sides import them.
"""

from __future__ import annotations

import re
import unicodedata

from services.common.redaction import redact_pii

# The command word and what may follow it before the content starts.
_COMMAND = r"(?:请帮我|请|帮我)记住(?:一下)?(?:这件事)?(?:[：:,，]\s*|\s+)?"
_EXPLICIT_REMEMBER = re.compile(rf"^{_COMMAND}(?P<content>.+)$")
# One or two short sentences ahead of the command.  The robot's own greeting or reply leaks back
# through its speaker and the ASR glues it to the child's words (2026-10-02 round 10, first
# sentence after wake: "晚上好，你在？ 帮我记住，我最喜欢蓝色。"), so the command is no longer
# sentence-initial although the child said nothing else.
_LEAD_IN_THEN_REMEMBER = re.compile(
    rf"^(?:[^。!?.\s][^。!?.]{{0,15}}[。!?.]+\s*){{1,2}}(?P<command>{_COMMAND}.+)$"
)
# A request to remember, wherever it sits in the sentence ("我记住了" and "你记住了吗" are not).
_REMEMBER_REQUEST = re.compile(
    r"(?:^|[,。!;:\s])(?:请你|请|麻烦你?|帮我|给我|替我|你要|你得|你可要|你能|你|能不能|可以)?"
    r"(?:记住(?!了)|记一下|记下来|别忘了|不要忘了|不要忘记)"
)

SENSITIVE_TERMS = (
    "身份证",
    "银行卡",
    "手机号",
    "家庭住址",
    "邮箱",
    "密码",
    "验证码",
    "病史",
    "诊断",
    "过敏",
    "血压",
    "血糖",
    "手术",
    "抑郁",
    "焦虑",
    "收入",
    "工资",
    "资产",
    "负债",
    "贷款",
    "诉讼",
    "案件",
    "犯罪",
    "判决",
    "声纹",
    "指纹",
    "人脸",
    "虹膜",
    "身高",
    "体重",
    "身体特征",
    "青春期",
    "妈妈",
    "爸爸",
    "父母",
    "妻子",
    "丈夫",
    "伴侣",
    "朋友",
    "同事",
    "家人",
    "离婚",
    "婚外",
)
_LOW_RISK_TEMPLATES = (
    (
        "preference",
        re.compile(r"^我(?:最|很|非常|比较|更)?(?:喜欢|不喜欢|偏好)(?P<value>.+)$"),
    ),
    (
        "habit",
        re.compile(r"^我(?:平时|通常|一直)?习惯(?P<value>.+)$"),
    ),
)
_LOW_RISK_VALUE_TOKENS = (
    "散步",
    "跑步",
    "运动",
    "阅读",
    "看书",
    "音乐",
    "电影",
    "旅行",
    "咖啡",
    "茶",
    "烹饪",
    "做饭",
    "每天",
    "早起",
    "早睡",
    "日记",
    "写作",
    "绘画",
    "摄影",
    "园艺",
    "植物",
    "宠物",
    "游戏",
    "晴天",
    "雨天",
    "颜色",
    "蓝色",
    "绿色",
    "红色",
    "清淡",
    "甜食",
)
_LOW_RISK_VALUE_SEQUENCE = re.compile(
    "(?:"
    + "|".join(re.escape(token) for token in sorted(_LOW_RISK_VALUE_TOKENS, key=len, reverse=True))
    + ")+"
)
_SENSITIVE_SELF_FACT = re.compile(
    r"(?:出生|生日|年龄|年纪|\d{1,3}\s*岁|"
    r"\d{4}\s*(?:年|[-/.])\s*\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
#: Whole-sentence contexts a minor long-term projection must not keep, even when
#: the same sentence also names a safe study word such as 练习 or 数学. This is
#: a fail-closed tightening, not an expansion of the allowlist. Criticism and
#: conflict never have a negation exception: "老师没批评" and "老师没有批评"
#: are still dropped.
_MINOR_HARD_CONTEXT = (
    "老师批评",
    "老师没批评",
    "老师没有批评",
    "老师骂",
    "被骂",
    "训斥",
    "罚站",
    "吵架",
    "打架",
    "被欺负",
    "闹矛盾",
    "闹别扭",
    "闹翻",
)
#: Feeling words match the mood follow-up list so a negative emotion cannot ride
#: in on a study keyword. A short local negation ("不用紧张") does not count;
#: anything else, including a missing or distant negation, stays fail-closed.
_MINOR_FEELING_CONTEXT = (
    "难过",
    "伤心",
    "不开心",
    "委屈",
    "生气",
    "气愤",
    "失望",
    "沮丧",
    "郁闷",
    "害怕",
    "恐惧",
    "紧张",
    "着急",
    "担心",
    "孤单",
    "孤独",
    "寂寞",
    "烦躁",
    "无聊",
    "难受",
)
_MINOR_FEELING_NEGATION = re.compile(
    r"(?:没有|不用|别|不|没)(?:太|很|非常|特别|那么|这么|有点|一点)?$"
)


def _normalized(text: object) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def explicit_remember_content(text: object) -> str | None:
    """Return the requested fact for a strict sentence-initial remember command.

    One or two short lead-in sentences before the command are tolerated only when what follows is
    a closed low-risk self fact (the only kind the archive confirms on its own): a lead-in that
    belongs to the content ("我明天考试。帮我记住这件事") must keep the whole sentence for the
    extractor, so it never takes this path.
    """

    value = _normalized(text)
    if not value or value.endswith(("?", "？")):
        return None
    match = _EXPLICIT_REMEMBER.fullmatch(value)
    lead_in = False
    if match is None:
        lead = _LEAD_IN_THEN_REMEMBER.fullmatch(value)
        match = _EXPLICIT_REMEMBER.fullmatch(lead.group("command")) if lead is not None else None
        lead_in = match is not None
    if match is None:
        return None
    content = match.group("content").strip()
    question_probe = content.rstrip("。.!！").strip()
    if not content.strip(" \t\r\n:：,，。.!！\"'“”‘’") or question_probe.endswith("吗"):
        return None
    if lead_in and low_risk_self_fact_predicate(content) is None:
        return None
    return content


def asks_to_remember(text: object) -> bool:
    """True when the sentence asks the listener to remember something (not a question about it)."""

    value = _normalized(text)
    if not value or value.rstrip("。.! ").endswith(("?", "吗")):
        return False
    return _REMEMBER_REQUEST.search(value) is not None


def contains_sensitive_text(value: str) -> bool:
    return redact_pii(value) != value or any(term in value for term in SENSITIVE_TERMS)


def _negated_feeling(value: str, term: str) -> bool:
    """True only when every hit sits immediately after a short negation."""

    start = 0
    found = False
    while True:
        index = value.find(term, start)
        if index < 0:
            return found
        found = True
        if _MINOR_FEELING_NEGATION.search(value[:index]) is None:
            return False
        start = index + len(term)


def minor_sensitive_context(value: str) -> bool:
    """True when a minor sentence carries criticism, conflict, or a negative feeling.

    Feeling words ignore a narrow local negation such as 不用/不/没/没有/别.
    Hard criticism and conflict terms do not, so the guard stays fail-closed.
    """

    if any(term in value for term in _MINOR_HARD_CONTEXT):
        return True
    return any(
        term in value and not _negated_feeling(value, term) for term in _MINOR_FEELING_CONTEXT
    )


def _is_closed_low_risk_value(value: str) -> bool:
    segments = re.split(r"[、，,和与或]", re.sub(r"\s+", "", value))
    return bool(segments) and all(
        segment and _LOW_RISK_VALUE_SEQUENCE.fullmatch(segment) for segment in segments
    )


def low_risk_self_fact_predicate(value: object) -> str | None:
    """Return the only server-verifiable facts eligible for auto-confirmation."""

    content = _normalized(value).rstrip("。.!！?")
    if not content or contains_sensitive_text(content) or _SENSITIVE_SELF_FACT.search(content):
        return None
    for predicate, template in _LOW_RISK_TEMPLATES:
        match = template.fullmatch(content)
        if match is None:
            continue
        fact_value = match.group("value").strip()
        if _is_closed_low_risk_value(fact_value):
            return predicate
    return None


def explicit_remember_will_save(text: object, *, minor: bool) -> bool:
    """Would the archive's write policy confirm this sentence on its own?

    Only the checks that depend on the sentence are repeated here; the extractor's answer is not
    known yet, so a ``True`` is a promise the policy keeps unless the extraction itself fails.
    """

    content = explicit_remember_content(text)
    if content is None:
        return False
    whole = _normalized(text)
    if contains_sensitive_text(whole) or low_risk_self_fact_predicate(content) is None:
        return False
    return not (minor and minor_sensitive_context(whole))
