# DuplexAgent-RSI 框架落地实施计划

**计划 ID**: P2-09  
**所属项目**: Memoria 语音协调层优化  
**创建日期**: 2026-10-26  
**负责人**: PM (待分配具体工程师)  
**预估周期**: 8-10 周

## 概述

根据 [duplexagent-rsi-adoption-decision.md](duplexagent-rsi-adoption-decision.md) 决策,本计划分三阶段落地 DuplexAgent-RSI 协调框架,提升 Memoria 全双工语音协调质量与可测试性。

阶段一引入契约层,阶段二构建时序仿真器,阶段三补齐真机模式与测试覆盖。

## 阶段一: 契约层 (2 周)

**目标**: 定义六模块协调契约,不改变现有行为。

### 任务清单

#### Task 1.1: 完善契约定义 (2 天)

- [x] `coordination_contracts.py` 基础契约定义 (已完成)
- [ ] 补充 `PolicyModule.route_utterance()` 完整签名
- [ ] 补充 `IntakeModule.admit_speech_packet()` 完整签名
- [ ] 补充 `HoldModule.decide_hold()` 完整签名
- [ ] 补充 `WorkerModule` 的 `delegate_task()` 与 `admit_output_intent()` 签名
- [ ] 补充 `DeliverModule.decide_delivery()` 完整签名
- [ ] 补充 `MemoryModule.commit_turn()` 完整签名
- [ ] 补充 `CoordinationHarness.coordinate_user_input()` 端到端签名

验收: 所有 Protocol 方法签名完整,类型检查通过。

#### Task 1.2: 现有代码添加契约注解 (5 天)

按文件逐个添加类型注解,不改变行为逻辑:

- [ ] `utterance_router.py`: `route()` 方法返回 `PolicyRoute`
- [ ] `interruption_guard.py`: 打断判定返回 `PolicyFailure | None`
- [ ] `media_session_input.py`: `_admit_vad_start()` / `_asr_precedes_pending_turn()` 返回 `IntakeRejectReason | None`
- [ ] `media_session_commit.py`: commit 决策函数返回 `HoldDecision`
- [ ] `delegation_coordinator.py`: `DelegationRequest` 对齐到 `WorkerDelegation`
- [ ] `task_manager.py`: `ToolTask` 添加 `WorkerFailure` 注解

验收:

- 类型检查无报错
- 所有单元测试通过
- 代码覆盖率不低于 92%

#### Task 1.3: 失败 Metrics 埋点 (3 天)

- [ ] 在 `MetricsRegistry` 添加六模块失败计数器
- [ ] 每个模块失败枚举发生时记录 Metrics
- [ ] Grafana 面板展示各模块失败率
- [ ] 建立基线失败率

验收:

- Grafana 面板可查询各模块失败率
- 基线失败率文档化

### 阶段一交付物

- [x] `coordination_contracts.py` (已完成)
- [ ] 六个文件的类型注解补全
- [ ] Metrics 失败率面板
- [ ] 基线失败率报告

### 阶段一风险

- 类型注解可能揭示现有逻辑的隐含假设不一致
- 缓解: 先标注类型,发现不一致时保持现有行为,记录技术债

## 阶段二: 仿真器基础设施 (4 周)

**目标**: 构建时序仿真器,跑通第一批测试案例。

### 任务清单

#### Task 2.1: TimeDriver 实现 (3 天)

- [x] `simulator_contracts.py` 契约定义 (已完成)
- [ ] `tests/voice_coordination/time_driver.py` sim 模式实现
- [ ] 虚拟时钟推进逻辑
- [ ] 事件优先级队列
- [ ] 单元测试

验收:

- `TimeDriver` 通过单元测试
- 可调度事件并按时序触发

#### Task 2.2: UserVoiceSimulator 实现 (5 天)

- [ ] `tests/voice_coordination/user_voice_simulator.py` sim 模式实现
- [ ] 伪 PCM 帧生成 (携带 VAD 标记)
- [ ] 用户输入序列调度
- [ ] 打断 / 犹豫 / 停顿 / 恢复时序模拟
- [ ] 单元测试

验收:

- 可按 `UserInputSpec` 序列生成伪 PCM 帧
- VAD 时序正确

#### Task 2.3: BackgroundTaskSimulator 实现 (3 天)

- [ ] `tests/voice_coordination/background_task_simulator.py` 实现
- [ ] 工具调用延迟返回
- [ ] 任务过期失效
- [ ] Fence 栅栏模拟
- [ ] 单元测试

验收:

- 可模拟工具调用延迟与过期
- Fence 栅栏正确触发

#### Task 2.4: CoordinationScorer 实现 (5 天)

