# P3-2 实现自然度受控实验

## 目标

建立自然度（语音质量）的 A/B 测试框架，在 AEC、双讲和 Playback fence 通过后，安全地实验不同 TTS provider 和 voice profile，提升用户体验。

## 背景

根据整改方案第 21.1 节：
> 基础 TTS、AEC、双讲和 Playback fence 通过后，才用独立 voice profile 做自然度 A/B；默认路径保持保守。

自然度实验是提升用户体验的重要手段，但必须在基础功能稳定后才能进行。

## 前置条件

**必须先完成**：
- ✅ P0: 单轮验证、播放终态、按钮打断、模型身份过滤
- ✅ P1: AEC Reference 对齐、本地停止词、语义打断、声学验收
- ✅ Playback fence 和 Actual Heard 精确追踪

**禁止在以下情况下进行自然度实验**：
- ❌ 基础 TTS 不稳定
- ❌ AEC 未通过声学验收
- ❌ Playback 终态回执不完整
- ❌ 打断路径有问题

## 实验框架设计

### 1. 实验配置

#### 1.1 Voice Profile 定义

**新增文件**: `services/agent/src/voice_profiles.py`

```python
"""Voice profile definitions for naturalness experiments."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VoiceProfile:
    """A voice profile configuration."""
    
    profile_id: str
    name: str
    description: str
    
    # TTS provider configuration
    provider: Literal["cosyvoice", "edge-tts", "azure", "openai"]
    provider_model: str
    provider_voice: str
    
    # Quality settings
    sample_rate: int  # 16000 | 24000
    bitrate: int
    
    # Style parameters
    speed: float  # 0.8 - 1.2
    pitch: float  # -50 - +50 Hz
    emotion: str  # "neutral" | "cheerful" | "calm"
    
    # Experiment metadata
    experiment_group: str  # "control" | "treatment_a" | "treatment_b"
    enabled: bool
    
    # Quality metrics thresholds
    expected_latency_p95_ms: int
    expected_mos_score: float  # Mean Opinion Score


# Define available profiles
VOICE_PROFILES = {
    "default-conservative": VoiceProfile(
        profile_id="default-conservative",
        name="默认保守",
        description="稳定的基础 TTS，P0/P1 验证通过",
        provider="cosyvoice",
        provider_model="CosyVoice-300M",
        provider_voice="zh-CN-XiaoxiaoNeural",
        sample_rate=16000,
        bitrate=64000,
        speed=1.0,
        pitch=0.0,
        emotion="neutral",
        experiment_group="control",
        enabled=True,
        expected_latency_p95_ms=1500,
        expected_mos_score=3.5,
    ),
    
    "treatment-high-quality": VoiceProfile(
        profile_id="treatment-high-quality",
        name="高质量实验",
        description="24kHz 高质量 TTS，自然度提升",
        provider="azure",
        provider_model="neural-hd",
        provider_voice="zh-CN-XiaoyiNeural",
        sample_rate=24000,
        bitrate=128000,
        speed=1.0,
        pitch=0.0,
        emotion="cheerful",
        experiment_group="treatment_a",
        enabled=False,  # 默认关闭，需要手动启用
        expected_latency_p95_ms=2000,
        expected_mos_score=4.2,
    ),
    
    "treatment-fast": VoiceProfile(
        profile_id="treatment-fast",
        name="快速响应实验",
        description="优化延迟，牺牲少量质量",
        provider="edge-tts",
        provider_model="edge-tts-v1",
        provider_voice="zh-CN-YunxiNeural",
        sample_rate=16000,
        bitrate=48000,
        speed=1.05,
        pitch=0.0,
        emotion="neutral",
        experiment_group="treatment_b",
        enabled=False,
        expected_latency_p95_ms=1000,
        expected_mos_score=3.3,
    ),
}


def get_voice_profile(profile_id: str) -> VoiceProfile:
    """Get voice profile by ID."""
    return VOICE_PROFILES.get(profile_id, VOICE_PROFILES["default-conservative"])
```

#### 1.2 实验分配策略

**新增文件**: `services/agent/src/experiment_allocator.py`

