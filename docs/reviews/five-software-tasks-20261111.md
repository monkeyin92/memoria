# 五项软件候选增量，2026-11-11

五项完整软件验收**尚未完成**。本分支交付下表已验证增量，不构成发布放行。 No hardware, production, paid model, external notification, push, PR, or merge operations ran.

Base `23f75b4d7eb05f587ca7c44a27273e5a2a9a0d55`. Worktree `/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9`, branch `t3/five-software-tasks-acceptance`. The original checkout and its unrelated uncommitted files were not used as implementation input.

## Workflow and completion predicates

Read the actual callers and persisted shape. Reproduce before changing behavior. Verify each increment and inspect the diff before committing. Freeze production source before the 900-second window. Receipts live in [five-software-20261111](five-software-20261111/). The decision trail is [decisions.tsv](five-software-20261111/decisions.tsv).

| Task | Implemented and verified increment | Missing from the requested completion predicate |
| --- | --- | --- |
| P0-04 | R2 reproduces stale signed private capability after real PG consent expiry/revocation and rejects that profile in Session current authority. New dual HTTP entry, age/retention, concurrent switch, projection divergence, authority failure and manager rebind controls. Signed bytes remain unchanged. | Independent cross-provider review remains with the parent. Retention consent itself cannot expire under the existing product contract; the matrix rejects that invalid grant and tests natural expiry on runtime private-recall consent. |
| P2-06 | R2 adds 16 new cases for PG task-summary continuity, authorized device tutoring and real recap timeout fallback. Fixes device-bound tutor decisions being denied when the signed profile lacks private recall. Previous screening changes, three-sentence and per-case 5/5 gates remain intact. | Automatic model continuation from the stored plan and real model wording quality remain unverified. The software continuity claim is a stored task summary available on the next session through the authorized read gate. Old model bundle still fails 19/40 and 1/8. Parent owns cross-provider review. |
| P1-06 | R3 implements exact equivalent-projection selection in the real Planner recall caller, preserves independent claims and multi-source episodes, expands atomic fixed/unseen PG fixtures, and exercises real planner HTTP, budgets, subject switching and private consent revocation with ReplyPlan cache consumption. | Software predicate complete subject to parent cross-provider review. Real Qwen/production embedding, deployed gateway and device recall remain unverified. |
| P1-05 | R3 combines real authenticated Go runtime HTTP with real Bearer Control HTTP, SQLite activation/auth and PG Session authority. Rejects stale generation with higher sequence, missing fences and stale private profiles; proves expiry, revocation, unbind, manager takeover, disconnect and reconnect. | Software device read predicate complete subject to parent cross-provider review. Session-only non-device API is outside scope. Client UI, firmware and device acceptance remain unverified. |
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

## R2 P2-06 continuity and recap controls

[The tutor reproduction](five-software-20261111/r2/p2-tutor-before.log) starts a real device-bound PG session with a confirmed guardian relationship, tutoring consent and a signed tutor capability. At action time the old presence recovery recognized only private recall. The public decision therefore denied tutoring even though its own authority had issued the capability. `wire.py` now also recognizes signed tutoring as device-bound evidence for another subject. An actor cannot gain that signed capability for another subject through an ordinary App confirmation. Private recall is still denied when absent from the profile.

`test_minor_tutoring_r2.py` crosses both minor age bands and retention allow/deny, using actual Session and Consent PG stores and the public policy decision route. The device profile is issued through the same Session start command used by media, not injected or re-signed in the fixture. Its paired response-plan/session-policy probes use the separately opened unknown-safe HTTP voice session. Those two probes do not claim that the device session was admitted through hardware or the media gateway.

`test_continuity_recap_r2.py` extends the existing replay harness with eight new cases. Two independent unseen summaries describe learning frustration and an unfinished next-session task in fractions and phonics. Both age bands cross retention allow/deny. The internal recap HTTP route persists through real PG ChildLayerStore, then a new HTTP session retains the same subject and the guardian reads the exact paraphrased plan from session-summaries. The subject's own PG RLS read succeeds and another subject's read is empty. An unrelated management account cannot read it, quoted summary input is rejected, and revocation closes the HTTP read gate. Refusal leaves no summary row. Self-read here is a storage gate test, not a new authenticated child HTTP API.

