# 项目交接

## 当前状态

- KWS/AEC 诊断 source commit / annotated tag：
  `d2d35ba52181e18068d8df074f3e515f406b4710 / 20260727-204609`。
- 生产 runtime：`20260727-204609`，于 `2026-07-27 21:07:40 CST` 开始切换，
  `21:10:43 CST` 完成全部生产门禁。
- 生产 H5：`20260723-192611`，本轮 runtime 发布没有切换 H5。
- 微信小程序体验版：`0.8.51`，于 `2026-07-27 17:48:27 CST` 上传，
  包体 `607,643` 字节；本轮没有客户端变更，因此没有重复上传、提交审核或正式发布。
- 当前交付客户端为 `apps/h5` 与 `apps/miniprogram`；legacy Web 与原生 iOS 源码已移除。
- 历史路线、架构决策和发布证据分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与
  `docs/releases/20260727-204609.md`。

## 最新实现

- `UtteranceRouter` 仍是 enroll、纯打断、打断后继续提问和普通聊天的唯一副作用入口。
- Agent 新增可选 Vosk 受限词表识别：只消费可信小程序 AEC 后 PCM，仅在助手播放期、
  VAD speech epoch 和至少 80 ms PCM witness 下运行；VAD 结束后完整结果必须精确等于
  纯控制词、不含 `[unk]` 且达到平均置信度门槛，命中仍经过 Router、
  TargetSpeakerFocus、playback epoch 与 generation fence。
- partial 不执行停止；Linux/amd64 实测证明“等一下我想问……”和“别说了这个词……”
  都会先出现纯控制词 partial，必须等完整话轮才能保住后续用户内容。
- 已固定真机失败回归：FunASR final 错成“他。”但 KWS 命中“停一下”时，generation
  只前移一次，旧播放停止，错误 final 不进入 chat/history。
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

## 生产健康

- `agent / control-api / speaker-model / miniprogram-gateway` 四容器均为
  `healthy`、restart 0。
- readiness 为 `ready / 20260727-204609`；9/9 core checks、Agent heartbeat、
  LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过。
- 生产 Agent one-off 使用正式 env 与宿主机只读模型成功构造
  `VoskKeywordSpotter`；四个 runtime 容器最近十分钟未出现 traceback、
  `keyword spotter unavailable` 或 `keyword_spotter_ready=error`。
- 公网根 H5、兼容 H5、SPA、API live/ready 与 WMS 为 200；
  `/memoria-api/internal/` 为 404。
- 小程序公网 WSS 成功升级，缺少 ticket 的 hello 按协议关闭为 `4401`。
- Nginx 配置、WMS、readiness timer 与 certbot timer 正常；IP 证书有效至
  `2026-08-03 01:40:00 UTC`，域名证书有效至 `2026-10-18 03:59:59 UTC`。
- PostgreSQL、MinIO、LiveKit、WMS、数据库快照与 Docker 数据卷未在清理中修改。

## 保留版本与回滚

- 当前 runtime：`20260727-204609`。
- 直接回滚 runtime：`20260727-170628`。
- 固定 H5：`20260723-192611`。
- 本机和生产均只保留当前与直接回滚两套 runtime 的四角色 Docker tag。
- 生产 source release 只保留 `20260727-204609` 与 `20260727-170628`；
  H5 release 只保留 `20260723-192611`。
- 回滚证据：
  `/var/backups/memoria/runtime-switch-20260727-204609-from-20260727-170628-20260727-210538/`。
- SQLite 快照 SHA-256：
  `3ffd8c2e199ee2b829f72b6b2d2165e8e6e5e9f6e291bee3dad06802fd7db72b`。
- PostgreSQL custom dump SHA-256：
  `17e46eae072bb185844549d774e37b722726878e58a7bdd3456bdcf3471ff5e3`。
- runtime 回滚不自动恢复数据库；只有数据迁移或数据异常时才使用快照。

## 2026-07-27 清理结果

- 删除非交付 `apps/web`、`infra/Dockerfile.web` 与对应 pnpm workspace 文件，
  约减少 `5,183` 行 tracked 内容。
- 删除三个干净临时 worktree、已合并 hotfix 分支、所有本机 Memoria 临时
  release/artifact、旧构建产物与测试缓存；文件系统回收约 `15.4 GiB`。
- 生产删除全部已导入 incoming 包、五套旧 source release、非当前 H5 候选、旧 artifact
  backup、旧 runtime-switch 目录和退出的 MinIO 初始化容器；文件系统回收约 `16.9 GiB`。
- 生产根分区约 `28 GiB` 已用、`86 GiB` 可用，使用率 `25%`。
- 本机和生产各删除五套旧 runtime 的 20 个 Memoria Docker tag。
- 本次发布后进一步删除服务器 `2.2 GiB` incoming、未激活的 `204609` H5 候选、
  临时候选 env、旧 runtime `120448` 及其四个镜像 tag，并移除退出的 MinIO provision
  容器；服务器根分区约 `31 GiB` 已用、`83 GiB` 可用。
- 本机删除 `204609` 临时 worktree、约 `2.1 GiB` 发布工件、`120448` 四个旧镜像 tag
  与 KWS smoke tag；Data 卷约 `294 GiB` 可用。
- 没有运行 `docker system prune -a`；没有删除卷、数据库、其他项目镜像或跨项目构建缓存。

## 验证

- Ruff：通过。
- `mypy services --strict`：160 个 source files 无问题。
- Python：`1295 passed, 27 skipped`。
- H5：`232/232`，production build 通过。
- 微信小程序：`46/46`，全部 JavaScript syntax check 通过。
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
- 关键覆盖率子门槛：Agent orchestration `92%`、provider protocols `92%`。
- 既有全 `services` 覆盖率门槛仍未闭环：实测 `81.54%`，低于 CI 配置的 `85%`；
  本轮没有降低门槛或伪报通过。

## 未闭环与下一步

1. 当前自动化与生产模型加载不能替代 iOS/Android 外放、听筒、蓝牙、系统录音中断和
   弱网下的真机声学验收，不能宣称高质量全双工已闭环。
2. 下一次真机测试前先取得目标 session ID，只为该 session 开启
   `MINIPROGRAM_GATEWAY_AEC_CAPTURE_SESSION_ID`，测试后立即导出 pre/post WAV 到
   root-only 备份并关闭开关。
3. 重点验证“等一下”“停一下”、控制词后跟内容、噪声/助手原声误触发、访客声音、
   迟到结果和下一段播放。
4. 单独处理全仓覆盖率门槛：优先补齐 PostgreSQL/外部边界测试，不通过降低标准换绿。

## 用户工作区边界

以下未跟踪内容属于用户，必须保留：

- `.workbuddy/`
- `apps/miniprogram/assets/bg/aurora-light.webp`
- `apps/miniprogram/assets/mascot-alpha.webp`
- `apps/miniprogram/design-preview/`
