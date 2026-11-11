# 五项软件候选增量，2026-11-11

五项完整软件验收**尚未完成**。本分支交付下表已验证增量，不构成发布放行。 No hardware, production, paid model, external notification, push, PR, or merge operations ran.

Base `23f75b4d7eb05f587ca7c44a27273e5a2a9a0d55`. Worktree `/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9`, branch `t3/five-software-tasks-acceptance`. The original checkout and its two protected uncommitted files were not used as implementation input.

## Workflow and completion predicates

Read the actual callers and persisted shape. Reproduce before changing behavior. Verify each increment and inspect the diff before committing. Freeze production source before the 900-second window. Receipts live in [five-software-20261111](five-software-20261111/). The decision trail is [decisions.tsv](five-software-20261111/decisions.tsv).

| Task | Implemented and verified increment | Missing from the requested completion predicate |
| --- | --- | --- |
| P0-04 | R2 reproduces stale signed private capability after real PG consent expiry/revocation and rejects that profile in Session current authority. New dual HTTP entry, age/retention, concurrent switch, projection divergence, authority failure and manager rebind controls. Signed bytes remain unchanged. | Independent cross-provider review remains with the parent. Retention consent itself cannot expire under the existing product contract; the matrix rejects that invalid grant and tests natural expiry on runtime private-recall consent. |
| P2-06 | U7 requires comfort and parent guidance. Initial secrecy agreement and permanent companionship promises are separate failures. Bare four-value emotion tags are removed at streaming/TTS boundaries, ordinary brackets survive. Old bundle replay is 19/40 and 1/8, still failing. | New continuity/recap age-by-retention matrix, unseen continuity set and end-to-end planner timeout evaluation remain. Existing continuity tests were run but do not prove the requested new coverage or wording quality. |
| P1-06 | Four English proper-name atomic claims compile and retrieve through real PG using a named deterministic extractor, with metric receipts. Existing fixed/unseen evaluation and planner client tests pass. | Source projection duplicate reproduction and dedup implementation, SQLite parity assessment, expanded catalog-limit/subject-switch planner HTTP evaluation remain. No production embedding change or backfill. |
| P1-05 | Python bridge fills per-connection client sequence. Edge stores phase, capture state, fence, stream and arrival time. Old sequence/stream/generation rejected; generation changes and reconnection clear state. Existing internal GET carries snapshot. Control exposes protected GET /v1/devices/{id}/session-state with 15-second freshness and offline/unknown states. | A real authenticated Go HTTP test also proves snapshot projection and reconnect clearing. One combined Go HTTP to Control HTTP test, runtime-profile expiry at the read endpoint, and session-only non-device access are not covered. Control HTTP tests use a reader double; Python gRPC and Go protocol tests run independently. No client UI or device acceptance. |
| P2-04 | Explicit 900-second real Edge HTTP/WSS test process window with a synthetic Voice Core, connection open/close cycles, race detector, resource/latency series and drain checks. Actual govulncheck and Trivy scans ran. | Three separately running Control, Python bridge and Edge services with bridge-restart/backpressure faults over the same window remain. Scan gate fails on reported vulnerabilities. This software window does not prove production bridge or device stability. |

## Commits

| Increment | Commit |
| --- | --- |
| P0-04 retention ceiling | fb70b035a6e870787e0bd39caba4b5c7e321293c |
| P2-06 screening and tag filtering | 8f1c6f22602cc5e9619b312b478822fb51833606 |
| Software window runner | e8348be7304cd600304b5365a0b353fef8417426 |
| Bracket flush correction | 4f00375e2abeef0ce558f1dd65cffca653346035 |
| P1-06 atomic claim evaluation | a4ab93e49f1bd770aff62ac459af74a25d3ea64f |
| P1-05 phase read path | 2a6f778adc4f25360eda0542301bda27147fec47 |
| Correct Darwin RSS sampling | 71c5131a08a5aca438e54fd4102df037a95ee3e5 |
| HTTP, tag and RSS verification controls | 355ae232 (tests only) |

The 900-second window freezes production source at `71c5131a08a5aca438e54fd4102df037a95ee3e5`. Subsequent receipt and documentation commits do not change the tested production source. [config.json](five-software-20261111/soak-final/config.json) records the binary hash and dirty receipt files.

## Reproduce the checks

Start a disposable local pgvector PostgreSQL 17 instance. Set `MEMORIA_TEST_POSTGRES_DSN` to that instance's admin DSN. The repository harness creates and drops isolated template/cloned databases and uses production-shaped roles. Never point it at shared or production data.

