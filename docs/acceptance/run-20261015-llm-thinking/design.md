# `llm_thinking` filler design

Status. Design only. Architect phases A and B. Nothing here is implemented or committed by this run.

Branch. `cursor/llm-failover-stream-clean-b67e`. HEAD at writing is `5637a6b5`.

Evidence labels. "Measured" means I observed it directly in code, git, or command output this session, with the citation given. "Inferred" means it follows from code I read but I did not run it. "Guess" means a prediction or an unseen magnitude.

## Product requirement

- If no reply audio has started 3 s after the child stops speaking, play 「嗯，我想一想。」.
- At most once per turn.
- Never after any reply text was spoken.
- Any stored asset carries AIGC metadata like the `llm_unavailable` offline line.
- Tests pin the timing and the once-per-turn rule.

## Phase A. Ground

### How the production turn and output path works today

The production path is `MediaVoiceCoreRegistry` built in `scripts/run_media_bridge.py`. The LiveKit `agent.py` path is not production (measured, registry construction in `run_media_bridge.py`).

1. Device VAD end arrives in `media_session_input.py` around lines 538 to 677. The segment carries `capture_start_sample` (the event sample) and `voiced_end_sample` on the 16 kHz uplink clock (measured). Their difference is the device VAD hangover (inferred).
2. `_schedule_turn_commit` waits an endpoint grace of 0.7 to 1.1 s (`turn_endpoint_grace_s = 0.9`, `media_session.py:101`, measured), then prepares and commits.
3. Commit sets `context.output.begin_turn_output(fence)` and `context.turn_started_ns` together (`media_session_commit.py` around line 1465, measured). Commit lands about 1.5 to 2.5 s after speech end (inferred from grace plus prepare comments).
4. `_dispatch_reply` enqueues a `CONVERSATION_REPLY` `OutputWork`. `_run_output_work` acquires the output owner lease at `media_session_output_dispatch.py:1061` before `_output_chunks` pulls the first LLM token (measured). So the conversation reply owns output for the whole time the LLM is thinking.
5. `_output_chunks` (`media_session_output_stream.py:1041`) wraps `provider.generate_reply(...)` and shifts every chunk by `source_start_sample` with inline code at lines 1053 to 1075 (measured).
6. `_stream_output` (`media_session_output_stream.py:687`) paces 20 ms frames, enters SPEAKING on the first frame, records the delivery ledger, and emits `tts_first_frame` and `first_audio` on the first frame (lines 925 to 940, measured). `first_audio` is measured from `turn_started_ns` against an SLO p95 of 1200 ms (measured from the SLO config read earlier).
7. Per-turn audio evidence already lives in one place. `_live_lookup_filler_was_heard` scans `reply_delivery.snapshots()` and `output_results` for any same-turn frame (`media_session_projection.py:106-117`, measured). `OutputState.audio_sent_for(fence)` answers the same question for one fence (`media_session_state.py:76`, measured).
8. Output preemption. `_enqueue_output_work` calls `_preempt_output_owner` whenever another current work arrives while an owner holds the lease (`media_session_output_dispatch.py:556-562`, measured). FAST_ACK ranks 4 and CONVERSATION_REPLY ranks 3 (`delegation_coordinator.py:126-135`, measured). An owner that has not started playback is cancelled with `_cancel_reply_task(..., reason="preempted")`, and `_queued_output_followups` excludes the owner, so it is not re-dispatched (`media_session_output_dispatch.py:957-1030`, measured).
9. Interrupted subtitle spans. `_record_interrupted_timed_spans` stores provider-relative samples without the `_output_chunks` shift (`media_session_output_owner.py:208-244`, measured). This is a latent offset bug whenever the shift is non-zero (inferred).
10. Voices. `DEFAULT_VOICE_PROFILE = "warm_companion"` maps to the male `zh_male_yangguangqingnian` (measured). Only `bright_peer` uses `zh_female_tianmeitaozi` (measured). Personal voice clones also exist (measured).
11. AIGC OpusTags exist only on firmware offline clips. `build_offline_stories.py` checks `CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED` for `PENDING_PLACEHOLDER_LINE_IDS` (measured). Streamed TTS carries no file metadata (measured). Firmware `OnLlmUnavailable(play_line)` only ever gets `play_line=false` from the protocol (measured).
12. History. A 250 ms `THINKING_FILLER` once lived in `agent.py` (83687b7e) and was dropped (97978e9). A second slow-lookup cue (2492175b) was removed (1dcdbb8e) after users heard two near-identical 「稍等」 lines. `test_slow_lookup_uses_a_single_cue` guards that (`test_media_session_delegation.py:2939`, measured).

