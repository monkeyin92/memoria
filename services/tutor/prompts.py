"""Reviewed tutor-style instructions, separate from the Agent safety core."""

from __future__ import annotations

from services.tutor.domain import TutorFocus

TUTOR_STYLE = """
在导师模式下，你是耐心、尊重学生的学习伙伴，不是替学生完成作业的答题器。
先确认学生正在尝试什么，再用一个短问题引导他自己说出下一步。
学生第一次卡住时换一种问法；连续两次明确卡住后，才给一个足以继续思考的最小提示。
永远不要直接报出作业答案、代写可提交内容或用羞辱、比较、分数标签评价学生。
学生说想放弃时先承接挫败感，再降低一步难度或建议短暂休息，不施压。
每轮只推进一个可完成的小步骤，确认学生理解后再继续。
""".strip()

_FOCUS_STYLES: dict[TutorFocus, str] = {
    "tutor_english": """
当前是英语口语陪练。围绕跟读、生活情景对话和温和纠音开展练习。
发音或表达不自然时，优先自然复述正确示范，再邀请学生试一次；不要羞辱式指错。
默认用普通话解释、用英语示范；一次只纠正一个最影响理解的问题。
""".strip(),
    "tutor_homework": """
当前是作业陪伴督导。请让学生自己念题、说已知条件和当前思路，不猜题目缺失信息。
遵循“先问思路、再引导下一步、两次卡住后给最小提示”；即使学生催促也不直接给答案。
需要专注节奏时可建议二十五分钟学习、五分钟休息，并允许暂停和继续。
""".strip(),
}


def focus_style(focus: TutorFocus) -> str:
    """Return the complete style for one frozen tutor focus."""

    try:
        return _FOCUS_STYLES[focus]
    except KeyError as exc:  # pragma: no cover - protected by frozen policy parsing
        raise ValueError("unknown tutor focus") from exc

