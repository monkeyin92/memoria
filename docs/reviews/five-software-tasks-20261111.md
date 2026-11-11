# 五项软件候选增量，2026-11-11

## 2026-11-12 独立审查后的软件修复

S2 生命周期回归已在 `ac3eeab6` 修复，新的跨厂商复审待主 Agent 执行。[原 Gemini 审查](five-software-final-review-20261112.md)对 `b2ffc904` 的 **Changes requested** 结论、SHA 和原始发现保持不变。本轮先提交真实 PG+ASGI 复现，再修复并验证，没有自行宣告正式 Review 通过。

`current()` 和实际私有 action authority 继续 fail-closed。撤回同意后，仍有效的控制会话可以安全降级和重新签发；显式切人、剩余 chat/tutor 决策及失败启动清理恢复工作。新测试检查真实 HTTP payload、签名和 PG epoch、能力、状态。21 项新 PG+ASGI 回归、11 项收据控制、原有综合选择 801 项均通过，必需 PG 矩阵零跳过。这些计数互有包含，不相加。[R6 验证](five-software-20261111/r6/verification.json)记录精确命令。

主 Agent 复核了 `7f339217` 产生的三个新增失败。U5#2、U5#5、U8#5 都包含“不过我是 AI”；其中“不”属于转折词，未否定身份声明。新增两个身份正控制和一个转折后的否定导向负控制，修复前为 2 failed、8 passed，修复后筛查器 66 项测试全通过，正向 fixture 仍为 40/40。[新保存的旧包重评](five-software-20261111/r6/recorded-bundle-after-concessive-fix.json)恢复为 19/40 次、1/8 例，整体仍失败。原 bundle SHA、PM 判断、三句和每例 5/5 门槛不变。R6 的历史 16/40 收据保持原样，不再作为当前判定。

干净候选 `2c4d4e61163b3804debd0bc75f2b85af03822e9a` 完成 45 秒故障预检和新的 900.007511 秒三进程窗口，[R6 summary](five-software-20261111/r6/summary.json)的 16 项谓词全通过。安全扫描仍 **FAIL**。桥 RSS 增长未归因，不证明泄漏或容量。新的独立复审、真实模型、生产和设备边界仍未验收，原产品任务复选项继续保持未完成。未执行 push、PR、merge、部署、生产 SSH、设备、外部通知或付费模型调用。

Base `23f75b4d7eb05f587ca7c44a27273e5a2a9a0d55`. Worktree `/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9`, branch `t3/five-software-tasks-acceptance`. The original checkout and its unrelated uncommitted files were not used as implementation input.

## Workflow and completion predicates

Read the actual callers and persisted shape. Reproduce before changing behavior. Verify each increment and inspect the diff before committing. Freeze production source before the 900-second window. Receipts live in [five-software-20261111](five-software-20261111/). The decision trail is [decisions.tsv](five-software-20261111/decisions.tsv).

| Task | Implemented and verified increment | Missing from the requested completion predicate |
| --- | --- | --- |
| P0-04 | R6 区分私有读取与旧 profile 生命周期权威。21 项新 PG+ASGI 回归覆盖撤回后的降级、切人、决策、终态清理及错误 actor/device/binding/fence、过期和关闭状态；原矩阵继续通过。 | 新候选跨厂商复审待主 Agent 执行。Retention 本身仍不可过期，原自然到期控制测试 runtime private-recall consent。 |
| P2-06 | 原 continuity、tutor 和 recap 软件控制继续通过。S3 否定及转折控制通过，筛查器 66 项测试、40/40 正向 fixture 通过。旧 bundle 当前实测 19/40、1/8，原 PM 判断未改。 | 原模型 bundle 仍失败。自动模型续接和真实措辞质量仍未验证，独立复审待执行。 |
| P1-06 | R3 implements exact equivalent-projection selection in the real Planner recall caller, preserves independent claims and multi-source episodes, expands atomic fixed/unseen PG fixtures, and exercises real planner HTTP, budgets, subject switching and private consent revocation with ReplyPlan cache consumption. | Software predicate complete subject to parent cross-provider review. Real Qwen/production embedding, deployed gateway and device recall remain unverified. |
| P1-05 | R3 combines real authenticated Go runtime HTTP with real Bearer Control HTTP, SQLite activation/auth and PG Session authority. Rejects stale generation with higher sequence, missing fences and stale private profiles; proves expiry, revocation, unbind, manager takeover, disconnect and reconnect. | Software device read predicate complete subject to parent cross-provider review. Session-only non-device API is outside scope. Client UI, firmware and device acceptance remain unverified. |
| P2-04 | R6 在干净候选 2c4d4e61 上完成 900.007511 秒三进程窗口，180 个周期和 181 次资源采样。签名权威、真实音频、TCP、SIGKILL、慢写与旧 PCM、排空和清理通过。 | 新候选独立复审待执行。当前实际重扫门禁 FAIL。RSS 增长未归因，不宣称无泄漏、容量、部署回滚或设备 terminate-no-reconnect 通过。 |

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

