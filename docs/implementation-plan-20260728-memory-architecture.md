# 记忆存储架构优化实施计划

> 日期：2026-07-28
> 范围：长期记忆模型、检索投影、评测、情节整合、程序技能和实验索引
> 不变边界：证据账本是唯一权威来源；工作记忆只在当前话轮动态组装。

## 成功标准

1. `memory_kind`、`domain_category`、`item_kind` 在领域模型、SQLite/PostgreSQL、API 和
   实时上下文链路中语义独立；旧 `category/kind/source_event_id` 只保留兼容读法。
2. 搜索投影包含实体、有效期、观察时间、稳定度、重要度、敏感度、冲突和全部来源，
   访客、候选、撤销与跨账户数据继续 fail closed。
3. 同一人生情节的多次提及归入同一 Episode，时间线原始条目和全部证据不被覆盖。
4. Skill 支持候选、主人审批、版本、简单 JSON Schema、顺序工具步骤、运行审计和逆序补偿。
5. 固定中文评测集输出抽取、召回、排序、时间、来源、冲突、泄漏、延迟和 Token 指标。
6. Mem0 只使用影子命名空间和影子结果表；失败不影响权威编译。
7. pgvector 固定维度、按模型/维度隔离并建立 HNSW；报告 P50/P95 与索引字节数。
8. TurboVec 默认关闭；未同时通过召回、P95、RAM、同步和重建门槛时不能启用。

## 实施切片

### P0 类型化投影

- 新增 `MemoryKind / DomainCategory / MemorySensitivity / ConflictState`。
- 扩展提取结果、搜索查询与搜索结果；兼容旧 API 字段。
- SQLite/PostgreSQL 增量迁移并回填旧投影。
- 搜索文档增加多来源关联，实时 Response Planner 消费完整来源和检索分数。

验证：领域单测、旧库升级、搜索/审核/撤销传播、跨账户与候选泄漏。

### P0 评测集

- 增加版本化中文 JSONL/JSON fixture 和纯指标计算模块。
- 提供当前实现、Mem0 和向量后端共用的评测接口与 CLI。

验证：固定样例的指标 golden test；同一输入重复执行结果稳定。

### P1 EpisodeConsolidator

- 使用显式 canonical key、实体、时间范围和标题相似度选择既有 Episode。
- 每次提及保留独立 timeline 与 episode evidence；Episode 摘要、范围和稳定度只做派生更新。

验证：跨会话合并、同名不同事件不合并、纠错/撤销只影响对应来源。

### P1 Skill Domain

- SQLite/PostgreSQL 双实现：definition/version/approval/run/step/evidence。
- 执行器只接受已批准版本和显式本次确认；失败后逆序执行已声明补偿。

验证：候选禁止执行、版本不可变、Schema 拒绝、成功审计、失败补偿、跨账户隔离。

### P2 Mem0 影子

- 通过惰性可选适配器和独立 shadow store 运行；使用隔离 user/experiment namespace。
- 只比较抽取、人物、时间与检索指标，不写 `memory_claims` 或生产搜索投影。

验证：Mem0 超时/非法输出不影响主结果；影子表不被 ContextAssembler 读取。

### P3 pgvector

- Embedder 声明并校验固定维度；向量主键包含模型与维度。
- 为当前模型/维度创建 partial HNSW，混合检索先取 ANN 候选再融合。
- benchmark 报告搜索 P50/P95、表和索引字节数。

验证：维度漂移 fail closed、旧模型向量隔离、全文降级、EXPLAIN/合同测试。

### P4 TurboVec

- 只实现评测适配器和门禁决策，不切换默认生产实现。
- 门禁同时检查 Recall@K/nDCG、P95、RAM、增量同步和重建时间。

验证：任一门槛失败均返回 disabled；缺少可选依赖时给出可执行诊断。

## 总门禁

- `uv run ruff check .`
- `uv run mypy services --strict`
- `uv run pytest`
- 有 `MEMORIA_TEST_POSTGRES_DSN` 时运行 PostgreSQL/pgvector 条件合同
- `uv run python scripts/run_e2e.py --profile offline`
- `git diff --check`

## 完成状态

- P0：类型化投影、旧库升级、多来源、固定中文 13 场景评测和统一指标已完成。
- P1：EpisodeConsolidator 与 Skill definition/version/evidence/run/step 已完成；技能搜索
  投影可从持久版本重建，账户导出、删除和 PostgreSQL 联合恢复已覆盖。
