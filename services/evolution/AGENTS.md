# services/evolution — self-evolution contracts (score 15)

## OVERVIEW
Auditable offline self-evolution: candidate artifacts move candidate→validated→canary→stable through validation gates, curated by a sleep-cycle control plane. Never patches live code.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Lifecycle, gates, rollback (SQLite) | `store.py` — `EvolutionStore`, inline `_SCHEMA` |
| PG facade (prod) | `postgres_store.py` — `PostgresEvolutionStore(EvolutionStore)`, thread bridge |
| Data contracts (pure, no I/O) | `domain.py` — `CandidateArtifact`, `LearningSignal`, `ValidationReport`, `REQUIRED_VALIDATION_GATES` |
| Release curation | `curation.py` — `EvolutionControlPlane`, `SleepLearningPolicy` |
| Runtime resolution | `resolver.py` — `EvolutionResolver`; signed receipts in `receipt.py` |
| Gate checks | `verifier.py` — `verify_trajectory` |
| Account-scoped writes | `account_fence.py` — guard classes; `account_repository.py` — 6 account tables |
| Sleep worker | `worker.py` — `EvolutionSleepWorker` (task `evolution-sleep-cycle`) |
| Release decision | `longitudinal_evaluation.py` — `decide_evolution_release` (no LLM call) |
| Holdout dataset | `evaluation/evolution_holdout_zh_v1.json` (10 cases, v1) |
| PG schema | `postgres_schema.sql` — mounted as `007-evolution-schema.sql`, role `memoria_evolution` |

## CONVENTIONS
- Scope model on every carrier: `owner_private` (requires account_id) vs `global_redacted` (account_id must be NULL); enforced in dataclasses, SQLite CHECK/triggers, and PG triggers.
- Canonical identity: `canonical_json` (sorted keys, compact separators, `allow_nan=False`) + sha256 hex; IDs uuid4/uuid5. `Literal` type aliases, never Enums.
- Store contract is a base class, not a Protocol: `PostgresEvolutionStore` reuses `store.py` private row helpers — keep SQLite/PG semantics shared.
- Frozen `@dataclass(frozen=True, slots=True)` records validated in `__post_init__` with `ValueError`.
- Account guards injected as constructor kwargs, no-op by default; control_api wires real fences.
- Runtime activation limited to low-risk prompt carriers for allowlisted task families (default `{"weather"}`).
- `__init__.py` re-exports the whole surface (64 names in `__all__`).

## ANTI-PATTERNS
- Global-redacted prompt payloads are allowlist-only; `diagnosis` text is excluded by design (can contain a guest transcript). Never add it back.
- Match terms in `diagnosis.py` are routing labels, never learned instructions.
- `EvolutionControlPlane` must never change stable prompt/tool/validator/release files.
- Resolver cannot hot-patch code or permissions; knowledge stays data, skills via SkillExecutor, harness/parameter need a normal code release. Fail closed to `()`.
- Verifier never asks an LLM to guess environment truth or permissions; harness-failure prefixes (`forbidden_tool:`, `tool_not_allowlisted:`) must never be relabelled by an evaluator.
- Only subject category `adult` may write account-scoped evolution; the account fence is never removed mid-lifecycle.
- Support uniqueness keys on evaluation, not candidate version — never manufacture rows by changing evaluation_id.
