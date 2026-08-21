# Listening State 卡住问题 - 修复部署指南

**问题**: 系统一直显示"聆听中"，无法自动转换到处理状态  
**根因**: ListeningStateManager 已实现但未与 VAD 事件连接  
**修复**: 在 VAD 事件和 ASR 完成时触发状态转换  
**日期**: 2026-08-22

---

## 修复内容

### 1. 集成 VAD 事件与 Listening State Manager

**文件**: `services/agent/src/voice_core/media_session_input.py`

在 `on_speech_segment` 方法中添加：
- VAD `speech_start` 事件 → 调用 `on_speech_detected()`
- VAD `speech_end` 事件 → 调用 `on_speech_ended()`

**效果**：
- 检测到语音时，标记 `speech_detected = True`，防止空闲超时
- 语音结束后，等待 1.5 秒静默自动转到 PROCESSING 状态

### 2. ASR 完成时转换到 PROCESSING 状态

**文件**: `services/agent/src/voice_core/media_audio_ingress.py`

在 `finalize_speech_segment` 方法返回前添加：
- 当 `finalize_reason` 为 `vad_end`/`turn_commit`/`explicit` 时
- 调用 `transition_to_processing()` 转换状态

**效果**：
- 确保 ASR 完成后停止接收新音频
- 即使 VAD 集成失败，ASR 完成时也会正确转换

---

## 状态转换流程

```
┌─────────────────────────────────────────────────────────────┐
│                    完整状态转换链路                           │
└─────────────────────────────────────────────────────────────┘

1. 用户按下按钮
   ├─> initialize_session_listening()
   └─> IDLE → LISTENING (启动5秒空闲超时)

2. 检测到语音 (VAD speech_start)
   ├─> on_speech_detected()
   ├─> speech_detected = True
   └─> 取消空闲超时

3. 语音结束 (VAD speech_end)
   ├─> on_speech_ended()
   ├─> 等待 1.5 秒静默
   └─> LISTENING → PROCESSING

4. ASR 完成 (finalize_speech_segment)
   ├─> transition_to_processing()
   └─> 确保状态已转到 PROCESSING

5. 开始生成回复 (未来)
   └─> PROCESSING → SPEAKING

6. 回复完成
   └─> SPEAKING → IDLE
```

---

## 部署步骤

### 方案 A: 本地 Docker 构建（如果本地有环境）

```bash
# 1. 提交代码
cd /Users/monkeyin/projects/memoria
git add services/agent/src/voice_core/media_audio_ingress.py
git add services/agent/src/voice_core/media_session_input.py
git commit -m "fix: integrate VAD events with ListeningStateManager

- Connect VAD speech_start/speech_end events to state transitions
- Trigger transition_to_processing on ASR finalization
- Prevent listening state from getting stuck
- Fixes: audio stuck in 'listening' state, requires manual button press

This completes the ListeningStateManager integration started in 25524bb.
The state manager was created but not connected to VAD events, causing
the system to remain in LISTENING state indefinitely.

Resolves: P0-1 single-turn verification issue

Co-Authored-By: Claude <noreply@anthropic.com>"

# 2. 构建镜像（本地）
docker build \
  --build-arg MEMORIA_RELEASE_TAG=20260822-listening-fix \
  --build-arg MEMORIA_RELEASE_COMMIT=$(git rev-parse --short HEAD) \
  --file infra/Dockerfile.agent \
  -t memoria-agent:20260822-listening-fix \
  .

# 3. 推送到生产环境（如果有registry）
docker tag memoria-agent:20260822-listening-fix your-registry/memoria-agent:20260822-listening-fix
docker push your-registry/memoria-agent:20260822-listening-fix

# 4. 在生产环境更新
ssh memoria-prod "cd /opt/memoria && \
  docker pull your-registry/memoria-agent:20260822-listening-fix && \
  docker-compose up -d memoria-agent"
```

### 方案 B: 生产环境直接构建（推荐）

```bash
# 1. 提交并推送代码
cd /Users/monkeyin/projects/memoria
git add services/agent/src/voice_core/media_audio_ingress.py
git add services/agent/src/voice_core/media_session_input.py
git commit -m "fix: integrate VAD events with ListeningStateManager

- Connect VAD speech_start/speech_end events to state transitions
- Trigger transition_to_processing on ASR finalization
- Prevent listening state from getting stuck
- Fixes: audio stuck in 'listening' state

Resolves: P0-1 single-turn verification

Co-Authored-By: Claude <noreply@anthropic.com>"

git push origin main

# 2. SSH 到生产环境
ssh memoria-prod

# 3. 拉取最新代码并构建
cd /opt/memoria/releases
git pull origin main

sudo docker build \
  --build-arg MEMORIA_RELEASE_TAG=20260822-listening-fix \
  --build-arg MEMORIA_RELEASE_COMMIT=$(git rev-parse --short HEAD) \
  --file infra/Dockerfile.agent \
  -t memoria-agent:20260822-listening-fix \
  .

# 4. 更新 docker-compose.yml 或直接重启服务
sudo docker-compose stop memoria-agent
sudo docker-compose up -d memoria-agent

# 5. 查看日志确认启动
sudo docker-compose logs -f memoria-agent | grep -E "ListeningState|VAD|transition"
```