No formal cross-provider review ran in this implementation child. The parent must review the actual diff and arrange independent review. Project task checkboxes stay open. Hardware, device photos, phone/browser UI, deployment, online auth gateways, deployed production bridge, actual Qwen/embedding output, professional crisis script review and external notification delivery remain unverified.

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

## R4 real-process preflight did not close P2-04

R4 did not deliver the complete three-process 900-second window. R5 below supersedes that completion status and preserves the failed receipts. The executable preflight on candidate `1941eccccaab85ee3e8833a4e67a552b2ca98b09` failed its positive Control gate. No production source or dependency changed. This is an incomplete test composition, not evidence of an insurmountable architecture boundary or a reproduced production defect. P2-04 stays open.

`python -m scripts.software_soak_r4 --output <new-ignored-directory>` builds a race-instrumented Edge test binary, creates one disposable local PostgreSQL 17 container, launches the actual `scripts.run_media_bridge` coroutine with a deterministic external provider, and launches a Uvicorn Control process. The Control device route uses actual SQLite authentication/activation and PG Session current authority. The Go device WSS RuntimeFactory connects to actual Python gRPC and registry; it never constructs `deviceTestCore`. Its signed device ticket remains synthetic and is not issued by Control. The Control application mounts the production device router using the repository test activation fixture; it is not the complete production Control composition.

[preflight.json](five-software-20261111/r4/preflight.json) associates Control PID 39478, bridge PID 39479 and Edge PID 39566 with the candidate and SHA-256 `2770e51108a58352af97ccea96b8cdfbdfa2f1665269d53852631d75eb3ab36b` for the race binary. [preflight.log](five-software-20261111/r4/preflight.log) retains the actual protobuf transcript and assistant-state envelope. The exact `你好` final transcript returns through real Opus uplink, gRPC and ASR fixture. The assistant-state envelope has `session_epoch=0`, `runtime_profile_id=null` and no actor/binding authority. Edge correctly rejects that snapshot. The real authenticated Control request returns 200 with `status=unknown`; the required online assertion fails. Missing Bearer returns 401.

The provider-only bridge creates its default DuplexRuntime without the signed PG profile. `ProductionMediaSessionFactory._bind_mode_policy` is the existing production authority-binding path. The preflight does not wire that composition and must not inject a fabricated snapshot or weaken Edge's positive epoch guard to make the assertion pass. No production fix is justified by this result. Completing the fixture with the production session factory and deterministic external providers remains required before fault acceptance and the full window.

An earlier two-process diagnostic returned exact transcripts at stream epochs 18 and 19 after actual device TCP interruption and resource drain. It initially tried to observe transcripts on the device socket, but that endpoint does not forward them; the final check observes the actual gRPC response and validates text, session, stream and finality. The successor requires the real `listening.resume` tap control and continuous uplink sample positions. That diagnostic is **not** same-window three-process fault acceptance. Bridge termination/restart, slow downlink, stale-generation audio rejection, complete generation terminal and three-process drain acceptance remain unexecuted. Long-window duration, frozen long-window schedule and resource samples are all zero or absent. No latency, memory-leak or capacity conclusion follows.

