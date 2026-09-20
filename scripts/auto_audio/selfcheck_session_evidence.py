#!/usr/bin/env python3
"""In-memory self-check for the session/audio evidence wiring.

Covers the three places where a machine-side run can be silently misread:

1. `Recorder.stop` is idempotent and the first `ended_local` survives cleanup, with one
   `recording_stop` event per recorder (the payload shape is unchanged, so the existing
   fixtures and reports keep parsing it).
2. `record_windows` binds the content `speaking` span to the completed generation's
   session/turn/generation/tool fence, and only when exactly one span contains that
   generation's `first_frame_sent` .. `playback_ended` delivery window.  An
   acknowledgement, a never-closed span, a foreign conversation or an ambiguous pair is
   recorded as diagnostic and is never credited as the answer.
3. A `wake_detected` line only opens a conversation when it can belong to the prompt that
   was just played; a stamped-before-the-prompt leftover does not bind.
4. The delivery stamps are used as they arrive: a missing, non-string, malformed or
   inverted `first_frame_sent`..`playback_ended` window is refused with a reason, and a
   span whose own edges cannot be parsed is dropped as a candidate rather than raising.

This file writes nothing: no fixture directory, no `results.json`, no WAV, no serial port,
no microphone and no playback.  Every case is built from log lines held in memory, and the
events an `EventLog` would append are collected by a stub instead.

    python3 selfcheck_session_evidence.py
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_audio_session as driver  # noqa: E402

ORIGIN = datetime.fromisoformat("2026-09-15T12:00:00.000+08:00")
OUR_SESSION = "684348e9-8f4e-4c6a-b5b9-58e0306d3875"
OTHER_SESSION = "11111111-2222-3333-4444-555555555555"
STREAM_EPOCH = 1955
SESSION_EPOCH = 1
TOOL_EPOCH = 0
TURN_ID = 3
# A path that does not exist: the cleanup fixture uses it so its report write fails inside
# the function's own OSError guard instead of creating a file anywhere.
NO_SUCH_RUN_DIR = Path("/nonexistent-codex-selfcheck/run")
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  {status} {name} {detail}")
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def stamp(seconds: float) -> str:
    return (ORIGIN + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def utc(seconds: float) -> str:
    naive = ORIGIN.replace(tzinfo=None) + timedelta(seconds=seconds) - timedelta(hours=8)
    return naive.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def serial_line(seconds: float, source: str, destination: str) -> tuple[float, str, str]:
    return (seconds, "serial",
            f"[{stamp(seconds)}] I (1000) StateMachine: State: {source} -> {destination}")


def wake_line(seconds: float | None) -> tuple[float, str, str]:
    """A board wake detection; `None` reproduces a line without an ISO stamp in brackets."""

    if seconds is None:
        return (0.0, "serial", "[12345 ms] I (1000) Custom wake word detected")
    return (seconds, "serial", f"[{stamp(seconds)}] I (1000) Custom wake word detected")


def bridge_line(seconds: float, body: str) -> tuple[float, str, str]:
    return (seconds, "bridge", f"{utc(seconds)} INFO:services.agent.src: {body}")


def commit_line(seconds: float, generation: int, *, session: str = OUR_SESSION,
                turn: int = TURN_ID) -> tuple[float, str, str]:
    return bridge_line(
        seconds,
        f"services.agent.src.voice_core.media_session_commit:media turn committed "
        f"session={session} session_epoch={SESSION_EPOCH} stream_epoch={STREAM_EPOCH} "
        f"turn_id={turn} generation_id={generation} tool_epoch={TOOL_EPOCH}",
    )


def delivery_line(seconds: float, event: str, *, generation: int, session: str = OUR_SESSION,
                  turn: int = TURN_ID, reason: str = "") -> tuple[float, str, str]:
    terminal = f" terminal_reason={reason}" if reason else ""
    return bridge_line(
        seconds,
        f"services.agent.src.voice_core.media_session_output_dispatch:media reply delivery "
        f"session={session} delivery_id={session}/epoch-{SESSION_EPOCH}/turn-{turn}/"
        f"generation-{generation}/tool-{TOOL_EPOCH} event={event}{terminal}",
    )


def phase_line(seconds: float, source: str, destination: str, cause: str, *,
               generation: int, session: str = OUR_SESSION,
               turn: int = TURN_ID) -> tuple[float, str, str]:
    return bridge_line(
        seconds,
        f"services.agent.src.duplex_runtime:interaction_phase from={source} "
        f"to={destination} cause={cause} session_id={session} turn_id={turn} "
        f"generation_id={generation}",
    )


class CollectingEvents:
    """Stands in for `EventLog`: collect payloads in memory, never open a file."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.closed = False

    def add(self, kind: str, **fields: object) -> None:
        self.payloads.append({"kind": kind, "t_local": stamp(0.0), **fields})

    def of_kind(self, kind: str) -> list[dict[str, object]]:
        return [payload for payload in self.payloads if payload["kind"] == kind]

    def close(self) -> None:
        self.closed = True


