"""Deterministic companion replies that must never rely on an LLM."""

from __future__ import annotations

import re
from typing import Final, Literal

from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    script_for_decision,
)

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
AFTER_FIXED_SAFETY_SESSION_INSTRUCTION: Final = (
    "本会话已经对用户说过固定安全话术，那一句已经说完。"
    "不要重复它，不要改写它，不要把它截短，也不要在后面加安慰或叫对方别担心的话。"
    "用户接下来如果是普通聊天，就按当前这件事用一两句短话回答。"
    "如果用户再次提出同类自伤、受害或转述他人危险的求助，系统会给出固定话术，你不要另写。"
)
_SOOTHING_DILUTION_PHRASES: Final[tuple[str, ...]] = (
    "刚才的事别担心",
    "别害怕",
    "不要怕",
    "没事的",
    "没关系",
    "别担心",
    "不用担心",
    "会好起来的",
    "一切都会好的",
    "我陪着你",
)

# Longer would be a sentence of its own, not the filler before the answer.
_SOOTHING_OPENER_MAX_CHARS = 8
_FILLER_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，,、；;：:.!！?？。])\s*")


def _non_soothing_chars(text: str) -> str:
    compact = "".join(ch for ch in text if not ch.isspace())
    for phrase in _SOOTHING_DILUTION_PHRASES:
        compact = compact.replace(phrase, "")
    return "".join(ch for ch in compact if ch.isalnum())


def strip_soothing_from_follow_up(text: str) -> str:
    """Remove soothing wording that opens a post-safety reply, keeping the rest.

    Field 2026-10-21 N2 t002: the reply after the crisis script opened with
    「没关系，」 and ``assistant_text_repeats_or_dilutes_safety`` dropped the
    whole first sentence with the wording, and the pipeline had nothing left
    to say.  Only a leading filler clause that holds nothing but soothing
    wording is cut; the rest of the sentence is the child's answer.

    A segment that is nothing but soothing wording is returned unchanged so
    the caller's dilution check still drops it: the 产品部 list stays unsaid,
    and only a filler in front of the child's answer is cut.
    """

    stripped = text.lstrip()
    if not any(phrase in stripped for phrase in _SOOTHING_DILUTION_PHRASES):
        return text
    parts = _FILLER_CLAUSE_SPLIT_RE.split(stripped.rstrip(), maxsplit=1)
    if len(parts) < 2:
        return text
    head = parts[0]
    if not any(phrase in head for phrase in _SOOTHING_DILUTION_PHRASES):
        return text
    if len(_non_soothing_chars(head)) > _SOOTHING_OPENER_MAX_CHARS:
        return text
    rest = parts[1].lstrip()
    return rest or text


def assistant_text_repeats_or_dilutes_safety(
    text: str,
    *,
    include_soothing: bool = True,
) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    compact = "".join(ch for ch in stripped if not ch.isspace())
    if include_soothing and any(phrase in compact for phrase in _SOOTHING_DILUTION_PHRASES):
        return True
    if len(compact) < 4:
        return False
    for script in (CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT):
        display = "".join(ch for ch in script.display if not ch.isspace())
        spoken = "".join(ch for ch in script.spoken if not ch.isspace())
        if compact in display or compact in spoken:
            return True
    return False


_STORY_RETELL_FRAME_RE = re.compile(r"(?:故事|绘本|动画片|童话|神话|哪吒)")


def guardian_attention_copy(*, possibly_story_retell: bool = False) -> str:
    """Parent title. ``possibly_story_retell`` stays on the row and is not written here."""

    return GUARDIAN_ATTENTION_COPY