```sh
uv sync --frozen --all-extras
uv run pytest services/control_api/tests/test_session_policy_retention_ceiling.py
uv run pytest services/control_api/tests/test_policy_chain_runtime_profile.py services/control_api/tests/test_policy_chain_subject_matrix.py services/control_api/tests/test_policy_chain_fail_closed.py services/control_api/tests/test_multi_subject_policy_decisions.py
uv run pytest services/companionship/tests services/agent/tests/unit/test_assistant_emotion.py services/agent/tests/unit/test_phrase_segmenter.py
MEMORIA_ATOMIC_EVAL_REPORT=/tmp/atomic-eval.json uv run pytest services/archive/tests/test_atomic_claim_unseen_evaluation.py
uv run pytest services/archive/tests/test_memory_evaluation.py services/agent/tests/unit/test_response_planner_client.py
uv run pytest services/control_api/tests/test_assistant_snapshot.py services/agent/tests/unit/test_grpc_bridge.py
go test -C services/media_edge -race -count=1 ./...
uv run pytest scripts/tests/test_local_process_probe.py services/agent/tests/unit/test_bridge_process_exit_injection.py
uv run ruff check .
uv run mypy services --strict
uv run python scripts/check_module_budget.py check
go test -C services/media_edge -c -race -o /tmp/edge.test
uv run python -m scripts.software_soak_window --binary /tmp/edge.test --output /tmp/unique-soak-receipt
```

The process sampler currently uses macOS `lsof`. The output directory must not exist. It records actual process RSS and numeric descriptors, while the Go log records cycle latency, Go heap, goroutines, connections and leases after drain. Between-cycle sampling observes closed sockets; it does not measure sustained live traffic or backpressure.

## Recorded bundle and semantic controls

The read-only old bundle SHA-256 remains `3fd4ec5762f95fb09db2d9fbc65171180246654b348d0feb4c1467105db59b97`. [Before](five-software-20261111/p2-before.json), [after](five-software-20261111/p2-after.json), and [all 40 verdicts](five-software-20261111/p2-changes.tsv) are separate receipts. The original bundle and reports were not changed.

Eight failures become passes: U1#1-4, U2#3, U4#1-3. U5#4 becomes a failure for the unbounded promise, independently of its persona wording. The net result is seven additional passes, not the design's predicted 14/40. U4#4/#5 and U6#5 remain failures. U3 and U7 remain 0/5. Every case still needs 5/5, and the hard length limit remains three sentences.

Independent controls cover U7 comfort alone, guidance alone, both together, and coercive purchase guidance. Permanent availability promises fail while a bounded conversation with the words AI 助手 can pass. These are deterministic screening controls, not a general semantic model or evidence of new model wording quality.

## Atomic memory fixture

[atomic-eval.json](five-software-20261111/atomic-eval.json) names `memoria-postgres-synthetic-atomic-tool-no-embedding`. Fixture values are Qdrant, PostgreSQL, Obsidian and OpenTelemetry. They are atomic claim values, not entire first-person source sentences. Each owner query has a paired other-account query. The fixture does not use real user data, production embeddings or a paid extractor. The existing evaluator's forbidden_text means forbidden persistence across the whole case; cross-account isolation instead uses empty relevance and the actual leakage metric.

## Scans and measurement corrections

[govulncheck.txt](five-software-20261111/govulncheck.txt) exits 3. It reports reachable `GO-2026-6859` in Go 1.27.3 net/http, fixed by Go 1.27.4. [Trivy summary](five-software-20261111/scan-summary.json) exits 1 and records 81 distinct scanner advisories across go.mod and uv.lock. Findings are scanner reports, not individually confirmed exploitability. Dependency upgrades are not part of this branch.

Trivy source installation failed Go checksum verification. Verification was not disabled. The official macOS binary was downloaded separately and checked against the published SHA-256 list; the digest receipt is [trivy-download-sha256.txt](five-software-20261111/trivy-download-sha256.txt). Tools and vulnerability databases were placed under /tmp. No global configuration or business dependency changed.

The first 146-second [preflight](five-software-20261111/soak/result.json) was terminated and is not acceptance. It exposed the Darwin probe reading CPU system time at byte 24 as RSS. The local SDK places resident bytes at byte 8. The corrected probe is checked against ps RSS units. Only soak-final uses the corrected probe.

## Verification limits

No formal cross-provider review ran in this implementation child. The parent must review the actual diff and arrange independent review. Project task checkboxes stay open. Hardware, device photos, phone/browser UI, deployment, online auth gateways, actual production bridge, actual Qwen/embedding output, professional crisis script review and external notification delivery remain unverified.

## Principles applied

Model the Domain put phase, capture state, sequence and fence in one snapshot. Prove It Works required real PG, real gRPC, Go race execution and the RSS comparison with ps. Build the Lever produced a rerunnable explicit software-window runner. Sequence Work into Verifiable Units kept behavior changes in separately inspectable commits. Technical Writing and Unslop shaped this evidence report. The prescribed child review was left to the parent because this task forbids recursive delegation.

## Verification receipts

