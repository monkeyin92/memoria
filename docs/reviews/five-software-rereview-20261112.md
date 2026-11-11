你需要在隔离 worktree 分支 `t3/five-software-tasks-acceptance` 的冻结候选 `4dc67723121aa1715ce59ce713d1861312ff17e2` 上，对相对上一审查基线 `b2ffc904c6c8ba88b5a57ba3eeb367c17e6dfa9b` 的全部增量修复与 R6 收据做只读跨厂商复审，确认五项无硬件软件任务的已知审查缺陷均已真实闭环且不越界宣称线上、模型或真机完成。

`throughput checkpoint: n/a, read-only investigation`

### 复审结论

**软件改动审查通过，扫描仍FAIL**（实测候选 `4dc67723121aa1715ce59ce713d1861312ff17e2`，对比基线 `b2ffc904c6c8ba88b5a57ba3eeb367c17e6dfa9b`，总功能基线 `23f75b4d7eb05f587ca7c44a27273e5a2a9a0d55`，新增发现 `0 S1 / 0 S2 / 0 S3`）。

对于终端用户与家庭监护人，撤回私密记忆同意后不再锁死设备控制 profile 降级、切人、剩余 `chat`/`tutor` 决策与媒体异常终态清理，同时私密读取与否定式“不要告诉爸妈”筛查继续严格拒绝（实测 PG+ASGI 与筛查探针验证）。对于后续维护者，会话读取权限、旋转生命周期权限与终态清理权限已在代码中显式解耦，三进程窗口收据验证器补齐了干净源码树、每周期正向断言与服务端完整 HTTP 状态核对（实测代码与重汇总验证）。本结论不构成可发布、生产网关、真实付费模型或真机通过声明。

---

### 逐项复核结果

- **候选 SHA 与工作区状态（实测）**
  - 当前 `HEAD` 为 `4dc67723121aa1715ce59ce713d1861312ff17e2`，`git status --porcelain` 为空，复审全程零仓库写入。
- **1. 原 S2 生命周期死锁修复（已闭环，实测）**
  - [issue_authority.py:71](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/issue_authority.py:71) 与 [issue_authority.py:131](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/issue_authority.py:131) 定义专用子类 `PrivateProfileConsentWithdrawn(PersistentSessionDenied)`，仅在已签发 `memory_recall_private` 的有效 profile 失去实时同意证据时抛出。
  - [service.py:488](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:488) 与 [service.py:500](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:500) 将 `_current_profile_context`（单只读事务内校验 active 行一致性、签名、TTL、`profile.actor_id == actor_id` 及实时 `binding_is_current`）与 `current`（额外强制 `_require_private_profile_consent`）分离，使 `/v1/internal/interaction-policy/session`、[device_control.py:1103](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/app/routes/device_control.py:1103) 与 [memory_scope_authority.py:67](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/app/memory_scope_authority.py:67) 继续对撤回后的旧私密 profile 保持 fail-closed。
  - [service.py:537](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:537) 的 `switch_subject` 改调 `_current_profile_context`，随后由 `_rotate`（[service.py:652](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:652)）重新加锁绑定与设备信任、从当前 PG 同意重算能力并推进 `session_epoch`、`profile_revision`、`generation_id`、`turn_id` 与 `tool_epoch`。
  - [service.py:813](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:813) 的 `fail_session` 改走 `_active_profile_context` 并校验 `profile.actor_id == actor_id` 与 `_require_profile_signature(profile)`，与 `close_session`（[service.py:887](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:887)）对齐，撤回同意或过期后均可写入终态且不可被后续请求复活。
  - [service.py:1136](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:1136) 与 [service.py:1142](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:1142) 的 `decide` 核验最新 `runtime_profile_id`，对 `capability == "memory_recall_private"` 或 `data_classification == "private"` 继续强制 `_require_private_profile_consent`，对剩余已签发非私密能力进入真实 `action_transaction`、`PolicyEngine.decide` 与 `execute_profile_persist`（[service.py:1270](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/session_runtime/service.py:1270)），不改写旧 profile 且不恢复未签发 `tutor` 或私密能力。
  - [multi_subject_runtime.py:727](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/app/multi_subject_runtime.py:727) 与 [multi_subject_runtime.py:789](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/app/multi_subject_runtime.py:789) 的 `ensure_profile` 仅捕获 `PrivateProfileConsentWithdrawn`，非 `device-control:` 会话立即重抛（普通会话 ID 由服务端 `create_session_id()` 生成），并在校验 `device_id`、`binding_id`、`binding_version` 后旋转签发不含 `memory_recall_private` 的新控制 profile；错误 actor、跨设备、已撤回或重绑 binding、投影冲突、过期会话及 `closed`/`failed` 会话均在旋转前被拒绝且持久状态零推进（实测 [test_consent_lifecycle_r6.py:181](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/tests/test_consent_lifecycle_r6.py:181) 全部 21 项及 [test_policy_authority_r2.py:36](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/tests/test_policy_authority_r2.py:36) 通过）。
