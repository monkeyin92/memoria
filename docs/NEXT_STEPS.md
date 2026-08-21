# Memoria ESP32 全双工整改 - 下一步行动清单

**更新日期**: 2026-08-22

## 当前状态

- ✅ P0（关键阻塞）：设计完成，部分代码实现
- ✅ P1（全双工前置）：设计完成，待固件实施
- ⏳ P2（产品完整性）：未开始
- ⏳ P3（运维与实验）：未开始

---

## 优先级 1：P0 验证与修复（本周）

### ① P0-1 单轮验证

**负责人**: 用户（需要真机）  
**预计时间**: 1-2 小时  
**文档**: `docs/verification/P0-1-single-turn-verification-guide.md`

**操作步骤**:
```bash
# 1. 连接串口
screen /dev/cu.usbmodem1101 115200

# 2. 触发对话（说一句话，不按第二次键）

# 3. 观察串口输出，检查是否看到：
#    - "speaking" 状态
#    - "Sending playback.ended"
#    - 回到 "idle" 或 "listening"
```

**预期结果**: 单轮对话完整，不需要按第二次键

---

### ② P0-2/P0-3 问题诊断

**负责人**: 用户 + 开发团队  
**预计时间**: 2-4 小时  
**文档**: 
- `docs/verification/P0-2-playback-ended-chain-analysis.md`
- `docs/verification/P0-3-button-interrupt-diagnosis.md`

**操作步骤**:
```bash
# 1. 运行播放终态诊断
cd /Users/monkeyin/projects/memoria
chmod +x scripts/diagnose_playback_ended_chain.sh
./scripts/diagnose_playback_ended_chain.sh <session_id>

# 2. 如果发现问题，按文档应用对应修复
# 3. 重新测试验证
```

**预期结果**: 
- 播放终态正确回执
- 按钮打断延迟 < 300ms

---

### ③ P0-4 模型身份过滤器集成

**负责人**: 开发团队  
**预计时间**: 2-3 小时  
**文档**: `docs/verification/P0-4-model-identity-filter.md`

**操作步骤**:
```bash
# 1. 运行单元测试
cd services/agent
python -m pytest tests/unit/test_output_content_filter.py -v

# 2. 集成到输出流
# 修改 services/agent/src/voice_core/media_session_output_stream.py
# 参考文档第 2.A 节

# 3. 真机验证
# 触发对话，确认不再说"我是 DeepSeek"
```

**预期结果**: 
- 测试全部通过
- 真机验证不泄露模型身份

---

## 优先级 2：P1 固件实施（本月）

### ④ P1-1 AEC Reference 实施

**负责人**: 固件开发团队  
**预计时间**: 3-5 天  
**文档**: `docs/implementation/P1-1-aec-reference-alignment.md`

**关键任务**:
1. 创建 `audio/aec_reference.h/cc`
2. 创建 `audio/afe_pipeline.h/cc`
3. 集成 ESP-SR AFE
4. 执行时间对齐校准

**验收标准**:
- Reference 与 Mic 对齐误差 < 10 samples
- Far-end only VAD 误触发率 < 5%

---

### ⑤ P1-2 本地停止词实施

**负责人**: 固件开发团队  
**预计时间**: 2-3 天（+ 模型训练时间）  
**文档**: `docs/implementation/P1-2-local-stop-keywords.md`

**关键任务**:
1. 联系 Espressif 训练自定义停止词模型
2. 创建 `audio/stop_keyword.h/cc`
3. 集成到 AFE Pipeline
4. 测试准确率和延迟

**临时方案**: 使用通用唤醒词模型测试流程

**验收标准**:
- 停止词检测率 ≥ 90%
- 延迟 ≤ 500ms

---

### ⑥ P1-3 语义打断实施

**负责人**: 服务端开发团队  
**预计时间**: 2-3 天  
**文档**: `docs/implementation/P1-3-semantic-interruption-classification.md`

**关键任务**:
1. 创建 `voice_core/interruption_classifier.py`
2. 创建 `voice_core/interruption_policy.py`
3. 集成到 `media_session_output_stream.py`
4. 测试分类准确率

**验收标准**:
- 附和识别准确率 ≥ 95%
- 停止指令识别准确率 ≥ 90%

---

## 优先级 3：P1 声学验收（下月）

### ⑦ P1-4 AEC 声学矩阵测试

**负责人**: 测试团队 + 用户  
**预计时间**: 7-12 天  
**文档**: `docs/implementation/P1-4-aec-acoustic-matrix-verification.md`

**执行阶段**:
1. **基础功能验证**（1-2 天）
   - Far-end only @ 50%
   - Near-end only @ 1m
   - Double-talk @ 50%, 1m

2. **核心矩阵验证**（3-5 天）
   - 音量矩阵：20%, 50%, 80%, 100%
   - 距离矩阵：0.5m, 1m, 2m, 3m
   - 说话人矩阵：成年男/女、儿童

3. **扩展场景验证**（2-3 天）
   - 环境噪声：音乐、电视、风扇
   - 方位角度：正面、侧面、背面

4. **长时间稳定性**（1-2 天）
   - 连续对话 100 轮、500 轮
   - 内存泄漏检查

**验收标准**:
- 所有"必须通过"项全部通过
- 至少 80% "建议通过"项通过
- 服务端标记为 `full_duplex_verified`

---

## 优先级 4：P2 产品功能（后续）

待 P0/P1 验证通过后开始：

- P2-1: BLE/SoftAP 配网
- P2-2: Soul/Persona 配置界面
- P2-3: 家庭成员与监护设置
- P2-4: 设备管理功能
- P2-5: 回顾与总结功能
- P2-6: 诊断功能
- P2-7: 小程序 PR-19 发布

---

## 优先级 5：P3 运维（并行）

可以与 P1/P2 并行进行：

- P3-1: 完善构建供应链门禁
- P3-2: 实现自然度受控实验

---

## 关键检查点

### Checkpoint 1: P0 验证通过（本周末）

- [ ] 单轮对话稳定
- [ ] 播放终态正确
- [ ] 按钮打断正常
- [ ] 模型身份不泄露

**决策点**: 是否开始 P1 固件实施

---

### Checkpoint 2: P1 固件完成（本月底）

- [ ] AEC Reference 对齐完成
- [ ] 本地停止词实现完成
- [ ] 语义打断分类完成

**决策点**: 是否开始 P1-4 声学验收

---

### Checkpoint 3: 声学验收通过（下月中）

- [ ] 声学矩阵测试通过
- [ ] 服务端标记 full_duplex_verified
- [ ] 长时间稳定性验证通过

**决策点**: 是否开放全双工到生产环境

---

## 团队分工建议

### 用户/项目负责人
- P0-1 真机验证
- P1-4 声学测试参与
- 整体进度跟踪

### 固件开发团队
- P0 问题修复（如果诊断发现问题）
- P1-1 AEC Reference 实施
- P1-2 本地停止词实施

### 服务端开发团队
- P0-4 过滤器集成
- P1-3 语义打断实施
- P2 产品功能开发

### 测试团队
- P0 验收测试
- P1-4 声学矩阵测试
- 回归测试

---

## 资源需求

### 人力
- 固件工程师: 1-2 人，3-4 周
- 服务端工程师: 1 人，1-2 周
- 测试工程师: 1 人，2 周
- 测试志愿者: 3-5 人（不同年龄段）

### 设备
- ESP32-S3 开发板：至少 2 块
- 测试环境：安静房间 + 噪声环境
- 音频测试设备：麦克风、音箱、分贝计

### 时间
- P0 验证: 1 周
- P1 实施: 3-4 周
- P1 验收: 2 周
- P2 开发: 4-6 周（如需要）

---

## 风险管理

### 高风险项

**风险 1**: AEC 硬件对齐失败  
**缓解**: 降级到 interrupt_assist 或考虑硬件改进  
**应急**: 保持半双工模式，优化其他体验

**风险 2**: 停止词模型训练延迟  
**缓解**: 先测试按钮和语义打断，停止词作为增强功能  
**应急**: 暂时不启用停止词功能

**风险 3**: 声学矩阵无法全部通过  
**缓解**: 按整改方案降级路径处理  
**应急**: 限制使用场景（如仅支持 50% 音量、1m 距离）

---

## 成功庆祝里程碑 🎉

- ✅ P0 验证通过 → 基础功能稳定
- ✅ P1 实施完成 → 全双工技术就绪
- ✅ 声学验收通过 → 达到生产标准
- ✅ 首个全双工用户对话 → 产品里程碑

---

## 联系与协作

- **技术文档**: `/Users/monkeyin/projects/memoria/docs/`
- **实施代码**: `/Users/monkeyin/projects/memoria/services/`
- **固件代码**: `/Users/monkeyin/projects/memoria/firmware/esp32/`
- **问题跟踪**: GitHub Issues 或项目管理工具
- **进度报告**: 建议每周更新 `IMPLEMENTATION_PROGRESS.md`
