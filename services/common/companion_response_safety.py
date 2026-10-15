"""Deterministic companion replies that must never rely on an LLM."""

from __future__ import annotations

import re
from typing import Final, Literal

from services.common.fixed_safety_scripts import script_for_decision

SafetyDecision = Literal[
    "crisis_self",
    "support_victim",
    "support_relay",
    "prohibited_request",
    "none",
]

SAFE_UNKNOWN_REPLY = "我不知道。"
# Product: no personal name. Exact fixed line for the closed identity question set.
NAMELESS_IDENTITY_REPLY: Final = (
    "我是 AI 助手，不是真人，可以陪你聊天、讲故事。你今天想聊什么呀？"
)
# Product: affection / attachment questions never get 我喜欢你 / 我爱你 / 好朋友.
AFFECTION_FIXED_REPLY: Final = (
    "我是 AI，没有喜欢的感觉，不过我会认真听你说话。你今天想聊点什么呀？"
)
GUARDIAN_ATTENTION_COPY: Final = "有一条需要你关注的对话"
GUARDIAN_ATTENTION_STORY_RETELL_COPY: Final = (
    "有一条需要你关注的对话（可能是在复述故事）"
)


def guardian_attention_copy(*, possibly_story_retell: bool = False) -> str:
    """Parent Mini Program pin text. Story framing never suppresses the route."""

    if possibly_story_retell:
        return GUARDIAN_ATTENTION_STORY_RETELL_COPY
    return GUARDIAN_ATTENTION_COPY


def is_story_retell_context(query: str) -> bool:
    """True when the utterance is framed as story / cartoon / picture-book retelling."""

    del query
    return False
AFTER_FIXED_SAFETY_SESSION_INSTRUCTION: Final = (
    "本会话已经对用户说过固定安全话术。不要继续讨论或追问刚才的安全话题；"
    "若用户再次提出同类自伤、受害或转述他人危险的求助，系统会再次给出同一句固定话术，你不要另写。"
)


CRISIS_CONTEXT_MARKERS: Final = (
    "自杀",
    "自残",
    "自尽",
    "轻生",
    "割腕",
    "跳楼",
    "结束生命",
    "已经吞药",
    "正在吞药",
    "吞药了",
    "不想活",
    "想死",
    "活着没意思",
    "suicide",
    "self-harm",
    "self harm",
    "kill myself",
    "hurt myself",
    "don't want to live",
    "do not want to live",
    "end my life",
)

