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

- 执行顺序：先升级 `/opt/livekit` LiveKit Server `v1.13.3 -> v1.13.5`（compose/livekit.yaml 已备份至 `/var/backups/memoria/*pre-20260820-livekit-v1.13.5`，`logging.level: warn` 保持），再切流 `agent / voice-core-media-bridge / media-edge`，最后发布 H5。传输工件 `images.tar.gz` SHA-256 `916c06fd0359c9e8e42e60ff1b4420f79436329202d5b82591c9ee4fae1ea108`，服务器校验一致后 `docker load`。
- 切流后三个目标容器均 healthy、restart count=0；Agent 在新 LiveKit Server 上重新注册 worker `AW_2p7oFEKqQmE7`；心跳代际保持 `MEMORIA_RELEASE_TAG` agent=`20260814-231749-direct-canary`、bridge=`20260816-bridge-liveness-83af813`，Control API 心跳 `status=ready`、`worker_ready=true`、`livekit_ready=true`。
- 候选 Agent 容器内 Provider smoke 全通过（Qwen realtime-search、Doubao、FunASR×6、DeepSeek、InterruptSemantic，exit_code=0）；LiveKit smoke `PASS: authenticated room-service access`。
- H5：候选目录 `/var/www/memoria-releases/20260820-010500-livekit-stack-upgrade`，immutable union 262 个资源、无 collision；回滚目录 `rollback-20260820-010500-livekit-stack-upgrade`（旧入口 `20260808-171749` + 候选 union）manifest SHA-256 `2921c4ff7bd0ada7db658fab3aa437dc41e9de7d841d706b7148488ce3fa5311`；软链原子切换后 `/memoria-h5/`、SPA 路由、live、ready 均 200。
- readiness 说明：切流前 smokes 已 expired，根因是既有 canary tag 漂移（`/opt/memoria/current/.env` 为 `20260812-173008`，Control API 实际运行 tag `20260814-231749-direct-canary`），`refresh_readiness.sh` 持续以旧 tag 标记被 409 拒绝；本次以运行 tag 覆盖在 Control API 容器内重新标记，ready 恢复 200。该漂移为既有运维问题，不属于本次升级引入。
- 证据目录 `/opt/memoria/direct-canaries/20260820-010500-livekit-stack-upgrade/`：`CUTOVER_RESULT.txt` `9afe0bdd7ad769854d25e3b8f82b8e4ccc673ecbe74d198e68cf2c4557181a71`、`POST_CUTOVER_STATE.txt` `67b92f7f450024ab31014fb7c2bf9b9f4268047bda466fcbd7ecacc161934e3f`、`PROVIDER_SMOKE_RESULT.txt` `c192007ccbaca74af3e84c014123c6a9a832db47b0332b006ef0ce2b1e3a0c54`、`LIVEKIT_UPGRADE_RESULT.txt` `2307e1d82c29b4e703b65b3b07b3d90b1d5b0f83ce9b7d535dc9cace250546e0`、`H5_DEPLOY_RESULT.txt` `99fcff0302c36a2091c5257c9819ede9e5be89f1f039ffdd2e33e467b75c7a30`；`EVIDENCE_MANIFEST.sha256` `55f888d100bc154d63001ed2f546754634cf91afe35133ff2a188fd31c51c5c7`，14/14 条目校验通过。回滚镜像 tag `rollback-20260820-010500-livekit-stack-upgrade-pre-agent/-bridge/-edge` 已冻结。
- 制品清理：删除三个已取代的 incoming 上传包（约 4.3 GB）、16 个旧 agent 镜像 tag、2 个旧 media-edge tag 与 1 个旧 control-api tag；仅保留当前运行版本与紧邻可运行回滚版本（agent `20260820` + rollback 指向 `20260819-185500` 同 ID，media-edge `20260820` + rollback 指向 `20260818-103228` 同 ID，livekit `v1.13.5` + `v1.13.3` 回滚点）；H5 release 目录按 immutable 追加式保留策略不清理。根盘由 47% 降至 38%。

## 验收边界

- 本 release 目标层级为 `code / wired / enabled / production runtime verified`（以切流后证据为准）。
- 不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`。
