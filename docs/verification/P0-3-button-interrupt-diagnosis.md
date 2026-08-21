# P0-3 播放中打断路径诊断与修复

## 问题现象

**用户反馈**（2026-08-21）：
> "AI 说话时无法打断"

## 预期行为

用户在 AI 播放回复期间按下物理按钮时，应该：

1. **设备端立即响应**（< 150ms）：
   - 本地队列立即清空并静音
   - 停止接收下行音频帧

2. **服务端协调**（< 300ms 端到端）：
   - 取消当前 Generation
   - 关闭下行 Gate
   - 拒绝该 Generation 的迟到 PCM 帧

3. **状态恢复**：
   - 设备回到 `idle` 或 `listening` 状态
   - 可以开始新的对话轮次

## 代码链路分析

### ✅ 已实现的组件

#### 1. 设备固件 - 按钮处理

**文件**: `firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc`

**关键方法**: `NotifyLocalFlush()` (第2135-2195行)

```cpp
void MemoriaProtocol::NotifyLocalFlush() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    
    if (protocol_version_ == kProtocolVersionV2) {
        // 1. 捕获当前 fence 和水位
        const GenerationFence fence = fence_;
        const uint64_t local_flush_sample_end =
            playback_output_frames_ > 0 ? playback_output_end_ : 0;
        const bool had_active_generation = playback_active_ && fence.valid();
        
        // 2. 清空状态标志
        playback_completion_pending_ = false;
        playback_active_ = false;
        playback_audio_ready_ = false;
        playback_paused_ = false;
        downlink_started_ = false;
        receipt_generation_id_ = 0;
        playback_started_receipted_ = false;
        
        // 3. 调用 flush 回调（AudioService 清空队列）
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(0);  // 参数 0 = 关闭所有 generation
        }
        
        // 4. 发送 button.stop 事件
        if (had_active_generation) {
            playback_terminal_receipted_ = true;
            SendButtonStop(fence, local_flush_sample_end);
        }
    }
}
```

**button.stop 消息格式** (第1520-1543行):

```json
{
  "type": "button.stop",
  "version": 2,
  "stream_epoch": 1,
  "control_sequence": 123,
  "device_monotonic_ms": 5000,
  "expected_fence": {
    "turn_id": 1,
    "generation_id": 1,
    "tool_epoch": 0,
    "session_epoch": 1
  },
  "local_flush_sample_end": 2400
}
```

#### 2. Media Edge - 事件转发

**文件**: `services/media_edge/device_ws_uplink.go`

**处理逻辑**:

```go
case "button.stop":
    return c.handleButtonStop(envelope, runtime)
```

**`handleButtonStop` 方法** (推断实现):

1. 验证 fence 有效性
2. 检查 `AllowedBargeIn` 包含 "button"
3. 调用 `runtime.CancelGeneration(eventID, "device.button.stop", expected, deviceMonotonicMS)`

#### 3. Voice Core - Generation Cancel

**文件**: `services/agent/src/voice_core/grpc_bridge.py`

**客户端事件处理** (第842-857行):

```python
if envelope.type == "client.stop_assistant":
    if not self.bridge.accept_client_event(envelope):
        await self._error(connection, "stale_client_event", "client event rejected")
        return
    if self.on_client_event is not None:
        await self.on_client_event(
            connection.session,
            envelope,
            int(event.monotonic_ms),
        )
    await self.emit_generation(
        connection.session.identity.session_id,
        connection.session.fence,
        action=media_pb2.GENERATION_ACTION_CANCEL,
        reason=str(envelope.payload.get("reason", "client_stop")),
    )
```

## 可能的问题点

### 问题 A: `on_local_flush_requested_` 未设置

**症状**: 按钮触发 `NotifyLocalFlush()`，但音频继续播放

**验证**:
```cpp
// 检查 Application 是否调用了 SetLocalFlushCallback
if (on_local_flush_requested_ == nullptr) {
    ESP_LOGW(kTag, "Local flush callback not set!");
}
```

**修复**: 确保 Application 在初始化时调用：
```cpp
protocol->SetLocalFlushCallback([&](uint32_t generation_id) {
    audioService->FlushGeneration(generation_id);
    audioService->Mute();
});
```

### 问题 B: AudioService 未正确实现 Flush

**症状**: callback 被调用，但队列未清空

