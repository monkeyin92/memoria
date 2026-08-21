# Memoria Reply Delivery Projection Release 20260821-121418-reply-delivery-projection

## 发布目标

- 将 Python Voice Core 的完整 generation fence 回复交付里程碑投影到 Control API 持久层，为
  `tts_started / first_audio_emitted / preempted / completed` 提供跨进程、可重放的观测证据。
- 投影不包含用户或助手文本、PCM、账号 ID，也不进入主人历史、记忆或摘要语义。
- Python Voice Core 继续保持交互权威；本次不改变路由、权限、主体、播放控制或 Go Media Edge
  决策边界。

## 候选范围

- 源码提交/tag：`caf70271222c43047c5d661052f3b717348cc209` /
  `20260821-121418-reply-delivery-projection`；tag 已冻结，不移动。
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

## 生产切流与证据

- 本地 clean tagged source 校验通过；Agent/Control `linux/amd64` 镜像归档 SHA-256
  `8d550424604c39b9247a97a2be5e6ddb246ab2ccacdcd19851d76b055b8f65c6`，source archive
  SHA-256 `3254ce64de3a23a454d552b8dc94e74dfe402d9ba586ffa676265b7a2ba63eb1`，服务器导入后
  OCI revision/version/role 与提交、tag、组件一致。
- Control API 运行 image ID `sha256:4c0d8966a885…`；Agent 与 Voice Core Media Bridge 运行
  `sha256:9b6260e5fc22…`。三个容器均 healthy、restart count=0；Agent/Control runtime authority
  tag 保持 `20260814-231749-direct-canary`，Bridge 保持 `20260816-bridge-liveness-83af813`。
- 生产 env 已安装独立投影 token 与 Agent-only Fernet key，两个文件均 `root:root 0600`；切流前
  SQLite 在线备份与两份 env 冻结备份位于 `/var/backups/memoria/`。内部端点验证错误 token=401、
  首次 POST=200/inserted、重复 POST=200/not inserted、精确 GET=200，合成事件随后删除。
- LiveKit、QwenRealtimeSearch、Doubao 五音色/时间戳/取消、FunASR 六轮 interim+final、DeepSeek 与
  InterruptSemantic 五类样本全部通过，readiness 恢复 200；Control/Bridge 相关错误日志计数均为 0。
- 回滚点分别为 `memoria-control-api:rollback-20260821-121418-reply-delivery-projection-pre-control`
  与 `memoria-agent:rollback-20260821-121418-reply-delivery-projection-pre-agent/-pre-bridge`。更早普通
  镜像标签与可回收 build cache 已定点删除，根盘 43%→40%，未删除数据卷、数据库或备份。
- root-only 证据目录 `/opt/memoria/direct-canaries/20260821-121418-reply-delivery-projection/`：
  `CUTOVER_RESULT.txt` SHA-256 `c68692b2f01de12124d88060df05cca39b584e03996246589b9e23bebd57cdb2`、
  `POST_CUTOVER_STATE.json` `97f26552009c04fdecb6fd865b274781645ba45386e411d3873524bfd4942c79`、
  `CONTROL_ENDPOINT_SMOKE.json` `d356ff591db3c6208e189207c183ea03022e132475e08faa8598d49dfb893f52`、
  `PROVIDER_SMOKE_RESULT.txt` `b820915c7b05573311bf1fdd1089b3264a7055b1d7db7a08d3591bc013efdf7e`、
  `ASSET_CLEANUP.txt` `29c85bfbe3b7c51ac482362b9bf7e8e86f10266bda109425b0b6a35591d4a174`，
  更新后 manifest SHA-256 `4f9bb2289fc1c520b5630669388647a5c1e090783d5f7a7f4c75e4b26434e637`。

## 验收边界

- 候选切流与零会话探针只可记为 `enabled + production runtime verified`，不能代替真实板卡出声、
  Actual Heard 或双讲验收。
- 完成服务端门禁后再通知用户对板说话，以同一 delivery/generation fence 采集
  `tts_started -> first_audio_emitted -> completed/preempted` 证据；在此之前保持
  `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1-T14 既有状态。

## 当前状态

- `code + wired + enabled + production runtime verified`（2026-08-21，零真机会话范围）。服务端已准备
  接收真机话轮；尚未取得候选真实板卡首帧、Playback/Actual Heard 或双讲证据。
