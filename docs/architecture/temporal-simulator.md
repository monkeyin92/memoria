# 时序对话仿真器架构

## 目标

为 Memoria 构建端到端时序对话回归测试能力,验证全双工协调质量。

- 模拟用户语音输入按时间轴到达(打断、犹豫、停顿)
- 模拟后台任务延迟返回或过期失效
- 验证协作分评分逻辑:给定终端失败分类法,评分协调质量
- 覆盖 19 类请求 × 22 种交互时序的组合矩阵

**非目标**: 不重新实现真实的 ASR/TTS/LLM 管线,而是在契约层面抽象它们的时序行为。

## 模式

仿真器支持两种运行模式:

1. **sim 模式**: 纯时间推进仿真,所有事件由时间轴驱动。用于快速回归和 CI。
2. **run 模式**: 真实音频流播放,驱动真机 ESP32 固件。用于端到端验证与调试。

## 核心组件

### 1. 时间轴驱动器 (TimeDriver)

虚拟时钟,以固定时钟速率推进虚拟时间。

**模块**: `tests/voice_coordination/time_driver.py`

**契约**:

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class TimelineEvent:
    """时间轴上的一个事件。"""
    timestamp_s: float
    event_id: str
    event_type: str
    payload: dict

class TimeDriver(Protocol):
    """虚拟时钟,推进虚拟时间。"""
    
    def now(self) -> float:
        """当前虚拟时间 (秒)。"""
        ...
    
    def advance(self, delta_s: float) -> list[TimelineEvent]:
        """推进虚拟时间,返回触发的事件列表。"""
        ...
    
    def schedule_event(
        self, 
        delay_s: float, 
        event_type: str, 
        payload: dict
    ) -> str:
        """在未来 delay_s 秒后调度事件,返回 event_id。"""
        ...
    
    def cancel_event(self, event_id: str) -> bool:
        """取消已调度的事件。"""
        ...
```

**说明**:

- sim 模式: `advance()` 立即返回,时间跳跃式推进
- run 模式: `advance()` 阻塞等待真实时间流逝

### 2. 用户语音输入模拟器 (UserVoiceSimulator)

按时间轴调度用户短语、打断、犹豫、停顿。

**模块**: `tests/voice_coordination/user_voice_simulator.py`

**契约**:

```python
from enum import StrEnum
from dataclasses import dataclass

class UserInputKind(StrEnum):
    """用户输入类型。"""
    UTTERANCE = "utterance"  # 完整话语
    INTERRUPT = "interrupt"  # 打断
    HESITATION = "hesitation"  # 犹豫 (「呃…」)
    PAUSE = "pause"  # 停顿 (VAD pause)
    RESUME = "resume"  # 恢复 (pause 后继续)

@dataclass(frozen=True)
class UserInputSpec:
    """用户输入规格。"""
    kind: UserInputKind
    text: str
    start_time_s: float
    duration_s: float | None = None  # None 表示瞬时事件

class UserVoiceSimulator(Protocol):
    """用户语音输入模拟器。"""
    
    def schedule_input(self, spec: UserInputSpec) -> str:
        """调度用户输入,返回 event_id。"""
        ...
    
    def generate_vad_frames(
        self, 
        spec: UserInputSpec
    ) -> list[tuple[float, bytes]]:
        """
        为给定输入生成 VAD 帧序列。
        返回 [(timestamp_s, pcm_bytes), ...]
        """
        ...
```

**说明**:

- sim 模式: 生成伪 PCM 帧,只携带 VAD 语音/静音标记
- run 模式: 使用真实 TTS 合成用户输入,播放给真实 ESP32 麦克风

### 3. 后台任务延迟模拟器 (BackgroundTaskSimulator)

模拟工具调用延迟返回、过期失效。

**模块**: `tests/voice_coordination/background_task_simulator.py`

**契约**:

```python
from typing import Callable, Any
from dataclasses import dataclass