def bound_session(lines: list[tuple[float, str, str]]) -> driver.LogSession:
    """A capture bound to this board's session, fed these lines in chronological order."""

    session = driver.LogSession({})
    session.bind(OUR_SESSION, stream_epoch=STREAM_EPOCH, source="fixture")
    for number, (_, source, raw) in enumerate(sorted(lines, key=lambda item: item[0])):
        session.feed(source, raw, arrived=float(number))
    return session


def tracker_for(session: driver.LogSession) -> driver.TurnTracker:
    tracker = driver.TurnTracker()
    for line in session.lines:
        tracker.observe(line)
    for line in session.lookups():
        tracker.observe(line)
    return tracker


def ack_and_content_lines() -> list[tuple[float, str, str]]:
    """One tool turn: the acknowledgement (gen 4) then the content (gen 5)."""

    return [
        serial_line(6.000, "listening", "speaking"),
        commit_line(6.100, 4),
        delivery_line(6.200, "first_frame_sent terminal=first_frame_sent=True", generation=4),
        serial_line(8.500, "speaking", "listening"),
        delivery_line(8.510, "playback_ended", generation=4, reason="playback_completed"),
        phase_line(8.520, "speaking", "tool_waiting", "media_playback_ack", generation=4),
        phase_line(12.000, "thinking_silent", "speaking", "assistant_speaking", generation=5),
        delivery_line(12.020, "first_frame_sent terminal=first_frame_sent=True", generation=5),
        serial_line(12.030, "listening", "speaking"),
        serial_line(20.000, "speaking", "listening"),
        delivery_line(20.010, "playback_ended", generation=5, reason="playback_completed"),
        phase_line(20.020, "speaking", "listening", "media_playback_ack", generation=5),
    ]


def content_case_checks() -> None:
    print("content window binding")
    session = bound_session(ack_and_content_lines())
    completion = tracker_for(session).completion()
    check("the turn completes on the content generation",
          bool(completion) and completion["generation_id"] == 5
          and completion["acknowledgement_marker"] is False, completion)
    if not completion:
        return
    check("the completed generation carries its own delivery window",
          completion["first_frame_t"] == stamp(12.020)
          and completion["playback_ended_t"] == stamp(20.010), completion)

    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    speaking = events.of_kind("device_speaking")
    content = [item for item in speaking if item["content_matched"]]
    check("exactly one span is credited as the content",
          len(content) == 1 and len(speaking) == 2,
          [item["content_reason"] for item in speaking])
    check("the credited span is the content's own span",
          bool(content) and content[0]["start_local"] == stamp(12.030)
          and content[0]["end_local"] == stamp(20.000),
          content[0] if content else None)
    check("the credited span carries the completed generation's whole fence",
          bool(content) and all(content[0].get(key) == value for key, value in (
              ("session_id", OUR_SESSION), ("session_epoch", SESSION_EPOCH),
              ("stream_epoch", STREAM_EPOCH), ("turn_id", TURN_ID),
              ("generation_id", 5), ("tool_epoch", TOOL_EPOCH),
          )), content[0] if content else None)
    check("the binding records its basis and the clock allowance",
          bool(content)
          and content[0]["content_basis"]
          == "serial_speaking_span_contains_first_frame_sent_to_playback_ended"
          and content[0]["clock_tolerance_ms"] == driver.DELIVERY_WINDOW_TOLERANCE_MS
          and content[0]["clock_tolerance_ms"] <= 150.0,
          content[0] if content else None)
    ack = [item for item in speaking if not item["content_matched"]]
    check("the acknowledgement span stays diagnostic",
          len(ack) == 1 and ack[0]["start_local"] == stamp(6.000)
          and ack[0]["content_reason"] == "other_speaking_span"
          and "generation_id" not in ack[0], ack[0] if ack else None)
    check("the run keeps one authoritative matching record",
          summary["matched"] is True
          and summary["reason"] == "one_window_contains_delivery"
          and summary["containing_windows"] == 1
          and summary["closed_windows"] == 2 and summary["unclosed_windows"] == 0,
          {key: summary[key] for key in ("matched", "reason", "containing_windows")})
    recorded = events.of_kind("turn_content_window")
    check("the record names the delivered generation and its window",
          len(recorded) == 1 and recorded[0]["generation_id"] == 5
          and recorded[0]["fence"]["generation_id"] == 5
          and recorded[0]["window_start_local"] == stamp(12.030)
          and recorded[0]["delivery_first_frame_local"] == stamp(12.020),
          recorded[0] if recorded else None)


