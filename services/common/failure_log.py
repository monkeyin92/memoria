"""The only fields a failure log line may carry: the exception type and a code.

Provider, HTTP and driver exceptions can quote the reply, the request, SQL
values or a vendor body in their text. That text is logged only when the
whole message is a snake_case token, which is how a denial names its reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_SQLSTATE: Final = re.compile(r"[A-Z0-9]{5}")
_CODE_TOKEN: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_REASON_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_CAUSE_DEPTH: Final = 10


@dataclass(frozen=True, slots=True)
class FailureLog:
    error_type: str
    error_code: str

    def __str__(self) -> str:
        return f"error_type={self.error_type} error_code={self.error_code}"


def failure_log(exc: BaseException) -> FailureLog:
    return FailureLog(error_type=type(exc).__name__[:64], error_code=_chain_code(exc))


def _chain_code(exc: BaseException) -> str:
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH + 1):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        code = _own_code(current)
        if code is not None:
            return code
        current = current.__cause__
    return "-"


def _own_code(exc: BaseException) -> str | None:
    status = _attr(exc, "status_code")
    if _is_int(status):
        return str(status)
    status = _attr(_attr(exc, "response"), "status_code")
    if _is_int(status):
        return str(status)
    sqlstate = _attr(exc, "sqlstate")
    if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate):
        return sqlstate
    code = _attr(exc, "error_code")
    if _is_int(code):
        return str(code)
    if isinstance(code, str) and _CODE_TOKEN.fullmatch(code):
        return code
    try:
        message = str(exc)
    except Exception:
        return None
    if _REASON_CODE.fullmatch(message):
        return message
    return None


def _attr(value: object, name: str) -> object:
    # Third-party exceptions expose some of these as properties that raise.
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["FailureLog", "failure_log"]