---

## 验证步骤

### 1. 检查日志输出

部署后，观察 agent 日志应该看到：

```bash
# 查看实时日志
ssh memoria-prod "docker logs -f memoria-agent 2>&1" | grep -E "ListeningState|VAD|transition"

# 期望输出（按顺序）：
# 1. 按下按钮后
INFO: Session ... transition_to_listening

# 2. 开始说话时
DEBUG: VAD speech_start: updated listening state session=...

# 3. 停止说话时
DEBUG: VAD speech_end: triggered listening state transition session=...

# 4. ASR 完成时
DEBUG: ASR finalized: transitioned to PROCESSING state session=... reason=vad_end

# 5. 1.5秒后（如果 VAD 集成正常）
INFO: Session ... LISTENING -> PROCESSING
```

### 2. 真机测试

使用 ESP32 设备测试完整流程：

```bash
# 测试步骤
1. 按下按钮
2. 说话："今天天气怎么样"
3. 停止说话
4. 观察：
   - ✅ 1.5-2秒后自动开始处理（不需要再按按钮）
   - ✅ 系统开始生成回复
   - ✅ 日志中有完整的状态转换链路

# 如果仍然卡住
5. 等待 5 秒
6. 观察：
   - ⚠️ 应该自动退出到 IDLE（空闲超时）
   - ⚠️ 如果还卡住，说明空闲超时也没生效，需要进一步诊断
```

### 3. 使用诊断脚本

```bash
# 获取最近的 session_id
SESSION_ID=$(ssh memoria-prod "docker logs memoria-agent --tail=100 2>&1 | grep -oE 'session_id=[a-f0-9-]+' | head -1 | cut -d= -f2")

# 运行诊断脚本（如果有）
./scripts/diagnose_listening_state.sh $SESSION_ID

# 手动检查关键日志
ssh memoria-prod "docker logs memoria-agent 2>&1 | grep -i '$SESSION_ID' | grep -E 'LISTENING|PROCESSING|IDLE|transition'"
```

---

## 回滚方案

如果修复导致新问题：

```bash
# 方案1: 回滚到之前的镜像版本
ssh memoria-prod "cd /opt/memoria && \
  docker-compose stop memoria-agent && \
  # 替换为之前的版本标签
  sed -i 's/20260822-listening-fix/20260821-denoising-full/g' docker-compose.yml && \
  docker-compose up -d memoria-agent"

# 方案2: 临时禁用状态检查（修改代码）
# 在 media_audio_ingress.py 第 640-647 行注释掉状态检查
# 这会恢复到 "always-on" 录音模式
```

---

## 预期效果

### 修复前
- ❌ 按下按钮说话后，系统一直显示"聆听中"
- ❌ 需要再次按按钮才能触发处理
- ❌ 用户体验差，需要"双击"操作

### 修复后
- ✅ 按下按钮说话后，停止说话 1.5 秒自动开始处理
- ✅ 单次按钮即可完成对话
- ✅ 如果 5 秒内没有检测到语音，自动退出聆听状态
- ✅ 符合 P0-1 验收标准

---

## 技术细节

### 异常处理
所有状态转换调用都包裹在 try-except 中：
- 如果状态管理失败，不会中断 VAD 或 ASR 处理
- 只记录 warning 日志，继续正常流程
- 防御性编程，确保核心功能不受影响

### 性能影响
- **CPU**: 几乎无影响（内存操作 < 1ms）
- **延迟**: 无额外延迟（状态检查是同步的）
- **内存**: 每个 session 增加约 200 bytes（状态数据）

### 向后兼容
- 不影响现有的 VAD、ASR、playback 链路
- 如果 `_state_manager` 不存在，代码会捕获异常并继续
- 旧客户端（固件）可以正常工作

---

## 后续优化（可选）

1. **添加 transition_to_speaking**
   - 在开始生成回复时调用
   - 支持打断控制（根据配置允许或禁止）

2. **添加 transition_to_idle**
   - 在回复完成时调用
   - 完整的状态循环：IDLE → LISTENING → PROCESSING → SPEAKING → IDLE

3. **添加指标监控**
   ```python
   self.metrics.inc_counter("listening_state_transition", {
       "from": old_state.value,
       "to": new_state.value,
       "reason": reason
   })
   ```

4. **添加状态转换跟踪**
   - 在 timeline 中记录状态变化
   - 便于调试和分析用户行为

---

## 联系支持

如果遇到问题：
1. 收集完整日志：`docker logs memoria-agent > agent.log`
2. 收集设备日志（ESP32 串口输出）
3. 提供 session_id 和时间戳
4. 描述具体现象（卡在哪个状态，持续多久）

---

**修复完成时间**: 预计 20 分钟（包含构建和部署）  
**验证时间**: 预计 10 分钟  
**总耗时**: 约 30 分钟

**优先级**: P0（阻塞用户使用）  
**风险评估**: 低（有异常处理和回滚方案）