### Concurrent implementation already on this branch

Another agent committed `47f3103b` and `5637a6b5` while this design ran, and has one more uncommitted line in `media_session_input.py:375` (measured, `git log` and `git diff`).

What it does (measured).

- `services/common/llm_thinking_filler.py` adds `LLM_THINKING_TEXT`, `LLM_THINKING_DELAY_S = 3.0`, `LLM_THINKING_AIGC`, and an `LlmThinkingFiller` gate with `arm`, `note_reply_spoken`, `should_play`, `mark_played`.
- Commit calls `arm_llm_thinking_filler` (`media_session_commit.py:1472`). It sleeps the remaining budget from `pending.child_speech_ended_at`, then speaks through `_speak_allowlisted_bridge_phrase(..., require_idle_input=False)` (`media_session_projection.py:491-537`).
- `_stream_output` decides "reply started" by comparing the spoken text to `LLM_THINKING_TEXT` (`media_session_output_stream.py:941-943`).
- `prompts.py` keeps 「稍等，我想一下。」 at `BRIDGE_PHRASES[4]` as `THINKING_FILLER` and appends `LLM_THINKING_TEXT` as `LLM_THINKING_FILLER` (lines 76 to 86).
- Firmware gains a 172-byte `llm_thinking.ogg` placeholder, an `index.json` entry, a header constant, build script entries, and test count changes.
- Tests cover only the pure gate. No registry test exists (measured, grep of `services/**/tests`).

Where it diverges from the requirement or the codebase.

| Issue | Evidence | Label |
| --- | --- | --- |
| The filler is a FAST_ACK `OutputWork` enqueued while the conversation reply owns the lease. Rank 4 beats rank 3, the owner has not started playback, so the LLM reply is cancelled and not re-dispatched. The child hears 「嗯，我想一想。」 and then silence. | Phase A items 4 and 8 | Inferred. Needs the reproduction test listed below. |
| The filler counts as `first_audio` and sets `first_frame_sent` on its fence. The SLO metric then reports filler latency and hides LLM latency. | `media_session_output_stream.py:925-940` | Inferred |
| "Reply started" uses a text compare against the filler string. | line 942 | Measured |
| `_reply_spoken_turn_id` duplicates what the delivery ledger already knows. | gate fields vs Phase A item 7 | Measured |
| Two thinking lines and two constants coexist in `prompts.py`. | lines 81 to 86 | Measured |
| The speech-end stamp is receipt time, not the last voiced sample. The filler fires late by the device VAD hangover. | `media_session_input.py:666` | Measured stamp. Hangover size is a guess, 0.3 to 0.6 s. |
| The firmware clip has no trigger. It is a dead asset. `LLM_THINKING_AIGC` has no consumer. | Phase A item 11 | Measured |
| The text is listed twice, once in `llm_thinking_filler.py` and once in `build_offline_stories.py` `OFFLINE_LINES`. | both files | Measured |
| `services/common/llm_thinking_filler.py` sits next to the unrelated `services/common/llm_thinking.py` (LLM reasoning mode). | both files | Measured |

### Constraints the design must honor

