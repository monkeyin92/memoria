# P0-2 播放终态回执链路验证

## 当前状态分析

根据代码审查，播放终态回执链路的关键组件**已经实现**：

### ✅ 已实现的组件

1. **设备固件** (`firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc`)
   - `NotifyPlaybackDrained()` 方法（第2197行）
   - 发送 `playback.ended` 回执（第2220行）
   - `playback_terminal_receipted_` 标志防止重复发送

2. **Media Edge** (`services/media_edge/device_ws_uplink.go`)
   - 接收设备 `playback.ended` 事件
   - 转发到 Voice Core gRPC

3. **gRPC Bridge** (`services/agent/src/voice_core/grpc_bridge.py`)
   - `_playback_event_type_from_proto` 映射（第54-65行）
   - `on_playback_progress` 处理器调用（第904-905行）
   - 正确解析 `PlaybackEventType.ENDED`

4. **Voice Core Output Stream** (`services/agent/src/voice_core/media_session_output_stream.py`)
   - `on_playback_progress` 方法（第107-206行）
   - `terminal` 参数映射逻辑（第129-133行）:
     ```python
     terminal=(
         None if progress.event_type is PlaybackEventType.WATERMARK
         else progress.event_type is PlaybackEventType.ENDED
     )
     ```
   - `_finish_completed_output` 记录 `PLAYBACK_ENDED`（第546-550行）

5. **PlaybackLedger** (`services/agent/src/voice_core/playback_ledger.py`)
   - `acknowledge` 方法处理 `terminal` 参数（第227-230行）
   - `is_playback_complete` 检查终态（第287-296行）
   - `_terminal_received` 集合追踪已收到的终态

6. **ReplyDeliveryLedger** (`services/agent/src/voice_core/reply_delivery.py`)
   - `PLAYBACK_ENDED` 事件定义（第28行）
   - 幂等记录和终态管理

### 🔍 可能的问题点

根据文档记录的现象（首帧到板但无播放终态），问题可能在于：

#### 1. 设备端未触发 `NotifyPlaybackDrained()`

**症状**：
- 串口显示 `speaking` 状态
- 没有看到 `Sending playback.ended`

**可能原因**：
- AudioService 未正确报告播放队列排空
- `IsPlaybackIdleCallback` 未设置或返回 false
- 生成完成时队列已空，但 idle 探针未触发

**验证方法**：
```bash
# 串口监视关键日志
grep -E "Playback drained|Sending playback.ended|playback_terminal_receipted" serial_output.txt
```

#### 2. `provider_complete` 标志未设置

**触发条件**（`_finish_completed_output` 第530-534行）：
```python
if (
    not context.provider_complete
    or not context.playback.is_playback_complete(fence)
    or not context.runtime.fence.matches(fence)
):
    return
```

**可能原因**：
- TTS provider 未正确发送 `final=True` 帧
- `_stream_output` 未完成导致 `provider_complete` 未设置

**验证方法**：
```bash
# 检查 provider_completed 事件
docker-compose logs memoria-agent | grep "provider_completed\|ReplyDeliveryEvent.PROVIDER_COMPLETED"
```

#### 3. 设备回执缺少 `event_type` 字段

**症状**：
- Media Edge 接收到 `playback.ended` 但 gRPC 消息中 `event_type` 为默认值

**可能原因**：
- 固件发送的 JSON 缺少 `event_type: "ended"` 字段
- Edge 转发时未正确映射

**验证方法**：
```bash
# 检查设备回执完整内容
docker-compose logs memoria-media-edge | grep -A 5 "playback.ended"
```

## 诊断步骤

### 1. 运行诊断脚本

```bash
cd /Users/monkeyin/projects/memoria
chmod +x scripts/diagnose_playback_ended_chain.sh
./scripts/diagnose_playback_ended_chain.sh <session_id>
```

### 2. 串口监视

启动串口监视并保存完整输出：

```bash
screen -L -Logfile serial_$(date +%Y%m%d_%H%M%S).log /dev/cu.usbmodem1101 115200
```

关键日志标记：
- `[AudioService] Playback drained` - AudioService 报告播放完成
- `[MemoriaProtocol] NotifyPlaybackDrained called` - 通知方法被调用
- `[MemoriaProtocol] Sending playback.ended` - 发送终态回执

### 3. 服务器日志分析

```bash
# 完整链路搜索
SESSION_ID="<实际会话ID>"

# Edge 接收
docker-compose logs memoria-media-edge | grep "$SESSION_ID" | grep "playback"

# gRPC 转发
docker-compose logs memoria-media-edge | grep "$SESSION_ID" | grep "PlaybackProgress"

# Voice Core 处理
docker-compose logs memoria-agent | grep "$SESSION_ID" | grep -E "on_playback_progress|PlaybackEventType"

# ReplyDelivery 事件
docker-compose logs memoria-agent | grep "$SESSION_ID" | grep "reply_delivery_event_total"
```

## 可能需要的修复

### 修复 A：确保 AudioService 正确通知 drain

如果 `NotifyPlaybackDrained()` 未被调用，需要在固件中添加：

```cpp
// 在 AudioService 播放完成时
void AudioService::OnPlaybackComplete() {
    if (protocol_) {
        protocol_->NotifyPlaybackDrained();
    }
}
```

### 修复 B：添加 DEBUG 日志

在 `media_session_output_stream.py` 的 `on_playback_progress` 中添加：

```python
logger.info(
    "Playback progress: session=%s fence=%s event_type=%s terminal=%s "
    "provider_complete=%s is_playback_complete=%s",
    context.identity.session_id,
    fence,
    progress.event_type,
    terminal,
    context.provider_complete,
    context.playback.is_playback_complete(fence),
)
```

### 修复 C：确保 event_type 字段传递

检查固件 `SendPlaybackReceipt` 是否包含 `event_type` 字段：

```cpp
cJSON_AddStringToObject(receipt.value, "type", "playback.ended");
cJSON_AddStringToObject(receipt.value, "event_type", "ended");  // 确保此行存在
```

## 成功标准

完整链路验证通过时，应观察到：

1. **设备串口**：
   ```
   [AudioService] Playback drained
   [MemoriaProtocol] Sending playback.ended
   ```

2. **Media Edge 日志**：
   ```
   Received device event: playback.ended
   Forwarding PlaybackProgress with event_type=ENDED
   ```

3. **Voice Core 日志**：
   ```
   on_playback_progress: event_type=PlaybackEventType.ENDED, terminal=True
   Playback complete check: True
   Recording PLAYBACK_ENDED event
   ```

4. **ReplyDelivery 指标**：
   ```
   voice_reply_delivery_event_total{event="playback_ended"} 1
   ```

## 下一步

- [ ] 执行 P0-1 真机验证并收集日志
- [ ] 运行诊断脚本定位具体断点
- [ ] 根据诊断结果应用对应修复
- [ ] 重新验证完整链路
