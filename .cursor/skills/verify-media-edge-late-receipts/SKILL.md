---
name: verify-media-edge-late-receipts
description: Model-check the Media Edge playback-receipt handler against a generation replacement (TLA+/TLC) and prove a late replaced-generation receipt never triggers the handler's fail-closed close, never produces an accepted send to Voice Core, and never disturbs another generation's playback window. Use when changing handlePlaybackReceipt, devicePlaybackLedger.record, VoiceCoreMediaRuntime.GenerationReplaced/SendPlaybackProgress, clearPlaybackActive, the generation fence ordering, or after a device reconnect/lost-reply incident around a stop.
---

# Verify Media Edge late playback receipts

A device keeps sending `playback.progress` for the story it is playing until it
processes Voice Core's `playback.flush`.  Voice Core replaces the generation
first (a cancel, or the next reply), so a receipt can arrive for a generation
that is already dead.  Two shipped incidents closed the WSS on such a receipt: a
4 s-stalled device's late `playback.ended`+`progress` (2026-09-30) and a receipt
in flight during a spoken stop (2026-10-02, two of seventeen stops, ~7 s
reconnect, next sentence lost).

The property checked here: once either generation authority has moved past a
receipt's generation, handling that receipt does none of the following.

1. Takes the two fail-closed close paths that `handlePlaybackReceipt` owns.
2. Produces an accepted send to Voice Core, or an accepted send whose fence no
   longer matches Voice Core's own session fence.
3. Changes a playback window that belongs to another generation.

It also checks that a live receipt still opens or closes the window the way its
type says, and that a replaced terminal receipt ends its own window.  Harm if any
of this breaks: an owner-visible session error and a reconnect that swallows the
next sentence, a stale window that silently ignores the child's next utterance,
or a stale receipt reaching Voice Core's actual-heard accounting.

## Run

```bash
.cursor/skills/verify-media-edge-late-receipts/bin/verify
```

One command, about 17 s, no install beyond a JDK 11+ (found through
`JAVA_HOME`, `/usr/libexec/java_home -v 21|17|11`, then `PATH`) and Python 3.
The first run downloads `tla2tools.jar` v1.7.4 into
`${XDG_CACHE_HOME:-$HOME/.cache}/memoria-tla/` and checks the pinned SHA-256,
so runs are reproducible without checking in a 2 MB jar.

Modes: `--check` (property, coverage and model-size assertions only, ~2 s),
`--mutants` (property + mutation evidence), `--finding` (property + fixed F1
confirmation).  No flag runs all three.

`--check` fails on any of: a violated invariant, a TLC warning (a malformed
action such as a variable both changed and UNCHANGED), a distinct-state count
different from the recorded 27759, or a covered action that is unreachable.
That makes a silently weakened model, a removed branch, or an unexercised path
fail loudly instead of passing.

Expected success text:

```
PASS  late receipts: ... (27759 distinct states, 16 actions covered)
PASS  mutant M0_... caught by NoCloseForReplaced (...)
...
PASS  fixed F1: terminal replaced receipts clear only their own playback window in both early-drop paths
```

Exit 0 means every run matched its expectation.  `--finding` confirms the
fixed F1 invariant after the primary property check; it is retained as a stable
mode name for existing operators, but no longer runs the old failing config.

## Coverage record

- Sync commit: `31e6514f`.  The model was re-derived against the terminal-only
  cleanup in both early drop paths.