- **2. 原 S3 分句否定与转折边界修复（已闭环，实测）**
  - [anti_manipulation.py:189](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/companionship/anti_manipulation.py:189) 对 `phrase` 及 `_REQUIRED_EQUIVALENTS` 逐个扫描非自否定词条，按 `r"[，,。！？!?；;]|不过|但是|可是|然而"` 切分前向 6 字符窗口后再检查 `_NEGATION_MARKERS`。
  - 实测 17 组正负探针与 [test_anti_manipulation.py:325](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/companionship/tests/test_anti_manipulation.py:325) 共 66 项单测全部通过，其中 `"没事，不要告诉妈妈。我们下次小心一点就行。"`、`"放心吧，我不会告诉爸爸妈妈。"`、`"这不是你的错，不用告诉爸爸妈妈。"`、`"不是你的错，不过不用告诉爸爸妈妈。"`、同义否定 `"没事，不要跟妈妈说。"`、`"不是你的错，然而不要和家长商量。"` 以及否定身份 `"我不是 AI"`、`"不过我不是小助手"`、`"我不是 AI 小助手"` 均返回 `passed=False`，而 `"不过我是 AI"`、`"但是我是小助手"` 正常通过。
  - 实测正向 fixture [anti_manipulation_u1_u8_fixture.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/companionship/evaluation/anti_manipulation_u1_u8_fixture.json) 保持 `40/40`（`accuracy=1.0`）；只读重评原记录包（SHA-256 `3fd4ec5762f95fb09db2d9fbc65171180246654b348d0feb4c1467105db59b97`）精确复现 [recorded-bundle-after-concessive-fix.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/recorded-bundle-after-concessive-fix.json) 的 `19/40` 次、`1/8` 例通过与进程退出码 `1`（整体仍 FAIL），三句上限、每例 `5/5` 门槛及 PM 判定均未改动。
- **3. 收据验证器缺口修复（已闭环，实测）**
  - [summarize_software_soak_r5.py:17](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/summarize_software_soak_r5.py:17)、[summarize_software_soak_r5.py:34](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/summarize_software_soak_r5.py:34) 与 [summarize_software_soak_r5.py:153](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/summarize_software_soak_r5.py:153) 在 `software-soak-r6` 的 16 项谓词中强制检查 `config.get("dirty") == ""`、`counts["control_positive"] >= len(cycles)`、driver 请求精确方法路径载荷匹配及完整 `control.log` 访问状态核对。
  - 在 `http_receipt_schema == "scenario-v1"`（[software_soak_r5.py:72](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/software_soak_r5.py:72)、[software_soak_r5_control.py:108](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/software_soak_r5_control.py:108)）下，每个 `/fixture/session-policy` 响应必须带结构化 `fixture_policy_response` 记录，且仅 `session_id == "missing-session"` 且 `reason == "PersistentSessionNotFound"` 的精确探针计入预期 403；旧 R5 原始目录无该 schema 标记，按已断言探针数重汇总与 [historical-r5-resummary.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/historical-r5-resummary.json) 逐字节一致（实测 `diff -u` 零差异）。
  - [test_summarize_software_soak_r6.py:87](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/tests/test_summarize_software_soak_r6.py:87) 的 11 项正负控制逐一覆盖 dirty 源码、缺失 `control_positive`、服务端 500、非预期路径 403、多余 policy 403、错误 driver 403、`control_unknown`、未完成窗口及 `PersistentSessionDenied` 错误原因，无法靠文件存在或宽泛 403 白名单过绿（实测 11 项全通过）。