[bridge.log](five-software-20261111/r4/bridge.log) and [control.log](five-software-20261111/r4/control.log) retain process behavior. All owned children were reaped and the run's PostgreSQL container removed. The existing unrelated container was preserved. No parent-checkout, hardware, production, external sender, paid provider, push, PR or merge operation ran.

Current-candidate `govulncheck ./...` exits 3 and reports reachable `GO-2026-6859`. Trivy exits 1. [scans.json](five-software-20261111/r4/scans.json) records full commands, versions, scanner hashes, DB timestamp, target counts and IDs, and the compact govulncheck text. The earlier experimental `govulncheck -json` invocation returned 0 while reporting findings; that formatting-mode exit is not a passing security gate. The final text-mode command retains exit 3. Production source and dependency files are unchanged from the previously scanned baseline, so these changes do not demonstrate an introduced production path. Exploitability was not verified. The raw JSON remains only in the ignored local `outputs/five-software-r4/` directory and is not available in a new clone. Committed summaries contain the necessary conclusions without another database-sized dump.

Verification on this harness source passed repository Ruff, strict mypy on 593 service files, all 33 unchanged module budgets, Go race suite, mediaruntime build, and the selected process-probe/process-exit tests with 15 passes and 2 Linux-only skips. The normal Go suite skips the explicit cross-process test unless configured. Those passes do not override the separately executed preflight failure. Receipt logs are in [r4](five-software-20261111/r4/).

```sh
.venv/bin/python -m scripts.software_soak_r4 --output outputs/five-software-r4-candidate-1941eccc
go test -C services/media_edge -race -count=1 ./...
go build -C services/media_edge -o /tmp/memoria-r4-edge ./cmd/mediaruntime
.venv/bin/pytest -o addopts= -q scripts/tests/test_local_process_probe.py services/agent/tests/unit/test_bridge_process_exit_injection.py
.venv/bin/ruff check .
.venv/bin/mypy services --strict
.venv/bin/python scripts/check_module_budget.py check
```

Prove It Works kept the failing actual Control response as a failed positive gate. The requested 900-second acceptance remains unfinished; no successful long window is claimed.

## R5 signed profile composition and reproduced publication defect

The R5 test session factory calls the production `ProductionMediaSessionFactory._new_runtime` and `_bind_mode_policy`. Its local HTTP policy endpoint queries real `build_postgres_session_runtime_service.current` and returns the unchanged issued signature. `ModePolicyClient` verifies it with the actual fixture signing key. The production registry, transport, device admission, subject fences and generation gates remain in use. Only external ASR, LLM and TTS are deterministic. This is an explicit test composition, not the complete production Control application or a production provider deployment.

The second authority-composition experiment admitted signed epoch 1 in the actual runtime but exposed a separate production publication defect. The nested `assistant_state` payload carried epoch 1 while the outer gRPC client-event JSON carried epoch 0 before the first media generation. `grpc_bridge.emit_event` inferred identity from its media-generation fence. R5 passes the runtime event's own epoch explicitly. Edge's positive-epoch validation and Control's unknown rejection remain intact. `r5/envelope-before.log` reproduces `assert 0 > 0`; `r5/envelope-after.log` records the local correction. The failed and passed profile preflight metadata are separate receipts.

The final fix limits this change to the client-event envelope. Floor-effect reconstruction retains its prior behavior. The two affected golden traces each change exactly one outer `session_epoch` from 0 to 1; their complete-trace assertions remain. The expanded first regression ran 450 passes, 10 skips and two failures on those old golden values. The subsequent targeted run passed 44 tests after the two exact expected-value corrections. The full final regression is recorded separately below when complete.