```python
"""Experiment allocation for naturalness A/B testing."""

import hashlib
from typing import Optional
from .voice_profiles import VoiceProfile, VOICE_PROFILES


class ExperimentAllocator:
    """Allocate users to experiment groups."""
    
    def __init__(self, experiment_config: dict):
        """
        Initialize allocator.
        
        Args:
            experiment_config: {
                "enabled": bool,
                "traffic_split": {
                    "control": 0.7,
                    "treatment_a": 0.2,
                    "treatment_b": 0.1,
                },
                "allowlist": ["user_123", "user_456"],
                "blocklist": ["user_789"],
            }
        """
        self.config = experiment_config
        self.enabled = experiment_config.get("enabled", False)
        self.traffic_split = experiment_config.get("traffic_split", {
            "control": 1.0,
        })
        self.allowlist = set(experiment_config.get("allowlist", []))
        self.blocklist = set(experiment_config.get("blocklist", []))
    
    def allocate_profile(
        self,
        user_id: str,
        device_id: str,
        *,
        force_profile: Optional[str] = None,
    ) -> VoiceProfile:
        """
        Allocate a voice profile for this user/device.
        
        Args:
            user_id: User identifier
            device_id: Device identifier
            force_profile: Force a specific profile (for testing)
        
        Returns:
            Allocated VoiceProfile
        """
        
        # Force profile (for testing/debugging)
        if force_profile:
            profile = VOICE_PROFILES.get(force_profile)
            if profile and profile.enabled:
                return profile
        
        # Experiment disabled, use default
        if not self.enabled:
            return VOICE_PROFILES["default-conservative"]
        
        # Blocklist check
        if user_id in self.blocklist:
            return VOICE_PROFILES["default-conservative"]
        
        # Allowlist override (always get treatment)
        if user_id in self.allowlist:
            # Give them the first enabled treatment
            for profile in VOICE_PROFILES.values():
                if profile.experiment_group.startswith("treatment") and profile.enabled:
                    return profile
        
        # Hash-based allocation (stable per user)
        allocation_key = f"{user_id}:{device_id}"
        hash_value = int(hashlib.md5(allocation_key.encode()).hexdigest(), 16)
        
        # Normalize to [0, 1)
        normalized = (hash_value % 10000) / 10000.0
        
        # Allocate based on traffic split
        cumulative = 0.0
        for group, percentage in self.traffic_split.items():
            cumulative += percentage
            
            if normalized < cumulative:
                # Find profile for this group
                for profile in VOICE_PROFILES.values():
                    if profile.experiment_group == group and profile.enabled:
                        return profile
                
                # Fallback to default
                break
        
        return VOICE_PROFILES["default-conservative"]
```

### 2. 集成到 Voice Core

#### 2.1 TTS Provider 适配器

**修改文件**: `services/agent/src/voice_core/tts_provider.py`

```python
"""TTS provider with voice profile support."""

from .voice_profiles import VoiceProfile


class TtsProvider:
    """Base TTS provider."""
    
    async def synthesize(
        self,
        text: str,
        voice_profile: VoiceProfile,
    ) -> AsyncIterator[bytes]:
        """
        Synthesize speech with given profile.
        
        Args:
            text: Text to synthesize
            voice_profile: Voice profile configuration
        
        Yields:
            Audio chunks
        """
        raise NotImplementedError


class CosyVoiceTtsProvider(TtsProvider):
    """CosyVoice TTS provider (default conservative)."""
    
    async def synthesize(
        self,
        text: str,
        voice_profile: VoiceProfile,
    ) -> AsyncIterator[bytes]:
        # Use voice_profile.provider_model and voice_profile.provider_voice
        # ...
        pass


class AzureTtsProvider(TtsProvider):
    """Azure Neural TTS provider (high quality treatment)."""
    
    async def synthesize(
        self,
        text: str,
        voice_profile: VoiceProfile,
    ) -> AsyncIterator[bytes]:
        # Use Azure SDK with profile settings
        # ...
        pass
```

#### 2.2 实验追踪

**修改文件**: `services/agent/src/voice_core/media_session_output_stream.py`