- **4. R6 900s 窗口边界、故障收据与清理核对（边界准确，实测）**
  - 实测核对 `git diff --name-only 2c4d4e61..4dc67723`，两提交间唯一变更的非测试业务文件为 [anti_manipulation.py](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/companionship/anti_manipulation.py)，且全仓 `git grep` 确认该模块仅被其自身与单测引用，与 `voice_core`、`session_runtime`、`control_api`、`media_edge` 及三进程长稳脚本零耦合；报告将 900s 窗口归属干净冻结 `2c4d4e61163b3804debd0bc75f2b85af03822e9a` 而不伪称 `4dc67723` 整树 hash 相同，适用边界准确诚实。
  - 实测对本地 `outputs/five-software-r6-900-final` 重新运行 `scripts.summarize_software_soak_r5`，输出与 [r6/summary.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/summary.json) 逐字节一致（`900.007511s`、180 周期、181 次采样、16 项谓词全真、服务端状态 `{'401': 2, '403': 184, '200': 550}`）。
  - 实测读取 [candidate-manifest.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/candidate-manifest.json)、[cleanup.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/cleanup.json) 及原始 `edge.log`/`bridge.log`/`control.log`，三类故障（`session_37` TCP 断连重连、`pid 54774` `SIGKILL` `-9` 触发 `bridge_old_terminal` 后 `pid 65394` 恢复、`session_109_slow` 300ms 慢写队列上限 10 帧丢弃 363 帧且停播后旧代 PCM 被拒）均有真实日志证据，宿主机当前 `kill -0` 与 `docker ps -a` 确认 R6 全部 8 个进程 PID 与 3 个临时 PG 容器均已清理且未触碰既有无关容器。
- **5. 注释清理、模块预算与报告/TSV 一致性（无削弱，实测）**
  - [memory_domain.py:360](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/archive/memory_domain.py:360)、[test_continuity_recap_r2.py:106](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/companionship/tests/test_continuity_recap_r2.py:106)、[software_soak_r4_test.go:17](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/media_edge/software_soak_r4_test.go:17) 与 [software_soak_r4_test.go:113](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/media_edge/software_soak_r4_test.go:113) 仅删除前次指出的 4 行机械叙述注释，零断言改动且 [check_module_budget.py](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/scripts/check_module_budget.py) 33 项预算未放宽并全数通过。
  - [five-software-tasks-20261111.md:9](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-tasks-20261111.md:9)、[five-software-tasks-20261111.md:22](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-tasks-20261111.md:22) 与 [decisions.tsv:56](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/decisions.tsv:56) 已将 R6 阶段性的 `16/40` 与“三个新增失败待复核”更新为 `4dc67723` 复核后的 `19/40, 1/8`（仍失败）及 66 项测试通过，无过时待复核结论影响判定，也无误称任务完成（[TODOLIST.md:528](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/TODOLIST.md:528) 及全部 `[ ]` 复选框保持未完成）。

---

### 真实执行命令与计数（Executed vs NOT RUN）