- [ ] `tests/voice_coordination/coordination_scorer.py` 实现
- [ ] 终端失败分类器 (8 种失败类型)
- [ ] 协作分计算公式
- [ ] 时间轴证据记录
- [ ] 单元测试

验收:

- 可对一个 episode 评分
- 失败分类正确

#### Task 2.5: TestCaseGenerator 与第一批案例 (5 天)

- [x] 3 个 YAML 示例案例 (已完成)
- [ ] `tests/voice_coordination/test_case_generator.py` 实现
- [ ] 生成 19×22 矩阵的前 50 个案例 (覆盖最常见场景)
- [ ] YAML 加载器与验证器

验收:

- 生成 50 个 YAML 案例
- 加载器可解析所有案例

#### Task 2.6: Episode 集成与 CI (4 天)

- [ ] `tests/voice_coordination/simulation_episode.py` 实现
- [ ] 端到端 episode 执行
- [ ] pytest 集成
- [ ] CI workflow 添加仿真回归测试
- [ ] 第一批 50 个案例全部跑通

验收:

- 第一批 50 个案例通过
- CI 每次 PR 跑仿真回归
- 协作分基线建立

### 阶段二交付物

- [ ] `TimeDriver` / `UserVoiceSimulator` / `BackgroundTaskSimulator` / `CoordinationScorer` 实现
- [ ] 第一批 50 个测试案例
- [ ] CI 集成
- [ ] 协作分基线报告

### 阶段二风险

- 仿真器与真实管线行为差异导致案例失败
- 缓解: 阶段二只做 sim 模式,阶段三补齐 run 模式真机验证

## 阶段三: 真机模式与覆盖补全 (2-4 周)

**目标**: 补齐 run 模式,跑通全部 19×22 矩阵。

### 任务清单

#### Task 3.1: RealHardwareDriver 实现 (5 天)

- [ ] `tests/voice_coordination/real_hardware_driver.py` 实现
- [ ] 通过串口驱动 ESP32
- [ ] 真实音频流播放
- [ ] 麦克风输入捕获
- [ ] 端到端 episode 跑通

验收:

- run 模式可驱动真机
- 真机测试可复现

#### Task 3.2: 补全 19×22 矩阵 (7 天)

- [ ] 生成剩余 368 个测试案例
- [ ] 分批跑通并修复失败案例
- [ ] 建立完整协作分基线

验收:

- 全部 418 个案例通过 (19×22)
- 协作分基线完整

#### Task 3.3: 性能基线与回归门禁 (3 天)

- [ ] 建立性能基线 (每个案例执行时间)
- [ ] CI 回归门禁: 协作分不得低于基线
- [ ] 失败案例自动 bisect

验收:

- 回归门禁阻止协调质量劣化
- 性能基线文档化

### 阶段三交付物

- [ ] `RealHardwareDriver` 实现
- [ ] 完整 19×22 测试矩阵 (418 个案例)
- [ ] 性能基线与回归门禁
- [ ] 完整协作分基线报告

### 阶段三风险

- 真机测试环境不稳定
- 缓解: 提供 sim 模式快速回归,run 模式作为补充验证

## 里程碑

| 里程碑 | 日期 | 交付物 |
| :--- | :--- | :--- |
| M1: 契约层完成 | Week 2 | 类型注解完成,Metrics 面板上线 |
| M2: 仿真器基础设施完成 | Week 6 | 第一批 50 个案例通过,CI 集成 |
| M3: 真机模式完成 | Week 10 | 全部 418 个案例通过,回归门禁上线 |

## 资源需求

- 后端工程师 1 名 (全职)
- 测试工程师 0.5 名 (兼职,阶段二与阶段三)
- ESP32 固件工程师 0.5 名 (兼职,阶段三)
- PM 协调与 Code Review

## 成功标准

1. 六模块协调契约清晰,失败归属明确
2. 时序仿真器可跑 19×22 矩阵,协作分基线建立
3. 回归测试纳入 CI,每次 PR 自动验证协调质量
4. 真机模式可复现失败场景,辅助调试

## 后续优化方向

阶段三完成后,可选增强项:

- RSI 辅助分析: 离线使用 LLM 分析失败日志,生成修复建议 (不自动改代码)
- 更多时序模式: 扩展 22 种模式到 50 种,覆盖长尾场景
- 性能回归: 添加延迟 / 吞吐量基线,防止性能劣化

## 相关文档

- 决策文档: [duplexagent-rsi-adoption-decision.md](duplexagent-rsi-adoption-decision.md)
- 架构设计: [coordination-harness.md](coordination-harness.md), [temporal-simulator.md](temporal-simulator.md)
- 论文原文: [arXiv 2610.11299v1](https://arxiv.org/abs/2610.11299v1)