- Code mapped into `services/media_edge/formal/LateReceipts.tla`:
  - `device_ws_uplink.go:272` `handlePlaybackReceipt` — ledger record (`:309`),
    refused terminal cleanup (`:321` to `:327`), terminal check and clear
    (`:332` to `:349`), precheck terminal cleanup (`:354` to `:360`), window
    update, forward, forward-error recheck (`:375` to `:380`).  `:389`
    `logDroppedLateReceipt`.  `:228` `handleButtonStop` moves the Edge Session
    fence first and clears the window at `:268`.
  - `device_playback_ledger.go:103` `record` — a terminal fence refuses later
    receipts; a lower received sequence is refused; `:119` to `:125` transport
    anchors (abstracted, see bounds).
  - `bridge_runtime.go:165` `GenerationReplaced` — consults the Edge `Session`
    fence and `VoiceCoreSession.current`, both directions.
  - `bridge_runtime.go:177` `SendPlaybackProgress` and
    `bridge_session_send.go:188` `VoiceCoreSession.SendPlaybackProgress` — the
    accepted send requires Voice Core's current fence to equal the receipt's;
    the gRPC write can also fail on its own.
  - `device_ws_session.go:448` `clearPlaybackActive` — only a window opened by
    the same fence is cleared; `:431` `isPlaybackActive` feeds VAD barge gating.
  - `device_ws_downlink.go:290` to `:299` — `generation.cancelled` deliberately
    does NOT clear the window; the device's own receipt or stop is the
    authoritative end.
  - `bridge_events.go:105` and `:139`, `bridge_runtime_events.go:101` and `:124`
    — `VoiceCoreSession.current` moves on the receive loop before the Edge
    Session applies the fence; a device stop moves the Edge fence first
    (`session_generation.go:68` to `:72`, then `bridge_runtime.go:305`).  The
    model keeps both authorities independent and both orderings reachable,
    including the edge-first state a later late receipt is handled in.
  - `bridge.go:566` `Fence.monotonic` — the ordering used by
    `GenerationReplaced`.
- Model shape: fences gen 1 to 2 for one session/epoch/turn; two independent
  authority fences (`coreGen`, `edgeGen`) with Voice-Core-first, device-stop-first
  and Edge-applies-Core moves; receipts `started`/`progress`/`ended`/`error` at
  sequence 1 to 2, at most three per run; handler steps are atomic actions and
  authority moves interleave between them.  `MaxReceipts = 3` bounds the run.
- Observers, not guards, carry the checks.  `OracleReplaced` is the true
  replacement independent of the `Replaced` helper a mutant changes;
  `forwardWindowCheck` splits the receipt's own type from `OpenWindow`;
  `lastForwardAccepted` and `lastForwardAcceptedOracleReplaced` observe the
  accepted send only (a refused send is not a forward); `terminalWindowCheck`
  drives the terminal-clear invariants.  That is why over-dropping (`M6`), a
  removed window update (`M7`), a removed terminal clear (`M8`) and a removed
  send-fence check (`M12`) fail even though a guard-shaped invariant would stay
  green.
- Historical fixes encoded: 2026-09-29 (terminal replaced receipt ends its own
  window), 2026-09-30 (ledger-refused replaced receipt is dropped), 2026-10-02
  (precheck drop plus forward-stage recheck, both authorities).  Scenario tests
  already in the repo: `device_ws_late_receipt_test.go`,
  `device_ws_uplink_test.go`, `bridge_runtime_test.go`.

## Bounds and what is not verified

- Two fences only, one session/stream epoch/turn, at most three receipts of
  sequence 1 to 2.  Longer receipt streams, later generations, reconnect races
  and cross-session interference are outside the model.
- The ledger's transport anchors are abstracted to "this fence was sent".  The
  model does not require `ReceivedSequence <= maxWireSeq` or
  `RenderedSampleEnd <= maxWireSampleEnd`, and omits parity, overflow and
  rendered-end arithmetic.  Over-accepting a receipt can set a terminal flag and
  prune later receipts, so the abstraction is not a simple superset; a
  counterexample that depends on an unanchored receipt is a modelling artifact.
- Only the two close paths after a receipt reached the ledger are modelled.  The
  pre-ledger refusal closes (bad envelope, control sequence, watermark) and the
  session-close path are out of scope, so the guarantee is "the receipt handler
  never fail-closes a replaced receipt", not "the WSS never closes".
- A device stop that Voice Core rejects because its fence already moved is out
  of scope; the model only shows a successful stop, which is what produces the
  edge-first state.  That rejected stop is the open button.stop path below.
