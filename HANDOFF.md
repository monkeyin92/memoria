# 项目交接

## 当前状态

- 当前 source commit / annotated tag：
  `a8d953f3c9a333659400f84653f75d3325797fa3 / 20260727-235959`。
- 生产 runtime：`20260727-235959`，已完成原子切换、生产门禁和清理后复验。
- 生产 H5：`20260723-192611`，本轮 runtime 发布没有切换 H5。
- 微信小程序体验版：`0.8.53`，上传成功，包体 `609,479` 字节；
  未提交审核或正式发布。
- 当前交付客户端为 `apps/h5` 与 `apps/miniprogram`；legacy Web 与原生 iOS 源码已移除。
- 历史路线、架构决策和发布证据分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与
  `docs/releases/20260727-235959.md`。

## 最新实现

- `UtteranceRouter` 仍是 enroll、纯打断、打断后继续提问和普通聊天的唯一副作用入口。
- 小程序改为受控话轮：AI 处于 thinking、tool waiting、speaking、recovering 等响应状态时
  暂停 `RecorderManager` 上行；Agent 回到 listening 后仍等待本地 pending PCM 和全部
  WebAudio source 结束，才恢复录音。
- 用户主动静音优先，回答结束不会擅自重新开麦；小程序“轻触打断”和本地
  `interruptPlayback` 路径已删除，播放期口头控制词不会停止回答或进入下一轮。
- Gateway 无论 AEC 是否可用都标记小程序平台；Agent 对该平台关闭 LiveKit interruption、
  KWS、歧义语意复核和播放期转写接纳，迟到终稿按原 speech epoch 丢弃。
- H5 的 `barge_in_enabled` 默认保持开启；Cascade `UtteranceRouter` 与 Omni 备用
  transport 均覆盖“等等、等一下、停一下、先别说”等控制语意。
- Vosk KWS 实现和宿主机模型仅为直接回滚版本及未来独立 RTC PoC 保留；
  当前小程序会话不会构造或运行 KWS。
- Gateway 新增精确 session、默认 5 秒、最大 15 秒的 AEC 前后 WAV 采样；默认关闭，
  文件/目录权限为 `0600/0700`，只记录 session hash 和音频参数。
- 小程序媒体契约由 `packages/contracts/miniprogram-media.json` 同时约束 Python 与
  JavaScript，固定下行 `24 kHz / mono / s16le / 20 ms / 960 bytes`。
- Socket 断开立即停止录音；mic 关闭压过迟到的 `RecorderManager.onStart`；同步发送失败
  不提交 sequence。
- 播放器使用固定帧校验、首批 lead、短 gain ramp 和 generation/reset barrier。
- 系统录音中断结束后发送 `uplink_discontinuity(next_sequence)`，同一 WSS 会话重置
  半帧、sequence 与 AEC 时序后恢复。
- Agent、H5 与小程序只消费带有效 `session_id`、generation 与权威来源的会话事件。
- 同一 `speech_epoch` 的打断确认语最多播放一次；迟到 ASR 终稿和下一次 VAD
  不再重复发布确认音频。
- 小程序回传首播、underflow、hard reset 和 conceal 的有界数值遥测；
  Gateway 白名单校验并限速，不接受文本、音频、token 或 cookie。
- 播放 underflow 只重建后续排程，不再硬停仍登记中的旧 source。
- FunASR 已支持可选热词表 ID 和噪声阈值，但生产没有录音校准证据，当前保持未配置。

## 生产健康

- `agent / control-api / speaker-model / miniprogram-gateway` 四容器均为
  `healthy`、restart 0。
- readiness 为 `ready / 20260727-235959`；9/9 core checks、Agent heartbeat、
  LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过。
- 四个 runtime 容器最近十五分钟未出现 traceback、关键 provider、provenance、
  archive durable/spool 或连接拒绝错误。
- 公网根 H5、兼容 H5、SPA、API live/ready 与 WMS 为 200；
  `/memoria-api/internal/` 为 404。
- 小程序公网 WSS 成功升级，缺少 ticket 的 hello 按协议关闭为 `4401`。
- Nginx 配置、WMS、readiness timer 与 certbot timer 正常；IP 证书有效至
  `2026-08-03 01:40:00 UTC`，域名证书有效至 `2026-10-18 03:59:59 UTC`。
- PostgreSQL、MinIO、LiveKit、WMS、数据库快照与 Docker 数据卷未在清理中修改。

## 2026-07-28：小程序 `connection refused` 诊断与待上传修复

- 真机报错发生在 `wx.connectSocket` 的 TCP/TLS 建连之前；不属于 FunASR、Agent、ticket 或
  播放器路径。开发者工具对当前生产
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 已完成
  `open → hello → ready`；生产网关 healthy，公网 WSS Upgrade 也返回 `101`。
- 当前网络出口直连 `8443` 的 TLS/WSS 成功。尝试把媒体路径迁移到 443 后，服务端实际发送
  TLS ServerHello/证书，而同一出口客户端立即 RST；该路线不能解决此设备网络，已完全回撤。
  生产 Control API 仍下发 `wss://aigcnice.com:8443/...`，Nginx active、runtime readiness 为
  `ready`。候选 443 Nginx 文件只保留在 root-only 运维备份中，未对外生效。
- 工作区待上传的小程序修复：仅当原始 SocketTask 错误为 `connection refused` 时，关闭旧
  socket、刷新同一 session 的短期 ticket 后自动重连一次；其余域名、证书、超时和鉴权错误不
  重试。第二次仍失败时提示关闭 VPN/代理或切换 Wi-Fi/移动网络。媒体回调按当前实例 fence，
  防止旧 socket 的迟到 close 覆盖新连接。