- Do not cancel or delay a reply that is ready. Do not lose the LLM reply.
- Do not revive a second lookup cue. Lookup turns already have their own acknowledgement.
- Keep `first_audio` and `tts_first_frame` honest.
- Keep the filler out of `assistant_text`, history, and the LLM context. The LLM imitates what it sees in history (inferred from the 2026 lookup-cue history).
- `media_session_turns.py` has a module budget of 1681 lines and is at 1681 (measured, `pyproject.toml:130` and `wc -l`). Do not touch it.
- An archived handoff recorded env default drift across four files. The delay must not become an env var (measured, handoff read earlier).
- Tests use pytest-asyncio auto mode and `media_session_support._wait_until` polling (measured).

## Phase B. Candidates

### Candidate A. In-band splice in the conversation stream

The filler is audio that precedes the reply inside the same conversation `OutputWork`. No new output intent, no new lease, no arm or cancel bookkeeping.

- At commit, snapshot the child's speech-end time on the session next to `turn_started_ns`.
- In `_output_chunks`, the conversation branch wraps `generate_reply` with a pure combinator. The combinator races the first reply chunk against the deadline. If the deadline wins and filler PCM is ready, it yields filler frames, then the reply shifted by the filler length.
- Once per turn and "never after reply" both come from one predicate. "Has this turn emitted any audio?" is read from the delivery ledger.
- Filler PCM comes from the provider in the session voice, through an optional `thinking_filler_pcm(fence)` capability probed with `getattr`, like `generate_output` already is.

Red-flag screen.

| Flag | Result |
| --- | --- |
| Shallow module | Pass. One function hides the race, the tie rule, framing, the sample shift, and task cleanup. |
| Information leakage | Partial. The sample shift must also reach `_record_interrupted_timed_spans`. Mitigated by one writer (`_output_chunks`) recording the origin on `OutputState`, and one shift rule on `MediaReplyChunk.shifted`. |
| Temporal decomposition | Pass. No arm, note, or mark steps. |
| Pass-through method | Pass. `_with_thinking_filler` adds eligibility policy. |
| Split ownership | Pass. Audio-started state has one owner, the delivery ledger. The speech-end stamp has one writer per lifecycle stage, input for the pending utterance and commit for the committed turn. |
| Two ways to do one task | Pass only if the concurrent path and the old 「稍等，我想一下。」 text are deleted in the same change. |
| Importable internals | Partial. Python cannot enforce privacy. Export two names via `__all__`. Keep helpers underscored. |
| Hand-synced list | Pass. One text constant. No firmware list entry. |

### Candidate B. Device-local clip triggered by a server cue

The server sends a small "play line `llm_thinking`" message. Firmware plays `offline_lines/llm_thinking.ogg`, a tianmeitaozi clip with AIGC OpusTags, from flash.

- New proto message, a `media_edge` Go forwarder, a firmware handler, capability gating for old firmware, and one more entry in `OFFLINE_LINES`, `PENDING_PLACEHOLDER_LINE_IDS`, `kOfflineLineIds`, and `index.json`.
- Strengths. Exact device-side timing. No TTS render cost.

Red-flag screen.

| Flag | Result |
| --- | --- |
| Shallow module | Pass. A one-field message. |
| Information leakage | Fail. The line id and the timing rule live in proto, server, and firmware. |
| Temporal decomposition | Neutral. |
| Pass-through method | Fail. The Go edge only forwards. |
| Split ownership | Fail. The server decides, the device plays, and the device must also suppress the clip if downlink audio already began. Two owners of "audio started for this turn". |
| Two ways to do one task | Fail. Server bridge phrases and device clips both voice in-turn cues. |
| Importable internals | Not applicable. |
| Hand-synced list | Fail. Four line-id lists. The id equality test only partly guards them. |

Experience costs. The clip voice differs from the companion voice for four of five designed voices and every clone (inferred from Phase A item 10). The server's ledger, heard text, and echo handling are blind to device-originated audio. In full duplex or interrupt-assist modes the server could treat the clip as child speech (inferred).