@dataclass(frozen=True)
class TaskSpec:
    """后台任务规格。"""
    task_id: str
    start_time_s: float
    latency_s: float
    result: Any | None = None  # None 表示超时失败
    expire_after_s: float | None = None  # 结果过期时间

class BackgroundTaskSimulator(Protocol):
    """后台任务模拟器。"""
    
    def schedule_task(
        self, 
        spec: TaskSpec,
        on_complete: Callable[[str, Any], None]
    ) -> str:
        """调度后台任务,返回 task_id。"""
        ...
    
    def cancel_task(self, task_id: str) -> bool:
        """取消后台任务。"""
        ...
    
    def is_result_valid(self, task_id: str, check_time_s: float) -> bool:
        """检查任务结果是否仍有效 (未过期)。"""
        ...
```

**说明**:

- 模拟天气查询、日历查询等工具调用的延迟
- 结果可以在返回后过期 (用户改变主意、切换话题)

### 4. 协作分评分器 (CoordinationScorer)

给定终端失败分类法,评分协调质量。

**模块**: `tests/voice_coordination/coordination_scorer.py`

**契约**:

```python
from enum import StrEnum
from dataclasses import dataclass

class TerminalFailureKind(StrEnum):
    """终端失败分类 (DuplexAgent-RSI 论文)。"""
    CORRECT = "correct"  # 正确完成
    IGNORED_INTERRUPT = "ignored_interrupt"  # 忽略打断
    FALSE_INTERRUPT = "false_interrupt"  # 误判打断
    LATE_RESPONSE = "late_response"  # 响应过慢
    STALE_OUTPUT = "stale_output"  # 输出过时内容
    ABANDONED_TOOL = "abandoned_tool"  # 工具调用被放弃
    TOOL_RESULT_IGNORED = "tool_result_ignored"  # 工具结果被忽略
    DOUBLE_SPEAKING = "double_speaking"  # 双方同时说话

@dataclass(frozen=True)
class CoordinationScore:
    """协调质量评分。"""
    terminal_failure: TerminalFailureKind
    score: float  # 0.0 ~ 1.0
    explanation: str
    timeline_evidence: list[str]  # 时间轴事件 ID 列表

class CoordinationScorer(Protocol):
    """协调质量评分器。"""
    
    def score_episode(
        self, 
        timeline_events: list[TimelineEvent],
        final_state: dict
    ) -> CoordinationScore:
        """对一次对话 episode 评分。"""
        ...
```

**说明**:

- 评分器读取时间轴事件序列,判断协调失败类型
- 提供可解释的证据链 (哪些时间戳的事件导致失败)

### 5. 测试案例生成器 (TestCaseGenerator)

生成 19 类请求 × 22 种交互时序的组合矩阵。

**模块**: `tests/voice_coordination/test_case_generator.py`

**契约**:

```python
from enum import StrEnum
from dataclasses import dataclass

class RequestKind(StrEnum):
    """19 类请求类型 (DuplexAgent-RSI 论文)。"""
    SIMPLE_QA = "simple_qa"  # 简单问答
    MULTI_TURN_QA = "multi_turn_qa"  # 多轮问答
    TOOL_CALL = "tool_call"  # 工具调用
    LONG_FORM_ANSWER = "long_form_answer"  # 长篇回答
    STORYTELLING = "storytelling"  # 讲故事
    CALENDAR = "calendar"  # 日历查询
    WEATHER = "weather"  # 天气查询
    TIMER = "timer"  # 定时器
    ALARM = "alarm"  # 闹钟
    REMINDER = "reminder"  # 提醒
    MUSIC = "music"  # 音乐播放
    DEVICE_CONTROL = "device_control"  # 设备控制
    CHAT = "chat"  # 闲聊
    MEMORY_CONFIRM = "memory_confirm"  # 记忆确认
    MEMORY_RECALL = "memory_recall"  # 记忆回忆
    CRISIS_SCRIPT = "crisis_script"  # 危机剧本
    CHITCHAT = "chitchat"  # 寒暄
    CLOSE_INTENT = "close_intent"  # 关闭意图
    DELEGATION = "delegation"  # 委托任务

