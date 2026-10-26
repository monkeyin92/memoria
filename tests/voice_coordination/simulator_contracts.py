"""时序对话仿真器契约定义。

本模块定义仿真器的所有接口,不包含实现逻辑。
实现路径见 docs/architecture/temporal-simulator.md。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

# ========== 时间轴驱动器 ==========


@dataclass(frozen=True)
class TimelineEvent:
    """时间轴上的一个事件。"""

    timestamp_s: float
    event_id: str
    event_type: str
    payload: dict[str, Any]


class TimeDriver(Protocol):
    """虚拟时钟,推进虚拟时间。

    - sim 模式: advance() 立即返回,时间跳跃式推进
    - run 模式: advance() 阻塞等待真实时间流逝
    """

    def now(self) -> float:
        """当前虚拟时间 (秒)。"""
        ...

    def advance(self, delta_s: float) -> list[TimelineEvent]:
        """推进虚拟时间,返回触发的事件列表。"""
        ...

    def schedule_event(self, delay_s: float, event_type: str, payload: dict[str, Any]) -> str:
        """在未来 delay_s 秒后调度事件,返回 event_id。"""
        ...

    def cancel_event(self, event_id: str) -> bool:
        """取消已调度的事件。"""
        ...


# ========== 用户语音输入模拟器 ==========


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
    """用户语音输入模拟器。

    - sim 模式: 生成伪 PCM 帧,只携带 VAD 语音/静音标记
    - run 模式: 使用真实 TTS 合成用户输入,播放给真实 ESP32 麦克风
    """

    def schedule_input(self, spec: UserInputSpec) -> str:
        """调度用户输入,返回 event_id。"""
        ...

    def generate_vad_frames(self, spec: UserInputSpec) -> list[tuple[float, bytes]]:
        """为给定输入生成 VAD 帧序列。

        返回: [(timestamp_s, pcm_bytes), ...]
        """
        ...


# ========== 后台任务延迟模拟器 ==========


@dataclass(frozen=True)
class TaskSpec:
    """后台任务规格。"""

    task_id: str
    start_time_s: float
    latency_s: float
    result: Any | None = None  # None 表示超时失败
    expire_after_s: float | None = None  # 结果过期时间


class BackgroundTaskSimulator(Protocol):
    """后台任务模拟器。

    模拟工具调用延迟返回、过期失效。
    """

    def schedule_task(
        self,
        spec: TaskSpec,
        on_complete: Callable[[str, Any], None],
    ) -> str:
        """调度后台任务,返回 task_id。"""
        ...

    def cancel_task(self, task_id: str) -> bool:
        """取消后台任务。"""
        ...

    def is_result_valid(self, task_id: str, check_time_s: float) -> bool:
        """检查任务结果是否仍有效 (未过期)。"""
        ...


# ========== 协作分评分器 ==========


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
    """协调质量评分器。

    给定时间轴事件序列,判断协调失败类型并评分。
    """

    def score_episode(
        self,
        timeline_events: list[TimelineEvent],
        final_state: dict[str, Any],
    ) -> CoordinationScore:
        """对一次对话 episode 评分。"""
        ...


# ========== 测试案例生成器 ==========


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
    description: str = ""


class TestCaseGenerator(Protocol):
    """测试案例生成器。

    生成 19 类请求 × 22 种交互时序的组合矩阵。
    """

    def generate_matrix(self) -> list[TestCase]:
        """生成 19 × 22 = 418 个测试案例。"""
        ...

    def generate_case(self, request: RequestKind, pattern: TimingPattern) -> TestCase:
        """生成单个测试案例。"""
        ...


# ========== 仿真 Episode ==========


@dataclass
class SimulationEpisode:
    """一次仿真 episode。

    集成所有组件,运行一个测试案例。
    """

    case: TestCase
    time_driver: TimeDriver
    user_sim: UserVoiceSimulator
    task_sim: BackgroundTaskSimulator
    scorer: CoordinationScorer

    def run(self) -> CoordinationScore:
        """运行仿真:

        1. 初始化 MediaVoiceCoreRegistry (真实组件)
        2. 按时间轴调度用户输入与后台任务
        3. 推进虚拟时间
        4. 收集时间轴事件
        5. 评分
        """
        raise NotImplementedError


# ========== 真机硬件驱动 ==========


class RealHardwareDriver(Protocol):
    """真机硬件驱动 (run 模式)。

    驱动真实 ESP32 固件,用于端到端验证。
    """

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


# ========== 测试案例加载器 ==========


class TestCaseLoader(Protocol):
    """从 YAML 文件加载测试案例。"""

    def load_case(self, yaml_path: str) -> TestCase:
        """加载单个案例。"""
        ...

    def load_all_cases(self, cases_dir: str) -> list[TestCase]:
        """加载目录下所有案例。"""
        ...