def is_story_retell_context(query: str) -> bool:
    """True only when the utterance contains an explicit story-medium cue word."""

    return _STORY_RETELL_FRAME_RE.search(query) is not None


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
    r"死|自杀|自残|自尽|轻生|结束生命|杀(?:了|死)?自己|跳楼|伤害自己|"
    r"(?:拿|用)?(?:刀|小刀|剪刀)?(?:划|割)(?:伤|破)?(?:自己|手腕)"
    r")"
    r")"
)
# Word-class hits for any subject. Bare 消失/死掉 stay quiet (fairy-tale plots).
_SELF_HARM_INTENT_WORD_CLASS_RE = re.compile(r"想消失")
_SELF_HARM_ACTION_WORD_CLASS_RE = re.compile(r"割(?:了)?自己")
_FIRST_PERSON_JUMP_RE = re.compile(r"我.{0,16}(?:从楼上跳|跳下去)")
# Despair phrase group: reason-to-live, meaninglessness, wish-not-to-exist.
# Any-subject for 找不到…理由 (story-retell). 不在了就好 stays first-person.
_NO_REASON_TO_LIVE_RE = re.compile(r"找不[到着](?:继续)?活下去的理由")
_LIFE_MEANINGLESS_RE = re.compile(r"活着(?:真的)?没意思")
_WISH_NOT_EXIST_RE = re.compile(r"(?:要是|如果)?我不在了就好(?:了)?")
_CRISIS_SELF_WORD_CLASS_RES: Final[tuple[re.Pattern[str], ...]] = (
    _SELF_HARM_INTENT_WORD_CLASS_RE,
    _SELF_HARM_ACTION_WORD_CLASS_RE,
    _FIRST_PERSON_JUMP_RE,
    _NO_REASON_TO_LIVE_RE,
    _LIFE_MEANINGLESS_RE,
    _WISH_NOT_EXIST_RE,
)
# First-person clause rule. Chinese drops the subject after the first clause
# (「我有点难过，不想活了」), so a risk phrase that opens a clause belongs to the
# subject in force: the speaker by default and after a clause opened by 我,
# someone else after a clause opened by another person or holding reported speech.
_CLAUSE_SPLIT_RE = re.compile(r"[\s，。！？、；：,.!?;:…~～]+")
_CLAUSE_INTERJECTIONS = r"(?:嗯|啊|呃|唉|哎|哦|呜)*"
_SELF_RISK_ADVERBS = (
    r"(?:真的?|现在|有时候?|有点儿?|有些|好|很|太|特别|非常|已经|都|也|就|总是|老是|"
    r"一直|其实|突然|实在|还|又|再|简直|宁愿|甚至|感觉|觉得|是)*"
)
_SELF_RISK_PHRASE = (
    r"(?:不想(?:再|继续)?活(?!动)|活不下去|"
    r"(?:想|要|准备|打算)(?:去)?(?:死|自杀|自残|自尽|轻生|跳楼|结束(?:自己的)?生命|"
    r"伤害(?:我)?自己|杀(?:了|死)?(?:我)?自己|"
    r"(?:拿|用)?(?:刀|小刀|剪刀)?(?:划|割)(?:伤|破)?(?:自己|手腕)))"
)
_OTHER_PERSON_NOUNS = (
    r"(?:同学|同桌|朋友|好朋友|闺蜜|哥哥|姐姐|弟弟|妹妹|爸爸|妈妈|爷爷|奶奶|姥姥|姥爷|"
    r"外公|外婆|叔叔|阿姨|老师|小朋友|孩子|家人|班)"
)
_SELF_PRONOUN = rf"我(?:们)?(?![们的]|{_OTHER_PERSON_NOUNS})"
_SELF_RISK_IN_CLAUSE_RE = re.compile(rf"{_SELF_PRONOUN}{_SELF_RISK_ADVERBS}{_SELF_RISK_PHRASE}")
_SUBJECTLESS_RISK_CLAUSE_RE = re.compile(
    rf"^{_CLAUSE_INTERJECTIONS}{_SELF_RISK_ADVERBS}{_SELF_RISK_PHRASE}"
)
_SELF_SUBJECT_CLAUSE_RE = re.compile(rf"^{_CLAUSE_INTERJECTIONS}{_SELF_PRONOUN}")
_OTHER_SUBJECT_CLAUSE_RE = re.compile(
    rf"^{_CLAUSE_INTERJECTIONS}(?:(?:我|我们)的?{_OTHER_PERSON_NOUNS}|{_OTHER_PERSON_NOUNS}|"
    r"他|她|它|有人|别人|人家|大家|ta)"
)
_REPORTED_SPEECH_CLAUSE_RE = re.compile(r"说|告诉|讲|提到|表示|问")
_SELF_RISK_PHRASE_RE = re.compile(_SELF_RISK_PHRASE)
# ASR may drop the comma (「我有点难过不想活了」): a clause the child opens keeps
# a later risk phrase unless another person or a story enters it.
_OTHER_PERSON_IN_CLAUSE_RE = re.compile(
    rf"{_OTHER_PERSON_NOUNS}|他|她|它|有人|别人|人家|大家|故事|绘本|动画片|童话|神话"
)