**验证**: 在 AudioService 中添加日志：
```cpp
void AudioService::FlushGeneration(uint32_t generation_id) {
    ESP_LOGI(TAG, "Flushing generation %u, queue size before: %d", 
             generation_id, GetQueueSize());
    
    // 清空队列逻辑
    ClearQueue();
    
    ESP_LOGI(TAG, "Queue size after flush: %d", GetQueueSize());
}
```

### 问题 C: 按钮事件未触发 `NotifyLocalFlush()`

**症状**: 按钮按下，但完全无响应

**验证**: 检查按钮回调路径：
```cpp
// 在按钮中断处理中
void ButtonCallback() {
    ESP_LOGI(TAG, "Button pressed, calling NotifyLocalFlush");
    protocol->NotifyLocalFlush();
}
```

### 问题 D: `button.stop` 被 Media Edge 拒绝

**症状**: 设备发送 button.stop，但 Edge 返回错误

**可能原因**:
1. `AllowedBargeIn` 不包含 "button"
2. `expected_fence` 无效
3. `stream_epoch` 不匹配

**验证**:
```bash
# 检查 Edge 日志
docker-compose logs memoria-media-edge | grep -E "button.stop|barge_source_forbidden|stop_rejected"
```

**修复**: 确保设备设置中允许按钮打断：
```json
{
  "device_settings": {
    "allowed_barge_in": ["button", "local_kws"]
  }
}
```

### 问题 E: Voice Core 拒绝 Generation Cancel

**症状**: Edge 调用 `CancelGeneration` 成功，但 Voice Core 未实际取消

**可能原因**:
1. `fence` 不匹配当前 generation
2. Generation 已经完成
3. `accept_client_event` 返回 false

**验证**:
```bash
# 检查 Voice Core 日志
docker-compose logs memoria-agent | grep -E "GENERATION_ACTION_CANCEL|client_stop|stale_client_event"
```

### 问题 F: 迟到的 PCM 帧未被拒绝

**症状**: 按钮触发取消，但音频继续播放几帧

**根因**: 已经在传输中的帧到达设备后仍被播放

**验证**: 检查设备是否拒绝旧 generation 的帧：
```cpp
// 在 ReceiveAudioFrame 中
if (frame.generation_id == stopped_generation_id_) {
    dropped_frames_.locally_stopped++;
    ESP_LOGD(TAG, "Dropping frame from stopped generation %u", frame.generation_id);
    return;
}
```

## 诊断步骤

### 1. 串口实时监视

```bash
screen -L -Logfile button_interrupt_$(date +%Y%m%d_%H%M%S).log /dev/cu.usbmodem1101 115200
```

在 AI 说话时按下按钮，查找：

- `[Button] Button pressed` - 按钮中断触发
- `[MemoriaProtocol] NotifyLocalFlush called` - Flush 方法被调用
- `[AudioService] Flushing generation` - 音频队列清空
- `[MemoriaProtocol] Sending button.stop` - 发送停止事件
- `speaking → idle` 或 `speaking → listening` - 状态转换

### 2. 服务器日志追踪

```bash
SESSION_ID="<实际会话ID>"
GENERATION_ID="<被打断的 generation_id>"

# Edge 接收 button.stop
docker-compose logs memoria-media-edge | grep "$SESSION_ID" | grep "button.stop"

# Edge 调用 CancelGeneration
docker-compose logs memoria-media-edge | grep "$SESSION_ID" | grep "device.button.stop"

# Voice Core 接收 cancel
docker-compose logs memoria-agent | grep "$SESSION_ID" | grep -E "GENERATION_ACTION_CANCEL|client_stop"

# 检查是否有拒绝的帧
docker-compose logs memoria-agent | grep "$SESSION_ID" | grep -E "stale_generation|transport_rejected"
```

### 3. 按钮延迟测试

创建测试脚本测量端到端延迟：

```python
# measure_button_latency.py
import time
import serial
import subprocess

def measure_interrupt_latency():
    """Measure time from button press to local silence."""
    
    # 1. 开始播放长回复
    print("Starting long response...")
    # (触发设备开始播放)
    
    time.sleep(2)  # 等待播放稳定
    
    # 2. 记录按钮按下时间（通过串口监视或外部触发）
    button_time = time.time()
    print(f"Button pressed at {button_time}")
    
    # 3. 监视串口，等待 "idle" 或 "listening"
    ser = serial.Serial('/dev/cu.usbmodem1101', 115200)
    while True:
        line = ser.readline().decode('utf-8', errors='ignore')
        if 'idle' in line or 'listening' in line:
            recovery_time = time.time()
            latency_ms = (recovery_time - button_time) * 1000
            print(f"State recovered at {recovery_time}")
            print(f"Total latency: {latency_ms:.1f} ms")
            break
    
    ser.close()
    
    # 目标：< 300ms P95
    return latency_ms
```

