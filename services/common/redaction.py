"""Single text-redaction policy used before model context, persistence, or a third-party send."""

from __future__ import annotations

import re

# A boundary-anchored admin prefix may not contain these clause characters, so
# 「救命南京市中山路1号」 keeps 「救命」. Unanchored addresses use a fixed
# two-character road stem instead, because leftmost matching stretches any
# variable-width run into the clause before the address and erases crisis words.
_NON_TOPONYM = "我你他她它咱您不没别想要会能该敢了着过吗呢吧啊呀哦嘛的得住在到去是说死杀割跳吞救打摸被"
_HAN = r"[\u4e00-\u9fff]"
_PLACE = rf"(?:(?![{_NON_TOPONYM}]){_HAN})"
_STREET = r"[\u4e00-\u9fffA-Za-z0-9]"
_TAIL = (
    r"(?:路|街|大道|街道|巷|弄|胡同|镇|乡|村)"
    r"[\u4e00-\u9fffA-Za-z0-9-]{0,6}?\d{1,6}号"
    r"(?:[A-Za-z0-9一二三四五六七八九十东西南北号-]{0,6}(?:栋|幢|座|单元|楼|室))*"
)
_INTRODUCED = r"(?:(?<=住在)|(?<=家在)|(?<=家住)|(?<=住址)|(?<=地址)|(?<=址是)|(?<=寄到)|(?<=送到)|(?<=搬到))"
_PUNCT = r"[\s，。！？、；：,.!?;:（）()“”\"']"
_BOUNDARY = rf"(?:^|(?<={_PUNCT})|(?<=^住)|(?<={_PUNCT}住))"


def _admin(c: str, *, required: bool) -> str:
    province, city, district = rf"{c}{{2,5}}(?:省|自治区|特别行政区)", rf"{c}{{2,4}}市", rf"{c}{{1,5}}(?:区|县|旗)"
    if not required:
        return rf"(?:{province})?(?:{city})?(?:{district})?"
    return rf"(?:{province}(?:{city})?(?:{district})?|{city}(?:{district})?|{district})"


_ADDRESS = re.compile(
    rf"{_INTRODUCED}{_admin(_HAN, required=False)}{_STREET}{{1,10}}{_TAIL}"
    rf"|{_BOUNDARY}{_admin(_PLACE, required=True)}{_STREET}{{1,10}}{_TAIL}"
    rf"|{_STREET}{{2}}{_TAIL}"
)

_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])"), "[身份证]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[银行卡]"),
    (
        re.compile(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}"
        ),
        "[邮箱]",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:sk|pk|api)[_-][A-Za-z0-9_-]{12,}(?![A-Za-z0-9])"
        ),
        "[密钥]",
    ),
    (_ADDRESS, "[地址]"),
)


def redact_pii(text: str) -> str:
    """Replace common Chinese PII and credential shapes with stable markers.

    Idempotent. Never changes the crisis or realtime-lookup decision of an
    utterance (pinned by the glue corpus in services/common/tests/test_redaction.py).
    """
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