def _first_person_self_risk(text: str) -> bool:
    subject_is_self = True
    for clause in _CLAUSE_SPLIT_RE.split(text.lower()):
        if not clause:
            continue
        if _REPORTED_SPEECH_CLAUSE_RE.search(clause):
            subject_is_self = False
            continue
        if _SELF_RISK_IN_CLAUSE_RE.search(clause):
            return True
        if _SELF_SUBJECT_CLAUSE_RE.search(clause):
            if _SELF_RISK_PHRASE_RE.search(clause) and not _OTHER_PERSON_IN_CLAUSE_RE.search(
                clause
            ):
                return True
            subject_is_self = True
        elif _OTHER_SUBJECT_CLAUSE_RE.search(clause):
            subject_is_self = False
        elif subject_is_self and _SUBJECTLESS_RISK_CLAUSE_RE.search(clause):
            return True
    return False


_OTHER_JUMP_RE = re.compile(r"(?:从楼上跳|跳下去)")
_CONFINEMENT_BEAT_RE = re.compile(r"关起来打")
_EXPLICIT_SELF_HARM_ACTION_RE = re.compile(
    r"我(?:已经|刚刚?|正在|现在)?(?:"
    r"割腕(?:了)?|割伤自己(?:了)?|"
    r"(?:拿|用)?(?:刀|小刀|剪刀)?(?:划|割)(?:伤|破)?(?:自己|手腕)(?:了)?|"
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
    r"(?:我想自杀|我想死|我不想活|想自残|想轻生|想结束生命|想伤害(?:他|她|我)?自己)"
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
    # Family or an older child hitting the child. Only 打: 哥哥大我三岁 is an age.
    r"(?:爸爸|妈妈|爸|妈|继父|继母|后爸|后妈|爷爷|奶奶|姥姥|姥爷|外公|外婆|"
    r"哥哥|姐姐|舅舅|姑姑|姨妈|伯伯).{0,8}(?<![不没别])打我(?!电话|的电话|手机|游戏)|"
    r"(?:有个)?(?:叔叔|阿姨|大人|老师|陌生人|爷爷|奶奶).{0,12}"
    r"摸(?:了)?我(?!的?(?:头|头发|脑袋|肩膀|手臂))|"
    r"摸(?:了)?我(?!的?(?:头|头发|脑袋|肩膀|手臂)).{0,12}(?:不让我|不让|别|不要)(?:告诉|说)|"
    r"有人摸我(?:的)?身体|摸我身体|"
    r"我正在被打|"
    r"(?:我|我们)(?:被|给|让人|被人)(?:关|锁)"
    r"(?:在(?![^，。！？、,.!?；:;]{0,5}(?:门外|外面|门口))|进|起来|了)|"
    r"(?:把|将)(?:我|我们)(?:给)?(?:关|锁)(?:在|进|起来)|"
    r"我被下毒|我中毒|"
    r"有人要杀(?:了)?我|"
    r"快救我|救救我|"
    r"(?<!稻草)救命(?!稻草)"
    r")"
)
_HARM_VERBS = r"(?:打|揍|踢|掐|推倒|欺负)"
# Passives also cover ordinary 打 compounds, so 被打扰了 / 被打电话 / 打游戏 are
# not read as a hit. The active rule already guards the object (打我电话).
_PASSIVE_HARM_VERBS = (
    r"(?:打(?!扰|听|断|开|扫|扮|包|分|呼|喷|印|字|针|雷|篮|电|手|游|招)"
    r"(?!了?.{0,2}(?:电话|手机))|"
    r"揍|踢|掐|推倒|欺负(?!小猫|小狗|小动物|宠物|玩具|小怪兽|怪兽))"
)
_HARM_NEGATION = r"(?<!不)(?<!没)(?<!别)(?<!没有)(?<!不曾)(?<!从未)"
_FIRST_PERSON_HARM_RE = re.compile(
    rf"(?:{_HARM_NEGATION}{_HARM_VERBS}(?:了|过)?(?:我|我们)"
    rf"(?!电话|的电话|手机|的手机|自己)(?:了|过|一下|一顿)?"
    rf"|把(?:我|我们)(?:给)?{_HARM_VERBS}(?:了)?(?:一下|一顿)?)"
)
_PASSIVE_HARM_ADVERBS = (
    r"(?:也|又|都|还|已经|刚|刚刚|刚才|正|正在|曾|曾经|今天|昨天|突然|就|才)"
)
_HARM_WITHIN_CLAUSE = r"[^，。！？、,.!?；:;]"
_CLAUSE_BREAK = r"[，。！？、,.!?；:;]"
_SELF_PASSIVE_HARM_RE = re.compile(
    rf"(?:我|我们){_PASSIVE_HARM_ADVERBS}{{0,4}}"
    rf"被{_HARM_WITHIN_CLAUSE}{{0,8}}?{_PASSIVE_HARM_VERBS}(?:了|过)?"
)
_SUBJECTLESS_PASSIVE_HARM_RE = re.compile(
    rf"^{_CLAUSE_INTERJECTIONS}{_CLAUSE_BREAK}?"
    rf"(?:今天|昨天|刚才|刚刚|早上|晚上|中午)?{_CLAUSE_BREAK}?"
    rf"被{_HARM_WITHIN_CLAUSE}{{0,8}}?{_PASSIVE_HARM_VERBS}(?:了|过)?"
)
_PASSIVE_HARM_RE = re.compile(
    rf"被{_HARM_WITHIN_CLAUSE}{{0,8}}?{_PASSIVE_HARM_VERBS}(?:了|过)?"
)
_INANIMATE_PASSIVE_SUBJECTS: Final[tuple[str, ...]] = (
    "桌子",
    "椅子",
    "杯子",
    "积木",
    "玩具",
    "门",
    "窗户",
    "花瓶",
    "书",
    "书包",
    "球",
    "娃娃",
    "机器人",
    "小车",
    "房子",
    "树",
    "花",
    "灯",
    "电视",
)
_REPORTED_HARM_RE = re.compile(
    rf"(?:听说|看到|看见|新闻|报道){_HARM_WITHIN_CLAUSE}{{0,16}}"
    rf"(?:有人|别人|小朋友|同学|他|她){_HARM_WITHIN_CLAUSE}{{0,16}}"
    rf"(?:欺负(?!小猫|小狗|小动物|宠物|玩具|小怪兽|怪兽)|挨打|拐走|骗走|伤害|"
    rf"{_PASSIVE_HARM_VERBS}|"
    r"被(?:打|揍|踢|掐|推倒|骗|欺负))"
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
# C9 is a question about keeping the child out of a bad actor's hands, or the
# child asking what to do when one targets them: 不被/别被/防止 坏人 骗, 骗走,
# 带走, 拐走, or 坏人骗我 / 坏人想抓我 with 怎么办. The bad actor and the child
# share one clause, and 带我去 needs a bad actor, so everyday 带我去医院 and
# plain knowledge about strangers stay quiet.
_CHILD_AS_TARGET = r"(?:我|你|我们)"
_BAD_ACTOR = r"(?:坏人|陌生人|有人|别人|人家|骗子)"
_IN_CLAUSE = r"[^，。！？、,.!?；:;]"
_NEAR_LEAD_IN = rf"(?:{_BAD_ACTOR}|差点|差一点|万一|要是|如果|马上|就要|说)"
# 走/去/到/离 only when the place is the child, not 去玩 or 去医院.
_MOVEMENT_TARGET = r"(?:走|去|到|离)(?!玩|睡觉|吃饭|上学|医院|超市|公园|幼儿园|厕所|房间|楼下)"
_TAKEN_AWAY = (
    rf"(?:骗|拐|抓|拉|抱|领)(?:突然|当场|直接|起来)?{_CHILD_AS_TARGET}?{_MOVEMENT_TARGET}|带走"
)
_PROTECTED_FROM_BAD_ACTOR_RE = re.compile(
    rf"(?:不|别|勿|防止|预防|避免|如何|怎么|怎样)(?:才能|要|会|能|可以|再|不)?"
    rf"(?:{_NEAR_LEAD_IN}{{0,3}})?被"
    rf"({_BAD_ACTOR})(?:(?:{_IN_CLAUSE}){{0,4}})"
    rf"(?:{_TAKEN_AWAY}|(?:骗|害)(?:了{_CHILD_AS_TARGET}|{_CHILD_AS_TARGET})?)"
)
_SNATCH_INTENT = (
    rf"(?:想|要|准备|打算|企图){_CHILD_AS_TARGET}?{_MOVEMENT_TARGET}"
    rf"|(?:想|要|准备|打算|企图)(?:抓|拉|抱|拐|骗){_CHILD_AS_TARGET}"
)
_BAD_ACTOR_SNATCH_RE = re.compile(
    rf"{_BAD_ACTOR}{_IN_CLAUSE}{{0,8}}(?:{_TAKEN_AWAY}|骗{_CHILD_AS_TARGET}|{_SNATCH_INTENT})"
)
_BAD_ACTOR_TAKES_CHILD_RE = re.compile(
    rf"{_BAD_ACTOR}{_IN_CLAUSE}{{0,6}}带{_CHILD_AS_TARGET}{_MOVEMENT_TARGET}"
)
_BAD_ACTOR_DECIEVES_CHILD_RE = re.compile(
    rf"{_BAD_ACTOR}{_IN_CLAUSE}{{0,6}}(?:骗|哄|诱惑)(?!人|大家){_CHILD_AS_TARGET}"
)
_QUESTION_FORM_RE = re.compile(r"怎么|如何|怎样|咋")
_WHAT_TO_DO_RE = re.compile(r"怎么办|咋办|该怎么|应该怎么|要怎么|如何处理")
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


def _passive_harm_targets_another_person(text: str) -> bool:
    """A passive harm clause whose subject is not the child or an object."""

    for match in _PASSIVE_HARM_RE.finditer(text):
        prefix = text[: match.start()].rstrip("，。！？、,.!?；:; ")
        if not any(prefix.endswith(subject) for subject in _INANIMATE_PASSIVE_SUBJECTS):
            return True
    return False


def _protected_from_bad_actor(text: str) -> bool:
    """A question about keeping the child from being taken or tricked by someone."""

    asks = _QUESTION_FORM_RE.search(text) or _WHAT_TO_DO_RE.search(text)
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if _PROTECTED_FROM_BAD_ACTOR_RE.search(clause):
            return True
        if not asks:
            continue
        if (
            _BAD_ACTOR_SNATCH_RE.search(clause)
            or _BAD_ACTOR_TAKES_CHILD_RE.search(clause)
            or _BAD_ACTOR_DECIEVES_CHILD_RE.search(clause)
        ):
            return True
    return False


def companion_safety_decision(query: str) -> SafetyDecision:
    """Classify one current-turn query without granting facts or permissions."""
    stripped_of_quotes = _QUOTED_TEXT_RE.sub("", query)
    crisis_query = stripped_of_quotes
    if crisis_query == query or not _REPORTED_OR_ANALYTICAL_RE.search(crisis_query):
        crisis_query = query
    # The child's own risk outranks anything they relay about someone else.
    if _first_person_self_risk(crisis_query):
        return "crisis_self"
    # Reported self-harm must survive quote stripping; otherwise fiction /
    # analysis that only quotes risk language would still trip the relay path.
    if _UNQUOTED_REPORTED_SELF_HARM_RE.search(stripped_of_quotes):
        return "support_relay"
    normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", crisis_query).lower()
    if (
        normalized in _EXPLICIT_SELF_HARM_EXACT
        or _EXPLICIT_SELF_HARM_INTENT_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ACTION_RE.search(normalized)
        or _SELF_HARM_METHOD_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ENGLISH_RE.search(crisis_query.lower())
        or any(pattern.search(normalized) for pattern in _CRISIS_SELF_WORD_CLASS_RES)
    ):
        return "crisis_self"
    full_normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    # Harm keeps clause punctuation so 打/被 in one clause cannot pair with an
    # object or verb in the next one.
    harm_surface = re.sub(r"\s+", "", query).lower()
    if (
        _FIRST_PERSON_HARM_RE.search(harm_surface)
        or _SELF_PASSIVE_HARM_RE.search(harm_surface)
        or _SUBJECTLESS_PASSIVE_HARM_RE.search(harm_surface)
    ):
        return "support_victim"
    if _VICTIM_SUPPORT_RE.search(full_normalized):
        return "support_victim"
    if _REPORTED_PEER_HARM_RE.search(full_normalized):
        return "support_relay"
    if _REPORTED_HARM_RE.search(full_normalized) or _passive_harm_targets_another_person(
        harm_surface
    ):
        return "support_relay"
    if _SAFETY_PREVENTION_RE.search(full_normalized):
        return "support_relay"
    if _protected_from_bad_actor(harm_surface):
        return "support_relay"
    if _OTHER_JUMP_RE.search(full_normalized) or _CONFINEMENT_BEAT_RE.search(
        full_normalized
    ):
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
