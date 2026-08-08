# 《AI Agent 深入理解》第 8 章 Agent 持续进化研究笔记

> 调研日期：2026-08-07<br>
> 原文：[《Agent 的持续进化》](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md)（固定于 `main` 提交 `2b949521f1aad778ffb1b55a60361ce9caa2d8f8`）<br>
> 范围：自我进化的定义、学习闭环、更新载体、评估、风险和对 Memoria 的审查切入点。文章中的实验结果与论文/官方文档的能力主张分开记录，不把教学性建议当成已验证定律。

## 结论

1. **自我进化不等于保存记忆。** 保存轨迹只能让 Agent 找回旧案例；只有经过评价、跨轨迹对照、归纳和验证，并且使后续行为发生可复现的改进，才算学会经验。[正文 L3-L11](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L3-L11)
2. **当前最可落地的“自主学习”在模型外围。** 在线任务只记录不可变证据；离线系统诊断根因、生成候选知识/Prompt/Skill/程序/参数更新，再经独立回归、安全检查、灰度和回滚后才影响下一轮运行。[正文 L239-L257](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L239-L257)
3. **学习信号必须是结构化诊断而不是总分。** 结果、规则/权限/动作过程、语言质量三层分别取环境真值、确定性规则和 Rubric 证据；高风险或低置信度结果不能自动成为学习信号。[正文 L19-L49](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L19-L49)
4. **更新位置由能力的可表达性决定。** 事实和行动经验适合知识库，可语言化的规则适合 Prompt/Skill，稳定且可检查的流程适合程序/Harness，高维感知和风格才考虑参数训练；四者可以组合，不能用参数记忆承载硬权限规则。[正文 L63-L78](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L63-L78)
5. **“更新器有效”与“任务 Agent 受益”是两件事。** 候选修改可能正确，但检索/路由没有激活它，或者任务模型激活后不遵循。因此必须分别测候选有效率、产物激活率、激活后的遵循成功率和留出任务增益。[正文 L255-L280](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L255-L280)
6. **安全边界是进化闭环的一部分。** 证据与指令隔离、候选与正式能力隔离、验证器/测试/发布门槛/审计日志/稳定备份不可被业务 Agent 修改；否则一次注入、恶意依赖或坏验证器会把局部错误扩散成长期能力。[正文 L297-L305](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L297-L305)

## 能力模型

### 自我进化的判定条件

本章给出的隐含判定式是：

```text
可追溯运行经验 + 后续行为改变 + 独立验证无明显退化 = 持续进化的最小闭环
```

其中“后续行为改变”不能只看上下文内的即时适应；需要在新的、未参与提炼的任务上复现，并同时检查旧能力保持、规则更新和安全边界。[正文 L5-L17](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L5-L17) [正文 L332-L342](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L332-L342)

### 四种更新载体

| 载体 | 适合承载 | 优点 | 典型风险 | 首选门禁 |
|---|---|---|---|---|
| 经验知识库 | 条件化事实、行动经验、例外、来源 | 快速、可追溯、可检索 | 检索不到、冲突、错误经验固化 | 跨轨迹支持、来源和时间、留出迁移 |
| Prompt / Skill | 能清楚写成自然语言的原则和操作规范 | 可解释、作用域可控 | 规则膨胀、冲突、被忽略 | 最小 diff、边界集、旧任务保留集 |
| 程序 / Harness | 稳定流程、路由、重试、权限和状态检查 | 确定性、可测试、成本低 | 代码和依赖维护、危险副作用 | 沙盒、静态/安全扫描、失败重放、回归 |
| 模型参数 | 感知、韵律、隐式决策和整体风格 | 泛化强、运行时开销低 | 遗忘、漂移、回归成本高 | 脱敏数据、独立回归、安全和分布外测试 |

这不是由“出现次数”决定的层级。稳定的付款审批规则仍应由服务端代码兜底；快速变化的风格也可以参数化。先把故障定位到最小、最容易验证和回滚的载体，再考虑上升到更大的搜索空间。[正文 L65-L78](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L65-L78) [正文 L207-L215](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L207-L215)

### 更新尺度

文章还区分“更新什么”和“如何生成/管理更新”。搜索尺度可以从小到大扩展为：

```text
单条规则或记忆 -> 结构化上下文 -> 工作流 -> Harness 代码 -> 候选生成/管理优化器
```

局部规则或条目补丁默认优先，只有跨组件故障长期无法解决、或上下文管理方法本身成为瓶颈，才上升到工作流/Harness/优化器。搜索空间越大，越需要把评价器、权限边界和留出测试放在可修改范围之外。[正文 L217-L227](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L217-L227)

## 可执行闭环

### 在线循环与离线循环

