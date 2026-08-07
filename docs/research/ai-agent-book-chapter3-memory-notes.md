# 《AI Agent 深入理解》第 3 章记忆研究笔记

> 调研日期：2026-08-06
>
> 原文：[《用户记忆和知识库》](https://github.com/bojieli/ai-agent-book/blob/ba26183f5fc3266264601f2bb89dc5eb0bfe6bc0/book/chapter3.md)
> 范围：短期/长期记忆、存储、检索、压缩与摘要、实体与时间关系、遗忘与重要性。

## 结论

章节最值得采用的不是某个框架，而是三条系统原则：

1. 原始对话/轨迹、可修改的长期知识、当前工作上下文必须分层；摘要和索引不能取代原始证据。
2. 少量结构化“概览”与按需检索“细节”互补。对 Memoria，更准确的落地是“不可变证据账本 → 类型化可重建投影 → 当前话轮工作上下文”，而不是直接把 Advanced JSON Cards 当主库。
3. 实体、关系、有效时间、来源和冲突状态应成为一等元数据；检索先做权限与有效期过滤，再做稀疏/稠密召回和排序。

近期最有价值的实验是给对话检索块增加可追溯的上下文前缀。最不应照搬的是“按重要性删除记忆”、默认引入 GraphRAG，以及把生成式摘要或参数化记忆当成事实权威。

## 文章的具体做法

| 维度 | 章节做法 | 应如何理解 |
|---|---|---|
| 短期与长期 | 轨迹是单会话、按时间追加的原始事件；工作记忆是按当前任务筛选的动态子集；长期记忆跨会话绑定用户 ID，可合并、更新或淘汰 | 三者分别回答“发生过什么”“本轮要看什么”“长期知道什么”，不应共用一张可覆盖表 |
| 选择性写入 | 会话后用独立 LLM 抽取未来有用的事实，去掉临时过程，做抽象化和结构化 | 抽取结果是声明候选，不是已经证实的事实 |
| 存储格式 | Simple Notes 保存原子事实；Enhanced Notes 保存带上下文段落；JSON Cards 用层次键值；Advanced JSON Cards 再加 backstory、person、relationship、timestamp | 格式越丰富越利于消歧，但生成成本、重复和错误面也越大；格式本身不保证检索复杂度 |
| 框架策略 | Mem0 v2 写入时在 ADD/UPDATE/DELETE/NOOP 中决策；v3 改为 ADD-only，再在查询时融合语义、BM25、实体等信号。Memobase 用可配置 Profile + Event Timeline，并用缓冲批处理摊薄抽取成本 | 写入时覆盖保持库简洁但可能丢历史；只追加更可审计，但必须有时间、冲突和当前状态解析 |
| 检索 | 稠密向量补语义召回，BM25 补精确词，融合后可重排序；复杂问题可让 Agent 迭代搜索；索引期给每个块生成包含人物、时间、文档来源和意图的上下文前缀 | 上下文前缀是检索信号，回答仍应回到原块和证据；Agentic RAG 增加延迟和不确定性，不应覆盖简单查询 |
| 压缩与摘要 | 低重要性记忆进入压缩/删除候选；相似记忆聚类后生成摘要；从情景事实抽象为语义/程序记忆；冲突用版本管理 | 聚类和抽象适合生成派生概览；“删除”只能作用于可重建服务投影或缓存，不能作用于权威证据 |
| 实体与时间 | Advanced Cards 保存主体、关系、叙事背景和时间；GraphRAG 抽取实体/关系并生成社区摘要；过期知识带版本和生效/失效时间，在检索期过滤 | 人物关系和时间限定比“最新文本”更可靠；图索引只在真实查询需要跨实体关系时才值得投入 |
| 周期整理 | 在线增量吸收新证据，离线全量去重、合并、核查遗漏、限定冲突场景；章节建议 Proposer 提 diff、Reviewer 回看原始证据后审核，索引在合入后重建 | 可审查、可回滚、可重建的方向正确，但“两个 Agent + PR”是作者的工程建议，不是已建立的记忆算法标准 |
| 可执行记忆 | User as Code 用只增事实日志保存信息，再周期性生成带类型 Python 状态，以固定代码做聚合和约束检查 | 可借鉴“类型化状态 + 确定性规则”，不等于允许运行未经信任的 LLM 生成代码 |

## 一手来源核验

### 长期对话评测

- [LoCoMo 论文](https://arxiv.org/abs/2402.17753)和[官方数据仓库](https://github.com/snap-research/locomo)支持章节所述规模：50 段对话，平均约 300 轮、9K tokens，最多 35 个 session，并包含问答、事件摘要和多模态生成任务。论文也明确承认数据主要由 LLM 生成后人工修订、仅英语、个人照片的视觉连续性不足。因此它适合测长程文本记忆，不足以代表真实中文语音陪伴。
- [LongMemEval 论文](https://arxiv.org/abs/2410.10813)把能力拆为信息抽取、多会话推理、时间推理、知识更新和拒答，并提出 session decomposition、fact-augmented keys、time-aware query expansion。章节的“八项能力”和“三层次框架”是作者自己的归纳，不是 LoCoMo 的原始分类。

### 存储、更新与时间关系

- [Mem0 2025 论文](https://arxiv.org/abs/2504.19413)支持 v2 的候选事实抽取、与既有记忆比较、ADD/UPDATE/DELETE/NOOP，以及 Mem0-g 图记忆。2026 年的[官方 README](https://github.com/mem0ai/mem0#new-memory-algorithm-april-2026)说明 92.5/94.4 是含专有优化的托管平台结果；[OSS v2→v3 迁移文档](https://docs.mem0.ai/migration/oss-v2-to-v3)报告的是 91.6/93.4，并确认 OSS 为单次 ADD-only、语义 + BM25 + 实体加权，已移除图存储和 `relations`。章节把平台指标、时间推理能力与 OSS 迁移说明放在同一段，容易让读者误以为这些能力全部属于 OSS。
- [Memobase 官方仓库](https://github.com/memodb-io/memobase)支持“Profile + Event Timeline + per-user buffer”的描述。其性能、成本和延迟数字仍是项目方自报，不是独立对比结论。
- [Zep/Graphiti 论文](https://arxiv.org/abs/2501.13956)是章节未引用、但对个人记忆很直接的一手补充：它同时保存事件时间与系统写入时间，并为关系边记录 `valid_at/invalid_at` 和 `created_at/expired_at`。这比只保存一个 timestamp 更适合处理“后来才得知的旧事”和“现在已失效的旧关系”。
- [User as Code 论文](https://arxiv.org/abs/2606.16707)确实提出“append-only fact log + periodic typed-code checkpoint”，并报告结构化阶段仍可能压掉原子细节。它与章节作者同源、发布时间很新，宜视为待复现的研究原型。

### 检索、摘要与图结构

- [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)明确做法是在 embedding 和 BM25 建索引前给块添加专属上下文。其 49% 是 Contextual Embeddings + Contextual BM25 在特定文档域、模型和 top-20 设置下把失败率从 5.7% 降到 2.9%；67% 还包含 reranker，成本 $1.02/百万文档 tokens 依赖文中给定的块大小、文档长度和 prompt caching 假设。这不是语音用户记忆上的普遍收益保证。
- [RAPTOR 论文](https://arxiv.org/abs/2401.18059)和[官方实现](https://github.com/parthsarthi03/raptor)支持“块 → 聚类 → 摘要 → 递归树”，目标是让检索同时覆盖细节和高层主题。
- [Microsoft GraphRAG 论文](https://arxiv.org/abs/2404.16130)的主要贡献是实体/关系图、层次社区和面向全语料全局问题的社区摘要；[官方查询文档](https://microsoft.github.io/graphrag/query/overview/)区分 entity-oriented local search 与 map-reduce global search。原论文使用 exact string matching 做实体合并，不能据此断言图天然提供可靠实体消歧或确定性多跳查询。
- [OpenViking 官方架构文档](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/01-architecture.md)、[存储文档](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/05-storage.md)和[上下文层文档](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/03-context-layers.md)支持 `viking://` 虚拟文件系统：内容层以 AGFS/RAGFS 组织，另有向量索引层；目录可异步生成 L0 `abstract.md` 与 L1 `overview.md`，L2 保留原始文本、代码或多模态内容，查询时按需加载。它还提供关系 API，但官方文档没有保证“按访问频率动态分配空间”或“每条知识都必须有 Wikipedia 双向链接”。章节提出的 Markdown/Git/PR 审核和强制双向链接仍是作者扩展的知识管理方案，不应写成 OpenViking 的既定能力。

### 重要性、遗忘与反思

- [Generative Agents 论文](https://arxiv.org/abs/2304.03442)的检索分数由 recency、relevance、importance 组成：recency 按“自上次访问后的时间”指数衰减，relevance 用向量相似度，importance 由 LLM 评估 poignancy；累计 importance 达阈值后生成带证据指针的高层 reflection。它保留完整 memory stream，并未提出按该分数删除原始记忆。
- [MemoryBank 论文](https://arxiv.org/abs/2305.10250)使用简化的遗忘曲线，根据经过时间衰减，并在记忆被召回时增强 memory strength；作者明确称其为 exploratory、highly simplified。
- 因此，章节提出的“访问频率 + 时间衰减 + 情感强度 + 信息独特性”四因素公式是启发式综合，不是上述论文中经过验证的统一算法；情感强度也不能当作事实可信度。

### 章节作者自己的实验记录

- [四种存储格式 latest evidence](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/user-memory/validation/latest.json)并不支持正文“Advanced JSON Cards 表现最好”的一般化叙述：60 个合成用例中 Enhanced Notes 的 overall mean reward/pass rate 为 0.8573/86.7%，高于 Advanced Cards 的 0.8156/81.7%；Advanced Cards 的 token 用量约 1.55M，也高于 Enhanced Notes 的 0.99M。该结果说明应按数据与查询选择表示，而不是把卡片复杂度当作能力等级。
- [上下文检索 + 双层记忆 latest evidence](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/contextual-retrieval-for-user-memory/validation/latest.json)对总体方向有正向自证：plain/contextual/dual-layer 的 mean reward 分别为 0.7750/0.8135/0.9167，pass rate 为 78.3%/86.7%/95.0%。但这是作者自建合成集、单次模型栈和 LLM judge，且没有“Enhanced Notes + RAG”对照臂，不能把增益单独归因于 Advanced Cards。
- 其他自有证据也提醒不要把章节措辞当定律：[检索流水线实验](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/retrieval-pipeline/validation/latest.json)中 reranker 的 MRR/nDCG 低于 dense 与 RRF；[结构化索引实验](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/structured-index/validation/latest.json)只有 8 个问题，GraphRAG 与 RAPTOR 在 relationship/multi-hop citation recall 上同分。

## 对 Memoria 的启示

### 保持现有权威边界

现有 [ADR-0001](../adr/0001-evidence-ledger-and-rebuildable-projections.md)、[ADR-0028](../adr/0028-typed-memory-projections-and-derived-experiments.md) 和[记忆架构 v1](../memory-persona-architecture-v1.md)已经比章节的通用方案多了关键治理边界：不可变 Evidence Event 是权威来源；claim、episode、关系、摘要、向量和第三方框架结果均为可重建投影；工作记忆只在当前话轮组装。无需改成 Mem0、OpenViking 或 GraphRAG 主库。

### 最值得做的派生实验

1. 在隔离检索投影中，为每个规范化对话块生成短前缀，至少包含 `account/speaker authority`、人物、关系、`occurred_at`、session/turn、主题和该块在事件链中的作用。
2. 前缀只能影响召回排序，不能生成 owner 身份、权限或事实；最终回答必须返回原始话轮和 source event ID。前缀错误、缺失或模型失败时退回原块索引。
3. 先以全文/BM25 + 稠密向量 + 实体/时间/状态过滤为基线。只有固定中文评测证明收益后再启用 reranker；只有频繁出现真实关系型、多跳或全局归纳查询后再评估图索引。
4. 时间字段保持双维度：现实发生/有效区间与系统观察/修订时间分离。冲突不是简单“最新覆盖”，而是保留 `candidate/confirmed/disputed/retracted`、适用对象和时间限定。

### 压缩与遗忘策略

- 重要性分数只用于工作上下文预算、召回排序、摘要刷新频率和冷热存储，不用于删除权威证据。
- 评分至少区分当前查询相关性、已确认程度、产品关键性、时间有效性、重复度和访问历史。情绪可影响陪伴表达或回顾呈现，但不能提高事实置信度或权限。
- 聚类摘要、人物概览和“程序记忆”都应保留生成版本及 source event 列表，并能从原始证据重建。定期整理应验证否定词、相对时间、说话人归属、纠错和撤销是否仍被保留。
- 对护照期限、过敏、授权、删除等高风险规则，采用可信代码中的固定类型与确定性检查；不要直接执行 LLM 生成的 Python。

### 产品评测必须补足语音现实

除 LoCoMo/LongMemEval 类文本问题外，固定中文评测还应覆盖：ASR 错字与用户纠错、相对日期、迟到终稿、否定和反讽、同名人物、主人/访客/ambiguous、跨 generation fence、撤销/删除、未知问题拒答，以及摘要或前缀中的权限污染。门禁至少同时看抽取 precision/recall、Recall@K/nDCG、时间正确率、来源归因、冲突处理、跨账户/非主人泄漏、延迟和 token，而不能只看 LLM judge 总分。

## 文章局限

- 章节是教学性综合，Simple Notes 的 “O(1)” 等说法由具体索引实现决定；Advanced Cards、四因素重要性、双 Agent 审核等多处是作者设计，不是所引论文的原始结论。
- 章节自有实验可审计性较好，但仍是自建合成用例、单一或少量模型栈和 LLM judge，未独立复现；部分正文结论与 latest evidence 不一致。
- LoCoMo 主要是英语合成长对话，Anthropic 是文档检索实验，GraphRAG 是全语料 sensemaking；三者都不能直接证明中文实时语音、说话人权限和终身个人档案上的效果。
- 上下文前缀、聚类摘要、实体关系和定期重写都会引入新的 LLM 断言；若没有 source pointers、版本和回放测试，压缩会把早期误读固化并代际传播。
- 章节对同意、敏感字段分级、非主人污染、跨租户隔离、删除传播和主动建议的风险控制讨论不足。陪伴产品中的“主动服务”必须受权限、事实确认和风险等级约束。
- [User as Engram 论文](https://arxiv.org/abs/2606.19172)评测的是文本事实写入 hash N-gram memory rows，没有脸、声纹或其他多模态实验；章节把它延伸为多模态 embedding 存储，并称“一条 embedding 占一个 token”，缺少一手证据，不应作为声纹或人脸记忆方案依据。