- P2：Mem0 仅通过惰性可选依赖运行合成影子评测；namespace 按实验、case、账户哈希隔离，
  失败只进入 `failed_cases`。
- P3：embedding 模型维度固定，旧向量原地升级，模型/维度复合主键和 partial HNSW 已完成；
  benchmark 输出 P50/P95、表/索引字节数及可选 shared-buffer residency。
- P4：TurboVec 仍是评测适配器；序列化大小只标记为 proxy，缺少真实 resident RAM 测量时
  门禁必定关闭。
- 实时 Agent 尚无可复用的跨服务工具注册表。本轮保留 `SkillExecutor` 公开执行 seam，
  不把技能自动接入语音主链，避免绕过现有工具权限、generation fence 和账户删除门禁。
- 收尾审查补齐：跨账户泄漏指标按来源事件归属判定；Episode canonical key 按
  `domain_category` 隔离；Skill JSON Schema 对未实现约束 fail closed；PostgreSQL Skill
  对非法 UUID 与 SQLite 保持同一 not-found 语义；Archive 类型化过滤返回稳定 422。

当前离线规则基线（2026-07-28）：

```text
Extraction Precision  0.3947
Extraction Recall     1.0000
Recall@5 / Recall@10  0.1538 / 0.1538
nDCG@10               0.1538
Temporal Accuracy     1.0000
Source Attribution    0.9333
Contradiction         0
Cross-account Leak    0
Candidate Leak        0
```

该基线用于比较，不代表产品目标已达成；较低检索指标明确暴露了当前离线规则检索缺少中文
同义、实体和语义召回。

## 评测与基准命令

当前实现：

```bash
uv run python scripts/evaluate_memory.py \
  --output artifacts/memory-eval/current.json
```

Mem0 影子模式（配置必须指向独立实验存储，不能复用生产记忆库）：

```bash
MEMORIA_MEM0_SHADOW_CONFIG_JSON='<mem0 isolated config json>' \
uv run python scripts/evaluate_memory_mem0.py \
  --experiment-id mem0-zh-001 \
  --store data/memory-shadow-evaluations.sqlite3 \
  --output artifacts/memory-eval/mem0-zh-001.json
```

pgvector 实库 benchmark：

```bash
MEMORIA_ARCHIVE_DATABASE_URL='<postgres dsn>' \
MEMORIA_MEMORY_EMBEDDING_URL='<embedding endpoint>' \
MEMORIA_MEMORY_EMBEDDING_API_KEY='<secret>' \
MEMORIA_MEMORY_EMBEDDING_MODEL='text-embedding-v4' \
MEMORIA_MEMORY_EMBEDDING_DIMENSIONS='1024' \
uv run python scripts/benchmark_pgvector_memory.py \
  --account-id '<benchmark account>' \
  --queries artifacts/memory-eval/queries.json \
  --iterations 20 \
  --output artifacts/memory-eval/pgvector.json
```

TurboVec（`.npy` 向量、查询与 JSON ID/relevance 必须来自同一固定数据集）：

```bash
uv run python scripts/benchmark_turbovec_memory.py \
  --vectors artifacts/memory-eval/vectors.npy \
  --queries artifacts/memory-eval/query-vectors.npy \
  --item-ids artifacts/memory-eval/item-ids.json \
  --relevance artifacts/memory-eval/relevance.json \
  --bit-width 4 \
  --output artifacts/memory-eval/turbovec.json
```

默认 CLI 只取得序列化大小代理值，因此会以
`resident_ram_measurement_required` 保持 disabled；只有注入真实 resident RAM sampler 的
受控 benchmark 才可能通过全部门槛。

## 最终门禁（2026-07-28）

- `uv run ruff check .`：通过。
- `uv run mypy services --strict`：175 个 source files 无问题。
- 带真实 PostgreSQL/pgvector 合同的 `uv run pytest`：
  `1421 passed, 2 skipped`。
- `uv run python scripts/run_e2e.py --profile offline`：通过。
- 固定 13 场景评测：无失败；当前规则基线的
  `Recall@5/10 = 0.1538`、`nDCG@10 = 0.1538`，
  `Cross-account Leakage = 0`、`Candidate Leakage = 0`。
- `git diff --check`：通过。
- 未部署、未提交、未推送；生产权威记忆仍使用现网版本。