```text
在线执行
  -> 不可变轨迹、工具/环境结果、版本与权限快照
  -> 三层验证器（结果 / 过程 / 质量）
  -> 带证据的结构化学习信号
  -> 跨轨迹聚类、支持/反驳和根因诊断
  -> 候选更新（知识 / Prompt / Skill / 程序 / 参数）
  -> 独立验证（迁移 / 保留 / 安全 / 成本）
  -> canary 发布、观察、回滚或过期
  -> 下一轮在线执行
```

在线循环不直接改写正式 Agent；离线循环才允许生成候选并合并版本。原始轨迹、单次分析、正式经验文档/代码应分层保存，正式产物必须能回到支持和反驳它的轨迹。[正文 L80-L92](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L80-L92) [正文 L239-L257](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L239-L257)

### 三层轨迹验证

| 层 | 回答的问题 | 首选证据 | 不应交给模型猜测的部分 |
|---|---|---|---|
| 结果验证 | 事情是否真的办成 | 测试结果、数据库状态、工具返回、退款金额 | 是否成功、是否真实发生 |
| 过程验证 | 是否按允许的方式办成 | 权限、政策、动作序列、数据访问日志 | 身份、隐私、承诺与动作一致性 |
| 质量验证 | 是否办得合适 | 有 Rubric 的 LLM/人工评审，逐项引用轨迹 | 不能只输出模糊总分 |

客服例子至少包含任务结果、规则遵从、隐私边界、事实可靠性、承诺—行动一致性、表达质量和合规变通七项。验证器需要专家校准集；高风险或低置信度案例进入第二验证器或人工复核。验证器只负责评价和证据，独立诊断器再决定改 Prompt、知识还是 Harness。[正文 L23-L49](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L23-L49)

### 经验沉淀的门槛

一次成功只产生 `candidate`。更稳妥的经验文档至少包含：

- 任务族、能力需求和适用前置条件；
- 推荐策略、禁止做法和例外；
- 支持与反驳的轨迹 ID、环境/版本和最近验证时间；
- 进入正式库的门槛、撤销条件和回滚版本。

只有跨轨迹支持、环境结果相符并在留出任务上产生正向迁移，候选才应进入正式文档。失败和部分成功同样是学习材料，用于排除错误路径，不应只保留幸存的成功案例。[正文 L82-L104](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L82-L104)

### 自我修改的发布协议

程序/Harness 修改应明确“可证伪的变更契约”：失败证据、推断根因、目标组件、候选 diff、预期修复、潜在回退，以及分别验证修复与保留行为的用例。候选只能写入隔离目录，依次通过静态检查、单测、安全扫描、原始失败轨迹重放、旧任务回归、canary 和 rollback-ready 检查；生成补丁的 Agent 不得修改稳定代码、验证器、审计日志或发布门槛。[正文 L183-L201](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L183-L201)

## 评估框架

### 必测指标

| 指标 | 说明 | 证据来源 |
|---|---|---|
| 候选修改有效率 | 生成的候选在独立验证中的接受率及增益 | 候选 manifest、验证结果 |
| 产物激活率 | 正确场景是否加载新知识/Skill/工具 | 检索、路由、工具调用轨迹 |
| 遵循成功率 | 激活后是否按新规则执行 | 动作序列、过程验证器 |
| 留出任务增益 | 未参与提炼的新任务是否改善 | held-out 成功率、质量和成本 |
| 迁移准确率 | 换表述、用户或局部环境后能否复用 | transfer 集 |
| 规则替换恢复 | 新规则出现后恢复正确所需任务数 | change 集、旧规则引用率 |
| 保持率/回退率 | 未变化能力是否保留、旧案例是否退化 | retention 集、回归集 |
| 负迁移率 | 新经验是否伤害不适用场景 | 对照组和边界集 |
| 安全通过率 | 隐私、拒绝和权限边界是否漂移 | 安全 Rubric、确定性门禁 |
| Token/延迟/存储 | 进化收益是否以不可接受成本换取 | provider receipt、运行遥测 |
| 长期工程质量 | 复杂度、兼容性、所有权和可维护性是否恶化 | 静态分析、架构检查、维护记录 |

这些指标必须分阶段报告，不能用一次最终准确率替代学习曲线、迁移、规则替换和保持能力。[正文 L261-L280](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L261-L280) [正文 L332-L342](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L332-L342)

### 章内实验的证据边界

