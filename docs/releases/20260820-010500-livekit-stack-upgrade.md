# Memoria LiveKit Stack Upgrade Release 20260820-010500-livekit-stack-upgrade

## 发布目标

- 升级第三方开源框架 LiveKit 与 StreamCore（自研 Go media edge）底层 Pion 依赖至最新补丁版本，获取稳定性与延迟修复。
- 不改变话轮路由、权限、主体、fence 或 fail-closed 行为。

## 升级内容

- Python：`livekit-agents / livekit-plugins-openai / livekit-plugins-silero` `1.6.5 -> 1.6.10`（打断时保留已完成 tool 结果、无归属 segment 不清除主说话人、`agent_false_interruption` 转发、STT hooks 穿透 MultiSpeakerAdapter、dynamic endpointing max_delay 稳定化）；传递依赖 `livekit rtc 1.1.13 -> 1.1.14`、`livekit-protocol 1.1.19 -> 1.1.22`、`openai 2.45.0 -> 2.54.0`。
- 自建 LiveKit Server：`v1.13.3 -> v1.13.5`（修复 pion/ice close 挂起、goroutine 泄漏、下行拥塞 DataChannel 缓冲加界、转发包 padding bit 与 max layer index panic）。
- H5：`livekit-client ^2.20.1 -> ^2.22.0`（可靠 DataChannel 并发写断死修复、重连/resume 后缓冲事件 flush）。
- StreamCore（Go media edge）：`pion/ice v4.4.0 -> v4.4.1`、`pion/turn v5.0.12 -> v5.0.13`、`pion/transport v4.0.2 -> v4.1.0`（turn 重复 CreateTCPConnection 死锁修复、ICE lite agent 超时调整）。

## 源码与部署范围

- 源提交/tag：`59fbf9969987bd9df13709a1cc1a0051aafc6aa6` / `20260820-010500-livekit-stack-upgrade`，已推送 `origin/main`，tag 不移动。
- Agent 注册探针契约已在 1.6.10 实测复核：`AgentServer._id/_closed/_connecting/_connection_failed` 私有字段与初值保持不变，`test_main.py` / `test_versions.py` 版本门禁同步更新。
- 切换范围：Agent、Voice Core Media Bridge（`memoria-agent:20260820-010500-livekit-stack-upgrade`）、Media Edge（`memoria-media-edge:20260820-010500-livekit-stack-upgrade`）、自建 LiveKit Server（`/opt/livekit`，`v1.13.5`）、H5（`livekit-client 2.22.0`）。
- Control API、Speaker Model、两个兼容 Gateway、数据库、Redis、MinIO、Nginx、ESP32 固件、小程序不随本次切换。

## 本地门禁

- Ruff、strict MyPy（400 个源码文件）、模块预算通过。
- Agent unit 全量通过；全量非 Postgres 套件 0 失败（Postgres 依赖测试与同名测试模块收集冲突为既有本地环境问题，已在干净 HEAD 复现确认与本次升级无关）。
- H5 `372` 项测试与 production build 通过；Go `go build ./...` 与 `go test ./...` 通过。

## 生产切流与证据

- 待切流后回填：容器状态、注册探针、LiveKit smoke、证据目录与 SHA-256。

## 验收边界

- 本 release 目标层级为 `code / wired / enabled / production runtime verified`（以切流后证据为准）。
- 不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`。
