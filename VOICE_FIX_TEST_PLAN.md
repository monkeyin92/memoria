# 语音交互修复测试计划

## 修复概览

本次修复针对三个核心问题：

### 问题 1: 识别率不稳定
**修复内容**：
- VAD 阈值从 0.3 提高到 0.5
- 静默超时从 1500ms 延长到 2000ms

**测试方法**：
1. 正常音量说话，验证识别率
2. 低声说话，验证是否仍能识别
3. 中途停顿 1-2 秒，验证是否被提前截断

**预期结果**：
- ✅ 正常音量识别率 >95%
- ✅ 不会在正常停顿时截断
- ✅ 环境噪音不再误触发

---

### 问题 2: 无法打断 AI 说话
**修复内容**：
- 在 `MediaAudioIngress` 添加 `start_speaking()` 方法
- 在 `on_playback_progress` 中集成状态转换：
  - `PlaybackEventType.STARTED` → `SPEAKING` 状态
  - `PlaybackEventType.ENDED` → `IDLE` 状态
- 系统现在知道 AI 何时在说话

**测试方法**：
1. 启动一个对话，让 AI 开始回复
2. 在 AI 说话过程中开始说话
3. 观察是否能成功打断 AI

**预期结果**：
- ✅ AI 开始说话时，系统进入 SPEAKING 状态
- ✅ 在 SPEAKING 状态下说话可以打断 AI
- ✅ AI 说话结束后，系统返回 IDLE 状态

**验证日志**：
```
Session {session_id}: IDLE -> SPEAKING
Session {session_id}: SPEAKING -> LISTENING (reason: interruption)
```

---

### 问题 3: 周围有人说话就自动进入聆听状态
**修复内容**：
- 移除会话连接时自动进入 LISTENING 状态的逻辑
- 现在需要显式触发才会开始聆听

**测试方法**：
1. 建立新会话连接
2. 观察系统初始状态（应该是 IDLE）
3. 周围有人说话，观察是否触发聆听
4. 通过按钮或命令显式触发聆听

**预期结果**：
- ✅ 连接后系统保持 IDLE 状态
- ✅ 周围声音不会触发聆听
- ✅ 只有显式触发才进入 LISTENING 状态

**注意**：此修复需要配合客户端实现按钮或唤醒词触发机制

---

## 回归测试

确保原有功能仍正常工作：

1. **正常对话流程**
   - IDLE → (触发) → LISTENING → (检测到语音) → PROCESSING → (AI 回复) → SPEAKING → IDLE

2. **超时处理**
   - LISTENING 状态下 5 秒无语音 → 自动返回 IDLE

3. **VAD 事件处理**
   - `SPEECH_START` 正确触发
   - `SPEECH_END` 正确触发
   - 状态转换日志清晰可读

---

## 部署检查清单

- [ ] 所有修改已完成
- [ ] 代码通过 lint 检查
- [ ] 单元测试通过
- [ ] 构建测试通过
- [ ] 创建新的部署分支
- [ ] 记录部署版本号
- [ ] 更新 DEPLOYMENT_COMPLETE.md

---

## 已知限制

### 临时限制
- **需要客户端支持**：移除自动聆听后，需要客户端实现触发机制（按钮或唤醒词）
- 如果客户端尚未实现，可以临时保留自动聆听逻辑

### 下一步优化
1. 集成唤醒词检测（"Hey Memoria"）
2. 添加按钮触发的 gRPC 事件
3. 根据实际使用情况进一步调整 VAD 参数
4. 添加用户可配置的灵敏度设置

---

## 验证命令

```bash
# 运行测试
pytest tests/voice_core/test_listening_state_manager.py -v

# 检查 lint
ruff check services/agent/src/voice_core/

# 构建测试
cd services/agent && python -m pytest
```

---

## 回滚方案

如果修复出现问题，可以快速回滚到上一个版本：

```bash
git checkout main
git checkout HEAD~1 -- services/agent/src/voice_core/
```

或使用上一个部署版本：`20260822-listening-fix`
