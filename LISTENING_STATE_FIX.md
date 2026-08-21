# 聆听状态卡住问题 - 根因分析与修复方案

**问题**: 系统一直显示"聆听中"，无法自动结束

**生成时间**: 2026-08-22

---

## 根因分析

### 1. 问题现象
- 用户按下按钮后，系统进入"聆听中"状态
- 说完话后，系统不会自动转到"处理中"状态
- 系统持续卡在"聆听中"，即使5秒空闲超时也未生效

### 2. 代码审查发现

#### ✅ 已实现的部分
```python
# services/agent/src/voice_core/listening_state_manager.py
class ListeningStateManager:
    async def transition_to_listening(self, session_id: str) -> bool:
        # 进入LISTENING状态，启动5秒空闲超时
        
    async def on_speech_detected(self, session_id: str) -> None:
        # 标记检测到语音，防止空闲超时
        
    async def on_speech_ended(self, session_id: str) -> None:
        # 语音结束后等待1.5秒静默，然后转到PROCESSING
        
    async def transition_to_processing(self, session_id: str) -> None:
        # 转到PROCESSING状态，停止接收音频
```

#### ❌ 缺失的集成
```python
# services/agent/src/voice_core/grpc_bridge.py (第750-796行)
# VAD事件处理代码已存在，但没有调用ListeningStateManager

async def _handle_vad_event(self, request):
    speech_end = int(event.type) == media_pb2.VAD_EVENT_SPEECH_END
    # ... 创建 SpeechSegment ...
    accepted = connection.session.timeline.add(segment)
    
    # ❌ 缺少：调用 on_speech_detected() 和 on_speech_ended()
```

```python
# services/agent/src/voice_core/media_audio_ingress.py (第640-647行)
# 已检查 should_accept_audio()，但状态转换未触发

if not self._state_manager.should_accept_audio(session_id):
    logger.debug("Dropping audio frame in state=%s", current_state.value)
    return

# ❌ 缺少：在ASR完成后调用 transition_to_processing()
```

### 3. 为什么5秒超时未生效？

可能原因：
1. **系统持续检测到语音**：denoiser可能将背景噪音识别为语音，持续触发`speech_detected`标志
2. **空闲超时任务被取消**：某个地方意外取消了timeout_task
3. **实际已超时但UI未更新**：后端已回到IDLE但前端显示未刷新

---

## 修复方案

### 方案1: 快速修复 - 集成VAD事件 ⭐ 推荐

**修改文件**: `services/agent/src/voice_core/grpc_bridge.py`

在VAD事件处理中添加状态管理器调用：

```python
# 在 _handle_vad_event 方法中 (第794行附近)

if accepted and self.on_speech_segment is not None:
    await self.on_speech_segment(connection.session, segment, 0)

# 🆕 添加：通知listening state manager
ingress = getattr(connection.session, '_audio_ingress', None)
if ingress is not None:
    session_id = connection.session.identity.session_id
    if speech_end:
        await ingress._state_manager.on_speech_ended(session_id)
    else:
        await ingress._state_manager.on_speech_detected(session_id)

return
```

**优点**：
- 最小改动
- 直接利用现有VAD事件
- 自动触发状态转换

**缺点**：
- 需要访问 `_audio_ingress`（私有属性）
- 需要在 `MediaVoiceSessionState` 中暴露 ingress

---

### 方案2: 完整修复 - 在ASR结果中触发

**修改文件**: `services/agent/src/voice_core/media_session_input.py`

在 ASR finalize 时调用状态转换：

```python
# 在 finalize_speech_segment 方法的末尾添加

async def finalize_speech_segment(...):
    # ... 现有逻辑 ...
    
    # 🆕 添加：转换到PROCESSING状态
    if finalize_reason in ("vad_end", "turn_commit"):
        ingress = getattr(context, '_audio_ingress', None)
        if ingress is not None:
            session_id = context.identity.session_id
            await ingress._state_manager.transition_to_processing(session_id)
    
    return True
```

**优点**：
- 更准确的状态转换时机（ASR真正完成）
- 逻辑清晰