### Baseline C0. Separate FAST_ACK output (the concurrent commits)

Screened for completeness. Fails temporal decomposition, split ownership, two ways to do one task, information leakage, and hand-synced list (see the divergence table). Its likely reply cancellation (inferred) is disqualifying on experience alone.

## Chosen design package

Candidate A, with three adaptations. From B, keep the AIGC clip recipe as a fallback source behind the same provider seam. From C0, keep the `child_speech_ended_at` field name and set sites to minimise the diff. From the history, keep the lookup cue as the only cue on lookup turns.

### 1. Types and state machine

New pure module `services/agent/src/voice_core/thinking_filler.py`. No registry imports.

```python
SPEECH_SAMPLE_RATE: Final = 16_000


def speech_end_monotonic(
    *,
    received_at: float,
    event_sample: int,
    voiced_end_sample: int | None,
) -> float:
    """Backdate a VAD-end receipt to the child's last voiced sample.

    Never later than ``received_at``. Uplink latency is not subtracted, so the
    filler can only fire late, never early.
    """
    raise NotImplementedError


def filler_wait_s(*, now: float, speech_ended_at: float, delay_s: float) -> float:
    """Seconds left until the filler is due. Zero when commit lag already used the budget."""
    raise NotImplementedError


async def splice_thinking_filler(
    reply: AsyncIterator[MediaReplyChunk],
    *,
    deadline: Callable[[], Awaitable[None]],
    filler_pcm: Callable[[], Awaitable[bytes | None]],
    frame_samples: int,
) -> AsyncIterator[MediaReplyChunk]:
    """Yield ``reply``, preceded by one filler iff the deadline beats its first chunk.

    - The filler can only precede the first reply chunk.
    - A tie goes to the reply. A ready reply is never delayed to start a filler.
    - Filler frames carry no text, ``thinking_filler=True``, ``first`` on frame 0 only.
    - Reply chunks after a filler are ``shifted`` by the filler sample count, and the
      reply's first chunk gets ``first=False``.
    - On aclose or cancellation every pending task is cancelled.
    """
    raise NotImplementedError


__all__ = ["filler_wait_s", "speech_end_monotonic", "splice_thinking_filler"]
```

Additions to existing types.

```python
# media_session_types.py
@dataclass(frozen=True, slots=True)
class MediaReplyChunk:
    ...
    thinking_filler: bool = False

    def shifted(self, samples: int) -> MediaReplyChunk:
        """Move audio, text bounds, and text spans by ``samples``. Identity at zero."""


# media_session_state.py
class OutputState:
    # The sample where the provider's own stream starts within ``fence`` playback.
    # Written only by ``_output_chunks``. Read by ``_record_interrupted_timed_spans``.
    provider_origin: tuple[GenerationFence, int] | None = None

    def turn_audio_started(self, fence: GenerationFence) -> bool:
        """True once any frame of ``fence``'s turn reached the device, from any output."""


class MediaVoiceSessionState:
    turn_speech_ended_at: float | None = None  # set at every commit, beside turn_started_ns


# media_session.py
class MediaVoiceCoreRegistry:
    thinking_filler_delay_s: float = 3.0
```

Combinator state machine, one per conversation stream.

```text
WAITING  --first reply chunk (also wins a tie)-->  PASSTHROUGH
WAITING  --deadline-->                              DUE
DUE      --first reply chunk (also wins a tie)-->  PASSTHROUGH
DUE      --filler PCM ready-->                      FILLING
DUE      --filler PCM is None-->                    PASSTHROUGH
FILLING  --last filler frame yielded-->             SHIFTED_PASSTHROUGH
any      --reply ends or raises-->                  done, error propagates
any      --aclose or cancel-->                      cancel deadline, filler, and first-chunk tasks
```

Once per turn, at the session level, is one predicate evaluated at splice entry.

```text
eligible(fence) =
    device session
    and turn_speech_ended_at is not None
    and provider has thinking_filler_pcm
    and not output.turn_audio_started(fence)
```