A 45-second actual three-process fault preflight passed. Its fixed schedule exercised TCP loss with higher-epoch reconnect, actual Python SIGKILL and restart with a new session, and a deliberately slow socket writer. TCP reconnect uses the shipped `listening.resume` tap contract and asserts exact `online/listening`, rather than claiming speech admission while an old wake acknowledgement still owns output. Fresh-session positive controls assert exact `online/speaking`, matching signed identity epoch and stream, final ASR text and actual Opus downlink. The slow transport delays actual test-socket writes by 300 ms and suspends client reads for eight seconds. Production queue admission drops actual audio and retains at most ten 20-ms frames. The fixture does not simulate drop or recovery results. A captured real PCM frame is rejected by the production Session gate after an actual device button stop advances its generation.

The R5 window below now has its final result and passing software summary. The production change requires a new parent-owned independent review; a review frozen at `f79aa4f5` cannot cover it. Security scanner findings remain a failed gate. No dependency upgrade is part of R5.

## R5 completed window and final verification

The software window predicate **passes** on clean candidate `85d254e7f66bceeaab2ce14e955e1221ef9e5266`. The [summary](five-software-20261111/r5/summary.json) passes all 13 declared predicates. The [configuration](five-software-20261111/r5/config.json), [result](five-software-20261111/r5/result.json), [event excerpts](five-software-20261111/r5/events.jsonl) and [fault timing](five-software-20261111/r5/fault-timing.json) are separate receipts. P2-04 is not a release approval; its security and device gates remain open.

The window ran from `2026-11-11 22:37:23.204345 +08:00` to `22:52:23.208540 +08:00`, measured as 900.004195 seconds. Control PID was 16508. Python bridge PID 16509 was killed with SIGKILL and replaced by PID 30083. Edge PID was 16510. The actual race-instrumented Edge test binary SHA-256 is `1603cca2377624c1e09a9679a6cbcc47a24f35c3fa0d8993a25df90e8537f60b`. The interpreter hash, runtime script hashes, lockfile hashes and production source digest are in the [candidate manifest](five-software-20261111/r5/candidate-manifest.json). The final source digest matches the frozen production tree. This process contains the real Go Edge implementation and its test driver; it is not the deployed mediaruntime daemon.

The predefined schedule uses five-second traffic and resource intervals after service readiness. Faults become eligible at 180, 360 and 540 seconds. There are 180 completed cycles and 181 three-process resource samples, spanning sampler elapsed 0.094 to 900.166 seconds. Each fresh positive control verifies final ASR text `你好`, an actual nonempty generation-fenced Opus downlink and exact authenticated Control `online/speaking` under the issued session epoch. The same-session reconnect verifies higher stream epoch and exact `online/listening` after the shipped tap control. The software provider generates deterministic wake acknowledgement audio and ASR events. It does not prove a model-generated answer, wording quality or user hearing.

| Fault | Actual same-window result |
| --- | --- |
| TCP interruption | `session_37` reconnects from stream 172 to 173; real transcript and exact Control phase recover. The completed cycle is at 180.338 seconds. |
| Bridge process death | The old device gets `session.error` code `voice_core_unavailable`; Edge is offline with zero leases. The process receipt records old exit -9. New `session_73_recovery`, stream 245, passes actual ASR, Opus and Control. The recovery cycle completes at 361.117 seconds. |
| Slow transport and old PCM | Actual 300-ms socket writes and eight seconds of suspended reads produce 363 fault-local audio drops, at most ten 20-ms queued audio frames. An actual button stop advances generation 1 to 2. Replaying the captured generation-1 PCM returns `ErrStaleDownlinkGeneration` and never reaches its sender. The recovery cycle completes at 548.834 seconds. |

The exact measured completion times are authoritative in fault-timing.json. The table's rounded times describe recovery-cycle completion, not fault injection instants. The bridge fault proves the old Edge and gRPC transport terminal plus a new-session recovery. It does not prove that PG automatically revoked or closed the old persistent authorization, and does not change disconnect semantics.