**缺点**：
- 需要在多个地方添加集成点

---

### 方案3: 临时方案 - 禁用状态检查

如果需要立即恢复功能，可以暂时注释掉状态检查：

**修改文件**: `services/agent/src/voice_core/media_audio_ingress.py`

```python
# 第640-647行：注释掉状态检查
# if not self._state_manager.should_accept_audio(session_id):
#     current_state = self._state_manager.get_state(session_id)
#     logger.debug("Dropping audio frame in state=%s", current_state.value)
#     return
```

**优点**：
- 立即恢复音频处理
- 零风险

**缺点**：
- 回到之前的"always-on"录音模式
- 不解决根本问题

---

## 推荐执行步骤

### 第一步：诊断验证（5分钟）

运行以下命令查看实际状态：

```bash
# 查看agent日志中的listening state
docker-compose logs memoria-agent -f | grep -E "ListeningState|transition_to|should_accept_audio"

# 或者SSH到生产环境
ssh memoria-prod "docker logs memoria-agent --tail=100 | grep -E 'LISTENING|IDLE|PROCESSING'"
```

**期望看到**：
- `transition_to_listening` 日志（按钮按下时）
- `should_accept_audio` 返回 False 的日志（状态检查）
- 但**没有** `transition_to_processing` 或 `on_speech_ended` 日志

### 第二步：应用快速修复（10分钟）

1. 使用**方案1**修改 `grpc_bridge.py`
2. 重新构建并部署agent服务
3. 测试单轮对话

### 第三步：完整集成（30分钟）

1. 同时实现**方案1 + 方案2**
2. 添加transition_to_speaking()调用（在开始生成回复时）
3. 完整测试P0-1验收流程

---

## 测试验证

### 单元测试
```bash
cd services/agent
pytest tests/test_listening_state_manager.py -v
```

### 集成测试
按照 `docs/verification/P0-1-single-turn-verification-guide.md` 执行：

1. 按下按钮 → 日志显示 `transition_to_listening`
2. 说话 → 日志显示 `on_speech_detected`
3. 停止说话 → 日志显示 `on_speech_ended`
4. 1.5秒后 → 日志显示 `transition_to_processing`
5. 系统开始生成回复

### 验收标准
- ✅ 单轮对话无需按第二次按钮
- ✅ 空闲5秒后自动退出聆听
- ✅ 语音结束后1.5秒内开始处理
- ✅ 日志中有完整的状态转换链路

---

## 相关文件

### 核心文件
- `services/agent/src/voice_core/listening_state_manager.py` - 状态管理器实现
- `services/agent/src/voice_core/media_audio_ingress.py` - 音频入口（已集成，但未触发转换）
- `services/agent/src/voice_core/grpc_bridge.py` - VAD事件处理（需要添加集成）
- `services/agent/src/voice_core/media_session_input.py` - ASR终止处理（需要添加集成）

### 测试文件
- `services/agent/tests/test_listening_state_manager.py` - 单元测试
- `docs/verification/P0-1-single-turn-verification-guide.md` - 验收指南
- `scripts/diagnose_playback_ended_chain.sh` - 诊断脚本

---

## 影响评估

### 回归风险
- **低风险**：修改只是添加状态转换调用，不影响现有流程
- 如果状态管理器调用失败，音频处理仍会继续（防御性编程）

### 性能影响
- **几乎无影响**：状态转换是内存操作，延迟 < 1ms
- 空闲超时使用 asyncio.Task，不占用额外线程

### 兼容性
- **向后兼容**：如果 `_audio_ingress` 不存在，代码会优雅降级
- 不影响现有的VAD、ASR、playback链路

---

## 总结

**根本原因**：ListeningStateManager 已实现但未与VAD/ASR事件连接

**推荐方案**：方案1（VAD集成）+ 完整测试

**工作量估算**：
- 代码修改：10分钟
- 测试验证：30分钟
- 部署上线：20分钟
- **总计**：1小时

**优先级**：P0（阻塞用户体验）

---

**下一步**：执行第一步诊断，确认日志输出后立即应用方案1修复