`test_guardian_recap_timeout_r2.py` adds four age-by-retention cases. The shipped guardian recap route and actual recap function receive a precise HTTP transport ReadTimeout. The fallback returns 200, counts only, no transcript and no highlights. Consent denial returns 403 without reaching that transport. An empty day returns the existing 404 with no model attempt. The source row is an explicitly synthetic historical retained row, since new minor events with subject_category correctly withhold raw text. Zero external requests run.

The deterministic summaries prove task-state persistence and read permission, not successful model-generated continuation or conversational warmth. The current product exposes dialogue summaries and whitelisted archive facts; it has no separate unfinished-plan record or child self-review HTTP surface. This change does not invent those product contracts. The recorded model bundle, its SHA and earlier receipts are untouched.

## R2 verification and commits

P0-04 commit `2fc62014` rejects obsolete private profiles and adds the 15-cell real-PG dual-entry matrix. P2-06 is committed separately after the checks below. The final source candidate passed 147 related tests with no skips, including 31 new cases, plus the existing real catalog, consent, profile and companionship controls. [acceptance.log](five-software-20261111/r2/acceptance.log) records the complete run. The subsequent private-recall negative control was verified separately in [p2-tutor-after.log](five-software-20261111/r2/p2-tutor-after.log).

```sh
MEMORIA_TEST_POSTGRES_DSN=<isolated-local-admin-dsn> uv run pytest -o addopts='' -q services/control_api/tests/test_policy_authority_r2.py services/control_api/tests/test_minor_tutoring_r2.py services/control_api/tests/test_guardian_recap_timeout_r2.py services/control_api/tests/test_policy_chain_runtime_profile.py services/control_api/tests/test_policy_chain_subject_matrix.py services/control_api/tests/test_policy_chain_fail_closed.py services/session_runtime/tests/test_postgres_action_authority.py services/session_runtime/tests/test_runtime_profile_service.py services/companionship/tests
MEMORIA_TEST_POSTGRES_DSN=<isolated-local-admin-dsn> uv run pytest -o addopts='' -q services/control_api/tests/test_minor_tutoring_r2.py
uv run ruff check .
uv run mypy services --strict
uv run python scripts/check_module_budget.py check
```

Ruff [passed](five-software-20261111/r2/ruff.log). Strict mypy [passed on 593 files](five-software-20261111/r2/typecheck.log). All 33 module budgets [passed without a budget change](five-software-20261111/r2/module-budget.log). Earlier R2 fixture runs exposed imported-fixture discovery and concurrent cluster-role password resets; final checks run serially on the disposable cluster. Failed fixture setup is not counted as a product reproduction. The retained before receipts show the actual software behavior failures.

No production deployment, push, PR, merge, paid model, external notification or device operation ran. Cross-provider review is not executed in this child. The parent must assess the actual commits and the automatic-continuation boundary before closing the wider product tasks.

## R3 P1-06 data contract and implementation

Archive-family storage is PostgreSQL-only. `services/archive/memory_catalog.py` and the archive fixtures explicitly establish that contract. The historical test named `matches_sqlite_contract` now runs PostgreSQL; it is not a SQLite archive implementation. SQLite remains authoritative for Control login sessions and device activation in this software test. No replacement SQLite archive, migration, backfill or embedding configuration was introduced.

The production compiler builds claim documents from atomic values, episode bodies from timeline titles and knowledge bodies from answer/applicability/counterexample. Episode and knowledge search bodies also include a retrieval-context suffix. Equivalent visible text is insufficient to merge sources. The new SQL selection compares the complete ordered source array, source anchor, exact title, visible body, domain, sensitivity and validity. Separate claim IDs always remain separate, including identical values under different predicates. Multi-source episodes remain separate from their individual claims.

`MemorySearchQuery.distinct_facts` selects this behavior before SQL LIMIT. The actual companion Planner caller enables it with catalog limit 8. The directory search and review callers retain their projection-enumeration contract. The initial global implementation failed six existing archive checks, including the pinned recall metric; [regression.log](five-software-20261111/r3/regression.log) preserves that failure. The revised implementation leaves those assertions intact.

