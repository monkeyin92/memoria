# Agent Unit Tests (services/agent/tests/unit)

## OVERVIEW
212 pytest modules pinning voice-agent behavior; shared support modules and byte-exact golden traces, driven through the real registry/gRPC bridge with fakes only for vendors.

## WHERE TO LOOK

| Area | Files |
|------|-------|
| Shared harness hub | `media_session_support.py` (>=30 importers), `test_media_session_golden.py` (`_Harness`, `SCENARIOS`) |
| Policy/profile helpers | `runtime_profile_test_helpers.py`, `runtime_state_helpers.py`, `agent_wiring_support.py`, `production_wiring_support.py` |
| Fixtures | `conftest.py` (autouse AIGC-off) |
| Golden traces | `golden/` (8 JSON, byte-exact) |
| Largest shards | `test_provider_adapter.py` (2416), `test_media_session_playback_stop.py` (1837), `test_media_session_interruption.py` (1814), `test_media_session_turn_pipeline.py` (1809), `test_media_session_delegation_lookup.py` (1716) |
| Contract families | `test_runtime_profile.py`, `test_voice_core_contracts.py`, `test_action_policy_client.py`, `test_response_planner_client.py` |

## CONVENTIONS
- Test names are behavioral sentences stating the invariant (`test_..._never_...`); module docstrings record the field incident/date they pin.
- Explicit `@pytest.mark.asyncio` on async tests even though `asyncio_mode = "auto"` - house style.
- Named fakes (`FakeMediaProvider`) over `unittest.mock`; time is injected (`now_ns=`, `clock=`), never slept.
- Cross-test-module imports of underscore helpers are the norm; private production access is annotated (`# noqa: SLF001`, `# type: ignore[...]`).
- New media-session tests import harnesses from `media_session_support`; do not duplicate them per file.
- Golden traces: `MEMORIA_UPDATE_GOLDEN=1 uv run pytest --no-cov -q services/agent/tests/unit/test_media_session_golden.py` regenerates after an intentional behavior change only.

## ANTI-PATTERNS
- Do not flip the autouse AIGC-off default in `conftest.py`; tests exercising the notice pass their own `AigcNoticeConfig`.
- Never hand-edit golden JSON (sorted-key byte-exact format is the contract).
- No `pytest.skip`/`xfail`/`skipif`; skips only via root markers (`sqlite_only`, `hermetic`).
- Planner failure must never downgrade a crisis reply; a failing memory fetch never blocks the reply.

Earned: score 13 (212 files, shared-harness fan-in >=30 modules, ~93k lines) - distinct test-conventions domain.