The driver records 184 actual final transcripts, 183 validated Opus downlink frame observations, 184 successful exact Control observations and 369 HTTP requests. Its status counts are 367 responses with 200, one expected 401 and one expected 403, with zero unexpected statuses. The full Control server log records 550 responses with 200, 184 expected missing-session 403 responses and two expected missing-Bearer 401 responses. [HTTP counts](five-software-20261111/r5/control-http-counts.json) distinguish the driver latency coverage from policy-query status coverage. The [final Edge counters](five-software-20261111/r5/edge-final-counters.json) record 185 uplink frames, 1080 downlink enqueue events and 364 backpressure drops across the whole window; the fault-local count is 363. Enqueue counts are not delivered-frame counts. All 180 cycles end with zero Edge connections and leases. Cycle-end goroutine samples are 12 or 14. The cycle latency p50 is 256.043 ms and p95 is 279.435 ms; the maximum 8831.392 ms includes the scheduled slow-consumer fault. These are one-window observations, not a comparison or throughput result.

[Per-PID resource observations](five-software-20261111/r5/resource-instances.json) preserve RSS, numeric FD counts and established TCP counts separately across the bridge restart. Sampling includes loader startup, and the race binary shares its process with the driver and retained session observations. Bridge RSS grows during the window. This result does not establish leak absence, identify a leak, or establish capacity. The 12-core host was shared, and regression checks and scans also ran during the early part of this window. Every raw sample retains the observed host load. No profiler or repeated comparative benchmark ran.

The final bridge logs zero authority clients after registry drain, and the retained buffer-clear receipts show empty ingress and denoiser queues. [Cleanup](five-software-20261111/r5/cleanup.json) verifies that all four PIDs are gone. Edge and the final bridge exit 0. The deliberately killed bridge exits -9. Control logs application shutdown and exits -15 after explicit SIGTERM; it is not recorded as exit 0. Only the two R5-owned test containers were removed, one for the window and one for the supplemental Control checks. The existing unrelated container remains running.

[Verification](five-software-20261111/r5/verification.json) records actual commands. The full selection passes 452 tests with 10 skips. Eight Control tests skipped without a PG DSN were separately rerun with a disposable local pgvector PostgreSQL DSN configured and all eight passed. The remaining two skips require Linux /proc and do not run on Darwin. Repository Ruff, strict mypy on 593 service files, all 33 unchanged module budgets, Go race suite and mediaruntime build pass. The ordinary Go suite skips this opt-in window; the separate race binary actually executes it. No tests or assertions were removed or weakened.

Current-candidate [scans](five-software-20261111/r5/scans.json) are real reruns, not reuse claims. govulncheck exits 3 and reports reachable `GO-2026-6859` in Go 1.27.3 net/http. Trivy exits 1 and records 81 distinct advisory IDs. [Scanner commands](five-software-20261111/r5/scan-commands.json) include versions and executable hashes. No dependency or toolchain upgrade ran. Security gate remains **FAIL** and exploitability is not individually confirmed.

The raw logs, full scanner JSON and resource series remain only under local ignored `outputs/five-software-r5-900-final/` and `outputs/five-software-r5-*.json` files. A new clone has committed summaries, configuration, hashes, selected log lines and runnable scripts, not those raw files. [Preflight history](five-software-20261111/r5/preflight-history.json) preserves separate failed attempts. Early profile-preflight metadata records HEAD rather than an exact frozen dirty source; its inherited `production_source_changed=false` field was incorrect. The clean final configuration and manifest supersede that metadata claim. Historical R4 failure receipts are unchanged. Obsolete R5 preflight binaries were removed after their hashes were recorded; the final window and last accepted short-preflight binaries remain local.

Reproduce from this isolated checkout with Docker and the locked Python environment:

```sh
.venv/bin/python -m scripts.software_soak_r5 --duration 45 --output outputs/new-r5-preflight
.venv/bin/python -m scripts.software_soak_r5 --duration 900 --output outputs/new-r5-window
.venv/bin/python -m scripts.summarize_software_soak_r5 outputs/new-r5-window
```

The output directory must not exist. The runner starts only its own local PG and stops only its own children and container. The parent owns formal review of the production epoch changes, particularly commits `4e794afc` and `a54c5c52`; candidate `85d254e7` freezes the test composition. Hardware, production deployment, production policy-app composition, rollback execution and model quality remain unverified. No serial port, production SSH, external notification, paid model, real user data, push, PR or main merge ran.