```python
from .experiment_allocator import ExperimentAllocator
from .voice_profiles import VoiceProfile

class MediaSessionOutputStream:
    
    def __init__(self, experiment_config: dict):
        self.experiment_allocator = ExperimentAllocator(experiment_config)
    
    async def _generate_reply(
        self,
        context: _MediaVoiceSession,
        user_text: str,
    ):
        """Generate reply with experiment tracking."""
        
        # 1. Allocate voice profile
        voice_profile = self.experiment_allocator.allocate_profile(
            user_id=context.identity.subject_id,
            device_id=context.identity.device_id,
        )
        
        logger.info(
            "Allocated voice profile: session=%s profile=%s group=%s",
            context.identity.session_id,
            voice_profile.profile_id,
            voice_profile.experiment_group,
        )
        
        # 2. Record experiment assignment
        await self._record_experiment_assignment(
            context,
            voice_profile,
        )
        
        # 3. Generate with profile
        async for chunk in self.tts_provider.synthesize(
            text=assistant_text,
            voice_profile=voice_profile,
        ):
            yield chunk
    
    async def _record_experiment_assignment(
        self,
        context: _MediaVoiceSession,
        voice_profile: VoiceProfile,
    ):
        """Record experiment assignment for analysis."""
        
        self.metrics.inc_experiment_assignment(
            profile_id=voice_profile.profile_id,
            experiment_group=voice_profile.experiment_group,
        )
        
        # Store in session metadata
        context.metadata["voice_profile_id"] = voice_profile.profile_id
        context.metadata["experiment_group"] = voice_profile.experiment_group
```

### 3. 指标收集

#### 3.1 质量指标

```python
# 添加到 MetricsRegistry

# 实验分配
self.experiment_assignment_total = Counter(
    "experiment_assignment_total",
    "Total experiment assignments",
    ["profile_id", "experiment_group"],
)

# TTS 延迟（按 profile 分组）
self.tts_latency_by_profile = Histogram(
    "tts_latency_by_profile_ms",
    "TTS first frame latency by profile",
    ["profile_id", "experiment_group"],
    buckets=[500, 1000, 1500, 2000, 3000, 5000],
)

# 播放完成率（按 profile 分组）
self.playback_completion_by_profile = Counter(
    "playback_completion_by_profile_total",
    "Playback completion by profile",
    ["profile_id", "experiment_group", "status"],  # status: completed | interrupted | error
)

# 用户反馈（如果有）
self.user_feedback_by_profile = Counter(
    "user_feedback_by_profile_total",
    "User feedback by profile",
    ["profile_id", "experiment_group", "rating"],  # rating: positive | negative
)
```

#### 3.2 自动质量检查

```python
class VoiceProfileMonitor:
    """Monitor voice profile quality metrics."""
    
    def __init__(self, voice_profile: VoiceProfile):
        self.profile = voice_profile
        self.latencies = []
        self.completion_rate = 0.0
    
    def record_tts_latency(self, latency_ms: int):
        """Record TTS latency."""
        self.latencies.append(latency_ms)
        
        # Check against threshold
        if len(self.latencies) >= 100:
            p95 = self._percentile(self.latencies, 95)
            
            if p95 > self.profile.expected_latency_p95_ms * 1.5:
                logger.warning(
                    "Voice profile %s latency degraded: P95=%dms (expected ≤%dms)",
                    self.profile.profile_id,
                    p95,
                    self.profile.expected_latency_p95_ms,
                )
                # 触发告警
                self._alert_quality_degradation("latency", p95)
    
    def _alert_quality_degradation(self, metric: str, value: float):
        """Alert on quality degradation."""
        # TODO: 发送告警到监控系统
        pass
```

### 4. 实验配置管理

#### 4.1 配置文件

**文件**: `config/experiments/naturalness-v1.yaml`

```yaml
experiment:
  name: "naturalness-v1"
  description: "High quality TTS vs default"
  enabled: false  # 默认关闭，需要手动启用
  
  # 实验条件
  prerequisites:
    - p0_verification_passed
    - p1_aec_verified
    - playback_fence_stable
  
  # 流量分配
  traffic_split:
    control: 0.8          # 80% 保守路径
    treatment_a: 0.15     # 15% 高质量
    treatment_b: 0.05     # 5% 快速响应
  
  # 白名单（测试用户）
  allowlist:
    - "user_test_001"
    - "user_test_002"
  
  # 黑名单（排除问题用户）
  blocklist:
    - "user_problematic"
  
  # 自动停止条件
  stop_conditions:
    - metric: "tts_latency_p95_ms"
      threshold: 3000
      window: "1h"
    - metric: "error_rate"
      threshold: 0.05
      window: "10m"
  
  # 最小样本量
  minimum_samples: 1000
  
  # 实验持续时间
  duration_days: 14
```

#### 4.2 动态配置加载