def acknowledgement_only_checks() -> None:
    print("acknowledgement is never the content")
    # Only the acknowledgement's span exists, while the content generation did deliver.
    lines = [line for line in ack_and_content_lines()
             if line[1] != "serial" or line[0] <= 8.5]
    session = bound_session(lines)
    completion = tracker_for(session).completion()
    check("the content generation still completes the turn",
          bool(completion) and completion["generation_id"] == 5, completion)
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    check("an acknowledgement span is not credited as the answer",
          summary["matched"] is False
          and summary["reason"] == "no_window_contains_delivery"
          and not any(item["content_matched"] for item in events.of_kind("device_speaking")),
          summary["reason"])

    # The harder shape: the runtime ends the acknowledgement's own playback into listening,
    # so the acknowledgement generation is the one that "completes" the turn.
    ack_completes = [
        serial_line(6.000, "listening", "speaking"),
        commit_line(6.100, 4),
        delivery_line(6.200, "first_frame_sent terminal=first_frame_sent=True", generation=4),
        serial_line(8.500, "speaking", "listening"),
        delivery_line(8.510, "playback_ended", generation=4, reason="playback_completed"),
        phase_line(8.520, "speaking", "tool_waiting", "media_playback_ack", generation=4),
        phase_line(8.530, "speaking", "listening", "media_playback_ack", generation=4),
    ]
    session = bound_session(ack_completes)
    tracker = tracker_for(session)
    completion = tracker.completion()
    check("the acknowledgement generation is flagged as one",
          bool(completion) and completion["generation_id"] == 4
          and completion["acknowledgement_marker"] is True
          and tracker.tool_waiting_acks == {4}, completion)
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    check("a span covering only the acknowledgement is refused despite the fence",
          summary["matched"] is False and summary["reason"] == "generation_is_acknowledgement"
          and not any(item["content_matched"] for item in events.of_kind("device_speaking")),
          summary["reason"])


def unclosed_window_checks() -> None:
    print("never-closed window")
    lines = ack_and_content_lines()
    session = bound_session(lines)
    completion = tracker_for(session).completion()
    # The turn is already complete; the device then starts speaking again and the log never
    # shows it leaving.  That stray span must not become the answer.
    session.feed("serial", serial_line(24.000, "listening", "speaking")[2], arrived=99.0)
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    unclosed = events.of_kind("device_speaking_unclosed")
    check("the content span is still the only credited one",
          summary["matched"] is True and summary["fence"]["generation_id"] == 5
          and sum(1 for item in events.of_kind("device_speaking") if item["content_matched"]) == 1,
          summary["reason"])
    check("a span that never closed is reported, not stretched",
          summary["unclosed_windows"] == 1 and len(unclosed) == 1
          and unclosed[0]["start_local"] == stamp(24.000)
          and unclosed[0]["content_matched"] is False
          and unclosed[0]["content_reason"] == "window_never_closed"
          and "end_local" not in unclosed[0], unclosed[0] if unclosed else None)
    check("an unclosed span does not repeat the fence of the content window",
          bool(unclosed) and "generation_id" not in unclosed[0], unclosed[0] if unclosed else None)

    # With no completed generation at all, the stray span is diagnostic and nothing matches.
    events2 = CollectingEvents()
    summary2 = driver.record_windows(events2, session, 0, 1)
    check("without a completed generation nothing is credited",
          summary2["matched"] is False
          and summary2["reason"] == "no_completed_generation"
          and not any(item["content_matched"] for item in events2.of_kind("device_speaking")),
          summary2["reason"])


