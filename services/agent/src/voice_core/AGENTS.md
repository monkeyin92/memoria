# Voice Core: Media Session (services/agent/src/voice_core)

## OVERVIEW
59 modules implementing the provider-neutral media voice session: `MediaVoiceCoreRegistry` (slots dataclass, 12 mixins) wired to the media-v1 gRPC bridge, plus device security, OTA and media telemetry.

## WHERE TO LOOK

| Area | Modules |
|------|---------|
| Registry + wire types | `media_session.py`, `media_session_state.py`, `media_session_types.py`, `media_protocol.py` (`SessionIdentity`, `MediaEnvelope`) |
| Device transport | `grpc_bridge.py` (`MediaBridgeGrpcServer`), `media_bridge_server.py` (transport-independent state gate) |
| Session concerns (mixins) | `media_session_{connection,lifecycle,input,commit,projection,turns,output_stream,playback_stop,standby,aigc_notice,final_fallback}.py`, `media_audio_ingress.py` |
| ASR coordination | `asr_stream_supervisor.py`, `speech_timeline.py`, `adaptive_vad.py`, `semantic_completeness.py`, `speech_end_denoise.py` |
| Playback accounting | `playback_ledger.py`, `reply_delivery.py`, `playback_text.py`, `aigc_notice.py`, `crisis_script_interrupt.py` |
| Device security / OTA | `device_security.py` (Ed25519), `ota.py`, `device_protocol.py` |
| Provider seams | `provider_adapter.py`, `provider_adapter_{generation,fixed_safety,spans}.py` |
| Telemetry | `telemetry.py` (validated names/trace fields), `slo.py`, `slo_reporter.py` |
| Subpackages | `interruption/` (evidence + policy), `generated/` (protobuf, never hand-edit) |

## CONVENTIONS
- Mixins are composed into `MediaVoiceCoreRegistry`; docstrings say so; cross-mixin attributes are declared under `TYPE_CHECKING`.
- `media_pb2: Any` alias keeps generated protobuf untyped; runtime enum lookups go through `DESCRIPTOR.enum_types_by_name`.
- Tunables are `_UPPER_SNAKE` module constants with units in the name and a dated incident rationale.
- Fencing everywhere via `contracts.ids.GenerationFence` + `stream_epoch` ints; clocks are injected/monotonic, never wall sleeps.

## ANTI-PATTERNS
- Python is the only interaction authority: Go shadow/authoritative hellos are answered `python_authoritative`.
- Partial or non-monotonic fence matches are never accepted; forged watermarks never promote heard text.
- Backchannel utterances (「嗯」「对」) and 「不要停」 never stop playback; reply echo must not trigger content barge-in.
- Fixed-safety line never truncates mid-script and never reaches a live TTS engine; Doubao never speaks a crisis turn.
- PCM tap and SLO reporting must never cost the audio path; forbidden fields are dropped before logging.

## NOTES
`__init__.py` docstring claims the package holds "only standard-library data models" - stale: it re-exports gRPC bridge and numpy denoiser symbols.

Earned: score 14 (59 files, 73-name `__all__` surface, `MediaBridgeGrpcServer` imported by 43 files).
