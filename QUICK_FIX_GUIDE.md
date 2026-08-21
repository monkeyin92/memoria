# 快速修复指南 - Listening State 卡住问题

## 问题
说话后系统一直显示"聆听中"，无法自动进入处理状态

## 已修复
✅ 代码已修复并提交到本地 Git (commit: 1193da9)

## 立即部署（3个简单步骤）

### 步骤1: 推送代码到远程仓库
```bash
cd /Users/monkeyin/projects/memoria
git push origin main
```

### 步骤2: SSH到生产环境并构建
```bash
ssh memoria-prod "cd /opt/memoria/releases && \
  git pull origin main && \
  sudo docker build \
    --build-arg MEMORIA_RELEASE_TAG=20260822-listening-fix \
    --build-arg MEMORIA_RELEASE_COMMIT=\$(git rev-parse --short HEAD) \
    --file infra/Dockerfile.agent \
    -t memoria-agent:20260822-listening-fix . && \
  sudo docker-compose stop memoria-agent && \
  sudo docker-compose up -d memoria-agent"
```

### 步骤3: 验证修复
```bash
# 查看日志确认服务启动
ssh memoria-prod "docker logs -f memoria-agent 2>&1 | grep -E 'ListeningState|VAD.*speech|transition'"

# 期望看到（按下按钮并说话后）：
# ✓ VAD speech_start: updated listening state
# ✓ VAD speech_end: triggered listening state transition  
# ✓ ASR finalized: transitioned to PROCESSING state
```

## 真机测试

1. **按下按钮** → 设备进入"聆听中"
2. **说话** → "今天天气怎么样"
3. **停止说话** → 等待 1.5-2 秒
4. **✅ 预期结果**：自动开始处理（无需再按按钮）

## 如果还有问题

### 问题A: 推送失败
```bash
# 检查远程仓库配置
git remote -v

# 如果需要设置远程仓库
git remote add origin <你的git地址>
git push -u origin main
```

### 问题B: 构建失败
```bash
# 查看具体错误
ssh memoria-prod "cd /opt/memoria/releases && git pull origin main"

# 手动构建查看详细输出
ssh memoria-prod
cd /opt/memoria/releases
sudo docker build --file infra/Dockerfile.agent -t memoria-agent:test .
```

### 问题C: 服务无法启动
```bash
# 查看完整错误日志
ssh memoria-prod "docker logs memoria-agent 2>&1 | tail -100"

# 回滚到之前的版本
ssh memoria-prod "cd /opt/memoria && \
  docker-compose stop memoria-agent && \
  sed -i 's/20260822-listening-fix/20260821-denoising-full/g' docker-compose.yml && \
  docker-compose up -d memoria-agent"
```

## 技术细节

**修改的文件**：
- `services/agent/src/voice_core/media_session_input.py` - VAD事件集成
- `services/agent/src/voice_core/media_audio_ingress.py` - ASR完成状态转换

**工作原理**：
1. VAD检测到语音开始 → 标记已检测到语音
2. VAD检测到语音结束 → 等待1.5秒静默
3. 1.5秒后自动转换到PROCESSING状态
4. ASR完成时也会确保转换到PROCESSING（双重保险）
5. 如果5秒内没有语音，自动退出LISTENING（空闲超时）

**安全性**：
- ✅ 所有状态转换都包裹在异常处理中
- ✅ 如果状态管理失败，不会中断VAD/ASR核心功能
- ✅ 向后兼容，不影响旧版本固件

## 完整文档

详细部署和故障排查指南：
- 📄 `docs/LISTENING_STATE_FIX_DEPLOYMENT.md` - 完整部署指南
- 📄 `LISTENING_STATE_FIX.md` - 问题根因分析
- 📄 `LISTENING_STATE_FIX_PATCH.py` - 代码补丁说明

---

**预计时间**：
- 部署：15-20分钟（包含构建）
- 验证：5-10分钟
- **总计**：约30分钟

**现在就开始执行步骤1吧！** 🚀