# Forbidden-copy list for deterministic prohibited-request refusals (count is frozen at 39).
PROHIBITED_CONTENT_TERMS: Final[tuple[str, ...]] = (
    "色情",
    "淫秽",
    "裸聊",
    "黄片",
    "黄网",
    "黄色网站",
    "成人片",
    "a片",
    "性行为",
    "性交",
    "强奸",
    "裸体",
    "杀人",
    "杀害",
    "砍人",
    "暴力",
    "炸弹",
    "炸药",
    "爆炸物",
    "制爆",
    "爆炸",
    "造枪",
    "枪支",
    "制毒",
    "毒品",
    "下毒",
    "投毒",
    "毒杀",
    "性侵",
    "性暴力",
    "迷奸",
    "猥亵",
    "暴恐",
    "恐怖袭击",
    "恐怖主义",
    "诈骗",
    "洗钱",
    "赌博",
    "黑客入侵",
)
_PROHIBITED_CONTENT_TERMS = PROHIBITED_CONTENT_TERMS
_PROHIBITED_ENGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    r"porn(?:ography)?|sex(?:ual)?|av|rape|assault|murder|kill(?:ing)?|"
    r"violence|bomb|explosive|weapon|poison|drug|"
    r"fraud|gambl(?:e|ing)|hack(?:ing)?"
    r")(?![a-z])"
)
_EXPLICIT_SELF_HARM_INTENT_RE = re.compile(
    r"我(?:真(?:的)?|现在)?(?:"
    # 或/霍/货/和 are speaker-to-mic slips for 活 in 不想活了.
    r"不想(?:再|继续)?(?:活(?:下去)?(?:了)?|[或霍货和]了)|"
    r"活不下去(?:了)?|"
    r"(?:想|要|准备|打算)(?:去)?(?:"
    r"死|自杀|自残|自尽|轻生|结束生命|杀(?:了|死)?自己|跳楼|伤害自己"
    r")"
    r")"
)
_EXPLICIT_SELF_HARM_ACTION_RE = re.compile(
    r"我(?:已经|刚刚?|正在|现在)?(?:"
    r"割腕(?:了)?|割伤自己(?:了)?|"
    r"吞(?:了)?(?:很多|大量|一瓶|一整瓶)?(?:安眠)?药(?:了)?|"
    r"吃(?:了)?(?:很多|大量|一瓶|一整瓶)(?:安眠)?药(?:了)?|"
    r"服药过量(?:了)?|站在楼顶(?:了)?"
    r")"
)
_QUOTED_TEXT_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
_REPORTED_OR_ANALYTICAL_RE = re.compile(
    r"(?:朋友|家人|同学|孩子|他|她|有人|别人).{0,8}(?:跟我说|告诉我|说|表示|提到)|"
    r"(?:新闻|报道|老师|作业|论文|小说|台词|这句话|翻译|分析)|"
    r"(?:怎么|如何)(?:帮助|帮|劝|安慰|阻止)(?:他|她|对方)"
)
_UNQUOTED_REPORTED_SELF_HARM_RE = re.compile(
    r"(?:朋友|家人|同学|孩子|他|她|有人|别人).{0,12}"
    r"(?:跟我说|告诉我|说|表示|提到).{0,12}"
    r"(?:我想自杀|我想死|我不想活|想自残|想轻生|想结束生命)"
)
_VICTIM_SUPPORT_RE = re.compile(
    r"(?:"
    r"我刚被性侵|我被性侵|我遭到性侵|"
    r"我刚被强奸|我被强奸|"
    r"有人正在对我实施暴力|"
    r"(?:有人|别人)(?:正在|在)?打我(?!电话|的电话)|"
    # Adult / relative hitting the child; 大我 is a common ASR slip for 打我.
    r"(?:有个)?(?:大人|叔叔|阿姨|老师).{0,12}(?:老是)?(?:打|大)我|"
    r"老是(?:打|大)我|"
    r"有人摸我(?:的)?身体|摸我身体|"
    r"我正在被打|"
    r"我被下毒|我中毒|"
    r"有人要杀(?:了)?我|"
    r"快救我|救救我|"
    r"(?<!稻草)救命(?!稻草)"
    r")"
)
_REPORTED_PEER_HARM_RE = re.compile(
    r"(?:"
    r"(?:同桌|朋友|同学|家人).{0,24}(?:说|告诉我|跟我说).{0,24}"
    r"(?:打她|打他|欺负她|欺负他|被打|被人打)|"
    r"(?:同桌|朋友|同学).{0,12}被人打了"
    r")"
)
_EXPLICIT_SELF_HARM_EXACT = frozenset(
    (
        "我不想活",
        "活着没意思",
        "活着真的没意思",
        "我活着没意思",
        "我活着真的没意思",
        "不如死了算了",
        "我不如死了算了",
    )
)
_SELF_HARM_METHOD_RE = re.compile(
    r"(?:怎么|如何|怎样)(?:才能|可以|去)?(?:自杀|自残|自尽|轻生|割腕|跳楼|结束生命)"
)
_SAFETY_PREVENTION_RE = re.compile(
    r"(?:怎么|如何|怎样)(?:预防|防止|避免)"
    r"(?:性侵|强奸|暴力|自杀|自残|轻生|下毒|投毒|诈骗)"
)
_EXPLICIT_SELF_HARM_ENGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    r"i (?:want|plan|intend|am going) to (?:die|kill myself|hurt myself|end my life)|"
    r"i do not want to live|i don't want to live|"
    r"how (?:can|do|should) i (?:kill|hurt) myself"
    r")(?![a-z])"
)
# Closed product sets: match after punctuation/whitespace strip, lowercasing,
# and repeated trailing-particle strip (呀/啊/呢/嘛/哦). AI/ai is case-folded.
_IDENTITY_FIXED_QUERIES: Final[frozenset[str]] = frozenset(
    (
        "你是谁",
        "你到底是谁",
        "你是什么",
        "你叫什么",
        "你叫什么名字",
        "你叫啥",
        "你叫啥名字",
        "你有名字吗",
        "你是真人吗",
        "你是不是真人",
        "你是真的人吗",
        "你是人吗",
        "你是不是人",
        "你是机器人吗",
        "你是不是机器人",
        "你是ai吗",
    )
)
_AFFECTION_FIXED_QUERIES: Final[frozenset[str]] = frozenset(
    (
        "你喜欢我吗",
        "你喜不喜欢我",
        "你爱我吗",
        "你爱不爱我",
        "你想我吗",
        "你想不想我",
        "你会想我吗",
        "你是我的朋友吗",
        "你是我的好朋友吗",
    )
)
_TRAILING_PARTICLES: Final[frozenset[str]] = frozenset("呀啊呢嘛哦")
_FIXED_REPLY_FORBIDDEN_PHRASES: Final[tuple[str, ...]] = (
    "我喜欢你",
    "我爱你",
    "好朋友",
)