def ambiguous_window_checks() -> None:
    print("ambiguous window")
    # Two spans that both cover one delivery window cannot both be the answer, so the
    # matcher refuses instead of picking the longest or the last.  The sequential pairing
    # cannot build that pair out of a well-formed log, so the guard is exercised directly.
    first, ended = stamp(12.020), stamp(20.010)
    two = driver.content_window_match(
        [{"start_local": stamp(11.000), "end_local": stamp(20.050)},
         {"start_local": stamp(12.030), "end_local": stamp(20.000)}],
        first_frame_local=first, playback_ended_local=ended,
    )
    check("two spans covering one delivery window credit neither",
          two["matched"] is False and two["reason"] == "ambiguous_containing_windows"
          and two["containing_windows"] == 2 and two["window"] is None,
          {key: two[key] for key in ("matched", "reason", "containing_windows")})
    one = driver.content_window_match(
        [{"start_local": stamp(12.030), "end_local": stamp(20.000)}],
        first_frame_local=first, playback_ended_local=ended,
    )
    check("the same window alone is credited",
          one["matched"] is True and one["containing_windows"] == 1, one["reason"])
    # A duplicated leave/enter pair in the log collapses back into one span, so the run
    # keeps crediting the content window rather than turning into an ambiguity failure.
    session = bound_session(ack_and_content_lines() + [serial_line(12.030, "listening", "speaking")])
    completion = tracker_for(session).completion()
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    check("a re-entered speaking line does not create a second content span",
          summary["matched"] is True and summary["containing_windows"] == 1
          and sum(1 for item in events.of_kind("device_speaking") if item["content_matched"]) == 1,
          {"reason": summary["reason"], "containing_windows": summary["containing_windows"]})


def delivery_window_checks() -> None:
    print("delivery window fail-closed")
    good = [{"start_local": stamp(12.000), "end_local": stamp(20.000)}]

    # The driver must hand the stamps over as they are: a completion whose delivery lines
    # never landed has no stamps at all, and stringifying that used to crash the parse.
    session = bound_session(ack_and_content_lines())
    completion = {"turn_id": TURN_ID, "generation_id": 5, "session_epoch": SESSION_EPOCH,
                  "tool_epoch": TOOL_EPOCH, "acknowledgement_marker": False}
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    check("a completion without delivery stamps is refused, not stringified",
          summary["matched"] is False and summary["reason"] == "delivery_window_missing"
          and summary["delivery_first_frame_local"] is None
          and not any(item["content_matched"] for item in events.of_kind("device_speaking")),
          summary["reason"])

    # `str(None)` = "None" is exactly what the old call site produced; it has to be a
    # refusal with a reason, never an exception out of the turn.
    literal = driver.content_window_match(
        good, first_frame_local="None", playback_ended_local="None"
    )
    check("the literal 'None' stamp is refused instead of raising",
          literal["matched"] is False and literal["reason"] == "delivery_window_unparsable",
          literal["reason"])

    malformed = driver.content_window_match(
        good, first_frame_local="not-a-timestamp", playback_ended_local=stamp(20.010)
    )
    check("a malformed delivery stamp fails closed",
          malformed["matched"] is False and malformed["reason"] == "delivery_window_unparsable",
          malformed["reason"])

    non_string = driver.content_window_match(good, first_frame_local=12.0,
                                             playback_ended_local=20.0)
    check("a non-string delivery stamp fails closed",
          non_string["matched"] is False and non_string["reason"] == "delivery_window_missing"
          and non_string["delivery_first_frame_local"] is None, non_string["reason"])

    inverted = driver.content_window_match(good, first_frame_local=stamp(20.010),
                                           playback_ended_local=stamp(12.020))
    check("an inverted delivery window fails closed",
          inverted["matched"] is False and inverted["reason"] == "delivery_window_inverted",
          inverted["reason"])

    # A span whose own edges cannot be parsed is dropped as a candidate and counted; the
    # good span beside it is still credited.
    span_bad = driver.content_window_match(
        [{"start_local": "bad", "end_local": stamp(20.000)}, good[0]],
        first_frame_local=stamp(12.020), playback_ended_local=stamp(20.010),
    )
    check("a span with an unusable edge is dropped, not fatal",
          span_bad["matched"] is True and span_bad["unusable_windows"] == 1
          and span_bad["containing_windows"] == 1, span_bad["unusable_windows"])

    span_inverted = driver.content_window_match(
        [{"start_local": stamp(20.000), "end_local": stamp(12.000)}],
        first_frame_local=stamp(12.020), playback_ended_local=stamp(20.010),
    )
    check("a span that ends before it starts is not a candidate",
          span_inverted["matched"] is False
          and span_inverted["reason"] == "no_window_contains_delivery"
          and span_inverted["unusable_windows"] == 1, span_inverted["reason"])


