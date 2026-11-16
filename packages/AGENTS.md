# packages — shared contracts & proto authority

**Score 10 — distinct domain: the only cross-service, cross-language contract source of truth.**

## OVERVIEW
Single authority for wire contracts: JSON Schema (multi-subject, device-media, onboarding, events, cost-ledger) generated into Python/JS, and buf proto for the media/Voice Core seam.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Canonical multi-subject schema | `contracts/schemas/multi-subject/multi-subject.schema.json` | 1913 lines; `$defs` enums + object contracts + lifecycle; pinned by ADR-0033 |
| Generated Python | `contracts/generated/python/multi_subject_contracts.py` | DO NOT EDIT; regenerate only |
| Device/edge frames | `contracts/device-media-v2.json` | WSS frames + binary audio params |
| Onboarding, UI events | `contracts/device-onboarding-v1.json`, `contracts/events.schema.json` | 15 `oneOf` UI event variants |
| Cost ledger schema | `contracts/schemas/cost-ledger/household-cost-ledger.schema.json` | offline usage → bill reconciliation |
| Media proto | `proto/memoria/media/v1/media.proto` | `MediaToCore`/`CoreToMedia` oneofs; streaming bridge + shadow parity |
| Proto lint config | `proto/buf.yaml`, `proto/buf.gen.yaml` | lint exceptions pinned for name compat |
| Golden vectors/corpora | `contracts/generation-fence-vectors.json`, `h5-interruption-corpus.json`, `child-speech-corpus.json` | read by Python AND Go tests |
| Regenerate / check | `scripts/generate_multi_subject_contracts.py` | `--check` exits 1 on drift; CI runs `git diff --exit-code` |
| Proto gen + checks | `scripts/generate_media_proto.py` (Python), `scripts/generate_media_go_proto.sh` (Go); `cd packages/proto && buf lint`, `buf breaking --against <base>` | Go: protoc-gen-go v1.36.11, go-grpc v1.5.1 |

## CONVENTIONS
- Schema is source; code is generated. Generated Python is committed, sha256-pinned to the schema, with `ConfigDict(extra="forbid", frozen=True)` and strict fields.
- Fail-closed is the default posture: unknown enum / missing value / out of range → reject; `from_value()` returns `None`, caller fails closed.
- Schema change protocol: bump `schema_version`/`objects_version` AND update ADR-0033 in the same change.
- Invariants are machine-readable via `x-memoria-*` extension vocabulary (`-time-order`, `-conditional-fields`, `-forbidden-values`, `-epoch-semantics`, …); every enum `$def` carries `contract_version`, `frozen_by`, `missing_value_policy: fail_closed`, Chinese `value_notes`.
- Schema `$id` schemes are not uniform (`memoria://`, `https://memoria.local/`, `https://voice-agent.local/`); data-only JSON has no `$schema`, version lives in a `schema_version`/`version` field.
- Proto: `INTERACTION_AUTHORITY_UNSPECIFIED` means `PYTHON_AUTHORITATIVE`; Go-shadow producers set `candidate_only`; `session_epoch=0` is legacy; `protected_playback` wire key is `interruptible` (opposite polarity).
- Contract seam rule: media proto carries only media/Voice Core — LLM, memory, and provider credentials never cross it.
- Schema enum values are UPPER_SNAKE strings prefixed with enum name; Python comments/descriptions in Chinese.

## ANTI-PATTERNS
- Never hand-edit `generated/python/multi_subject_contracts.py`; never embed a second enum set under `objects` in the schema.
- Lifecycle: `RuntimeProfile`, `RuntimeProfileSigned`, `PolicyReceipt` are `deprecated`/`migration_only`, `new_producer: forbidden` — new producers must use the V2 variants (`is_new_producer_contract` / `require_new_producer_contract`).
- Cost ledger: never invent prices; missing bills are `未验`; currency amounts only copied from real bills.
- Fence vectors: cancel bumps only `generation_id` +1 — never skip a generation or change turn/tool/session epoch.
- `device-media-v2`: queue gaps flagged `0x0001` at write time; `generation.completed` queued after all `assistant.audio.frame`, dropped when stale.
- Child speech corpus: `consent_required_for_recorded_audio` is `const true`; `consented_recorded` needs a consent artifact and ≥200 fixtures.
