# services/self_model — cognitive claims & relationship profiles (score 12)

## OVERVIEW
The model of the user's self: cognitive claims, decision cases, relationship profiles with a review lifecycle, one activation policy, PG registry.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Entities + `SelfModelRegistryPort` | `domain.py` — `CognitiveClaim`, `DecisionCase`, `RelationshipProfile`, `SelfModelSource` |
| Activation policy (single source) | `policy.py` — `activation_decision`, `is_effective`, `HIGH_SENSITIVITY_CLAIM_TYPES` |
| PG adapter | `postgres_registry.py` — `PostgresSelfModelRegistry` (~35 methods; `_review_item`, `_add_sources`, `_trusted_source`, `revise_relationship_profile`) |
| Schema | `postgres_schema.sql` — 6 tables + audit, RLS + guards |
| Tests | `tests/test_self_model_registry.py` (754), `tests/test_self_model_postgres_registry.py` (491) |

## CONVENTIONS
- Policy separated from adapter: registry never re-implements `activation_decision`.
- Tenant isolation in SQL: FORCE RLS per account, `_scope`/`_lock_account` helpers, `CHECK (actor_account_id = account_id)` on audit.
- Status casing: relations use `support`/`counterexample`; adoption `CHECK (NOT adopted OR relation = 'support')`.
- Imports `SpeakerClass` from `services.archive.domain`.
- `HIGH_SENSITIVITY_CLAIM_TYPES` = frozenset `{value, decision_rule, red_line, conflict, support}` drives `activation_decision`.
- `SelfModelItem` is a union alias re-exported from `__init__.py`, not defined in `domain.py`.
- Consumed by control_api wiring and `growth/postgres_reader.py`.
- Schema read from source at test time (`parents[1] / "postgres_schema.sql"`); guard/trigger names asserted as static text.

## ANTI-PATTERNS
- Guard/trigger names (`self_model_relationship_profile_guard`, `self_model_audit_immutable_guard`) are test-asserted contracts — renames break `test_postgres_schema_security`.
- Unique profile constraint shape `(account_id, person_id, version_number)` is asserted by tests; never hand-edit it.
- Untrusted sources raise `UntrustedSelfModelSourceError` — never widen source trust silently.
- Activation of high-sensitivity claim types stays policy-only; no ad-hoc activation checks in the registry.
- Unit tests never instantiate a parallel PG store: the adapter has its own contract suite.
- `SourceInput`/`SourceRelation` untrusted inputs fail closed (`UntrustedSelfModelSourceError`) — no silent coercion.