| Check | Result | Receipt |
| --- | --- | --- |
| Real PG policy matrix | PASS, 18 tests, no skips | p0-pg.log |
| Integrated archive, companionship, Control and Agent selection | PASS, 577 tests, 1 skip | integrated-tests.log |
| Runtime, consent and actual Python bridge exit injection | PASS, 456 tests, no skips | runtime-consent-tests.log |
| Memory fixed/unseen plus atomic fixture and planner client | PASS, 63 tests | p1-memory.log |
| Python gRPC plus Control snapshot projection | PASS, 33 tests | p1-phase-python.log |
| Go package race suite | PASS | go-race.log |
| New authenticated Go HTTP snapshot and reconnect test | PASS | p1-http-go.log |
| Final streaming and TTS controls | PASS, 34 tests | p2-tags-final.log |
| Darwin process probe | PASS, 7 tests, 2 Linux-only skips | process-probe.log |
| Mini Program tests after existing lockfile dependencies installed | PASS, 670 tests, 1 skip | miniprogram-tests.log |
| Ruff | PASS | lint.log |
| Strict mypy | PASS, 593 source files | typecheck.log |
| Module budgets | PASS, 33 modules, unchanged budgets | module-budget.log |
| Go mediaruntime build and Python changed-module compilation | PASS | Commands recorded below |
| govulncheck | FAIL, exit 3 | govulncheck.txt, govulncheck.exit |
| Trivy | FAIL, exit 1 | trivy.json, scan-summary.json |

```sh
go build -C services/media_edge -o /tmp/memoria-five-mediaruntime ./cmd/mediaruntime
uv run python -m compileall -q services/control_api/app/assistant_snapshot.py services/agent/src/orchestration/assistant_emotion.py
npm ci --ignore-scripts --no-audit --no-fund --prefix apps/miniprogram
node --test apps/miniprogram/tests/*.test.js
(cd services/media_edge && /tmp/memoria-five-tools/govulncheck ./...)
/tmp/memoria-five-tools/trivy filesystem --scanners vuln --cache-dir /tmp/memoria-five-trivy-cache --skip-dirs .venv --skip-dirs .git --skip-dirs outputs --skip-dirs firmware/.cache --format json --output /tmp/trivy.json --exit-code 1 .
```

The initial Mini Program run failed because this fresh worktree lacked its declared miniprogram-ci dependency. The lockfile install fixed the environment without a manifest edit or any upload. The RSS cross-check initially raced child startup; waiting for the child ready line fixed the fixture without changing its assertions. Tests that do not apply on Darwin remain explicitly skipped.

The single software window runs a race-instrumented test binary with a synthetic core on a shared 12-core machine. Observed load averages during the run were 16.91, 13.45 and 8.20. Cycle timings include connection setup and closure, not ASR, TTS, model generation or sustained audio. They are observations, not a performance comparison or capacity claim.

The summary command rejects a missing end receipt and the terminated preflight. After the final process exits, rerun `uv run python -m scripts.summarize_software_soak docs/reviews/five-software-20261111/soak-final`. The result distinguishes window completion, resource readability, cycle drain and the unknown production/memory-leak boundaries.

Process sampling begins during loader startup. The minimum RSS includes that initial observation; first-to-last RSS is not a leak test. No production source differs between the frozen window candidate and the subsequent test-only commits.

## Completed window

The test ran for 901.69 seconds and the sampler process completed in 905.67 seconds, exit 0. The receipt contains 179 resource samples and 180 completed connection cycles. Every cycle drained to zero connections and zero leases. Final FD count is 5. The synthetic-core cycle p50 is 8.089 ms and p95 is 10.109 ms, measured once on the shared, race-instrumented host. RSS ranges include loader startup, so no memory-leak absence is claimed.

[Summary](five-software-20261111/soak-final/summary.json), [raw series](five-software-20261111/soak-final/process.jsonl), [Edge log](five-software-20261111/soak-final/edge.log), [configuration](five-software-20261111/soak-final/config.json), and [terminal receipt](five-software-20261111/soak-final/result.json) are retained. Both disposable test containers were stopped and removed. The existing unrelated container was not touched.

## R2 P0-04 authority correction

The stale-capability reproduction is [p0-before.log](five-software-20261111/r2/p0-before.log). The real PG runtime decision denied recall after consent expired or was revoked, while session-policy still exported the signed private capability. Session current now discovers the current exact private-recall consent through the existing action authority and refuses the obsolete profile. It does not rewrite signatures, issue another capability projection, or persist a decision on a read.

The new `test_policy_authority_r2.py` covers 15 independent cells. Three cells use a genuinely issued adult private-recall profile and real consent evidence, with valid, naturally expired and revoked states. Twelve cells cross under_14, 14_17, adult and unknown_safe with valid, revoked and invalid-expiry retention requests. `memory_retention` explicitly prohibits expiry, so that cell asserts the existing domain rejection rather than inventing an expiring retention grant. The runtime private-recall grant supplies the natural expiry control.

Every age cell drives session-policy, policy/decisions and response-plan, then races subject switches, rejects the old profile and corrupts the durable projection. Device, binding version, subject and epoch divergences are represented by the four age cases. The valid adult cell injects an unavailable consent discovery boundary and performs an actual binding-version-2 manager takeover in the disposable PG database. No entire authority service is stubbed. Existing real catalog and legacy manager-change controls remain part of the acceptance command.

R2 resources use the explicitly local disposable `memoria-r2-20261111` pgvector container on 127.0.0.1:55443. Guardian and runtime fixtures use random databases and actual application roles. All data is synthetic. No notification sender, remote host, production database or paid model is invoked.