def cross_session_checks() -> None:
    print("cross-session evidence")
    # Another device's conversation arrives while we are bound: it may not complete a turn
    # for us, and its own log lines must not be adopted.
    foreign = [
        serial_line(6.000, "listening", "speaking"),
        commit_line(6.100, 4, session=OTHER_SESSION, turn=9),
        delivery_line(6.200, "first_frame_sent terminal=first_frame_sent=True",
                      generation=9, session=OTHER_SESSION, turn=9),
        serial_line(8.500, "speaking", "listening"),
        delivery_line(8.510, "playback_ended", generation=9, session=OTHER_SESSION, turn=9,
                      reason="playback_completed"),
        phase_line(8.520, "speaking", "listening", "media_playback_ack", generation=9,
                   session=OTHER_SESSION, turn=9),
    ]
    session = bound_session(foreign)
    # Serial lines carry no session, so they are admitted on arrival (that is why the audio
    # binding cannot lean on a session id); no *bridge* line of the foreign capture may be.
    adopted = [line for line in session.lines if line.source != "serial"]
    check("a foreign conversation is refused, not adopted",
          len(session.foreign) == 4 and not adopted, len(session.foreign))
    completion = tracker_for(session).completion()
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    check("no completion means no content window for us",
          completion is None and summary["matched"] is False
          and summary["reason"] == "no_completed_generation"
          and not any(item["content_matched"] for item in events.of_kind("device_speaking")),
          summary["reason"])

    # Our own turn, with a later span from the same board in the same window: the binding is
    # by delivery containment, so the neighbouring span is never credited as our answer.
    lines = ack_and_content_lines() + [
        serial_line(24.000, "listening", "speaking"),
        serial_line(26.000, "speaking", "listening"),
    ]
    session = bound_session(lines)
    completion = tracker_for(session).completion()
    events = CollectingEvents()
    summary = driver.record_windows(events, session, 0, 1, completion=completion)
    credited = [item for item in events.of_kind("device_speaking") if item["content_matched"]]
    check("a neighbouring span is not credited to the completed generation",
          summary["matched"] is True and len(credited) == 1
          and credited[0]["start_local"] == stamp(12.030),
          [item["start_local"] for item in events.of_kind("device_speaking")])


def wake_gate_checks() -> None:
    print("wake freshness gate")
    prompt_start = stamp(20.000)

    fresh = bound_session([wake_line(21.000)])
    line, stale, reason = driver.fresh_wake_line(fresh, 0, prompt_start)
    check("a wake stamped after the prompt is accepted",
          line is not None and reason == "stamped_after_prompt" and not stale, reason)

    leftover = bound_session([wake_line(17.000)])
    line, stale, reason = driver.fresh_wake_line(leftover, 0, prompt_start)
    check("a wake stamped before the prompt does not open a session",
          line is None and reason == "no_fresh_wake" and len(stale) == 1
          and stale[0]["behind_prompt_s"] == 3.0, stale)

    # A line that arrived in this window cannot be minutes old, so a stamp that far back
    # means the board clock is not comparable: it is accepted and flagged, not dropped.
    skewed = bound_session([wake_line(20.000 - 3600.0)])
    line, _, reason = driver.fresh_wake_line(skewed, 0, prompt_start)
    check("a board clock far out of range is flagged, not silently refused",
          line is not None and reason == "board_clock_out_of_range", reason)

    unstamped = bound_session([wake_line(None)])
    line, _, reason = driver.fresh_wake_line(unstamped, 0, prompt_start)
    check("an unstamped wake line falls back to arrival order",
          line is not None and reason == "unparsed_stamp_accepted_on_arrival", reason)

    # A wake that arrived before the mark belongs to the previous attempt.
    session = bound_session([wake_line(19.000)])
    mark = session.mark()
    session.feed("serial", wake_line(21.500)[2], arrived=5.0)
    line, stale, reason = driver.fresh_wake_line(session, mark, prompt_start)
    check("the mark keeps an earlier attempt's wake out",
          line is not None and line.t_local == stamp(21.500) and reason == "stamped_after_prompt",
          line.t_local if line else stale)