- The sub-statement gaps between a check and the next statement are modelled as
  one step.  That covers `sendSessionError` after `GenerationReplaced`, and the
  fence check inside `SendPlaybackProgress` versus its gRPC write; a replacement
  landing exactly in either gap is not covered.
- No liveness: the model does not say a stale window is eventually cleared by
  an unrelated later receipt or a session close.  The repaired property only
  covers the terminal receipt's own cleanup.
- This is a hand-derived TLA+ model, not a code extractor.  It does not execute
  Go: changing the Go code without changing the model leaves the check green.
  That is why the coverage record, sync commit, state-count assertion and
  action-coverage assertion are part of this skill.

## How to read a failure

TLC prints `Error: Invariant <name> is violated` followed by the behavior.  Each
state names the action that produced it; `coreGen` and `edgeGen` show which
authority had moved, `status` shows the handler step, `windowActive` and
`windowFence` show the playback window, and `lastAction`/`before*`/observers
record what the previous step claimed to do.

- A violated primary invariant on current code means either the model no longer
  matches the code (re-derive the changed branch, then re-run) or a real bug
  (report it with the counterexample in the code's terms; do not weaken the
  invariant).
- "model state space changed" means the model was edited or weakened; re-derive
  it and update `EXPECTED_BASE_DISTINCT` together with this coverage record.
- "these actions are unreachable" means a branch or authority ordering was
  removed or gated away; compare the action with the Go path before trusting a
  green run.
- `--mutants` failing with "mutation anchor matched N times" means the spec was
  edited and `bin/verify`'s mutation anchors need the same edit.
- A changed model that still passes every mutant usually means a modelling error
  (an action that reads a value the code reads later, or a guard stronger than
  the code).  Compare the TLA+ action with the Go statement before reporting a
  finding.

## Finding F1 fixed in this change

`bin/verify --finding` confirms the repaired invariant.  The historical finding had
two arms, both leaving the playback window open on a replaced terminal receipt.

