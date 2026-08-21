# Memoria Reply Delivery Projection Release 20260821-121418-reply-delivery-projection

## 发布目标

- 将 Python Voice Core 的完整 generation fence 回复交付里程碑投影到 Control API 持久层，为
  `tts_started / first_audio_emitted / preempted / completed` 提供跨进程、可重放的观测证据。
- 投影不包含用户或助手文本、PCM、账号 ID，也不进入主人历史、记忆或摘要语义。
- Python Voice Core 继续保持交互权威；本次不改变路由、权限、主体、播放控制或 Go Media Edge
  决策边界。

## 候选范围

- 源码基线：`31259d95699788d3f310f1f408fdac68dd47a8f0` 及本发布文档/配置说明提交；发布
  tag 在文档提交后冻结，不移动。
- 切换：Control API、Agent、Voice Core Media Bridge。
- 保持不变：Media Edge、LiveKit、Speaker Model、两个兼容 Gateway、H5、小程序、Nginx、
  PostgreSQL、Redis、MinIO 与 ESP32 固件。
- Agent 与 Bridge 保持既有 runtime authority tag，只替换提交绑定的应用镜像。

## 配置与安全边界

- Control API 与 Agent 使用同一份、独立于全部既有 capability token 的
  `MEDIA_REPLY_DELIVERY_TOKEN`。
- Agent 独占独立 Fernet `MEDIA_REPLY_DELIVERY_SPOOL_KEY`，失败事件加密写入
  `/data/media-reply-delivery.spool`；Control API 不持有该 key。
- 生产候选显式启用 `MEDIA_REPLY_DELIVERY_ENABLED=true`，内部 URL 固定为 Docker DNS
  `http://control-api:8000/v1/internal/media-runtime/reply-delivery`。
- 两份生产 env 先做 root-only 冻结备份，再以 `root:root 0600` 原子安装；secret 值不得进入
  日志、命令输出、发布证据或仓库。

## 发布门禁

- 本地 Agent 全量 tracked unit、Control API 相关回归、Ruff、strict MyPy 与
  `git diff --check` 已通过；发布提交需再次通过 source/tag 与镜像 provenance 校验。
- 候选镜像必须为 `linux/amd64`，OCI revision/version/role 与冻结提交、tag、组件一致。
- 切流前冻结当前 Control、Agent、Bridge 镜像与 Compose 配置，备份 SQLite 与两份 env；失败时
  恢复镜像与 env，不触碰数据库、Redis、MinIO 或 H5。
- 切流后要求三个容器 healthy、restart count=0、Control live 正常、Bridge gRPC socket 正常、
  新内部端点鉴权/幂等/精确 delivery 查询通过，且无投影拒绝、spool 损坏或 key mismatch 日志。

## 验收边界

- 候选切流与零会话探针只可记为 `enabled + production runtime verified`，不能代替真实板卡出声、
  Actual Heard 或双讲验收。
- 完成服务端门禁后再通知用户对板说话，以同一 delivery/generation fence 采集
  `tts_started -> first_audio_emitted -> completed/preempted` 证据；在此之前保持
  `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1-T14 既有状态。

## 当前状态

- 待构建与切流；生产尚未启用，尚未请求用户真机说话。
