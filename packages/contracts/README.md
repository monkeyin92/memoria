# Shared contracts

Executable contracts shared by the Agent, H5, and Mini Program media gateway.

- `events.schema.json` defines Agent-authoritative UI events. Clients **must**
  drop events with a smaller `generation_id` than the latest accepted one.
- `miniprogram-media.json` defines the Mini Program WebSocket handshake, PCM
  headers, fixed 20 ms downlink audio shape, control-event fields, and
  cross-language golden frames consumed by both Python and JavaScript tests.
- `realtime-facade.json` defines the deliberately small OpenAI Realtime-style
  mapping surface. It delegates to the existing `DuplexRuntime` and does not
  expose a second audio transport or pipeline.
- `device-onboarding-v1.json` defines the strict signed QR, online proof,
  Claim, Binding initialization, Activation/ACK and post-activation device
  media challenge/session payloads. It deliberately contains no Wi-Fi
  credential, user access token, LiveKit credential or device private key field.
- `device-media-v2.json` is the ESP32 ↔ Go Hardware Media Edge authority for
  capability negotiation, interruption/playback control events, full
  generation fences, priority lanes and byte-exact `MemoriaAudioFrameV1`
  fixtures. The v2 downlink sequence/sample clock resets to 0 per
  authoritative `generation.started` or `playback.flush` replacement, and a
  same-generation forward gap caused by queue drops MUST be marked with the
  downlink-only discontinuity flag (header flags bit 0, 0x0001) at
  socket-write time; the device accepts a flagged gap only when the sequence
  and sample deltas are consistent multiples of `frame_samples` and rejects
  backward, forged or unflagged gaps. A v1 hello/session is served only by the legacy
  livekit_compat gateway; the direct edge is v2-only and never accepts a
  v1 hello, never uses the legacy `N + 1` generation mapping, and
  `full_duplex_verified` requires server-side acoustic attestation.
- `media-events.schema.json` defines the versioned media-v1 DataChannel envelope;
  every envelope carries the complete session/stream/sequence/generation/tool
  fence and a required object payload. Clients reject stale fences and require
  `tool_epoch` to move monotonically within one turn/generation. This contract
  remains separate from the Agent-authoritative UI event schema.
- `../proto/memoria/media/v1/*.proto` is the language-neutral Media Edge ↔ Voice
  Core contract. It is authored in this repository and deliberately contains no
  provider, memory, persona or tool payloads.

These files are compatibility fixtures, not runtime configuration. Change a
contract and both implementations in the same commit.

## Multi-subject canonical contracts (schema_version 2 / objects_version 2)

`schemas/multi-subject/multi-subject.schema.json` is the single authority for
the multi-subject vocabulary, object wire shapes, lifecycle policy and
cross-field invariants. It canonicalizes identity/binding, directed
relationships, consent/policy, memory scope, notifications, session fences and
Device Fleet terms. Object definitions reference the root `$defs`; an inline
second enum is rejected by the generator.

The memory authority vocabulary intentionally separates:

- private `memory_capture` from private `memory_promotion`;
- `family_shared_memory_proposal` from per-subject
  `family_shared_memory_approval` and compiler
  `family_shared_memory_promotion`.

A proposal receipt cannot authorize a vote or promotion. `object` and
`withdraw` are authenticated privacy/fail-closed actions and do not require an
allow receipt. A `confirm` vote carries its own approval receipt/fence and
immutable snapshot revision/hash. Final promotion starts only from
`approvals_complete`, embeds a fresh promotion receipt, and binds the exact
sorted approval snapshot set, current consent/membership evidence, proposal
revision and session/runtime fence in `PolicyActionResourceFence`.

### Contract lifecycle is machine-enforced

`objects.contracts` is a lifecycle record list, not an undifferentiated name
list. It is the only producer-lifecycle authority:

- `RuntimeProfile`, `RuntimeProfileSigned` and `PolicyReceipt` are
  `deprecated + migration_only + new_producer=forbidden` and point to their V2
  replacements. They remain parseable only for legacy consumer migration and
  the Control runtime-profile-v1 golden compatibility test.
- `RuntimeProfileV2`, `RuntimeProfileSignedV2` and `PolicyReceiptV2` are
  `current + canonical + new_producer=allowed`.

Generated Python exposes `CONTRACT_LIFECYCLE` and
`require_new_producer_contract`; TypeScript exposes `CONTRACT_LIFECYCLE` and
`requireNewProducerContract`; Go exposes `ContractLifecycleByName` and
`RequireNewProducerContract`; firmware compact JSON carries the identical
`contract_lifecycle`, `canonical_producer_contracts` and
`migration_only_contracts`; the Mini Program CommonJS artifact exposes the
same lifecycle map and executable producer guard. Unknown contract names also
fail closed. README text is explanatory only—the generated guards are the
enforceable boundary.

### Generated validation

The deterministic generator writes Pydantic v2, erasable TypeScript, Go and a
compact firmware manifest under `generated/`, plus a dependency-free CommonJS
artifact at `apps/miniprogram/generated/multi-subject-contracts.js`. Each
artifact embeds the source SHA-256 and schema/object version; object-capable
artifacts also carry the normalized invariant DSL. No timestamp is emitted.

- Python models are frozen, `extra="forbid"`, strict for wire primitives and
  strict RFC3339 date-time strings.
- The repository has no Zod dependency. TypeScript `isX` delegates to the real
  generated `validateX` runtime validator, including nested constraints,
  unknown keys and cross-field rules.
- Go `Validate()` checks enum/range/pattern/RFC3339/array/nested/cross-field
  constraints. Security-critical `DecodeXStrict` additionally rejects unknown
  JSON fields and missing required-nullable keys.
- Firmware compact JSON recursively retains field constraints, lifecycle,
  context-hash inputs and mandatory cross-field rules for constrained
  consumers.
- Mini Program code imports generated canonical enum/default/guard/lifecycle
  exports through `utils/multi-subject-contracts.js`, which is only a thin
  compatibility re-export and contains no hand-maintained values.

Sensitive `MemoryWriteFence`, `NotificationFence`, `SessionEpochFence` and
policy action fences have non-null bounded validity windows and require
`valid_until > issued_at`. `binding_version` is always strict integer `>= 1`;
issued profile/action epochs are `>= 1`. Session lifecycle epoch 0 is a
separate nullable envelope and can never authorize sensitive behavior.

Device Fleet contracts distinguish raw declared `capabilities` from
`attested_capabilities`, lifecycle-dependent nullable binding, certificate
status, physical mute/privacy-light evidence, monotonic counters/command
sequence and integer firmware security versions. Semver strings are display
metadata, never the anti-rollback total order. Remote command vocabulary has no
unmute command and execution must match current certificate, binding, firmware
security version and monotonic counter.

```sh
.venv/bin/python scripts/generate_multi_subject_contracts.py --write
.venv/bin/python scripts/generate_multi_subject_contracts.py --check
.venv/bin/pytest scripts/tests/test_generate_multi_subject_contracts.py \
  scripts/tests/test_multi_subject_object_contracts.py \
  scripts/tests/test_multi_subject_contract_v2.py -q
node --test apps/miniprogram/tests/*.test.js
```