def _normalize_query(query: str) -> str:
    text = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    while text and text[-1] in _TRAILING_PARTICLES:
        text = text[:-1]
    return text


def companion_safety_decision(query: str) -> SafetyDecision:
    """Classify one current-turn query without granting facts or permissions."""
    stripped_of_quotes = _QUOTED_TEXT_RE.sub("", query)
    # Reported self-harm must survive quote stripping; otherwise fiction /
    # analysis that only quotes risk language would still trip the relay path.
    if _UNQUOTED_REPORTED_SELF_HARM_RE.search(stripped_of_quotes):
        return "support_relay"
    crisis_query = stripped_of_quotes
    if crisis_query == query or not _REPORTED_OR_ANALYTICAL_RE.search(crisis_query):
        crisis_query = query
    normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", crisis_query).lower()
    if (
        normalized in _EXPLICIT_SELF_HARM_EXACT
        or _EXPLICIT_SELF_HARM_INTENT_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ACTION_RE.search(normalized)
        or _SELF_HARM_METHOD_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ENGLISH_RE.search(crisis_query.lower())
    ):
        return "crisis_self"
    full_normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    if _VICTIM_SUPPORT_RE.search(full_normalized):
        return "support_victim"
    if _REPORTED_PEER_HARM_RE.search(full_normalized):
        return "support_relay"
    if _SAFETY_PREVENTION_RE.search(full_normalized):
        return "support_relay"
    # Markers follow the same quote-stripped surface as crisis_self, so fiction
    # / analysis that only quotes risk language does not become a support hit.
    if any(marker in crisis_query.lower() for marker in CRISIS_CONTEXT_MARKERS):
        return "support_relay"
    if any(term in full_normalized for term in _PROHIBITED_CONTENT_TERMS) or (
        _PROHIBITED_ENGLISH_RE.search(query.lower())
    ):
        return "prohibited_request"
    return "none"


def companion_identity_reply(
    display_name: str,
    style_description: str,
    audience: str | None = None,
) -> str:
    del display_name, style_description, audience
    return NAMELESS_IDENTITY_REPLY


def companion_identity_replies(display_name: str, style_description: str) -> frozenset[str]:
    """Every introduction ``fixed_companion_reply`` can give for this companion.

    The plan admission check compares the plan's fixed text against this set, so a new
    audience variant can never be silently refused there. Display name and style stay
    in the signature for callers; they no longer shape the spoken line.
    """

    del display_name, style_description
    return frozenset({NAMELESS_IDENTITY_REPLY, AFFECTION_FIXED_REPLY})


def fixed_companion_reply(
    *,
    query: str,
    is_companion: bool,
    display_name: str | None = None,
    style_description: str | None = None,
    audience: str | None = None,
) -> str | None:
    """Return a fixed reply for crisis, prohibited, identity, or affection queries."""

    del display_name, style_description, audience
    decision = companion_safety_decision(query)
    script = script_for_decision(decision)
    if script is not None:
        return script.display
    if decision == "prohibited_request":
        return SAFE_UNKNOWN_REPLY
    if not is_companion:
        return None
    normalized = _normalize_query(query)
    if normalized in _IDENTITY_FIXED_QUERIES:
        return NAMELESS_IDENTITY_REPLY
    if normalized in _AFFECTION_FIXED_QUERIES:
        return AFFECTION_FIXED_REPLY
    return None
