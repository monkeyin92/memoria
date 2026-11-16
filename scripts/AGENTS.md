# scripts/ — Operational Toolchain

## OVERVIEW
132 standalone Python/Bash CLIs (plus 4 Node scripts for the Mini Program build): hardware acceptance, voice/software soak, release & deploy, backup/restore, data migrations, contract codegen. Earned file: score 14 (132 files, `__init__.py` boundary, high reference centrality from CI/Makefile/deploy gates).

## WHERE TO LOOK
| Task | File | Notes |
|------|------|-------|
| Contract codegen | generate_multi_subject_contracts.py | Schema → Python + Mini Program JS; CI runs `--check` |
| Hardware acceptance | hardware_realtime_acceptance.py | T1-T14 orchestrator: list/run/collect/verify |
| Agent deploy | deploy_agent_component.sh | git-archive overlay, ERR-trap rollback, `--cutover` gated |
| Control deploy | deploy_control_component.sh, release_ops.sh | release_ops runs server-side as root; state under /opt/memoria, /root/memoria-release |
| Release verification | create_release_manifest.py, verify_release_source.py, package_release_verifier.py | deterministic zip; verifier output outside release source |
| Voice soak | voice_soak.py, voice_soak_serial_logger.py, voice_soak_serial_command.py | serial logger is the ONLY process allowed on the USB port |
| ASR eval | evaluate_streaming_asr.py | reuse this; never write a third evaluator |
| Soak verdicts | voice_soak_evidence.py, voice_soak_verdict.py | offline, machine-only evidence |
| Module budgets | check_module_budget.py | downward-only; covers services/** only |
| Environment | split_production_env.py, prepare_production_upgrade_env.py | atomic 0600 writes; retired keys rejected |
| Mini Program upload | upload_miniprogram_test.js, compile_miniprogram_preflight.js | CI runtime gate |
| Tests | tests/ (52 pytest modules) | offline contract tests; see CONVENTIONS |
| Subdirs | auto_audio/, cost_ledger/, e2e/, voice_soak_scenarios/ | live-session driver + selfchecks; A17 ledger domain; 1 container e2e (not in CI); scenario banks |

## CONVENTIONS
- Every CLI: argparse, `main(argv: list[str] | None = None) -> int`, `raise SystemExit(main())`; module docstring often doubles as `--help` description.
- Mandatory repo bootstrap: `ROOT = Path(__file__).resolve().parents[1]` then `sys.path.insert(0, str(ROOT))`; post-bootstrap imports carry `# noqa: E402`. Run as `python scripts/x.py` or `python -m scripts.x`.
- Fail-closed JSON receipts everywhere: `schema_version`, evidence sha256, TTL; private data never written; `--out` must not exist; duplicate JSON keys and NaN rejected.
- Mutating tools dry-run by default; destructive ones require `--apply` or `--confirm-*`.
- Optional deps are NOT lockfile deps: `uv run --no-project --with pyserial --with sounddevice --with numpy scripts/...`.
- Bash: `set -Eeuo pipefail`, `step_*()` + `case "$1"` dispatch, exit 2 usage / exit 1 failure, never print secrets, ERR-trap rollback for cutovers.
- stdout is the machine interface (`provider_smoke_test PASS: <engines>`, `runtime_inventory_ok`, `control_component_source=PASS`); tests grep these markers — keep them in sync.
- Generated artifacts carry `DO NOT EDIT. Regenerate with: python scripts/...` headers and embed the schema sha256.
- CI rule: "A test file left off every list never ran" — every scripts/tests file must appear in a CI path or list.

## ANTI-PATTERNS
- Never let CI touch real hardware/network/production: scripts/tests fake serial, WeChat, Docker, WebSocket; lab-only drills must not claim production sign-off.
- Never weaken a refusal: settle time is entry cost (never exposure/false wake); a short or quiet take is never a pass; the wake word is never played because it false-triggers.
- Release tools fail closed: hard-coded scope allowlists, `--skip-gates` only with a filed reason, `--cutover` needs a distinct rollback target; never rebase commits backing published images.
- Never edit generated contract files by hand.
- Acceptance tooling: no PCM injection, no device/config edits, no wake/VAD threshold changes; wake word never used to wake the robot in tests (rule dated 2026-10-02).

## COMMANDS
```bash
make test            # ruff + module budget + mypy + pytest (scripts/tests NOT in testpaths)
uv run pytest --import-mode=importlib --no-cov scripts/tests   # CI runs the whole dir
python scripts/generate_multi_subject_contracts.py --check
bash -n scripts/deploy_agent_component.sh scripts/deploy_control_component.sh
uv run --no-project --with pyserial python scripts/voice_soak.py --scenario scripts/voice_soak_scenarios/child.json --bank outputs/voice-bank --serial-log outputs/serial/robot.log --out outputs/acceptance/run-<stamp>-soak
```
