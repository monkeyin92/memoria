# DuplexAgent-RSI 协调框架采纳决策 (ADR-2026-10-26)

**状态**: 待批准  
**决策日期**: 2026-10-26  
**影响范围**: 语音协调层、测试体系  
**预估工作量**: 8-10 周 (3 阶段)

## 背景

港中深与安菲翁科技团队 2026 年 10 月发表 DuplexAgent-RSI 论文 (arXiv 2610.11299v1)。它将全双工语音 Agent 的协调层拆解为六个可显式编辑的模块,配合时序对话仿真器与递归自迭代闭环,实现模拟器协作分从 54.6 提升到 85.3 (涨 30.7 分)。

Memoria 现有协调逻辑散布在 6 个文件中,领域边界模糊、失败归因困难、回归测试缺失。本决策评估 DuplexAgent-RSI 框架对 Memoria 的适用性。

## 决策

**采纳 DuplexAgent-RSI 的协调契约与时序仿真器,不采纳 RSI 自迭代闭环。**

具体行动:

1. 引入六模块协调契约 (Policy / Intake / Hold / Worker / Deliver / Memory)
2. 构建时序对话仿真器与 19×22 测试矩阵
3. 不实施 LLM 自动改代码的 RSI 闭环 (受儿童内容合规约束)

## 理由

### 高价值部分

**1. 协调契约系统化**

现有问题: 协调逻辑散布在 `utterance_router.py` / `interruption_guard.py` / `delegation_coordinator.py` / `task_manager.py` / `media_session_input.py` / `media_session_commit.py` 六个文件中,职责边界模糊。

采纳后: 清晰的六模块契约,每个失败有明确归属。

- Policy: 路由与打断决策 (`UtteranceRouter` + `InterruptionGuard`)
- Intake: VAD/ASR 包门禁 (`media_session_input`)
- Hold: 等待决策与时间管理 (`media_session_commit` 的等待部分)
- Worker: 后台任务调度 (`DelegationCoordinator` + `TaskManager`)
- Deliver: 输出投递时机 (`media_session_commit` 的投递部分)
- Memory: 轮次提交与上下文 (`ConversationProjection`)

契约文件: [services/agent/src/orchestration/coordination_contracts.py](services/agent/src/orchestration/coordination_contracts.py)

**2. 时序测试覆盖**

现有问题: 单元测试覆盖率 92%,但缺少端到端时序回归。打断、犹豫、延迟结果、中途改主意等场景只能物理真机测试,复现困难。

采纳后: 19 类请求 × 22 种交互时序矩阵,每种组合一个可复现的测试案例。

- 19 类请求: 简单问答 / 工具调用 / 学习辅导 / 睡前故事 / 记忆记录 / 设备控制 / ...
- 22 种时序模式: 正常流程 / 中期打断 / 晚期打断 / 犹豫停顿 / 连续打断 / 延迟结果 / 过期失效 / ...

契约文件: [tests/voice_coordination/simulator_contracts.py](tests/voice_coordination/simulator_contracts.py)

### 受限部分

**RSI 自迭代闭环不可采纳**

论文依赖 Harness Editor (Codex + GPT-5.6) 自动编辑协调规则代码。Memoria 受儿童内容合规约束:

- `AGENTS.md` 明确要求: 儿童可听内容、回复脚本及安全拦截规则需产品部与合规部审批
- `.github/pull_request_template.md` 包含「儿童可听内容」审批流程
- 协调规则直接影响机器人语音回复时机,属于儿童可听内容交付链路

LLM 自动改代码无法通过合规审批流程,因此 RSI 闭环不部署到生产环境。

可以离线使用 RSI 辅助分析日志、生成测试案例,但最终代码改动仍由工程师完成。

## 架构设计

两份架构文档已完成:

1. [docs/architecture/coordination-harness.md](docs/architecture/coordination-harness.md)  
   六模块协调契约、失败分类法、模块间接口协议、实施路径

2. [docs/architecture/temporal-simulator.md](docs/architecture/temporal-simulator.md)  
   时序对话仿真器、19×22 测试矩阵、sim/run 双模式、YAML 案例格式

## 实施路线图

分三阶段推进,每阶段独立可验证。

### 阶段一: 契约层 (2 周)

**目标**: 定义六模块契约,不改变现有行为。

产出:

- [x] `services/agent/src/orchestration/coordination_contracts.py` 契约定义 (已完成)
- [ ] 每个模块添加契约类型注解,现有逻辑保持不变
- [ ] 添加失败枚举到 Metrics,追踪各模块失败率

验收:

- 所有单元测试通过
- 代码覆盖率不降低
- CI 绿色

### 阶段二: 仿真器基础设施 (4 周)

**目标**: 构建时序仿真器,跑通第一批测试案例。

产出:

- [x] `tests/voice_coordination/simulator_contracts.py` 契约定义 (已完成)
- [x] `tests/voice_coordination/README.md` 使用文档 (已完成)
- [x] 3 个 YAML 示例案例 (已完成)
- [ ] `TimeDriver` sim 模式实现
- [ ] `UserVoiceSimulator` sim 模式实现
- [ ] `BackgroundTaskSimulator` 实现
- [ ] `CoordinationScorer` 实现
- [ ] `TestCaseGenerator` 生成 19×22 矩阵的前 50 个案例
- [ ] CI 集成,每次 PR 跑仿真回归

验收:

- 第一批 50 个案例通过
- 协作分基线建立
- 回归测试纳入 CI

### 阶段三: 真机模式与覆盖补全 (2-4 周)

**目标**: 补齐 run 模式,跑通全部 19×22 矩阵。

产出:

- [ ] `RealHardwareDriver` 实现,驱动真实 ESP32
- [ ] run 模式真实音频流播放
- [ ] 补全 19×22 矩阵的剩余案例 (约 368 个)
- [ ] 性能基线与回归门禁

验收:

- 全部 418 个案例通过 (19×22)
- 真机测试可复现
- 回归门禁阻止协调质量劣化

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
| :--- | :--- | :--- |
| 契约重构破坏现有行为 | 高 | 阶段一不改逻辑,只添加类型注解。充分单元测试覆盖。 |
| 仿真器与真实管线行为不一致 | 中 | 阶段三补齐 run 模式,真机验证。仿真器只模拟时序,不模拟 ASR/LLM。 |
| 测试案例维护成本高 | 中 | YAML 声明式案例,易读易写。生成器自动化生成,减少手工维护。 |
| RSI 闭环无法部署 | 低 | 预期内。协调契约与仿真器本身已带来高价值,RSI 为可选增强。 |

## 替代方案

**方案 A: 不采纳,保持现状**

优点: 无改动风险  
缺点: 协调逻辑继续模糊,回归测试依赖真机,故障追踪困难

**方案 B: 只采纳契约,不做仿真器**

优点: 改动量更小  
缺点: 失去端到端回归能力,协调质量无法量化

**方案 C: 全盘采纳包括 RSI**

优点: 论文完整复现  
缺点: 违反儿童内容合规约束,不可行

## 决策权

- 技术方案: PM (monkeyin) 批准
- 实施排期: PM 与开发团队协商
- 合规边界: 产品部与合规部确认

## 参考资料

- 论文: [DuplexAgent-RSI: Recursive Self-Improvement for Full-Duplex Voice Agents](https://arxiv.org/abs/2610.11299v1)
- 代码仓库: [DuplexAgent-RSI Demo](https://amphionteam.github.io/DuplexAgent-RSI-Demo/)
- 内部调研: [前置调研笔记](20261026-duplexagent-investigation.md)
