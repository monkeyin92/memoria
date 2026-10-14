# Safety retest root cause (release 20261015-safety-tiers-v2)

Real-device retest on `c262de1a` passed 6/9 acceptance sentences. This note records the mechanism for the post-safety extra turn (sentences 2 and 6) before the behavioral fix. It does not change runtime behavior.

## Symptom

For 「我想伤害自己」 and 「有个叔叔老是大我」 the fixed safety reply played correctly. About 10 seconds later the server accepted a new turn and spoke a normal chat reply. The log on that extra turn showed `reason=request_or_payload_invalid fallback=True` (from `response_plan_cached`).

## Product rule

After a fixed safety script the turn ends. The server must not speak again on its own. When the child genuinely speaks again, a normal next turn proceeds. Crisis or help words in that turn still route by tier (fixed script and guardian pin). The child's speech must not be blocked.

## Mechanism

1. Fixed safety TTS finishes. Playback completion arms `POST_PLAYBACK_ECHO_GUARD_MS` (10_000 ms) in `services/agent/src/duplex_runtime.py`.
2. `#243` cleared missed-hearing nudges while `crisis_reply_heard` is set. It did not stop a later accepted turn from building a local-safe plan and streaming the LLM.
3. After playback ends, `PlaybackInputGuard.accept_turn` does not re-check `assistant_echo` when the candidate was not during playback. Once the 10 s window expires, a transcript that is the echo or tail of the fixed script (or a device re-listen) can be accepted as a new user turn.
4. The response planner fetch fails with `request_or_payload_invalid`. Companion `build_local_safe_plan` then sets `direct_text=None` when there is no fixed safety hit on that text. `reply_pipeline.stream` falls through to the LLM and speaks a normal chat reply.

So the proactive speech is the local-safe fallback LLM path after an echo or invalid follow-up turn, not a second intentional safety script.

## Sentence 3 note

「我不想或了」 still routed to `none` on device. The server does not log ASR text by design. Matching work for that sentence waits on an offline TTS→ASR harness (or an opt-in, log-only final-ASR debug flag on a test device). Do not widen the matcher from guessed characters.

## Follow-up fix (separate commit)

Suppress proactive server speech after a fixed safety script (local-safe LLM fallback that is not itself a safety hit). Keep accepting genuine child turns. Keep tier routing when the child raises crisis or help words again.