- Once per turn. Filler frames go through `_stream_output` and land in the ledger like any frame. A later generation of the same turn sees `turn_audio_started` and gets no filler (inferred).
- Never after reply text. Reply audio sets the same ledger evidence. Inside one stream the combinator can only place the filler before the first reply chunk.
- No stored "played" flag, no arm, no cancel task. Per model-the-domain, the turn's audio evidence is the state.

### 2. Files and functions to touch

| File | Change |
| --- | --- |
| `services/agent/src/voice_core/thinking_filler.py` | New. `speech_end_monotonic`, `filler_wait_s`, `splice_thinking_filler`. |
| `services/agent/src/voice_core/media_session_types.py` | `MediaReplyChunk.thinking_filler`, `MediaReplyChunk.shifted`. |
| `services/agent/src/voice_core/media_session.py` | Registry field `thinking_filler_delay_s: float = 3.0`. |
| `services/agent/src/voice_core/media_session_input.py` | In the VAD-end branch, capture `received_at` before the `finalize_speech_segment` await. Set `pending.child_speech_ended_at = speech_end_monotonic(...)` at line 666. Keep receipt time at the endpoint path, line 375. |
| `services/agent/src/voice_core/media_session_pending_turn.py` | Keep `child_speech_ended_at` and its `reset()` clear as committed. |
| `services/agent/src/voice_core/media_session_commit.py` | Beside `turn_started_ns`, set `context.turn_speech_ended_at = context.pending.child_speech_ended_at` unconditionally. Delete the `arm_llm_thinking_filler` call and its Protocol stub. |
| `services/agent/src/voice_core/media_session_state.py` | Add `turn_speech_ended_at`, `OutputState.provider_origin`, and `OutputState.turn_audio_started`, which moves the scan out of `projection.py:106-117` together with `_same_turn_fence`. Delete `llm_thinking_filler` and `llm_thinking_task`. |
| `services/agent/src/voice_core/media_session_output_stream.py` | `_output_chunks` conversation branch calls `_with_thinking_filler`, yields `chunk.shifted(source_start_sample)`, and writes `provider_origin` once the filler length is known. `_stream_output` skips `tts_first_frame` and `first_audio` for filler frames, so both fire on the first reply frame. Delete the `LLM_THINKING_TEXT` compare and import. Add the `thinking_filler_delay_s` Protocol stub. |
| `services/agent/src/voice_core/media_session_output_owner.py` | `_record_interrupted_timed_spans` shifts provider spans by `provider_origin` when its fence matches. |
| `services/agent/src/voice_core/media_session_projection.py` | `_live_lookup_filler_was_heard` calls `OutputState.turn_audio_started`. Delete `arm_llm_thinking_filler`, `note_llm_thinking_reply_started`, `_cancel_llm_thinking_filler`, `_llm_thinking_filler_wait`, and the call at line 717. |
| `services/agent/src/voice_core/provider_adapter.py` | `ExistingVoiceProviderAdapter.thinking_filler_pcm(fence) -> bytes \| None`. It renders `THINKING_FILLER` through `_provider_pcm_chunks` in the fence's bound voice, caches PCM per bound voice in a small bounded map, and runs the render in a shielded task so a lost race still fills the cache. `MediaVoiceProvider` Protocol is unchanged. |
| `services/agent/src/prompts.py` | `BRIDGE_PHRASES[4]` becomes 「嗯，我想一想。」. Delete the appended `LLM_THINKING_TEXT` entry, `LLM_THINKING_FILLER`, and the `services.common.llm_thinking_filler` import. |
| `services/common/llm_thinking_filler.py`, `services/common/tests/test_llm_thinking_filler.py` | Delete. |
| Firmware files from `47f3103b` | Revert `llm_thinking.ogg`, its `index.json` entry, the header constant, the `OFFLINE_LINES` and `PENDING_PLACEHOLDER_LINE_IDS` entries, and the test count changes. Keep them only if product picks the clip fallback. |
| `services/agent/tests/unit/test_thinking_filler.py` | New. Pure tests. |
| `services/agent/tests/unit/test_media_session_thinking_filler.py` | New. Registry tests. |
| `services/agent/tests/unit/test_media_session_delegation.py`, `test_media_session_turns.py` | The lookup guards also assert the fake's `thinking_filler_pcm` was never awaited, because filler PCM has no text for `output_texts` to catch. |