- 已完成定向测试与语法检查；尚未上传新的体验版。真机验收仍需在同一手机分别关闭 VPN/代理、
  切换 Wi-Fi/移动网络后复测；若仍失败，复现同时抓取 8443 的 SYN/TLS 包，区分设备侧拒绝、
  运营商路径和主机来源规则。

## 保留版本与回滚

- 当前 runtime：`20260727-235959`。
- 直接回滚 runtime：`20260727-221555`。
- 固定 H5：`20260723-192611`。
- 本机和生产均只保留当前与直接回滚两套 runtime 的四角色 Docker tag。
- 生产 source release 只保留 `20260727-235959` 与 `20260727-221555`；
  H5 release 只保留 `20260723-192611`。
- 回滚证据：
  `/var/backups/memoria/runtime-switch-20260727-235959-from-20260727-221555-20260727-235959/`。
- SQLite 快照 SHA-256：
  `b9a77c6df5d97e6234795b7a08a0a145c079378822d2aa4aca347b5e25ea1ec4`。
- PostgreSQL custom dump SHA-256：
  `29fc666cfd9ea75219939dd5f170bd5e6b0147be53fbf1e22e12c8a6be19338a`。
- runtime 回滚不自动恢复数据库；只有数据迁移或数据异常时才使用快照。

## 2026-07-27 清理结果

- 删除非交付 `apps/web`、`infra/Dockerfile.web` 与对应 pnpm workspace 文件，
  约减少 `5,183` 行 tracked 内容。
- 前序阶段已删除三个干净临时 worktree、已合并 hotfix 分支、旧发布工件、构建缓存、
  五套旧 source/runtime/image 和非交付客户端；累计回收约 `32 GiB`。
- 本次发布后删除服务器 `2.2 GiB` incoming、未激活 H5 候选、全部过时临时候选 env、
  旧 runtime/source/image `20260727-204609` 和三套更老 runtime-switch 备份。
- 服务器只保留当前、直接回滚与最新切换备份；根分区约 `28 GiB` 已用、
  `86 GiB` 可用，使用率 `25%`。
- 本机删除本轮临时 worktree、约 `2.1 GiB` 发布工件、测试/构建缓存和旧
  `20260727-204609` 四角色镜像标签；Data 卷约 `288 GiB` 可用。
- 本机和生产各删除五套旧 runtime 的 20 个 Memoria Docker tag。
- 没有运行 `docker system prune -a`；没有删除卷、数据库、其他项目镜像或跨项目构建缓存。

## 验证

- Ruff：通过。
- `mypy services --strict`：160 个 source files 无问题。
- Python：`1310 passed, 27 skipped`。
- H5：`236/236`，production build 通过。
- 微信小程序：`48/48`，全部 JavaScript syntax check 和 JSON 配置检查通过。
- `scripts/run_e2e.py --profile offline`：通过。
- Agent Linux/amd64 镜像以 `--require-hashes` 成功构建；`vosk==0.3.45` 和控制词文件
  均进入镜像，官方模型通过宿主机只读挂载。
- `vosk-model-small-cn-0.22` 官方模型页标记 Apache-2.0；归档 SHA-256 为
  `3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba`。
  成品 Linux/amd64 Agent 镜像实测“停一下”命中、“等一下我想问……”拒绝。
  模型未进入仓库、镜像或发布包；生产安装路径为
  `/var/lib/memoria-agent/models/vosk-model-small-cn-0.22`，目录/文件权限为
  `0750/0640`，owner/group 为 `65532:65532`。
- 候选 env、commit-bound manifest/verifier、镜像导入、候选 H5/API/SQLite restart
  server smoke、生产 Provider/readiness、公网 H5/API/WMS/TLS/WSS 均通过。
- 本次工件 SHA-256、镜像 ID、备份和生产验收见
  `docs/releases/20260727-235959.md`。
- 关键覆盖率子门槛：Agent orchestration `92%`、provider protocols `92%`。
- 既有全 `services` 覆盖率门槛仍未闭环：实测 `81.54%`，低于 CI 配置的 `85%`；
  本轮没有降低门槛或伪报通过。

## 未闭环与下一步

1. 上传 `connection refused` 单次重连候选体验版，并在 iOS/Android 真机验证：先关闭
   VPN/代理，再分别使用 Wi-Fi 与移动网络；同时验证 AI 思考/播放期上行 PCM 为零、本地尾音
   结束后自动恢复、不吞首字、手动静音优先，以及弱网/前后台/系统录音中断恢复。
2. H5 仍需真实浏览器和设备验证“等等、等一下、停一下、先别说”等语意打断，
   同时覆盖“我等一下再说”等非打断语句，避免误触发。
3. 继续观察小程序 underflow、hard reset、lead 指标；只有需要定位非播放期噪声时才为
   单一 session 开启有界 AEC pre/post 采样，测试后立即关闭。
4. 单独处理全仓覆盖率门槛：优先补齐 PostgreSQL/外部边界测试，不通过降低标准换绿。

## 用户工作区边界

以下未跟踪内容属于用户，必须保留：

- `.workbuddy/`
- `apps/miniprogram/assets/bg/aurora-light.webp`
- `apps/miniprogram/assets/mascot-alpha.webp`
- `apps/miniprogram/design-preview/`