class TimingPattern(StrEnum):
    """22 种交互时序模式。"""
    NORMAL = "normal"  # 正常无打断
    INTERRUPT_EARLY = "interrupt_early"  # 早期打断 (thinking 阶段)
    INTERRUPT_MID = "interrupt_mid"  # 中期打断 (speaking 前半)
    INTERRUPT_LATE = "interrupt_late"  # 晚期打断 (speaking 后半)
    DOUBLE_INTERRUPT = "double_interrupt"  # 双重打断
    INTERRUPT_THEN_RESUME = "interrupt_then_resume"  # 打断后恢复
    FALSE_START = "false_start"  # 假启动 (用户开始说话又停下)
    HESITATION = "hesitation"  # 犹豫停顿
    VAD_PAUSE = "vad_pause"  # VAD 检测到停顿
    VAD_PAUSE_RESUME = "vad_pause_resume"  # VAD 停顿后恢复
    TOOL_DELAY_SHORT = "tool_delay_short"  # 工具调用短延迟
    TOOL_DELAY_LONG = "tool_delay_long"  # 工具调用长延迟
    TOOL_TIMEOUT = "tool_timeout"  # 工具调用超时
    TOOL_RESULT_STALE = "tool_result_stale"  # 工具结果过时
    USER_CHANGED_MIND = "user_changed_mind"  # 用户改变主意
    BACKCHANNEL_NOISE = "backchannel_noise"  # 反向通道噪音
    SIMULTANEOUS_SPEECH = "simultaneous_speech"  # 同时说话
    RAPID_TURN_TAKING = "rapid_turn_taking"  # 快速轮流
    OVERLAPPING_TURNS = "overlapping_turns"  # 重叠轮次
    LONG_SILENCE = "long_silence"  # 长时间静音
    CASCADED_TOOLS = "cascaded_tools"  # 级联工具调用
    TOOL_DURING_PLAYBACK = "tool_during_playback"  # 播放时工具返回

@dataclass(frozen=True)
class TestCase:
    """一个测试案例。"""
    case_id: str
    request_kind: RequestKind
    timing_pattern: TimingPattern
    user_inputs: list[UserInputSpec]
    background_tasks: list[TaskSpec]
    expected_terminal: TerminalFailureKind
    expected_score_min: float

class TestCaseGenerator(Protocol):
    """测试案例生成器。"""
    
    def generate_matrix(self) -> list[TestCase]:
        """生成 19 × 22 = 418 个测试案例。"""
        ...
    
    def generate_case(
        self, 
        request: RequestKind, 
        pattern: TimingPattern
    ) -> TestCase:
        """生成单个测试案例。"""
        ...
```

**说明**:

- 每个测试案例包含:
  - 用户输入序列 (时序、文本、类型)
  - 后台任务规格 (延迟、过期)
  - 预期的终端失败类型与最低评分

## 模块间集成

### SimulationEpisode

`tests/voice_coordination/simulation_episode.py`

```python
from dataclasses import dataclass

@dataclass
class SimulationEpisode:
    """一次仿真 episode。"""
    case: TestCase
    time_driver: TimeDriver
    user_sim: UserVoiceSimulator
    task_sim: BackgroundTaskSimulator
    scorer: CoordinationScorer
    
    def run(self) -> CoordinationScore:
        """
        运行仿真:
        1. 初始化 MediaVoiceCoreRegistry (真实组件)
        2. 按时间轴调度用户输入与后台任务
        3. 推进虚拟时间
        4. 收集时间轴事件
        5. 评分
        """
        pass  # 实现占位
```

### RealHardwareDriver

`tests/voice_coordination/real_hardware_driver.py`

用于 run 模式,驱动真机 ESP32。

```python
class RealHardwareDriver(Protocol):
    """真机硬件驱动。"""
    
    def connect(self, device_port: str) -> None:
        """连接真机。"""
        ...
    
    def play_audio(self, pcm_bytes: bytes, timestamp_s: float) -> None:
        """播放音频到真机麦克风 (通过扬声器)。"""
        ...
    
    def capture_display(self) -> bytes:
        """截取真机屏幕。"""
        ...
    
    def send_usb_command(self, command: str) -> str:
        """发送 USB 命令。"""
        ...
