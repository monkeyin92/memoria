# 时序对话仿真器

端到端全双工对话协调质量测试框架。

## 目录结构

```
tests/voice_coordination/
├── simulator_contracts.py       # 契约定义 (Protocol 接口)
├── cases/                       # 测试案例 YAML 文件
│   ├── weather_interrupt_mid.yaml
│   ├── simple_qa_normal.yaml
│   └── tool_call_timeout.yaml
├── time_driver.py              # 时间轴驱动器 (未实现)
├── user_voice_simulator.py     # 用户语音模拟器 (未实现)
├── background_task_simulator.py # 后台任务模拟器 (未实现)
├── coordination_scorer.py      # 协调质量评分器 (未实现)
├── test_case_generator.py      # 测试案例生成器 (未实现)
├── simulation_episode.py       # 仿真 episode (未实现)
├── real_hardware_driver.py     # 真机驱动 (未实现)
└── test_case_loader.py         # YAML 加载器 (未实现)
```

## 快速开始

### 1. 架构设计

完整架构文档见 `docs/architecture/temporal-simulator.md`。

核心理念:

- 时间轴驱动: 所有事件按虚拟时钟调度
- 契约优先: 先定义接口,再分阶段实现
- 双模式: sim 模式 (快速回归) + run 模式 (真机验证)

### 2. 契约查看

所有接口定义在 `simulator_contracts.py`:

```python
from tests.voice_coordination.simulator_contracts import (
    TimeDriver,
    UserVoiceSimulator,
    BackgroundTaskSimulator,
    CoordinationScorer,
    TestCaseGenerator,
    SimulationEpisode,
    RealHardwareDriver,
)
```

每个 Protocol 都有清晰的文档字符串。

### 3. 测试案例格式

案例用 YAML 编写,存放在 `cases/` 目录。

示例: `cases/weather_interrupt_mid.yaml`

```yaml
case_id: weather_interrupt_mid
request_kind: weather
timing_pattern: interrupt_mid

user_inputs:
  - kind: utterance
    text: "今天天气怎么样?"
    start_time_s: 0.0
    duration_s: 1.2

  - kind: interrupt
    text: "等等,我是说明天的天气"
    start_time_s: 3.5
    duration_s: 1.5

background_tasks:
  - task_id: weather_query_today
    start_time_s: 1.5
    latency_s: 1.0
    result: { ... }

expected:
  terminal_failure: correct
  score_min: 0.8
```

### 4. 运行仿真 (未实现)

```python
from tests.voice_coordination.simulation_episode import SimulationEpisode
from tests.voice_coordination.test_case_loader import YAMLTestCaseLoader

loader = YAMLTestCaseLoader()
case = loader.load_case("cases/weather_interrupt_mid.yaml")

episode = SimulationEpisode(
    case=case,
    time_driver=...,
    user_sim=...,
    task_sim=...,
    scorer=...,
)

score = episode.run()
print(f"Terminal: {score.terminal_failure}, Score: {score.score}")
```

## 测试案例矩阵

### 19 类请求类型

1. `simple_qa` - 简单问答
2. `multi_turn_qa` - 多轮问答
3. `tool_call` - 工具调用
4. `long_form_answer` - 长篇回答
5. `storytelling` - 讲故事
6. `calendar` - 日历查询
7. `weather` - 天气查询
8. `timer` - 定时器
9. `alarm` - 闹钟
10. `reminder` - 提醒
11. `music` - 音乐播放
12. `device_control` - 设备控制
13. `chat` - 闲聊
14. `memory_confirm` - 记忆确认
15. `memory_recall` - 记忆回忆
16. `crisis_script` - 危机剧本
17. `chitchat` - 寒暄
18. `close_intent` - 关闭意图
19. `delegation` - 委托任务

### 22 种交互时序模式

1. `normal` - 正常无打断
2. `interrupt_early` - 早期打断 (thinking)
3. `interrupt_mid` - 中期打断 (speaking 前半)
4. `interrupt_late` - 晚期打断 (speaking 后半)
5. `double_interrupt` - 双重打断
6. `interrupt_then_resume` - 打断后恢复
7. `false_start` - 假启动
8. `hesitation` - 犹豫停顿
9. `vad_pause` - VAD 停顿
10. `vad_pause_resume` - VAD 停顿后恢复
11. `tool_delay_short` - 工具短延迟
12. `tool_delay_long` - 工具长延迟
13. `tool_timeout` - 工具超时
14. `tool_result_stale` - 工具结果过时
15. `user_changed_mind` - 用户改变主意
16. `backchannel_noise` - 反向通道噪音
17. `simultaneous_speech` - 同时说话
18. `rapid_turn_taking` - 快速轮流
19. `overlapping_turns` - 重叠轮次
20. `long_silence` - 长时间静音
21. `cascaded_tools` - 级联工具调用
22. `tool_during_playback` - 播放时工具返回