## 修复清单

### 修复 1: 确保 flush callback 已设置

**文件**: `firmware/esp32/Application` (或相应的初始化文件)

```cpp
// 在初始化 MemoriaProtocol 后
protocol->SetLocalFlushCallback([this](uint32_t generation_id) {
    ESP_LOGI(TAG, "Local flush requested for generation %u", generation_id);
    
    // 清空音频队列
    if (audio_service_) {
        audio_service_->FlushGeneration(generation_id);
        audio_service_->Mute();
    }
    
    // 清空网络接收队列
    if (generation_id == 0) {
        // 0 表示清空所有 generation
        protocol->ClearDownlinkQueue();
    }
});
```

### 修复 2: AudioService 实现原子 Flush

```cpp
void AudioService::FlushGeneration(uint32_t generation_id) {
    std::lock_guard<std::mutex> lock(queue_mutex_);
    
    // 停止当前播放
    if (current_generation_ == generation_id || generation_id == 0) {
        StopPlayback();
        current_generation_ = 0;
    }
    
    // 清空队列中匹配的帧
    auto it = audio_queue_.begin();
    while (it != audio_queue_.end()) {
        if (it->generation_id == generation_id || generation_id == 0) {
            it = audio_queue_.erase(it);
        } else {
            ++it;
        }
    }
    
    // 立即静音 DAC
    Mute();
}
```

### 修复 3: 添加诊断日志

在关键路径添加日志，便于定位问题：

**固件端**:
```cpp
void MemoriaProtocol::NotifyLocalFlush() {
    ESP_LOGI(kTag, "NotifyLocalFlush: playback_active=%d, fence.valid=%d",
             playback_active_, fence_.valid());
    
    // ... 现有逻辑 ...
    
    if (on_local_flush_requested_ != nullptr) {
        ESP_LOGI(kTag, "Calling flush callback for generation 0");
        on_local_flush_requested_(0);
    } else {
        ESP_LOGW(kTag, "Flush callback is NULL!");
    }
    
    if (had_active_generation) {
        ESP_LOGI(kTag, "Sending button.stop for generation %u", fence.generation_id);
        SendButtonStop(fence, local_flush_sample_end);
    }
}
```

**Voice Core**:
```python
# 在 on_client_event 处理中
logger.info(
    "Received button stop: session=%s fence=%s playback_active=%s",
    session.identity.session_id,
    fence,
    context.playback.current_fence == fence,
)
```

### 修复 4: 拒绝迟到帧

确保设备和 Voice Core 都拒绝旧 generation 的帧：

**固件** (在 `HandleAssistantAudioFrame` 中):
```cpp
if (frame.generation_id != 0 && frame.generation_id == stopped_generation_id_) {
    dropped_frames_.locally_stopped++;
    return;
}
```

**Voice Core** (已在 `_stream_output` 中实现):
```python
if not self._output_owner_is_current(context, lease):
    self.metrics.inc_media_stale_generation()
    await self._cancel_reply_task(context, fence, reason="superseded")
    return OutputDispatchResult(...)
```

## 验收测试

### 测试用例 1: 播放期间按钮打断

1. 触发一个 > 10 秒的回复
2. 在播放 3 秒后按下按钮
3. **预期**: 音频立即停止（< 150ms），设备回到 idle/listening

### 测试用例 2: 首帧前按钮打断

1. 触发回复
2. 在首帧到达前按下按钮
3. **预期**: 不播放任何音频，直接取消

### 测试用例 3: 迟到帧拒绝

1. 触发回复并立即按钮打断
2. 检查是否有迟到帧被拒绝
3. **预期**: 日志中出现 `dropped_frames.locally_stopped` 计数

### 测试用例 4: 打断后新对话

1. 打断当前回复
2. 立即开始新的对话
3. **预期**: 新对话正常进行，不受影响

## 成功标准

✅ 本地可听停止 P95 ≤ 150ms  
✅ 端到端确认 P95 ≤ 300ms  
✅ 迟到帧正确拒绝，计数 > 0  
✅ 打断后可立即开始新对话  
✅ 串口日志完整追踪: 按钮 → flush → button.stop → idle

## 下一步

- [ ] 执行诊断步骤，定位具体断点
- [ ] 应用对应修复
- [ ] 运行验收测试
- [ ] 测量并记录延迟指标
- [ ] 更新 T8 验收状态
