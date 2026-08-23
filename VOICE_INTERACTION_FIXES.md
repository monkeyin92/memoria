# 语音交互修复总结

**修复日期**: 2026-08-23  
**版本**: 20260823-voice-interaction-fixes

## 问题诊断

用户报告的三个核心问题：

1. **识别率不稳定** - 不能每次都识别到说话
2. **无法打断 AI** - AI 说话时无法打断
3. **误触发聆听** - 周围有人说话就自动进入聆听状态

## 根因分析

### 问题 1: 识别率不稳定
- 降噪配置 `skip_stage2_on_silence=True` 导致低声说话时跳过深度降噪
- 可能过度处理了语音信号

### 问题 2: 无法打断 AI
- 系统缺少 SPEAKING 状态集成
- AI 开始说话时没有调用 `transition_to_speaking()`
- 播放完成时没有调用 `stop_speaking()`
- 导致状态机不知道 AI 何时在说话

### 问题 3: 误触发聆听
- 会话建立时自动进入 LISTENING 状态
- 缺少按钮或唤醒词控制

## 实施的修复

### 修复 1: 优化降噪配置
**文件**: `services/agent/src/voice_core/media_audio_ingress.py:99`

```python
# 修改前
skip_stage2_on_silence=True

# 修改后
skip_stage2_on_silence=False  # Always apply deep denoising for better quality
```

**效果**: 所有语音输入都经过完整的两级降噪，提高低声说话的识别率

### 修复 2: 集成 SPEAKING 状态转换
**文件**: `services/agent/src/voice_core/media_session_output_stream.py`

#### 2.1 开始说话时转换状态
**位置**: `_stream_output` 方法，第一帧发送时

```python
# 第 60-61 行
# Transition to SPEAKING when AI starts responding
await self._audio_ingress.start_speaking(session_id)
```

#### 2.2 播放完成时恢复状态
**位置**: `on_playback_progress` 方法

```python
# 第 173 行：播放错误时
await self._audio_ingress.stop_speaking(session.identity.session_id)

# 第 193 行：播放完成时
await self._audio_ingress.stop_speaking(session.identity.session_id)
```

**效果**: 
- 系统知道 AI 何时在说话
- `should_accept_audio()` 可以根据 `allow_interruption` 决定是否接受打断
- 支持用户在 AI 说话时打断

### 修复 3: 禁用自动聆听
**文件**: `services/agent/src/voice_core/media_audio_ingress.py:164-170`

```python
# 修改前
async def initialize_session_listening(self, session_id: str) -> None:
    """Temporary: Auto-enters LISTENING state on connection."""
    await self._state_manager.transition_to_listening(session_id)

# 修改后
async def initialize_session_listening(self, session_id: str) -> None:
    """Initialize session but don't auto-enter LISTENING state.
    
    Listening state should be triggered explicitly by:
    - User pressing push-to-talk button
    - Wake word detection
    - Manual API call
    """
    # Don't auto-enter LISTENING state
    pass
```

**效果**: 
- 会话建立时不再自动进入聆听状态
- 需要通过按钮或唤醒词显式触发
- 消除误触发问题

## 状态转换流程（修复后）

```
IDLE → LISTENING → PROCESSING → SPEAKING → IDLE
  ↑       ↑           ↑            ↑         ↑
  |       |           |            |         |
  |    按钮/唤醒词    检测到语音    AI开始说话  播放完成
  |                                          |
  +------------------------------------------+
```

## 验证方法

运行验证脚本：
```bash
python verify_fixes.py
```

期望输出：
```
✅ All fixes verified successfully!
```

## 测试建议

### 场景 1: 正常对话
1. 按下说话按钮
2. 说话并等待识别
3. AI 开始回复
4. 观察是否能正确识别并回复

### 场景 2: 打断 AI
1. 按下说话按钮并说话
2. 等待 AI 开始回复
3. 在 AI 说话过程中再次按下按钮
4. 说话尝试打断
5. 观察 AI 是否停止并处理新输入

### 场景 3: 环境噪音
1. 在有其他人说话的环境中
2. 不按按钮
3. 观察系统是否误触发（应该不会）

### 场景 4: 低声说话
1. 按下说话按钮
2. 用较低的音量说话
3. 观察识别准确度（应该比之前好）

## 配置参数

当前关键参数配置：

```python
# VAD 配置
vad_threshold=0.5  # 适中的阈值，平衡灵敏度和误触发
vad_speech_ms=300  # 检测到语音后持续300ms才确认

# 降噪配置
skip_stage2_on_silence=False  # 总是应用深度降噪
```

## 后续优化建议

1. **添加唤醒词支持** - 实现 "Hey Assistant" 等唤醒词
2. **可调节打断策略** - 让用户选择是否允许打断
3. **动态 VAD 阈值** - 根据环境噪音自动调整
4. **降噪强度可配置** - 让用户根据环境选择降噪级别
5. **添加视觉反馈** - 在 UI 上显示当前状态（IDLE/LISTENING/SPEAKING）

## 相关文件

- `services/agent/src/voice_core/media_audio_ingress.py` - 音频输入和降噪
- `services/agent/src/voice_core/media_session_output_stream.py` - 音频输出和状态转换
- `services/agent/src/voice_core/listening_state_manager.py` - 状态管理器
- `verify_fixes.py` - 修复验证脚本

## 回滚方案

如果修复导致问题，可以回滚以下更改：

```bash
git diff HEAD~1 services/agent/src/voice_core/
git checkout HEAD~1 -- services/agent/src/voice_core/media_audio_ingress.py
git checkout HEAD~1 -- services/agent/src/voice_core/media_session_output_stream.py
```
