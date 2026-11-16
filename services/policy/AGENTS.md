# services/policy — Policy Engine & Receipts

## OVERVIEW
Deterministic, evidence-fenced multi-subject decisions (PolicyEngine) recorded as append-only PolicyReceiptV2 rows, with Postgres adapters and one production assembly seam.

## WHY THIS FILE
Score 11 (>8, distinct domain): engine/context/wiring kernel + receipts + legacy decoders; consumers include agent, control_api, session_runtime, device_fleet, consent, tutor.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Decision matrix: minor/adult/crisis/guardian branches | `engine.py` |
| Context field validation | `context.py` |
| Production Postgres adapters, 8 builder seams | `production_wiring.py` |
| Decision service + repository port | `decision_service.py` |
| Receipt V2 model, evidence fences, conflicts | `receipts.py` |
| Action/resource fences and approval snapshots | `action_fence.py` |
| Resource-scoped action authority | `action_authorizer.py` |
| Wire <-> receipt conversion | `wire.py` |
| Session batch authorization | `session_batch.py` |
| Obligation codes | `obligations.py` |
| Pool vs caller-transaction receipt repositories | `postgres_receipt_repository.py` |
| Receipt DDL: append-only, role-split | `postgres_receipt_schema.sql` |
| Legacy v1 decoders (non-authorizing) | `migration/legacy.py` |
| Test helpers / generated-contract boundary tests | `tests/fakes.py`, `tests/test_generated_producer_boundary.py` |

## CONVENTIONS
- Contracts are generated (`PolicyContext`, `PolicyDecision`, `PolicyReceiptV2` from `packages.contracts.generated.python.multi_subject_contracts`): re-export, never redefine; every new producer calls `require_policy_producer(contract_name)` before constructing output.
- Repository split by lifetime: `PostgresPolicyReceiptRepository` (pool) vs `ConnectionBoundPolicyReceiptRepository` (caller transaction, enforced by `_require_caller_transaction`).
- Schema ships as `.sql` beside the Python, loaded via `Path(__file__).with_name(...)`.
- DSN comes only from `MEMORIA_POLICY_POSTGRES_DSN`; dsn fragments raise ValueError.
- Reason codes: `minor_capability_forbidden`, `capability_forbidden`, `guardian_summary_forbidden`, `crisis_notification_forbidden`.
- SQL placeholders bound by helper closures producing `$1..$n` (e.g. `_bind_dollar`).
- Tests: absolute `services.policy.tests.*` imports; `fakes.py` is the shared factory module; Postgres tests gated on `MEMORIA_TEST_POSTGRES_DSN` ("A skip is NOT a pass").

## ANTI-PATTERNS
- Never grant UPDATE/DELETE/TRUNCATE on `policy_receipts_v2`; the row is the immutable audit record (no separate audit/outbox table); runtime roles always LOGIN, NOSUPERUSER, NOBYPASSRLS.
- New producer modules must NOT define local `PolicyDecision`/`PolicyReceiptV2`, must NOT import deprecated `PolicyReceipt`/`RuntimeProfile`, must NOT use `.model_copy(`; legacy decoders stay confined to `migration/`.
- `MINOR_HARD_FORBIDDEN` (voice_clone_use, digital_self_preview, legacy_grant_create, device_ownership_transfer): no evidence can flip these; payment / raw_audio_retention / model_training_contribution default-denied.
- `production_wiring.py` returns no validated context, boolean, or token: public authorization operations are callback-only.
- Policy owns `device_transfer` as the canonical purpose for `device_ownership_transfer`.
