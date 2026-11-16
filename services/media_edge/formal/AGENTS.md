# formal — LateReceipts TLA+ model

TLA+/TLC model of the Media Edge playback-receipt handler when a generation is
replaced (Voice Core cancel, next reply, device stop). Earned this file: distinct
domain — verification workflow and frozen-state rules are not visible in Go code.

## WHERE TO LOOK

| File | Role |
|------|------|
| `LateReceipts.tla` | Module `LateReceipts`: 37 operators, 11 invariants + `TypeOK`, `CHECK_DEADLOCK FALSE` |
| `LateReceipts.cfg` | Primary TLC config |
| `LateReceiptsFinding.cfg` | Retired finding config — keep empty, stable path |

Modeled code: `device_playback_ledger.go` (receipt recording), `session_generation.go`
(generation advance/cancel), `device_ws_downlink.go` (late-receipt drop).

## CONVENTIONS

- Invariants named `*Oracle*` observers; fence helpers `F1`/`F2`; receipt types as
  string literals `{"started","progress","ended","error"}`.
- Verify via repo skill `.cursor/skills/verify-media-edge-late-receipts/bin/verify
  [--check|--mutants|--finding]` (TLC 1.7.4, JDK 11+).

## ANTI-PATTERNS

- TLC violation means model drifted from code or a real regression: read the skill's
  "How to read a failure" before changing anything.
- `EXPECTED_BASE_DISTINCT` (27759) is frozen: changing the state count requires
  updating the constant AND the SKILL coverage record together, so a silently
  weakened model cannot pass.
- Never edit `LateReceipts.tla` without re-running `--check` and `--mutants`.
- Never hand-edit generated code that feeds the model — the model must mirror
  `services/media_edge` behavior, not aspirations.
