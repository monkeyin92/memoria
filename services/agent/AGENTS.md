# Memoria Voice Agent Service

## OVERVIEW
Real-time full-duplex voice agent: devices connect over the media-v1 gRPC bridge into `MediaVoiceCoreRegistry`, which drives ASR/LLM/TTS orchestration behind signed `RuntimeProfile` policy fences.

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Agent runtime, policy clients, config | `src/` (flat modules; see its AGENTS.md) |
| Media session state machine | `src/voice_core/` (12-mixin `MediaVoiceCoreRegistry`) |
| Vendor ASR/TTS/LLM adapters | `src/providers/` |
| Turn planning, interruption, projection | `src/orchestration/` |
| Shared identity/fencing contracts | `src/contracts/` (`GenerationFence` in `ids.py`) |
| Telemetry/metrics | `src/observability/` |
| Behavior pins (unit tests) | `tests/unit/` (see its AGENTS.md) |
| Vendor protocol tests (in-process mocks) | `tests/integration/` |
| Crisis scoring harness | `evaluation/` |
| DTLN denoiser models (binary, pinned) | `models/dtln/` |

Entry points: `src/media_agent_factory.py` (`build_production_media_session_factory`) and `scripts/run_media_bridge.py` at repo root; `src/heartbeat.py` exposes the health endpoint.

## CONVENTIONS
- Python 3.12 only (`>=3.12,<3.13`), managed by uv; the single root `pyproject.toml` governs this service - no per-service config.
- Absolute imports rooted at `services.agent.src...`; `services` is a namespace package; every module opens with `from __future__ import annotations`.
- Fail-closed is the default: missing/expired/unknown input degrades to `None`/deny/`unknown_safe`, never best effort.
- Code, identifiers and comments are English; Chinese domain strings (utterances, stop words, city aliases) are inline constants.
- Docstrings cite design-doc chapters, decision IDs and field incidents - read them before changing behavior.

## ANTI-PATTERNS
- Never persist child conversation audio or raw transcripts; AIGC round logs reject text keys.
- Never put session/person IDs or utterance text into metric labels or trace fields.
- Never let a memory or planner failure block the reply path.
- Bare dataclasses are never authority: only `Verified*` wrappers from the owning module count as proof.
- Policy behavior never derives from client packets or room metadata.

## COMMANDS
```bash
make install        # uv sync --frozen --all-extras
make test           # ruff + module-budget + mypy services --strict + pytest
make lint / make typecheck / make module-budget
uv run pytest services/agent/tests          # focused
make smoke          # live provider smoke (scripts/provider_smoke_test.py)
make e2e / make offline                     # scripts/run_e2e.py
```

## NOTES
- `[tool.memoria.module-budgets]` pins line ceilings downward-only for large src files; any growth fails `make test`.
- `models/dtln/` holds sha256-pinned upstream ONNX models; tests fail closed if they are missing.

Earned: service anchor for a distinct domain - the agent service subtree (>300 files) needs a routing hub.
