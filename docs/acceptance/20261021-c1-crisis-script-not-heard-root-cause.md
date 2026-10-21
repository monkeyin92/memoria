# C1 crisis script not heard: root cause (bridge `bf245ed1`, session `a6b8d6ac`, 2026-10-21 03:51:53Z)

The child said 「我不想活了」. The server produced the approved `crisis-self-v1` script and sent no audio. This note records the mechanism and the fix in `fix/crisis-playback-c1-stop`.

## Timeline (production log)

| time (UTC) | event |
| --- | --- |
| 03:51:53.2 | turn committed, server emits `crisis-self-v1` (`fixed_safety_playout reply_template_id=crisis-self-v1`) |
| 03:51:53.441 | device `vad.start` on the robot's own leaked voice; user turn opens with no words |
| 03:51:53.4 | first Doubao attempt fails: `failed to synthesize speech text_len=100` |
| 03:51:55.377 | retry produces the first PCM packet |
| 03:51:55.379 | `media prepared reply held for a user turn with no words` |
| 03:51:55.555 | `media prepared reply dropped cause=textless_turn_over` |
| 03:51:55.5 | `media output result ... status=aborted reason=superseded emitted_audio=False` |

The child heard nothing.

## Why text_len=100 matters

The approved `crisis-self-v1` script is 100 characters long, so `text_len=100` in the first failure line is the whole script in one provider request. That is also why the provider layer now synthesizes the script as one unit with retry and cache instead of streaming it phrase by phrase.

## Mechanism

1. The script reply is prepared, but its first PCM is held until the speech provider delivers audio.
2. The device VAD edge from room noise / echo opens a user turn and closes the assistant output floor.
3. The provider's first attempt fails and its retry only delivers 1.9 s later. By then the wordless turn has endpoint-committed as `empty_media_turn` (no ASR text), which retires the pending turn but leaves the assistant floor closed for the reply task.
4. `_wait_for_unheard_output_floor` in `services/agent/src/voice_core/media_session_output_stream.py` saw `_floor_held_by_textless_turn` turn false and treated it as "the user really holds the floor", dropping the reply as `textless_turn_over` before its first frame.
5. Nothing at the provider layer knew the reply was an approved safety script: it was streamed phrase by phrase, so a mid-script provider fault could also truncate it.

## Fix

- Session layer: a reply the provider marks as an approved fixed safety script (`is_fixed_safety_reply(fence)`) is not dropped while the turn that blocked it holds no words. When the wordless turn is gone it reclaims the floor (`open_assistant_floor(cause="fixed_safety_reply")`) and logs `media fixed safety reply reclaimed the floor after a turn with no words`; its wait is bounded at 30 s instead of 7 s. Turns with words still supersede the script, and every other reply keeps the previous decision (covered by `test_media_session_output.py::test_higher_priority_intent_supersedes_main_reply_before_pcm`).
- Provider layer: the whole script is synthesized as one unit before the first frame, retried on failure, cached per voice, and replayed from that cache when the provider stays down (`fixed safety speech ready source=synthesis|cache`).
- StepFun→Doubao failover now restarts a unit that dies after its first packet instead of silently truncating it.

## Not changed

- Script wording (owned by `services/common/fixed_safety_scripts.py`, unchanged).
- Stop-word handling (owned by `fix/stop-immediate-mute`).
- `daily_chat_usage` accounting.

## Evidence

- `services/agent/tests/unit/test_fixed_safety_reply_survives_textless_turn.py` replays the timeline (held script, VAD edge, empty turn retired by the cap, then the first packet) and asserts the script still reaches the device while an ordinary held reply still yields.
- `services/agent/tests/unit/test_fixed_safety_playout.py` covers the retry, the cache replay and the no-cache failure.
- `services/agent/tests/unit/test_stepfun_tts.py` covers the mid-unit failover.
