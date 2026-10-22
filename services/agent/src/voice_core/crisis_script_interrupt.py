"""Report that an in-progress crisis-self or victim script was cancelled.

The POST body is only the crisis event id. The parent sentence is chosen
later, when the guardian reads the notification.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)
from services.guardian.crisis import minor_crisis_event_id

logger = logging.getLogger(__name__)

_INTERRUPT_PATH = "/v1/guardian/crisis-script-interrupted"
# The crisis row can land after the playback-loss mark. Retry only the miss.
_NOT_FOUND_ATTEMPTS = 3
_NOT_FOUND_BACKOFF_S = 0.2


def crisis_script_interrupt_endpoint(response_plan_url: str) -> str:
    parsed = urlsplit(response_plan_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("response plan URL has no origin")
    return urlunsplit((parsed.scheme, parsed.netloc, _INTERRUPT_PATH, "", ""))


def _covers(script: FixedSafetyScript, text: str) -> bool:
    return script.display.startswith(text) or script.spoken.startswith(text)


def announced_interruptible_script(text: str) -> bool:
    """True when ``text`` is uniquely the crisis-self or victim script, or a prefix of one."""

    stripped = text.strip()
    if not stripped:
        return False
    covered = [
        script
        for script in (CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT)
        if _covers(script, stripped)
    ]
    if len(covered) != 1:
        return False
    return covered[0] is not SUPPORT_RELAY_SCRIPT


def active_subject_id(context: Any) -> str:
    profile = None
    runtime = getattr(context, "runtime", None)
    policy = getattr(runtime, "mode_policy", None)
    if policy is not None:
        profile = getattr(policy, "runtime_profile", None)
    nested = getattr(profile, "profile", None)
    active = getattr(nested, "active_subject_id", "") if nested is not None else ""
    if active:
        return str(active)
    identity = getattr(context, "identity", None)
    return str(getattr(identity, "subject_id", "") or "")


def crisis_self_interrupt_event_id(
    *,
    announced_text: str,
    crisis_reply_heard: bool,
    minor_user_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
) -> str | None:
    if crisis_reply_heard or not minor_user_id:
        return None
    if not announced_interruptible_script(announced_text):
        return None
    return minor_crisis_event_id(
        minor_user_id=minor_user_id,
        session_id=session_id,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
    )


def unfinished_safety_announcement(context: Any) -> str | None:
    """Crisis or victim script still on the speaker, including its opening cue.

    The cue is audible before any script characters are appended. A later
    ordinary reply has its own text, so a previous script does not stay
    unfinished forever.
    """

    if getattr(context, "crisis_reply_heard", False):
        return None
    output = getattr(context, "output", None)
    announced = str(getattr(output, "assistant_text", "") or "").strip()
    if announced:
        return announced if announced_interruptible_script(announced) else None
    runtime = getattr(context, "runtime", None)
    echo = str(getattr(runtime, "fixed_safety_echo_text", "") or "").strip()
    if not announced_interruptible_script(echo):
        return None
    floor = getattr(runtime, "floor", None)
    speaking = bool(getattr(floor, "assistant_speaking", False))
    marker_playing = (
        getattr(context, "aigc_opening_marker_fence", None) is not None
        or getattr(context, "aigc_closing_marker_fence", None) is not None
        or getattr(context, "aigc_closing_marker_state", "") == "playing"
    )
    if speaking or marker_playing:
        return echo
    return None


def publish_crisis_self_interrupt(
    publisher: Callable[[str], None] | None,
    *,
    announced_text: str,
    crisis_reply_heard: bool,
    minor_user_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
) -> None:
    if publisher is None:
        return
    event_id = crisis_self_interrupt_event_id(
        announced_text=announced_text,
        crisis_reply_heard=crisis_reply_heard,
        minor_user_id=minor_user_id,
        session_id=session_id,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
    )
    if event_id is None:
        return
    try:
        publisher(event_id)
    except Exception:
        logger.exception(
            "crisis script interrupt mark admission failed session=%s",
            session_id,
        )


class CrisisScriptInterruptReporter:
    """Fire-and-forget POST. A slow control API must not stall playback stop."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        not_found_attempts: int = _NOT_FOUND_ATTEMPTS,
        not_found_backoff_s: float = _NOT_FOUND_BACKOFF_S,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("crisis script interrupt endpoint must be an HTTP(S) URL")
        if len(token) < 32:
            raise ValueError("crisis script interrupt token must contain at least 32 characters")
        if not_found_attempts < 1:
            raise ValueError("crisis script interrupt 404 attempts must be at least 1")
        if not_found_backoff_s < 0:
            raise ValueError("crisis script interrupt 404 backoff must be non-negative")
        self._endpoint = endpoint
        self._token = token
        self._client = client
        self._owns_client = client is None
        self._not_found_attempts = not_found_attempts
        self._not_found_backoff_s = not_found_backoff_s
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, crisis_event_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info(
                "crisis script interrupt mark skipped without a running loop crisis_event_id=%s",
                crisis_event_id,
            )
            return
        task = loop.create_task(self._post(crisis_event_id))
        self._tasks.add(task)

        def _finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.info(
                    "crisis script interrupt mark failed crisis_event_id=%s error=%s",
                    crisis_event_id,
                    type(error).__name__,
                )

        task.add_done_callback(_finished)

    async def _post(self, crisis_event_id: str) -> None:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=2.0)
            self._client = client
        for attempt in range(self._not_found_attempts):
            try:
                response = await client.post(
                    self._endpoint,
                    headers={"X-Memoria-Internal-Token": self._token},
                    json={"crisis_event_id": crisis_event_id},
                )
            except (httpx.HTTPError, OSError):
                logger.info(
                    "crisis script interrupt mark failed crisis_event_id=%s",
                    crisis_event_id,
                )
                return
            if response.status_code == 404 and attempt + 1 < self._not_found_attempts:
                await asyncio.sleep(self._not_found_backoff_s)
                continue
            if response.status_code == 404:
                logger.info(
                    "crisis script interrupt mark missed crisis_event_id=%s",
                    crisis_event_id,
                )
                return
            if response.status_code >= 400:
                logger.info(
                    "crisis script interrupt mark rejected crisis_event_id=%s status=%s",
                    crisis_event_id,
                    response.status_code,
                )
            return

    async def aclose(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