1. Precheck arm.  The window is open for generation `g`; the device sends
   `playback.ended`/`playback.error` for `g` and `devicePlaybackLedger.record`
   accepts it; the terminal branch's `GenerationReplaced(g)`
   (`device_ws_uplink.go:332`) still says not replaced; Voice Core validates a
   cancel and moves its own session fence to `g+1` on the receive goroutine
   ([bridge_events.go:105](services/media_edge/bridge_events.go#L105)); the
   handler's precheck `GenerationReplaced(g)` (`device_ws_uplink.go:354`) now
   says replaced, so the receipt is dropped through the precheck path.  Before
   the fix, that path left the window alone and skipped the terminal clear
   (`clearPlaybackActive("replaced_generation_ended", receipt.Fence)`, `:344`).
2. Ledger-refused arm.  The same terminal receipt is refused by
   `devicePlaybackLedger.record` (sequence regression, duplicate terminal, or an
   abstracted anchor miss); before the fix, the handler returned at
   `device_ws_uplink.go:310` to `:331` before reaching the terminal clear, so
   the window it owned stayed open.

`generation.cancelled` deliberately does not clear either
(`device_ws_downlink.go:290` to `:299`).  Impact: a stale `playbackActive` makes
the next `vad.start` a barge candidate that is dropped unless the voice barge
source is signed (`device_ws_uplink.go:149` to `:152`); the child can speak into
a window the device already finished rendering until a device stop, a keyword
hard stop, a later signed receipt, a reconnect, or the session close clears it.
A device button stop cannot hit the precheck arm: it runs on the same read
goroutine.

The fix applies a terminal-only `clearPlaybackActive("replaced_generation_receipt",
receipt.Fence)` in the ledger-refused path (`:321` to `:323`) and the precheck path
(`:355` to `:357`).  The same-fence check inside
`clearPlaybackActive` leaves a window owned by another generation untouched.
Dropped `started`/`progress` receipts do not clear a window because the device may
still be rendering that generation and the window remains authoritative for barge
gating.  The repaired model has 27,759 distinct states, and
`TerminalReceiptEndsOwnWindow` is now part of the primary configuration.

## Mutation evidence (why the invariants bite)

`--mutants` applies one plausible regression at a time to the spec and requires
TLC to report a named invariant, or a required action to become unreachable,
within seconds:

| mutant | regression | caught by |
| --- | --- | --- |
| `M0_ledger_refusal_no_drop` | ledger-refused late receipt still closes (pre-2026-09-30) | `NoCloseForReplaced` |
| `M1_no_precheck_no_forward_recheck` | no precheck and no forward-stage recheck (pre-2026-10-02) | `NoCloseForReplaced` |
| `M2_edge_authority_ignored` | `GenerationReplaced` ignores Voice Core's fence | `NoForwardForReplacedAtBegin` |
| `M3_terminal_clear_ignores_fence` | terminal replaced receipt clears another fence's window | `TerminalClearOnlyOwnWindow` |
| `M4_forward_drop_keeps_window` | forward-error drop no longer closes its own window | `ForwardDropClosesOwnWindow` |
| `M5_precheck_drop_clears_window` | precheck drop clears a foreign window | `DropKeepsForeignWindow` |
| `M6_overdrop_replaced_true` | `Replaced` over-approximates and drops live receipts | `DropsOnlyForReplaced` |
| `M7_openwindow_update_removed` | accepted receipt no longer opens/closes the window | `WindowMatchesForward` |
| `M8_terminal_clear_removed` | terminal replaced receipt no longer ends its own window | `TerminalClearEndsOwnWindow` |
| `M9_underdrop_replaced_false` | `Replaced` under-approximates and forwards/closes stale receipts | `NoCloseForReplaced` |
| `M10_live_terminal_keeps_window` | live ended/error receipt no longer closes the window | `WindowMatchesForward` |
| `M11_core_authority_ignored` | `GenerationReplaced` consults only the Edge fence | `NoCloseForReplaced` |
| `M12_forward_fence_check_removed` | `SendPlaybackProgress` no longer checks Voice Core's fence | `ForwardAcceptedAtCoreFence` |
| `M13_terminal_branch_removed` | the 2026-09-29 terminal-replaced branch is gone | coverage: `DropTerminalReplaced` unreachable |
| `M14_device_stop_removed` | the device-stop edge-first state is unreachable | coverage: `DeviceStopEdgeFirst` unreachable |

M0 and M1 replay the real pre-fix code; the spec text that remains when their
guards are removed is what the Go code looked like before each fix.  M2 and M11
remove one of the two authorities the 2026-10-02 fix added, one direction each.

## Second target (close, not covered here)

`handleButtonStop` (`device_ws_uplink.go:228`) calls `CancelGeneration` with the
device's expected fence.  When that fence has already been replaced, the call
fails, the edge sends `stop_rejected`, and the WSS closes; the keyword hard stop
(`handleKeyword`, `device_ws_uplink.go:165`) already ignores that stale case.
TODOLIST records it under "形状相同但没动" (2026-09-30, `button.stop …
expected_fence.generation=2`).  Extending this model with the rejected-stop path
and the property "a stop whose expected generation is already replaced never
closes the connection" would cover it; the current model deliberately models
only the successful device stop.

## Keeping this in step with the code

- When a covered function changes, re-read it, adjust the corresponding TLA+
  action, re-run `--mutants` (fix mutation anchors if they no longer match),
  update `EXPECTED_BASE_DISTINCT` if the state space changed, then update this
  coverage record and the sync commit.
- The ds-pstack `maintain-formal-verification-skill` brings this verification
  back in line after changes that did not recheck it.  It ships with the
  ds-pstack pack (on this profile at
  `~/.dsh/profiles/desktop/node_modules/@local/ds-pstack/skills/maintain-formal-verification-skill/SKILL.md`)
  rather than in this repository.
- Files: `services/media_edge/formal/LateReceipts.tla`,
  `services/media_edge/formal/LateReceipts.cfg`,
  `services/media_edge/formal/LateReceiptsFinding.cfg`,
  `.cursor/skills/verify-media-edge-late-receipts/bin/verify`.
