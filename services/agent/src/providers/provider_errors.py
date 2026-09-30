"""Provider failure vocabulary shared by the ASR, LLM and TTS adapters.

These replace the ``livekit.agents`` exception and connect-option types the
adapters were written against, with the same retry semantics:

- ``APIError.retryable`` decides whether a stream may start a fresh attempt;
  ``APIConnectionError`` and ``APITimeoutError`` default to retryable.
- ``APIStatusError`` is never retryable for a 4xx status other than 408, 429
  and 499, whatever the caller asked for.
- ``APIConnectOptions`` bounds those attempts: ``max_retry`` extra attempts,
  0.1 s before the first retry and ``retry_interval`` before each later one.
"""

from __future__ import annotations

from dataclasses import dataclass


class APIError(Exception):
    """A provider request failed."""

    def __init__(self, message: str, *, body: object | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.body = body
        self.retryable = retryable

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self.message!r}, body={self.body!r}, "
            f"retryable={self.retryable!r})"
        )


class APIStatusError(APIError):
    """The provider answered with a 4xx or 5xx status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = -1,
        request_id: str | None = None,
        body: object | None = None,
        retryable: bool | None = None,
    ) -> None:
        if retryable is None:
            retryable = True
        # A client error keeps failing on retry, except the transient ones.
        if 400 <= status_code < 500 and status_code not in (408, 429, 499):
            retryable = False
        super().__init__(message, body=body, retryable=retryable)
        self.status_code = status_code
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [
            f"message={self.message!r}",
            f"status_code={self.status_code}",
            f"retryable={self.retryable}",
        ]
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.body:
            parts.append(f"body={self.body}")
        return ", ".join(parts)


class APIConnectionError(APIError):
    """The provider connection failed or broke."""

    def __init__(self, message: str = "Connection error.", *, retryable: bool = True) -> None:
        super().__init__(message, body=None, retryable=retryable)

    def __str__(self) -> str:
        # Wrappers raise ``... from exc`` with a placeholder message of their
        # own, so name the root of the ``__cause__`` chain (at most 10 deep and
        # stopping at a cycle) to keep the actual failure in the log line.
        root: BaseException | None = None
        seen = {id(self)}
        cause = self.__cause__
        for _ in range(10):
            if cause is None or id(cause) in seen:
                break
            seen.add(id(cause))
            root = cause
            cause = cause.__cause__
        if root is None:
            return self.message
        if isinstance(root, APIConnectionError):
            root_message = root.message
        else:
            try:
                root_message = str(root)
            except Exception:
                root_message = ""
        detail = type(root).__name__
        if root_message:
            detail += f": {root_message}"
        return f"{self.message} (caused by {detail})"


class APITimeoutError(APIConnectionError):
    """The provider request timed out."""

    def __init__(self, message: str = "Request timed out.", *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)


@dataclass(frozen=True, slots=True)
class APIConnectOptions:
    max_retry: int = 3
    retry_interval: float = 2.0
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.max_retry < 0:
            raise ValueError("max_retry must be greater than or equal to 0")
        if self.retry_interval < 0:
            raise ValueError("retry_interval must be greater than or equal to 0")
        if self.timeout < 0:
            raise ValueError("timeout must be greater than or equal to 0")

    def interval_for_retry(self, num_retries: int) -> float:
        """The first retry waits 0.1 s, every later one ``retry_interval``."""

        return 0.1 if num_retries == 0 else self.retry_interval


DEFAULT_API_CONNECT_OPTIONS = APIConnectOptions()

__all__ = [
    "DEFAULT_API_CONNECT_OPTIONS",
    "APIConnectOptions",
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
]
