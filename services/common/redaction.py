"""Single text-redaction policy used before model context or persistence."""

from __future__ import annotations

import re

_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])"), "[身份证]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[银行卡]"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.\w+"), "[邮箱]"),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:sk|pk|api)[_-][A-Za-z0-9_-]{12,}(?![A-Za-z0-9])"
        ),
        "[密钥]",
    ),
    (
        re.compile(
            r"(?:"
            r"(?:[\u4e00-\u9fff]{2,10}(?:省|自治区|特别行政区))?"
            r"(?:[\u4e00-\u9fff]{2,10}市)?"
            r"(?:[\u4e00-\u9fff]{1,10}(?:区|县|旗))?"
            r")"
            r"[\u4e00-\u9fffA-Za-z0-9]{1,30}"
            r"(?:路|街|大道|街道|巷|弄|胡同|镇|乡|村)"
            r"[\u4e00-\u9fffA-Za-z0-9-]{0,30}?"
            r"\d{1,6}号"
            r"(?:[\u4e00-\u9fffA-Za-z0-9-]{0,20}(?:栋|幢|座|单元|楼|室))?"
        ),
        "[地址]",
    ),
)


def redact_pii(text: str) -> str:
    """Replace common Chinese PII and credential shapes with stable markers."""
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted

