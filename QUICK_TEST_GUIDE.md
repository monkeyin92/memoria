# 语音交互修复 - 快速测试指南

## 修复内容概览

✅ **修复 1**: 提升语音识别稳定性（特别是低声说话）  
✅ **修复 2**: 支持打断 AI 说话  
✅ **修复 3**: 消除误触发聆听（周围人说话不再触发）

## 快速测试步骤

### 测试 1: 基础识别（2分钟）

1. 启动应用并建立语音会话
2. **观察初始状态** - 系统应该在 IDLE 状态，不会自动开始聆听
3. 按下说话按钮
4. 正常音量说："今天天气怎么样？"
5. 释放按钮，等待 AI 回复

**预期结果**: 
- ✅ 能正确识别并给出回复
- ✅ 周围有人说话时不会误触发

### 测试 2: 低声说话（2分钟）

1. 按下说话按钮
2. **用较低的音量**说："帮我计算一下 25 加 37"
3. 释放按钮

**预期结果**: 
- ✅ 即使音量较低也能正确识别
- ✅ 比之前的版本识别率更高

### 测试 3: 打断 AI（3分钟）

1. 按下说话按钮
2. 说："给我讲一个长故事"
3. 等待 AI 开始回复
4. **在 AI 说话过程中**，再次按下按钮
5. 说："停，换个话题，告诉我现在几点"

**预期结果**: 
- ✅ AI 停止当前回复
- ✅ 系统接受新的输入
- ✅ AI 回答新问题

### 测试 4: 环境抗干扰（3分钟）

1. 在有其他人对话的环境中
2. **不按按钮**
3. 让周围的人说话 1-2 分钟
4. 观察系统是否误触发

**预期结果**: 
- ✅ 系统保持 IDLE 状态
- ✅ 不会误识别周围的对话

## 常见问题排查

### 问题: 按按钮后没有反应

**检查项**:
```bash
# 1. 确认服务正在运行
ps aux | grep voice

# 2. 查看日志
tail -f logs/voice_core.log | grep "transition"
```

**预期日志**: 应该看到 `IDLE -> LISTENING` 的状态转换

### 问题: 仍然无法打断 AI

**检查项**:
```bash
# 确认修复已部署
grep "start_speaking" services/agent/src/voice_core/media_session_output_stream.py
```

**预期输出**: 应该找到 `await self._audio_ingress.start_speaking(session_id)`

### 问题: 识别率仍然不好

**检查项**:
```python
# 确认降噪配置
grep "skip_stage2_on_silence" services/agent/src/voice_core/media_audio_ingress.py
```

**预期输出**: `skip_stage2_on_silence=False`

## 关键配置参数

如果需要微调，可以调整这些参数（位于 `media_audio_ingress.py`）:

```python
# VAD 灵敏度（0.0-1.0，越高越不敏感）
vad_threshold=0.5

# 静音判定时间（秒，判定说话结束的等待时间）
silence_timeout=2.0

# 是否允许打断
allow_interruption=True
```

## 部署确认

运行验证脚本确认所有修复已正确部署：

```bash
python verify_fixes.py
```

**预期输出**:
```
✅ All fixes verified successfully!
```

## 反馈记录

测试后请记录：

- [ ] 识别率是否提升？（特别是低声说话）
- [ ] 能否成功打断 AI？
- [ ] 是否还有误触发？
- [ ] 使用体验如何？

## 详细文档

完整技术细节见: `VOICE_INTERACTION_FIXES.md`