Fix Root Causes placed the fix at the event identity publication that lost the epoch. Prove It Works required the actual three-process positive chain and actual faults before accepting the window. Model the Domain keeps profile identity epoch distinct from transport stream epoch and output generation. Benchmark Checklist limits the measurements to one shared-host software observation. No recursive delegation or formal child review ran, as required by this implementation brief.

## R6 生命周期修复与新候选收据

实施基线是 `7f339217c25ccbd485a4677ef0b6cd23654b9900`。原审查目标仍为 `b2ffc904`。所有写入仍位于本报告指定的隔离 worktree，原 checkout 只读取了授权的旧 bundle。`62d9786d` 先提交失败回归，`ac3eeab6` 修复生命周期，`2c4d4e61` 固定收据验证与测试组成。完整 commit 为 `62d9786d9cc360ac871f1eaa94df7c787cc03e7e`、`ac3eeab65d49c3987ff88027571561463607a9fc`、`2c4d4e61163b3804debd0bc75f2b85af03822e9a`。公开方法签名、合同、预算、依赖锁文件、实际 action authority、S3 源码及原审查文档均未改，[scope audit](five-software-20261111/r6/scope-audit.json)保留检查结果。

[修复前](five-software-20261111/r6/lifecycle-before.log)在真实隔离 PG 和 ASGI 上得到 `8 failed, 12 passed`。GET、显式 refresh、resolve、切人、降为 unknown、chat/tutor 决策和 fail_session 均复现旧权限阻断。修复后的 21 项生命周期测试和 11 项收据控制得到 `32 passed`，随后全部纳入 801 项零跳过综合回归。额外 Voice Runtime 选择为 `460 passed, 2 skipped`，两个跳过仅适用于 Linux /proc。这些选择重叠，63 项 S3 测试也包含在 801 中。

| 撤回同意后的动作 | 实测结果 |
| --- | --- |
| 旧私有 profile 的 current、session-policy 与 session-state | 继续拒绝私有读取。Session policy 为原有 503，session-state 为 200 unknown 且不返回 snapshot。 |
| 控制 profile GET、显式 refresh、resolve-subject | 重新签发带真实签名的非私有 profile，PG epoch 前进，当前能力不含 memory_recall_private。 |
| active-subject 与未确认降级 | 合法切换到另一真实绑定且有委托关系的主体，或降到 unknown；旧 profile 随后被拒绝。 |
| chat/tutor decision | 只允许剩余已签发且实时 Policy 允许的能力。未签发 tutor 返回 deny；旧私有 profile 的 private 分类和私有召回继续拒绝。降级后私有召回 decision 为 deny。chat 不改写旧 profile，也不能通过实际 action authority 恢复私有能力。 |
| fail_session 和 close_session | 写入 failed/closed 和新的终态 fence，后续读取或控制 GET 不会复活该会话。 |
| 错误 actor/device、撤回或重绑 binding、投影冲突、过期 conversation、closed/failed | 仍拒绝，非法控制请求不推进持久 epoch 或能力。 |

最小结构修复保留 `current()` 的实时私有同意检查。私有 `_current_profile_context` 复用原 active-context、签名、TTL 和 binding 验证；switch 和非私有 decide 使用该生命周期路径。`fail_session` 按 close 的终态路径验证签名和 actor。Control 仅捕获明确的 PrivateProfileConsentWithdrawn 子类，并在设备、绑定及当前 fence 合法时重新旋转，不捕获全部 Denied。`_rotate` 仍从当前 PG 同意重新派生能力。没有公开 unchecked boolean，也没有重签旧 profile 以保留过时能力。

新增 ASGI 测试使用真实 Bearer、SQLite auth session、专用非 superuser PG Identity 和真实 Session/Consent stores。session-state 单元只注入运行状态读取边界，用来先证明 online 再证明撤回后的 unknown；没有替换 runtime 或 consent 权威。原四项真实 Go HTTP+Control+PG 联合测试也全部执行。四条被原 Reviewer 指出的叙述性注释已直接删除，没有其他风格重构。

