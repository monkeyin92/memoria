"""时序对话仿真器包。

端到端全双工对话协调质量测试框架。

模块:
- simulator_contracts: 契约定义 (Protocol 接口)
- time_driver: 时间轴驱动器
- user_voice_simulator: 用户语音模拟器
- background_task_simulator: 后台任务模拟器
- coordination_scorer: 协调质量评分器
- test_case_generator: 测试案例生成器
- simulation_episode: 仿真 episode
- real_hardware_driver: 真机驱动
- test_case_loader: YAML 加载器

使用示例:
    from tests.voice_coordination.simulator_contracts import (
        TimeDriver,
        UserVoiceSimulator,
        CoordinationScorer,
    )

文档:
- 架构设计: docs/architecture/temporal-simulator.md
- 使用指南: tests/voice_coordination/README.md
"""

__all__ = [
    "TimeDriver",
    "UserVoiceSimulator",
    "BackgroundTaskSimulator",
    "CoordinationScorer",
    "TestCaseGenerator",
    "SimulationEpisode",
    "RealHardwareDriver",
    "TestCaseLoader",
    "TimelineEvent",
    "UserInputKind",
    "UserInputSpec",
    "TaskSpec",
    "TerminalFailureKind",
    "CoordinationScore",
    "RequestKind",
    "TimingPattern",
    "TestCase",
]