`media_session_turns.py` is untouched, so its budget holds.

### 3. Server TTS or firmware clip

Server TTS in the session's own voice, spliced in-band. No firmware clip.

- The child hears one companion. A tianmeitaozi line before a `warm_companion` reply is a different speaker on the default voice (inferred from Phase A item 10). Per experience-first, that outweighs reusing a clip.
- No stored asset exists, so there is no file to tag. The filler is labelled like every other live reply sentence.
- The device needs no protocol, firmware, or OTA change.

This deviates from the brief's bullet that names an AIGC-tagged asset in `zh_female_tianmeitaozi_uranus_bigtts`. It is open question 1. If product mandates that clip, only the source behind `thinking_filler_pcm` changes. `build_offline_stories.py` stays the single generator with `CONTENT_ID=llm_thinking`, `SERVICE_PROVIDER=Memoria`, `AI_GENERATED=true`. The server loads that one file at startup, validates the tags there per boundary-discipline, decodes and resamples to 24 kHz with PyAV (`av` is already a dependency, measured), and caches the PCM. The splice, the predicate, and the tests stay the same.

### 4. Tests that pin timing without flaking

No test asserts on wall-clock time. Each test fixes the event order with `asyncio.Event` gates, or picks a delay that is either already past or effectively infinite.

Pure tests in `test_thinking_filler.py`. Each uses literal expected values.

- `speech_end_monotonic(received_at=10.0, event_sample=80_000, voiced_end_sample=72_000) == 9.5`. Returns `received_at` when `voiced_end_sample` is None or not before the event.
- `filler_wait_s(now=101.5, speech_ended_at=100.0, delay_s=3.0) == 1.5`. Returns `0.0` when commit lag is 4 s.
- The deadline fires, then filler PCM resolves, then the reply is released. Output is filler frames, then reply chunks shifted by the filler length. Only filler frame 0 has `first=True`.
- The reply's first chunk arrives before the deadline. Output equals the reply unchanged and `filler_pcm` is never awaited past cancellation.
- The tie case. The first chunk and the deadline are both ready in the same loop step. The reply wins.
- Filler PCM is still pending when the reply arrives after the deadline. No filler.
- `filler_pcm` returns None. No filler.
- The reply raises after the filler. The error propagates after the filler frames.
- `aclose()` while waiting cancels the deadline, filler, and first-chunk tasks.

Registry tests in `test_media_session_thinking_filler.py`. These use a fake provider whose `generate_reply` waits on an Event and which implements `thinking_filler_pcm`.

- First, a reproduction test for the concurrent design. A FAST_ACK enqueued while the conversation owner waits for the LLM cancels that reply. This pins why the filler must stay in-band. Run it before deleting C0, per the bug-fix discipline.
- `thinking_filler_delay_s=0.0` with the reply gated. Filler frames reach the device before reply frames, the full reply text is heard afterwards, and `first_audio` is observed only on the first reply frame.
- `thinking_filler_delay_s=3600.0` with the reply released at once. No filler frames.
- Same-turn generation bump after the filler played. The second generation gets no filler.
- Lookup turn. `thinking_filler_pcm` is never awaited and only the lookup cue plays. `test_slow_lookup_uses_a_single_cue` stays green.
- Barge-in during the reply after a filler. Interrupted subtitle spans land at the shifted samples, so heard text matches what played.
- A non-device session, or a commit with no speech-end stamp. No filler.
- A registry default contract test asserting `MediaVoiceCoreRegistry.thinking_filler_delay_s == 3.0`.