章节 README 明确区分离线入口和真实运行证据；8-1、8-2、8-3 的 JSON 没有顶层 source hash manifest，审计强度低于 8-5、8-7，不能把提交时存在的结果等同于运行时源码已被固定。[章节实验 README L7-L25](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/chapter8/README.md#L7-L25)

| 实验 | 固定证据观察 | 应如何解读 |
|---|---|---|
| 8-1 轨迹验证 | 28 次真实客服调用、8 条专家标注；维度完全一致率 `0.929`。但 `compliant_flexibility` 失败召回率为 `0`，`factual_reliability` 和 `promise_action_consistency` 失败精确率为 `0.667`；证据还标记 `stable_key_violation_detection=false`、`all_manuscript_result_claims_observed=false`。 | 支持“多维诊断比标量更可定位”的方向，不支持把验证器概括为已稳定识别所有关键违规。 [evidence](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/chapter8/trajectory-verifier/validation/real_20260729T165247Z/evidence.json) |
| 8-2 GAIA 经验 | 无经验和单轨迹摘要迁移成功率均 `0.50`；跨轨迹知识文档仅 `0.25`，相对无经验负迁移 `0.25`。 | 章节把“跨轨迹文档更易迁移”作为待检验假设；这次运行是负结果，不能把知识文档收益当普遍结论。 [evidence](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/chapter8/gaia-experience/validation/real_20260729T164012Z/evidence.json) |
| 8-5 自我修改 | 确定性与真实 Coding Agent 候选都将不可重试调用降至均值 `1.0`，临时错误恢复率 `1.0`，旧任务回归 `0`，得到 `release_to_canary`；故意过宽的控制候选恢复率 `0` 并被拒绝。候选沙盒无网络、只读根文件系统、非特权用户，trusted surface hash 前后不变。 | 在一个窄的重试策略故障上验证了“诊断契约 + 候选隔离 + 失败重放 + 回归 + 发布门”的流程，不等于通用代码自修改已证明。 [evidence](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/chapter8/self-modifying-agent/validation/latest.json) |
| 8-7 长期评估 | `static/append_only/evolving` 三臂、3 seeds、每臂 14 题。evolving 的迁移、规则替换、保持率分别为 `1.0/1.0/1.0`，过时规则引用率 `0`，负迁移 `0.125`；append-only 虽迁移 `1.0`，规则替换为 `0`、过时引用 `1.0`、负迁移 `0.375`。 | 证明评估 Harness 能区分静态、只追加与可替换版本；任务规模和规则空间仍很窄，不能外推到 Memoria 的真实语音、身份和隐私链路。 [evidence](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/chapter8/self-evolution-eval/validation/latest.json) |
| 8-6 Hermes | 文章称它完成了“阅读→对照→选题→修改→验证”，但明确承认没有证明下游任务成功率提升。 | “能自改一次”不等于“长期能力得到收益”；必须另做消融和留出评估。 [正文 L229-L237](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L229-L237) |

## 对 Memoria 的审查切入点

这不是对当前代码的完成度结论，而是一份把第 8 章落到本项目时应逐项核验的清单。现有项目已经把 Evidence ledger、可重建投影、candidate/confirmed、授权和版本回滚作为重要边界；这些边界应成为持续进化的可信根，而不是由 Agent 自己绕过。参见 [ADR-0001](../adr/0001-evidence-ledger-and-rebuildable-projections.md)、[ADR-0028](../adr/0028-typed-memory-projections-and-derived-experiments.md) 和[记忆人格架构](../memory-persona-architecture-v1.md)。

1. **把用户记忆与行动经验分开。** 用户记忆回答“用户/世界是什么样”，行动经验回答“在什么条件下应该怎样行动”；经验学习不能因为一条对话事实被写入长期记忆就自动改变 Agent 的工具路由、权限或回复策略。[正文 L5-L7](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L5-L7)
2. **建立统一的 `LearningSignal`/进化控制面。** 每次候选学习信号至少绑定任务族、session/turn/generation/tool epoch、说话人/权限快照、工具与环境状态、结果/过程/质量三层 verdict、证据 ID、置信度、支持/反驳轨迹和版本。不同入口（对话、工具、记忆、Persona、实时语音）不要各自维护一套“自动学习” if。
3. **保持语音隐私和身份 fail-closed。** 结果层可测任务是否完成，过程层必须测 owner/guest/ambiguous、历史资格、工具权限、实际来源和跨 generation fence；LLM Judge 只能评价质量，不能凭字幕或模型置信度授予主人身份、主人历史或敏感工具权限。
4. **先做低风险、可回滚的外部产物。** 优先将稳定的控制约束编译为确定性 Harness，将可解释经验放入带来源和时效的 candidate 知识/Skill；直接参数训练应排在后面，并必须保留独立安全集、遗忘集和回滚版本。
5. **把“更新正确但没被使用”单独测出来。** 记录候选是否被检索/路由激活、激活后是否遵循、是否在新的中文多轮语音任务中改善；不能只看总回复分数或“写入成功”日志。
6. **引入离线睡眠学习门控。** 按新增已评价轨迹数、错误簇频率或固定窗口触发；批量合并、标记冲突/过期、生成候选，跑迁移集/保留集/安全集后再发布。拒绝候选和负面结果必须可检索，防止下一轮重复同一错误。
7. **把真实语音验收纳入进化指标。** 除文本任务外，应覆盖 ASR 错字与纠错、迟到终稿、打断/回声、speaker authority、generation fence、访客污染、工具副作用、断线恢复和客户端实际播放；“更新后字幕变好”不证明实际音频或权限边界变好。

建议首先用一组固定中文任务建立三臂基线：`static`（不持久化经验）、`append_only`（只追加不替换）和 `evolving`（版本化替换、保留来源和回滚），再逐步开放 Candidate → Canary → Stable。每轮同时报告迁移、规则更新恢复、旧能力保持、负迁移、隐私/权限泄漏、候选激活和成本。

## 一手来源与适用边界

| 来源 | 章节使用方式 | 适用边界 |
|---|---|---|
| [Reflexion](https://arxiv.org/abs/2303.11366) | 自然语言反思可生成候选教训 | 反思不是环境证据，仍需结果验证、跨轨迹支持和留出迁移。[正文 L90-L104](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L90-L104) |
| [GAIA](https://arxiv.org/abs/2311.12983) | 多步骤搜索、阅读、文件和计算任务的评测载体 | 是通用助手基准，不等于中文实时语音或长期个人档案；本章 8-2 的实际知识文档组还出现负迁移。 |
| [Voyager](https://arxiv.org/abs/2305.16291) | 自动课程、可执行技能库、环境验证和迁移曲线 | Minecraft 环境反馈很强；现实业务需要额外的权限、隐私、审计和人工高层监督。[正文 L245-L247](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L245-L247) |
| [PreAct](https://arxiv.org/abs/2606.17929) | 重复浏览器任务的流程编译和加速 | 加速必须同时有动作前/后检查、最终状态检查和独立重置验证；动作被点击不等于任务成功。[正文 L160-L179](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L160-L179) |
| [ACE](https://arxiv.org/abs/2510.04618) / [MCE](https://arxiv.org/abs/2601.21557) | 增量上下文条目、内外循环和上下文管理方法进化 | 研究原型支持“不要反复重写整份 Prompt/记忆”的方向，不自动提供生产权限或安全门禁。[正文 L217-L227](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L217-L227) |
| [Harness Updating Is Not Harness Benefit](https://arxiv.org/abs/2605.30621) | 提出拆分更新能力和受益能力 | 说明评估要隔离更新器、任务模型和激活/遵循瓶颈；不应由一个端到端分数猜根因。 |
| [Anthropic 项目记忆文档](https://code.claude.com/docs/en/memory) | 有界索引、按需加载和主动整理的记忆案例 | 文档说明分层和容量治理，不等同于固定的后台“睡眠学习”；仍需自有证据、版本和审批。 |
| [Harness Engineering](https://lilianweng.github.io/posts/2026-07-04-harness/) | 将组件、经验、决策和评估历史作为可观测的可编辑对象 | 文章/研究建议不能替代 Memoria 自己的安全边界和真实设备验收。 |

## 局限

- 第 8 章是教学性综合，很多 2025/2026 论文为预印本，实验数字依赖特定模型、数据、提示和评委；不能直接推导 Memoria 的生产收益。
- 章节配套实验虽然保留真实调用证据，但样本仍小且任务多为可控环境；8-1 有维度误差，8-2 有负结果，8-7 的规则空间和任务数量都有限。README 还明确提示 8-1/8-2/8-3 缺少顶层 source hash manifest。
- 对开放科研、战略和长期产品质量，流程完成、测试通过或用户满意都可能是代理指标；实现漂移、过度乐观、忽略阴性结果和搜索同质化仍需要人类定义目标、审查评价标准和决定停止。[正文 L282-L295](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L282-L295)
- “睡眠学习”是离线整合的比喻，不要求夜间运行；真正要求的是采集与整理分离、门控、冲突处理、验证、修剪和可回滚。[正文 L307-L330](https://github.com/bojieli/ai-agent-book/blob/2b949521f1aad778ffb1b55a60361ce9caa2d8f8/book/chapter8.md#L307-L330)

## 本次落地边界

Memoria 已把上述控制面、候选生命周期、三臂 holdout、受限 replay bundle、generation
receipt、删除 tombstone 和独立 PostgreSQL/RLS 接线落到代码；在线 Agent 仍不会自行调用
LLM judge。真实评估器需要从 `trajectory-replay-bundles` 取输入、在隔离环境完成判定后提交
`trajectory-evaluations`，因此“自动采集/候选治理”与“独立质量评估”仍是两个可观测、可替换
的部署组件，不能把本地 synthetic/control-plane 结果当作生产收益。