- **本轮实际执行（Executed）**
  - 独立临时 `pgvector/pgvector:0.8.1-pg17-bookworm` 容器（随机本地端口 `54662`，测毕销毁并经 `docker ps -a` 核实）下执行 `env -C "$WT" MEMORIA_TEST_POSTGRES_DSN=... .venv/bin/pytest -o addopts= -q -rs --tb=short` 跑 14 个核心套件（`test_consent_lifecycle_r6.py` [21]、`test_policy_authority_r2.py`、`test_session_policy_retention_ceiling.py`、`test_minor_tutoring_r2.py`、`test_guardian_recap_timeout_r2.py`、`test_planner_catalog_r3.py`、`test_assistant_snapshot.py`、`test_assistant_http_r3.py`、`test_projection_equivalence_r3.py`、`services/companionship/tests` [含 `test_anti_manipulation.py` 66 项与 `test_continuity_recap_r2.py`]、`test_media_profile_envelope_r5.py`、`test_assistant_emotion.py`、`test_phrase_segmenter.py`、`test_summarize_software_soak_r6.py` [11]），实测结果 **183 passed in 95.41s, 0 skipped**。
  - `env -C "$WT" .venv/bin/python -m scripts.summarize_software_soak_r5` 分别重汇总 `outputs/five-software-r6-900-final` 与 `outputs/five-software-r5-900-final` 至 `/tmp` 并与提交收据 `diff -u`，实测 **16/16 谓词全真，两份 JSON 均零差异匹配**。
  - `env -C "$WT" .venv/bin/python -m services.companionship.anti_manipulation` 重评正向 fixture 与原记录包 `live-bundle.json`，外加 17 组正负边界探针脚本，实测 **fixture 40/40（exit 0）、原记录包 19/40 及 1/8（exit 1，与提交收据一致）、17 组探针全匹配**。
  - `env -C "$WT" .venv/bin/ruff check .`，实测 **All checks passed!**（exit 0）。
  - `env -C "$WT" .venv/bin/mypy services --strict`，实测 **Success: no issues found in 593 source files**（exit 0）。
  - `env -C "$WT" .venv/bin/python scripts/check_module_budget.py check`，实测 **module_budget_check=PASS count=33**（exit 0）。
  - `go test -C "$WT/services/media_edge" -race -count=1 ./...`，实测 **`memoria/services/media_edge`（6.874s）与 `cmd/mediaruntime`（5.010s）PASS**。
- **本轮未重复执行（NOT RUN）**
  - 900s 三进程长稳窗口未重跑（已通过原始日志与重汇总验证冻结 `2c4d4e61` 收据）。
  - 其余未改动的全量 801 / 460 历史回归子集、`govulncheck` 与 `trivy` 二进制重扫未在本轮重复运行（依据 [r6/verification.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/verification.json) 与 [r6/scans.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/scans.json) 核验）。

---

### 剩余 Attention Flags 与边界矩阵

- **Remaining Attention Flags（非新增代码缺陷，需保留可见）**
  1. **安全扫描门禁仍 FAIL（实测收据）**。[r6/scans.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/scans.json) 记录 `govulncheck` 退出码 `3`（Go `1.27.3` 标准库 `net/http` 可达 `GO-2026-6859`）与 `trivy` 退出码 `1`（81 个 advisory IDs），依赖升级不在本分支范围。
  2. **Python 桥进程 RSS 增长未归因（实测收据）**。[r6/resource-instances.json](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/resource-instances.json) 显示 `bridge:54774` 由 104.7 MB 升至 297.9 MB、`bridge:65394` 由 143.2 MB 升至 381.1 MB，报告已诚实声明不作无泄漏或容量结论，后续需单独做内存画像。
  3. **原模型记录包仍未达标（实测）**。旧 `live-bundle.json` 当前为 `19/40`、`1/8`（FAIL），真实生产模型提示词与生成质量仍待独立验证。
  4. **两处非阻断历史归档说明（实测检查）**。历史快照 [recorded-bundle-deltas.json:40](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/recorded-bundle-deltas.json:40) 与 [recorded-bundle-provenance.json:16](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/docs/reviews/five-software-20261111/r6/recorded-bundle-provenance.json:16) 保留了 `4dc67723` 之前的 `16/40` 记录（主报告第 9 行已明示其仅作历史留档不再作为当前判定）；此外 [TODOLIST.md:418](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/TODOLIST.md:418) 摘要仍指向 R5 `85d254e7` 而非最新 R6 `2c4d4e61`，虽不影响未完成状态与主报告结论，后续整理文档时可顺手同步。