### 5. What not to do

- Do not revive the second lookup cue. Do not splice the filler into FAST_ACK, DEEP_RESULT, or `generate_output` sources.
- Do not speak the filler as a separate `OutputWork` while the conversation reply owns output. It preempts the reply (inferred).
- Do not anchor the delay on commit time. Do not fall back to commit time when the speech-end stamp is missing. Skip the filler instead.
- Do not inject the filler as LLM token text or as TTS input text. It would enter `assistant_text`, subtitles, history, and fixed-script prefix detection.
- Do not count filler frames in `first_audio` or `tts_first_frame`.
- Do not make the delay an env var.
- Do not keep a `played` or `reply_spoken` flag. Read the ledger.
- Do not compare spoken text to the filler string to tell filler audio from reply audio.
- Do not keep both 「稍等，我想一下。」 and 「嗯，我想一想。」.
- Do not ship a firmware clip or an AIGC constant that nothing plays or reads.
- Do not touch the LiveKit `agent.py` path or `media_session_turns.py`.

## Rationale

### Problem

The child can wait 3 s or more in silence while the LLM produces its first token. Commit alone happens 1.5 to 2.5 s after speech end (inferred), and the first-token timeout is 5 s (measured). The product wants one short 「嗯，我想一想。」 to cover that gap. The shape is non-obvious because the conversation reply already owns the output lease while the LLM thinks, a higher-rank output preempts and cancels it, the SLO metrics count the first frame of a turn, and two earlier thinking cues were removed for sounding repetitive. A concurrent implementation on this branch takes the separate-output route and likely loses the reply (inferred).

### Usage (caller's view)

The child's timeline. Times after "stopped speaking" are guesses except the 3 s rule.

```text
t = 0.0 s   child's last voiced sample
t ≈ 0.4 s   device VAD end reaches the server
t ≈ 2.0 s   turn commits, LLM request starts
t = 3.0 s   no reply frame sent yet, so 「嗯，我想一想。」 plays in the companion's voice
t ≈ 4.2 s   the reply follows in the same stream with no gap and no second cue
```

The registry's view inside `_output_chunks`.

```python
reply = context.provider.generate_reply(context.identity, work.conversation_text, work.fence)
reply = self._with_thinking_filler(context, work.fence, reply)
async for chunk in reply:
    yield chunk.shifted(source_start_sample)
```

```python
def _with_thinking_filler(self, context, fence, reply):
    render = getattr(context.provider, "thinking_filler_pcm", None)
    speech_ended_at = context.turn_speech_ended_at
    if (
        context.identity.client_type != "device"
        or speech_ended_at is None
        or not callable(render)
        or context.output.turn_audio_started(fence)
    ):
        return reply
    wait_s = filler_wait_s(
        now=time.monotonic(),
        speech_ended_at=speech_ended_at,
        delay_s=self.thinking_filler_delay_s,
    )
    return splice_thinking_filler(
        reply,
        deadline=lambda: asyncio.sleep(wait_s),
        filler_pcm=lambda: render(fence),
        frame_samples=int(getattr(context.provider, "output_frame_samples", 480)),
    )
```

A test author's view.

```python
due = asyncio.Event()
release = asyncio.Event()
chunks = [c async for c in splice_thinking_filler(
    gated_reply(release, REPLY_CHUNKS),
    deadline=due.wait,
    filler_pcm=ready(FILLER_PCM),
    frame_samples=480,
    # test drives: due.set(), then release.set()
)]
assert [c.thinking_filler for c in chunks] == [True, True, True, False, False]
```

### Shape