新的收据 schema 是 `software-soak-r6`。保留原 13 项条件，并新增 clean candidate、control_positive 不少于 cycle、完整 Control server 状态检查。新窗口逐条记录 policy 请求的 session、status 和真实错误类型，只有 missing-session 加 PersistentSessionNotFound 支持预期 403；401 由无 Bearer 的 readiness 和明确探针支持。不存在任意 403 白名单。历史日志按旧 fixture 的已断言 missing-session 探针和真实 provider admission 数量核对，旧 900 秒原始收据仍通过，[重汇总](five-software-20261111/r6/historical-r5-resummary.json)另存，原 R5 summary 未覆盖。历史 550/184/2 不是未来窗口的常数。

11 项独立收据控制覆盖有效运行、dirty source、缺正向控制、server 500、额外 403、错误 driver 403、unknown、缺结束收据和错误拒绝原因。500、unknown 或缺证据均不能通过。原 R4 和失败预检保持失败记录。

本次窗口冻结候选为 `2c4d4e61163b3804debd0bc75f2b85af03822e9a`。源码集合包含 699 个 tracked services/packages 文件，排除 tests 与 *_test.go，digest 为 `ab59975ad01f527d6712d0ea649939b6aae0629600312b6dbe9dce1ab1f465c8`。[候选 manifest](five-software-20261111/r6/candidate-manifest.json)记录算法、解释器、锁文件、全部运行脚本以及预检与长窗相同的 Edge binary SHA-256 `6d60ffd53b5a238573796e27234949157311c6c374bf64a83af5256eb2483ca5`。结束时重新逐文件核对源码无变化。后续只提交文档与收据。

窗口实际运行 900.007511 秒，完成 180 个已排空周期和 181 次三进程资源采样。全部 16 个谓词通过，完整服务端状态为 `{'401': 2, '403': 184, '200': 550}`，意外状态为零。每个正向控制检查实际 ASR 你好、真实非空 Opus、精确 online phase 与签名 epoch。三次故障和恢复见[events](five-software-20261111/r6/events.jsonl)。慢写实际丢弃 363 帧，最大音频队列十帧，停止后旧 generation PCM 被生产 gate 拒绝。

[按 PID 的资源](five-software-20261111/r6/resource-instances.json)区分桥重启前后。RSS 增长仍仅是观测，不证明内存泄漏、无泄漏或容量。早段同时运行回归检查，共享主机和 race driver 也限制性能解释。真实 SIGKILL 证明旧 transport 终止和新会话恢复，不证明旧 PG authorization 自动撤销。没有改变断连语义。[cleanup](five-software-20261111/r6/cleanup.json)核对所有 owned PID 消失、两类本轮 PG 测试容器已移除。Control 退出 -15、被杀桥 -9、最终桥和 Edge 0，未将 -15 记为 0。

Ruff、593 文件 strict mypy、33 项原模块预算、Go race 和 mediaruntime build 均通过。当前实际 [scans](five-software-20261111/r6/scans.json)仍为 govulncheck exit 3、Trivy exit 1，后者 81 个 distinct advisory IDs。reachable GO-2026-6859 和单个 advisory 的 exploitability 边界不变，未升级依赖或修复漏洞。扫描失败不被测试或窗口通过覆盖。

原始日志、完整 scanner JSON、逐样本资源和最终二进制只留在 ignored `outputs/five-software-r6/`、`outputs/five-software-r6-preflight/` 和 `outputs/five-software-r6-900-final/`。新 clone 只有摘要、配置、hash、精确命令和必要摘录，不含大体积原始数据。公开发布、生产、设备、真实模型与新的正式跨厂商复审均未执行，主 Agent 负责 fresh Review。

Model the Domain 将读取、旋转和终态清理分到各自的域路径。Boundary Discipline 只允许明确撤回状态触发降级，保留 actor/binding/fence 拒绝。Fix Root Causes 修复过度使用读取权限的调用点，Prove It Works 要求真实 PG 和三进程故障证据。Benchmark Checklist 将本次资源数据限定为一次共享主机观测。