**完整矩阵**: 19 × 22 = 418 个测试案例

## 终端失败分类

评分器判定 8 种终端失败类型:

- `correct` - 正确完成
- `ignored_interrupt` - 忽略打断
- `false_interrupt` - 误判打断
- `late_response` - 响应过慢
- `stale_output` - 输出过时内容
- `abandoned_tool` - 工具调用被放弃
- `tool_result_ignored` - 工具结果被忽略
- `double_speaking` - 双方同时说话

## 实现路径

按阶段实现,每个阶段独立可验证。

### 第一阶段: 契约与骨架 ✅

- [x] 定义所有 Protocol 接口
- [x] 实现架构文档
- [x] 编写 3 个示例案例 YAML

### 第二阶段: 时间驱动器 (待实现)

- [ ] 实现 `TimeDriver` sim 模式
- [ ] 实现事件调度与触发
- [ ] 单元测试: 时间推进逻辑

### 第三阶段: 用户模拟器 (待实现)

- [ ] 实现 `UserVoiceSimulator`
- [ ] 生成伪 PCM 帧 (VAD 标记)
- [ ] 单元测试: VAD 帧生成

### 第四阶段: 后台任务模拟器 (待实现)

- [ ] 实现 `BackgroundTaskSimulator`
- [ ] 延迟与过期逻辑
- [ ] 单元测试: 任务调度

### 第五阶段: 评分器 (待实现)

- [ ] 实现 `CoordinationScorer`
- [ ] 启发式规则: 8 种失败类型
- [ ] 单元测试: 评分逻辑

### 第六阶段: 案例生成器 (待实现)

- [ ] 实现 `TestCaseGenerator`
- [ ] 19 × 22 矩阵生成逻辑
- [ ] 验证案例覆盖率

### 第七阶段: Episode 集成 (待实现)

- [ ] 实现 `SimulationEpisode`
- [ ] 集成 MediaVoiceCoreRegistry
- [ ] 端到端测试: 3 个示例案例

### 第八阶段: 真机模式 (待实现)

- [ ] 实现 `TimeDriver` run 模式
- [ ] 实现 `RealHardwareDriver`
- [ ] 真机验证: 1 个案例

### 第九阶段: CI 集成 (待实现)

- [ ] 选择核心案例子集 (50 个)
- [ ] 添加到 `.github/workflows/ci.yaml`
- [ ] Nightly 完整矩阵

## 依赖项

- Python 3.11+
- pytest
- PyYAML (加载案例)
- 现有组件: `MediaVoiceCoreRegistry`, `DuplexRuntime`

真机模式额外依赖:

- `pyserial` (已有)
- `sounddevice` (播放音频)

## 参考文献

- **DuplexAgent-RSI 论文**: 19 类请求, 22 种时序模式, 终端失败分类法
- 项目现有测试: `test_media_session_golden.py`, `test_duplex_runtime_wiring.py`
- 架构文档: `docs/architecture/temporal-simulator.md`

## 贡献指南

1. 每个新实现模块都应有对应的单元测试
2. 测试案例 YAML 文件应包含清晰的 `description`
3. 评分器规则应有可解释的 `timeline_evidence`
4. 真机模式改动需在真机上验证

## FAQ

### 为什么不用 pytest fixture 驱动时间?

Fixture 适合设置测试环境,不适合驱动时序逻辑。`TimeDriver` 显式推进时间,易于调试。

### 为什么评分器不用 LLM?

LLM 评分不稳定且慢。启发式规则足够,且可解释。未来可选择性用 LLM 作为二次验证。

### 为什么要支持真机模式?

真机模式验证端到端路径,包括 ESP32 固件、真实 ASR/TTS 延迟。Sim 模式无法覆盖这些。

### 如何调试失败的案例?

1. 运行 episode 收集时间轴事件
2. 查看 `timeline_evidence` 中的事件 ID
3. 对照案例 YAML 中的 `checkpoints`
4. 检查状态转换是否符合预期

## 联系

问题与讨论见项目 issue tracker。