[projection-metrics.json](five-software-20261111/r3/projection-metrics.json) records 10 raw projections and 7 distinct results across three sources. All six independent claims and the three-source episode survive. The source ratio changes from 0.3 to 3/7. This is an occupancy observation on synthetic compiler output, not a retrieval-quality score or a requirement to collapse all facts from one source.

The fixed and independent proper-name fixtures contain eight cases each, alongside the original four cases. The extractor is `synthetic-atomic-tool-v1`. It returns atomic `uses_tool` values through the real compiler and PG search. It is a deterministic software double with lexical retrieval and no embedding. These fixtures are not Qwen output. Their JSON receipts retain extraction precision/recall, recall and nDCG, source attribution and leakage separately.

`test_planner_catalog_r3.py` uses the real ResponsePlannerClient, ASGI HTTP application routes, persistent Session authority and real PG catalog. Twelve confirmed facts produce eight directory context items and four plan items. The same account switching to a child receives none. Real `ReplyPlanMixin.cache_response_plan` and `response_plan_authorized` consume the fetched plan using a local holder for the actual current profile. This proves the plan parsing and cache authorization methods; no model generation, TTS or complete live voice pipeline runs. A delayed catalog operation hits the actual 0.25-second route budget, followed by a restored positive control. A separate actual TCP server checks the client's 0.3-second network timeout and a successful response.

The revocation control uses a separate self-use session with a real private-recall grant and actual consent heads. A confirmed Milvus claim is visible before revocation. The same user's new request after revocation returns the existing safe 200 plan with zero grounded items and source refs. A second account's Qdrant claims remain invisible. The cache consumer accepts the issued profile and rejects the old plan after its holder loses that authority. The child matrix seeds an actual confirmed guardian relationship in PG before switching; a fixture without that relationship correctly fails consent discovery and is not a valid admission control.

## R3 P1-05 combined state chain

`test_assistant_http_r3.py` compiles a race-instrumented Go test binary and launches a real internal HTTP listener. Its device transport uses the existing software WSS admission and core-event forwarding, with correctly signed synthetic device claims. The Python test sends the real assistant-state JSON/protobuf envelope, then calls the actual Control HTTP route. Control makes an authenticated network GET to Edge and calls the existing PG Session current authority. It does not use a runtime reader double. Bearer JWT validation, persisted auth-session activity and SQLite device activation run without an authentication override. Missing Bearer is 401 and another account is denied.

Four independent cells cover profile expiry, consent revocation, binding revocation and actual version-2 manager takeover. Every cell first proves a matching online snapshot, exact session/stream/generation/sequence, and rejection of wrong-session, old-stream and out-of-order events. The old-generation-with-higher-sequence failure is preserved in [generation-before.log](five-software-20261111/r3/generation-before.log). Edge now compares a new observation with both the media fence and its already accepted snapshot. A generation that has not yet advanced the media fence cannot roll the snapshot backward.

After authority withdrawal the public response is unknown and contains no private snapshot. Disconnect is offline. Reconnect starts unknown. Wrong internal token and a stopped HTTP server return unknown. Pure projection controls cover the 15-second freshness boundary, naive and future timestamps, unknown phases, missing fences, nonpositive sequences and malformed JSON shapes. Existing Python gRPC tests retain the actual per-connection sequence checks. No timer, subtitle inference, alternate cache or new session-only API was added. Connection-level diagnostics retain their existing contract.

The fixture commands exist only in a Go `_test.go` listener. They are absent from the built mediaruntime binary. A normal Go package run skips that one opt-in fixture entry; Python explicitly launches it four times and checks its race-instrumented exit status.

## R3 verification receipts and boundaries

The revised integrated regression selection passed 185 tests with no skips in 68.68 seconds. The final expanded selection passed 55 tests with no skips in 32.39 seconds. These selections overlap; they are not 240 unique tests. The final selection includes the real-consent planner cell, four compiled Go HTTP cells and 36 existing Agent interaction-mode cases. The latter selection emitted one pending `interaction-delegation-start` task diagnostic after the tests passed. That resource-lifecycle diagnostic is retained in [acceptance-final.log](five-software-20261111/r3/acceptance-final.log); it is not evidence of a clean live voice lifecycle.

