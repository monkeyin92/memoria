# P0-1 真机单轮对话验证指南

## 目标

完成一次完整的单轮对话，**不按第二次键**，让回复自然结束，取得完整的 `playback.ended` 和 `actual_heard` 证据。

## 前提条件

- ✅ 固件候选已刷入 `/dev/cu.usbmodem1101`（2026-08-21）
- ✅ 服务器候选已切流（Agent/Bridge/Edge）
- ✅ 串口监视已启动
- ⚠️ **关键**：本次测试必须让回复**自然播放完毕**，不要按第二次键打断

## 操作步骤

### 1. 确认环境

```bash
# 检查串口连接
ls -la /dev/cu.usbmodem*

# 启动串口监视（新终端）
screen /dev/cu.usbmodem1101 115200
```

### 2. 确认服务器状态

```bash
# 检查容器健康
cd /opt/memoria
docker-compose ps | grep -E "memoria-(agent|media-edge)"

# 检查活跃设备连接（应为 0，等待设备连接）
curl -s http://localhost:9091/metrics | grep active_device_connections

# 查看最新日志
docker-compose logs --tail=50 -f memoria-agent
```

### 3. 执行单轮对话

**重要提示**：

1. 只说**一句话**（5-10 秒长度）
2. 说完后**保持安静**，等待 AI 完整回复
3. **绝对不要**在 AI 说话时按键或说话
4. 让回复自然播放到结束

**推荐测试话术**：

```
"今天天气怎么样？"
"你好，请介绍一下自己"
"给我讲一个简短的故事"
```

### 4. 观察设备串口输出

期待看到的关键状态转换：

```
listening → user_speaking → finalizing → thinking → assistant_speaking → idle
```

**关键日志标记**：

- `[MemoriaProtocol] First playable downlink frame` - 首帧到达
- `[AudioService] Starting playback` - 开始播放
- `[MemoriaProtocol] Sending playback.started` - 播放开始回执
- `[MemoriaProtocol] Sending playback.progress` - 播放进度回执
- `[AudioService] Playback drained` - 播放队列已排空
- `[MemoriaProtocol] Sending playback.ended` - **播放结束回执（关键）**
- `idle` - 回到空闲状态

### 5. 收集服务器日志

```bash
# 记录会话 ID（从串口或日志中找到）
SESSION_ID="<从日志中获取>"

# 导出完整会话日志
mkdir -p /opt/memoria/verification/p0-1-$(date +%Y%m%d-%H%M%S)
cd /opt/memoria/verification/p0-1-$(date +%Y%m%d-%H%M%S)

# Agent 日志（包含 ReplyDeliveryLedger）
docker-compose logs memoria-agent | grep -A 20 -B 5 "$SESSION_ID" > agent.log

# Bridge 日志（包含 PlaybackEventType）
docker-compose logs memoria-voice-core-bridge | grep -A 20 -B 5 "$SESSION_ID" > bridge.log

# Media Edge 日志（包含 device playback receipt）
docker-compose logs memoria-media-edge | grep -A 20 -B 5 "$SESSION_ID" > edge.log

# 搜索关键证据
echo "=== ReplyDelivery Events ===" > evidence.txt
grep "reply_delivery_event_total\|playback_ended\|actual_heard" agent.log >> evidence.txt

echo "=== PlaybackEventType ===" >> evidence.txt
grep "PlaybackEventType\|PLAYBACK_ENDED\|playback.ended" bridge.log edge.log >> evidence.txt

echo "=== Device Receipts ===" >> evidence.txt
grep "playback\.started\|playback\.progress\|playback\.ended" edge.log >> evidence.txt
```

## 成功标准

✅ **必须同时满足所有条件**：

1. 串口显示完整状态流：`listening → user_speaking → thinking → assistant_speaking → idle`
2. 串口输出包含 `Sending playback.ended`
3. Agent 日志包含 `playback_ended=True` 或 `PLAYBACK_ENDED` 事件
4. Edge 日志包含设备上报的 `type: "playback.ended"` 回执
5. 同一 `session_epoch + turn_id + generation_id + tool_epoch` 的完整链路可追溯
6. 用户确认**听到了完整回复**且声音清晰

## 失败模式与排查

### 场景 A：首帧后进入 recovering

**症状**：`speaking → recovering → listening → recovering`

**排查**：

1. 检查是否有 `transport_rejected` 或 `fence_mismatch`
2. 确认 `session_epoch` 是否一致
3. 查看 WSS 关闭原因（1006 / 1000）

### 场景 B：播放期间设备重启或静音

**症状**：串口突然不再输出或重启

**排查**：

1. 检查看门狗超时
2. 检查内存溢出（PSRAM）
3. 检查音频队列欠载

### 场景 C：没有 playback.ended 回执

**症状**：`assistant_speaking` 停止但没有发送 `playback.ended`

**排查**：

1. 检查 `NotifyPlaybackDrained()` 是否被调用
2. 检查 `playback_terminal_receipted_` 标志
3. 检查 AudioService 的 drain 通知路径

### 场景 D：服务器日志显示 playback_ended=False

**症状**：Agent 日志中 `playback_ended=False` 或没有 ENDED 事件

**排查**：

1. 检查 Media Edge 是否正确转发了设备回执
2. 检查 Voice Core gRPC bridge 的 PlaybackEventType 解析
3. 检查 ReplyDeliveryLedger 的事件更新逻辑

## 数据保留

```bash
# 保存串口完整输出
# （从 screen 会话中按 Ctrl+A : 进入命令模式）
# 输入：hardcopy -h serial_output.txt

# 备份到时间戳目录
cp serial_output.txt /opt/memoria/verification/p0-1-$(date +%Y%m%d-%H%M%S)/

# 计算证据文件摘要
cd /opt/memoria/verification/p0-1-*
sha256sum *.log *.txt > checksums.txt
```

## 下一步

✅ **如果验证通过**：

- 更新 `architecture-status.yaml` 中 `direct_real_device_verified=true`
- 推进 T5-T7（Exact Playback）验收
- 继续 P0-2、P0-3

❌ **如果验证失败**：

- 根据失败模式修复对应代码
- 重新构建、切流、刷板
- 重新执行本验证

---

**执行人**: 用户本人（需要物理设备操作）  
**预计时间**: 10-15 分钟  
**阻塞后续**: P0-2, P0-3, T5-T7