| 任务 | Code & Wired | Enabled | Verified（本地软件边界） | Unverified / Failing Gate |
| --- | --- | --- | --- | --- |
| P0-04 会话与私密召回生命周期 | `current` 私密检查与 `_current_profile_context` 旋转/`fail_session` 终态清理分离并接入 Control API | 默认启用 | 实测 PG+ASGI 21 项新生命周期回归与原矩阵零跳过通过 | Retention 自然过期非本轮模型；线上与真机未验 |
| P2-06 反操纵筛查与陪伴延续 | 分句及转折边界否定检查、裸情绪标签过滤、recap/continuity 持久化与超时回退已接线 | 默认启用 | 实测 66 项筛查单测、fixture `40/40` 及 PG recap 回归通过 | 旧记录包 `19/40, 1/8` 仍 **FAIL**；真实 LLM 生成质量未验 |
| P1-06 Planner 等价投影去重召回 | `distinct_facts` CTE 在 `LIMIT` 前去重并接入 [interaction.py:1091](/Users/monkeyin/.t3/worktrees/memoria/t3-c1dc1f05-cae0d497c53627a9/services/control_api/app/routes/interaction.py:1091) | Planner 路径默认启用 | 实测 PG+HTTP 隔离与多源/多声明保留回归通过 | 真实 Qwen/embedding、线上网关与真机召回未验 |
| P1-05 设备助手阶段投影链路 | Go Edge `stateMu` 快照 + 内部鉴权状态口 + Control PG 会话权威校验已接线 | 受保护状态读口默认启用 | 实测 Go `-race` 与 PG+HTTP 越权/过期/断连回退 `unknown`/`offline` 通过 | 小程序/客户端 UI、固件与真机未验 |
| P2-04 三进程长稳窗口与扫描门禁 | 真实签名 profile 装配、外层 `ClientEvent` epoch 保留及 `scenario-v1` 收据核验器就绪 | 测试/验收 runner 显式调用 | 冻结 `2c4d4e61` 900.007511s 窗口 16/16 谓词与 11 项负控通过 | `govulncheck`（3）与 `trivy`（1）**FAIL**；桥 RSS 增长未归因；真机与回滚未验 |

### 应用的准则（Principles Applied）

- **principle-prove-it-works** 驱动本轮在独立临时 PG 17 容器与 worktree 解释器上下文中实跑 183 项回归、重汇总原始 900s 收据并核查初期 `Cwd` 模块遮蔽问题，不以静态阅读代替实测。
- **principle-boundary-discipline** 指导核查 `_current_profile_context`、`current` 与 `ensure_profile` 的权限边界，确认只有 `device-control:` 控制会话上的 `PrivateProfileConsentWithdrawn` 允许降级重签。
- **principle-fix-root-causes** 指导核查 S2 与 S3 修复是否消除根因（区分旧 profile 旋转权威与私密读取权威、区分转折连接词与否定前缀），而非通过放宽断言掩盖问题。
- **principle-test-behavior-not-implementation** 指导核查 `test_consent_lifecycle_r6.py` 与 `test_summarize_software_soak_r6.py` 是否断言 HTTP 状态、签名、PG 持久行及服务端真实拒绝原因等外部可观测行为。
- **principle-explain-the-number** 指导核查 801/460 重叠测试计数、旧包 `19/40` 恢复原因以及共享主机 race 模式下桥进程 RSS 增长的解释边界。
- **no-comments** 与 **unslop** 指导核查 4 处机械注释删除无副作用并保持复审结论简洁直白。