```python
import yaml
from pathlib import Path


class ExperimentConfig:
    """Load and manage experiment configuration."""
    
    @classmethod
    def load_from_file(cls, config_path: Path) -> dict:
        """Load experiment config from YAML."""
        with config_path.open() as f:
            config = yaml.safe_load(f)
        
        # Validate prerequisites
        if config["experiment"]["enabled"]:
            cls._validate_prerequisites(config["experiment"]["prerequisites"])
        
        return config["experiment"]
    
    @classmethod
    def _validate_prerequisites(cls, prerequisites: list[str]):
        """Validate that prerequisites are met."""
        # TODO: Check P0/P1 verification status
        pass
```

### 5. 实验分析

#### 5.1 数据导出

```python
# 导出实验数据供分析
def export_experiment_data(experiment_name: str, start_date: str, end_date: str):
    """Export experiment data for analysis."""
    
    query = """
    SELECT
        session_id,
        user_id,
        device_id,
        voice_profile_id,
        experiment_group,
        tts_first_frame_ms,
        playback_status,
        user_feedback,
        created_at
    FROM media_sessions
    WHERE created_at BETWEEN %s AND %s
      AND experiment_name = %s
    """
    
    # Execute query and export to CSV
    # ...
```

#### 5.2 统计分析

**脚本**: `scripts/analyze_experiment.py`

```python
import pandas as pd
from scipy import stats


def analyze_experiment(csv_path: str):
    """Analyze experiment results."""
    
    df = pd.read_csv(csv_path)
    
    # 分组统计
    control = df[df["experiment_group"] == "control"]
    treatment = df[df["experiment_group"].str.startswith("treatment")]
    
    # TTS 延迟对比
    print("TTS Latency Comparison:")
    print(f"Control P95: {control['tts_first_frame_ms'].quantile(0.95):.0f}ms")
    print(f"Treatment P95: {treatment['tts_first_frame_ms'].quantile(0.95):.0f}ms")
    
    # 统计显著性检验
    t_stat, p_value = stats.ttest_ind(
        control["tts_first_frame_ms"],
        treatment["tts_first_frame_ms"],
    )
    print(f"T-test p-value: {p_value:.4f}")
    
    # 播放完成率
    control_completion = (control["playback_status"] == "completed").mean()
    treatment_completion = (treatment["playback_status"] == "completed").mean()
    
    print(f"\nPlayback Completion Rate:")
    print(f"Control: {control_completion:.2%}")
    print(f"Treatment: {treatment_completion:.2%}")
    
    # 用户反馈（如果有）
    if "user_feedback" in df.columns:
        control_positive = (control["user_feedback"] == "positive").mean()
        treatment_positive = (treatment["user_feedback"] == "positive").mean()
        
        print(f"\nPositive Feedback Rate:")
        print(f"Control: {control_positive:.2%}")
        print(f"Treatment: {treatment_positive:.2%}")
```

## 执行流程

### 阶段 1: 准备（1 周）

1. 确认前置条件全部满足
2. 配置实验参数
3. 准备监控和告警
4. 选择测试用户白名单

### 阶段 2: 小规模测试（1 周）

1. 启用实验，流量分配 95% control / 5% treatment
2. 仅对白名单用户生效
3. 密切监控质量指标
4. 收集用户反馈

### 阶段 3: 扩大规模（2 周）

1. 如果小规模测试通过，调整流量到 80% / 20%
2. 移除白名单限制
3. 持续监控并收集数据
4. 准备统计分析

### 阶段 4: 分析与决策（1 周）

1. 导出完整数据
2. 进行统计分析
3. 评估是否达到改进目标
4. 决定是否全量上线或回滚

## 验收标准

### 实验框架

✅ 实验配置可通过 YAML 管理  
✅ 流量分配支持百分比控制  
✅ 支持白名单/黑名单  
✅ 自动质量监控和告警  

### 前置条件验证

✅ P0 验证全部通过  
✅ P1 AEC 声学验收通过  
✅ Playback fence 和 Actual Heard 稳定  
✅ 基础 TTS 延迟和完成率达标  

### 数据收集

✅ 实验分配记录完整  
✅ 质量指标按 profile 分组  
✅ 数据可导出供分析  
✅ 支持统计显著性检验  

### 安全保障

✅ 实验默认关闭  
✅ 质量劣化自动告警  
✅ 支持快速回滚  
✅ 不影响默认保守路径  

## 参考

- 整改方案第 21.1 节：执行顺序
- 整改方案第 17.2 节：TTS 首帧/打断基准
- 整改方案第 17.8 节：自然度独立 voice profile A/B