class FakeProc:
    """Stands in for the ffmpeg child: no process, no audio device, no WAV."""

    def __init__(self) -> None:
        self.running = True
        self.signals: list[int] = []
        self.returncode: int | None = 0

    def poll(self):
        return None if self.running else 0

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        self.running = False

    def wait(self, timeout: float | None = None):
        self.running = False
        return 0

    def kill(self) -> None:
        self.running = False


class LegacyRecorder:
    """A recorder object from before this change: no `stop_event_written` attribute."""

    def __init__(self) -> None:
        self.proc = None
        self.out_path = Path("legacy.wav")
        self.ended_local = stamp(9.0)


def stop_checks() -> None:
    print("recorder stop")
    ticks = [stamp(1.0), stamp(600.0), stamp(601.0)]
    original_now_local = driver.now_local
    driver.now_local = lambda: ticks.pop(0) if ticks else stamp(700.0)
    try:
        recorder = driver.Recorder(Path("/nonexistent-codex-selfcheck/recorder.log"), 0,
                                   Path("/nonexistent-codex-selfcheck/rec.wav"), 5)
        recorder.proc = FakeProc()
        recorder.stop()
        first = recorder.ended_local
        recorder.stop()
        check("a second stop does not move the recorded stop time",
              first == stamp(1.0) and recorder.ended_local == first,
              f"first={first} now={recorder.ended_local}")
        check("the second stop is a no-op", recorder.stopped is True, recorder.stopped)

        events = CollectingEvents()
        wrote = driver.emit_recorder_stop(events, recorder)
        rewrote = driver.emit_recorder_stop(events, recorder)
        stops = events.of_kind("recording_stop")
        check("the stop event is written exactly once",
              wrote is True and rewrote is False and len(stops) == 1, stops)
        check("the stop event keeps its original payload shape",
              set(stops[0]) == {"kind", "t_local", "recording", "start_local"}
              and stops[0]["recording"] == "rec"
              and stops[0]["start_local"] == first, stops[0])

        legacy = LegacyRecorder()
        legacy_first = driver.emit_recorder_stop(events, legacy)
        legacy_second = driver.emit_recorder_stop(events, legacy)
        check("a recorder without the new flag still emits exactly one stop event",
              legacy_first is True and legacy_second is False
              and len(events.of_kind("recording_stop")) == 2, legacy_second)

        results: dict[str, object] = {}
        warnings = io.StringIO()
        with redirect_stderr(warnings):
            driver.cleanup(None, [recorder], events, results, NO_SUCH_RUN_DIR, budget_s=1.0)
        check("cleanup neither re-dates nor re-writes an already stopped recorder",
              len(events.of_kind("recording_stop")) == 2 and recorder.ended_local == first
              and events.closed is True,
              f"stops={len(events.of_kind('recording_stop'))} ended={recorder.ended_local}")
        check("cleanup still reports the recorder it stopped",
              results["cleanup"]["recorders_stopped"] == ["rec.wav"]
              and results["cleanup"]["recorder_returncodes"] == {"rec.wav": 0},
              results["cleanup"])
        check("the fixture wrote no report (the lookup is read-only)",
              "could not write results.json" in warnings.getvalue(),
              warnings.getvalue().strip())
    finally:
        driver.now_local = original_now_local


def main() -> int:
    content_case_checks()
    acknowledgement_only_checks()
    unclosed_window_checks()
    ambiguous_window_checks()
    delivery_window_checks()
    cross_session_checks()
    wake_gate_checks()
    stop_checks()
    print("")
    if FAILURES:
        for failure in FAILURES:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("self-check passed: content-window binding, wake freshness and recorder stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