[regression-final.log](five-software-20261111/r3/regression-final.log) retains the 185-test check. The fixed and unseen eight-case atomic receipts each measure extraction precision and recall 1.0, recall@5 and recall@10 1.0, nDCG@10 1.0, cross-account leakage 0 and candidate leakage 0. Cross-session and comfort metrics remain 0 in these exact-fact fixtures and are not claimed as coverage. The old fixed dataset's separate pinned metrics remain unchanged and its regression test passes. [Projection metrics](five-software-20261111/r3/projection-metrics.json) separately retain source occupancy.

Go `-race -count=1 ./...`, mediaruntime build, strict mypy on 593 source files, Ruff and all 33 unchanged module budgets passed. The real cross-language fixture is explicitly executed four times outside the ordinary Go suite. Receipts include `go-http-expired.log`, `go-http-revoked.log`, `go-http-unbound.log` and `go-http-manager.log`, each ending in PASS. [go-build.json](five-software-20261111/r3/go-build.json) identifies the final binary.

Commands run from the isolated worktree, with `MEMORIA_TEST_POSTGRES_DSN` pointing only to this run's disposable local PostgreSQL 17 pgvector container. A temporary local wrapper supplied that DSN from the container metadata. The underlying commands are recorded here without the synthetic database password.

```sh
.venv/bin/pytest -o addopts= -q services/control_api/tests/test_assistant_http_r3.py services/archive/tests/test_memory_catalog.py services/archive/tests/test_postgres_memory_catalog.py services/archive/tests/test_memory_evaluation.py services/control_api/tests/test_policy_authority_r2.py services/control_api/tests/test_policy_chain_subject_matrix.py services/agent/tests/unit/test_grpc_bridge.py services/agent/tests/unit/test_response_planner_client.py services/archive/tests/test_projection_equivalence_r3.py services/control_api/tests/test_planner_catalog_r3.py services/control_api/tests/test_assistant_snapshot.py services/agent/tests/unit/test_planner_network_r3.py
.venv/bin/pytest -o addopts= -q services/archive/tests/test_atomic_claim_unseen_evaluation.py services/archive/tests/test_projection_equivalence_r3.py services/control_api/tests/test_planner_catalog_r3.py services/control_api/tests/test_assistant_http_r3.py services/control_api/tests/test_assistant_snapshot.py services/agent/tests/unit/test_planner_network_r3.py services/agent/tests/unit/test_interaction_mode_agent.py
go test -C services/media_edge -race -count=1 ./...
go build -C services/media_edge -o /tmp/memoria-r3-edge ./cmd/mediaruntime
.venv/bin/mypy services --strict
.venv/bin/ruff check .
.venv/bin/python scripts/check_module_budget.py check
```

Set `MEMORIA_ATOMIC_EVAL_REPORT` to an absolute output prefix to write dataset-specific JSON reports. `MEMORIA_R3_DEDUP_REPORT` selects the source-occupancy JSON, and `MEMORIA_R3_GO_REPORT_DIR` selects the existing directory for compiled fixture logs. These options change receipts only.

Model quality, production embeddings, actual online gateways, deployment, firmware, Mini Program UI, phone and device behavior remain unverified. No production data, SSH, serial port, external sender, paid model, push, PR or merge was used. The parent owns formal cross-provider review. The broader TODOLIST device/product items remain open even though these two software predicates are complete.

Model the Domain kept fact identity separate from source identity and connection snapshots separate from persistent authorization. Prove It Works required real PG and both HTTP applications, then rejected an actual stale-generation overwrite. Those principles changed the implementation boundaries and its acceptance tests. Recursive Skill workflows and independent review were not invoked because this implementation brief forbids further delegation.

R3 implementation commits are separate. `3e747f50` implements P1-06. The following `fix(runtime)` commit implements P1-05 and carries the final shared receipts. Receipt log trailing whitespace is normalized for Git checks; test results are unchanged.