- One pure combinator owns the decision of whether the filler plays, per boundary-discipline. The registry shell supplies the deadline, the PCM source, and eligibility. Nothing else in the registry knows the race.
- One predicate, `OutputState.turn_audio_started`, encodes both "once per turn" and "never after reply". It reuses the ledger that lookup cues already trust, per model-the-domain. No new turn-keyed state.
- `MediaReplyChunk.shifted` is the single sample-shift rule. `_output_chunks` and the combinator both use it, per laziness-protocol (consolidate decisions).
- `provider_origin` has one writer and one reader. It fixes interrupted span offsets, which the shift would otherwise make wrong.
- The filler is audio only. It never enters `assistant_text`, subtitles, history, or the LLM context.
- The public surface is three functions, one chunk flag, one chunk method, one registry field, and one optional provider method. It hides the deadline race, tie policy, framing, offset math, cache, and render concurrency.
- The design does not touch turn commit timing, input gating, lookup cues, failover, or firmware.

### Synthesis decision

Candidate A is the base. It is the only shape that keeps one output owner per turn, one voice, and one source of truth for "audio started". From B, it keeps the AIGC clip recipe as a drop-in source behind `thinking_filler_pcm` if product mandates the tianmeitaozi line. From C0, it keeps the `child_speech_ended_at` field and set sites. It rejects C0's separate FAST_ACK output, gate class, text compare, duplicate prompt constants, and dead firmware clip.

### Tradeoffs accepted

- We accept that a reply arriving during the filler waits for the filler to finish, up to about 1.2 s (guess), in exchange for never cutting the filler mid-word.
- We accept one TTS render per voice per provider instance, in exchange for the companion's own voice. The first turn per voice can miss the filler if the render is slower than the LLM (guess, rare).
- We accept that the server's first-frame-sent time, not the device's play time, decides "reply started", in exchange for using evidence the server already has.
- We accept no subtitle for the filler, in exchange for keeping it out of history and the LLM context.
- We accept the `provider_origin` field, in exchange for correct heard text on barge-in after a filler. This also fixes a latent bug.
- We accept that `speech_end_monotonic` ignores uplink latency, so the filler can only be late, never early.

### Alternatives considered

- Candidate B, a device clip on a server cue. It lost on interface depth. It exposes the line id and timing rule to proto, edge, and firmware, splits ownership of "audio started", needs four hand-synced lists, and speaks in a different voice.
- C0, a separate FAST_ACK output. It lost because it likely cancels the reply it covers (inferred), skews SLO metrics, and duplicates ledger state in a temporal arm, note, and mark gate.
- Pushing filler text into the live TTS session. It lost because the text enters TTS subtitle spans and heard history, stripping it is brittle, it changes `generate_reply` across about 30 fakes, and it waits on TTS first audio.
- Injecting the filler as LLM token text, the 83687b7e approach. It lost because it pollutes `assistant_text`, transcripts, and history, and breaks fixed-script prefix detection.

### Open questions and risks

1. Does product accept the companion's session voice for the filler, or must it be the tianmeitaozi clip with AIGC OpusTags as the brief states? The fallback is specified in section 3.
2. Should the firmware `llm_thinking` placeholder from `47f3103b` be reverted, or kept for a future device-local fallback?
3. Should the filler play before a slow fixed safety script, such as a crisis reply?
4. How large is the device VAD hangover in practice? The design assumes 0.3 to 0.6 s (guess). A field log of `event_sample - voiced_end_sample` would settle it.
5. Does rendering the filler while the reply's live TTS stream is open for the same fence collide in `bind_fence` or in TTS concurrency quotas? This is a risk to check in the provider before wiring the splice (guess).
6. When the LLM is unavailable, the child hears the filler at 3 s and `LLM_UNAVAILABLE_TEXT` at about 7 s (inferred). Is that sequence acceptable? `_maybe_standby_after_llm_unavailable` is unaffected because the filler adds no text (inferred).
7. Who reconciles `47f3103b` and `5637a6b5` on this branch, and should they be reverted or edited forward?

### Next implementation step

Write the registry reproduction test that shows a FAST_ACK enqueued during a waiting conversation reply cancels that reply. It decides whether C0 must be removed before anything else lands.