```

## 测试案例格式

每个测试案例是一个 YAML 文件,存放在 `tests/voice_coordination/cases/`。

示例: `tests/voice_coordination/cases/weather_interrupt_mid.yaml`

```yaml
case_id: weather_interrupt_mid
request_kind: weather
timing_pattern: interrupt_mid
description: 用户询问天气,在助手回答中途打断

user_inputs:
  - kind: utterance
    text: "今天天气怎么样?"
    start_time_s: 0.0
    duration_s: 1.2
  
  - kind: interrupt
    text: "等等,我是说明天的天气"
    start_time_s: 3.5  # 助手开始说话 0.5s 后
    duration_s: 1.5

background_tasks:
  - task_id: weather_query
    start_time_s: 1.5  # 用户话语结束后 0.3s
    latency_s: 1.0  # 工具调用延迟 1s
    result:
      temperature: 25
      condition: "晴"
    expire_after_s: null

expected:
  terminal_failure: correct  # 或 stale_output
  score_min: 0.8
  explanation: |
    用户在助手播放天气回答中途打断,改为询问明天天气。
    正确行为: 立即停止播放,发起新的工具调用。
    失败模式: 继续播放今天天气 (stale_output)。
```

## 实现路径

### 第一阶段: 契约与骨架

1. 定义所有 Protocol 接口 (`simulator_contracts.py`)
2. 实现 `TimeDriver` 的 sim 模式
3. 实现 `UserVoiceSimulator` 的基础调度
4. 编写 1-2 个手工测试案例 YAML

### 第二阶段: 评分器

1. 实现 `CoordinationScorer` 的启发式规则
2. 定义时间轴事件的标准格式
3. 编写评分器单元测试

### 第三阶段: 生成器

1. 实现 `TestCaseGenerator` 的 19 × 22 矩阵
2. 为每个 (request, pattern) 组合编写生成逻辑
3. 验证生成的案例覆盖率

### 第四阶段: 真机模式

1. 实现 `TimeDriver` 的 run 模式 (真实时间等待)
2. 实现 `RealHardwareDriver` (ESP32 USB 连接)
3. 集成真实 TTS 合成用户输入音频

### 第五阶段: CI 集成

1. 在 `.github/workflows/ci.yaml` 添加 voice_coordination 测试
2. 选择核心案例子集 (约 50 个) 作为 PR gate
3. 完整矩阵作为 nightly 回归

## 依赖项

- 现有组件: `MediaVoiceCoreRegistry`, `DuplexRuntime`, `MediaBridgeGrpcServer`
- 新增依赖: 无 (纯 Python stdlib + pytest)
- 真机模式依赖: `pyserial` (已有), `sounddevice` (播放音频)

## 设计决策

### 为什么不用 pytest-asyncio fixture 驱动时间?

Fixture 适合设置测试环境,不适合驱动时序逻辑。`TimeDriver` 显式推进时间,易于调试与复现。

### 为什么评分器不用 LLM?

LLM 评分不稳定且慢。启发式规则足够,且可解释。未来可选择性用 LLM 作为二次验证。

### 为什么要支持真机模式?

真机模式验证端到端路径,包括 ESP32 固件、网络栈、真实 ASR/TTS 延迟。Sim 模式无法覆盖这些。

## 非覆盖范围

- 不模拟网络抖动 (可以在 `BackgroundTaskSimulator` 中添加 jitter 参数)
- 不模拟 LLM 生成延迟变化 (假设固定延迟)
- 不测试多设备并发 (单会话仿真)

## 参考文献

- DuplexAgent-RSI 论文 (19 类请求, 22 种时序模式, 终端失败分类法)
- 项目现有测试: `test_media_session_golden.py`, `test_duplex_runtime_wiring.py`
