# 项目交接

## 当前目标

- 收尾仓库、发布工件与 Docker 清理。
- 修复并验收微信小程序打断后重复上一问题、缺确认音和话轮等待偏长的问题。
- 当前不扩展新功能；历史路线、架构决策和发布过程分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与 `docs/releases/`。

## 当前代码

- 2026-07-27 已发布 runtime `20260727-120448`：可信小程序 AEC barge-in 首事件改为
  `assistant_audio duck/gain=0.0`；歧义 sticky `interrupt_then_chat` final 仅调用
  `qwen-flash` 做严格三态复核，`UtteranceRouter` 仍是唯一副作用策略入口。
- 新复核输入只含 final、首个 sticky 文本和 barge-in 冻结助手文本；结果绑定 speech epoch、
  playback epoch 和 generation fence。分类与 SpeakerAuthority 并行，超时/异常/非法输出
  fail closed 并提示重说；确认后的纯控制仍保留原回答供“继续”。
- 本地门禁已通过：Python `1269 passed, 27 skipped`，Ruff、strict mypy、
  H5 `232/232`、小程序 `32/32` 与 `git diff --check`。本机没有
  `DASHSCOPE_API_KEY`；真实 `qwen-flash` Provider smoke 已在候选生产 Agent 容器通过五个严格样本。
- source commit / annotated tag：
  `db07f8a11fb08ee9282a4530ebf2394d50280104 / 20260727-120448`。
- 当前版本在统一 `UtteranceRouter` 中识别打断后旧话轮重放，并把自建 endpointing
  目标统一为 `0.90 / 1.50 / 1.70`。
- 本轮媒体改造开始前，`main` 与 `origin/main` 基线均为 `489800a`；本轮 source 变化
  尚未提交、推送或部署。
- `a0308a4` 在统一 `UtteranceRouter` 中把规范化后完全等于“等下”的文本识别为纯打断，
  同时保持“我等下再说 / 等下我想问……”为普通聊天。
- 本版本在共享回声门禁中补充“助手说等一下 / ASR 缩成等下”的同义拒绝，防止助手回声
  自己触发纯打断。不要把临时 DEBUG 取证重新带入后续候选。

## 2026-07-27 本地架构与小程序媒体改造（未发布）

- Agent `transcript_delta` 已补齐 `session_id`；共享 Schema、H5、legacy Web 和对应
  回归测试均按真实权威载荷对齐，避免 H5/旧 Web 严格解析丢弃终稿或跨会话混入。
- 新增 `packages/contracts/miniprogram-media.json`：Python/JavaScript 共用 PCM
  golden frames、固定 `20 ms / 960 bytes` 下行契约和控制字段；小程序测试、JS 语法检查
  已加入 CI。
- Control API 的小程序 URL/ticket/响应模型已从大 `session.py` 收口到
  `services/control_api/app/miniprogram_gateway_session.py`，创建与刷新路径、权限、JWT
  claims 和不泄露 LiveKit token 的行为保持不变。
- 小程序媒体会话现在在 Socket 断开时立即停录并终止本地上传；mic 关闭压过迟到
  `RecorderManager.onStart`；同步发送失败不提交 sequence；播放器硬校验固定 PCM 帧，
  首批使用 lead，gain duck/restore 使用 10 ms ramp。
- 系统录音中断在支持 `onInterruptionEnd` 时同一 WSS 会话恢复：发送
  `uplink_discontinuity(next_sequence)`，网关清空半帧并重置 AEC 时序；不支持该回调时
  仍 fail closed，提示用户手动恢复。网关把 `audio_reset` 作为不可丢失 barrier，匹配当前
  generation 的 `playout_interrupt` 只抑制对应 reverse reference，且丢弃错误长度下行帧。
- 共享媒体契约已由 JavaScript/Python 测试共同约束 hello、ready、控制事件与 PCM
  golden frames；系统中断期间 mic 切换受 `systemInterrupted` fence 约束，静音状态不会
  提前发送 discontinuity 或伪报 resumed。
- 本地门禁：Python `1282 passed, 27 skipped`，Ruff、strict mypy、H5 `232/232` 与
  production build、legacy Web `28/28`、lint/build、小程序 `46/46`、JS syntax、
  `run_e2e --profile offline` 通过；Provider smoke 因本机缺少 `DASHSCOPE_API_KEY`、
  `DOUBAO_TTS_AUTH` 仅 SKIP。
- 上述 source 变化尚未部署 runtime、H5 或重新上传小程序体验版；当前已上传的
  `0.8.50` 不包含本轮改造。未做真实手机声学验收，不能据此宣称“全双工已完成”。

## 当前生产

- runtime：`20260727-120448`，于 2026-07-27 12:23:59 CST 原子激活。
- H5：`20260723-192611`。
- 小程序体验版 `0.8.50` 已于 2026-07-27 15:16:19 CST 通过 `wechatide upload`
  上传成功，包体 `603,490` 字节；未提交审核或正式发布。
- 本机微信开发者工具已更新为 Nightly `2.02.2607252`，agent 侧
  `wechatide-skill` 已从工具内置版本单向同步至 `0.3.4`；`Codex` CLI 授权有效且
  `tokenRequired=false`。
- `agent / control-api / speaker-model / miniprogram-gateway` 四个容器均为
  `healthy`、restart 0；readiness 为 `ready / 20260727-120448`，Agent、
  9/9 core checks、LiveKit、FunASR、Qwen、Doubao 与 `InterruptSemantic` 正常。
- Agent 文件 env、容器 env 和运行时配置均为 `0.90 / 1.50 / 1.70`。
- 新增 Agent env 已精确生效：
  `INTERRUPT_SEMANTIC_ENABLED=true / INTERRUPT_SEMANTIC_MODEL=qwen-flash /
  INTERRUPT_SEMANTIC_TIMEOUT_S=0.6`。
- 固定语音链路：
  `小程序 PCM → MiniProgramMediaGateway APM → LiveKit → FunASR → Qwen → Doubao → 小程序`。
- PostgreSQL、MinIO、LiveKit 与 WMS 未在本轮清理或诊断中修改。
- 服务器和 Provider 基础门禁已通过；真实手机外放打断、确认音和慢语速拆轮仍待用户终验。

## 保留版本与回滚

- 当前版本：`20260727-120448`。
- 直接回滚：`20260726-233337`，必须同时恢复本轮备份的四份 env。
- 次级回滚：`20260726-215154`；`20260726-133033`、`20260726-181813` 保留为 Router 修复证据版本，
  `20260726-111550` 保留为更早稳定点。
- H5 固定保留：`20260723-192611`。
- 本机与生产均保留上述五套 runtime 的四角色镜像，共 20 个 tag；服务器正式 release
  与下列本地诊断 artifact 按回滚边界保留。其他旧 runtime tag、明确过时
  release/incoming 和诊断候选 `20260726-193953` 已删除。
- runtime 回滚不自动恢复数据库。详细边界见：
  - `docs/releases/20260726-111550.md`
  - `docs/releases/20260726-133033.md`
  - `docs/releases/20260726-181813.md`
  - `docs/releases/20260726-215154.md`
  - `docs/releases/20260726-233337.md`

## “等一下”已确认的根因与修复

### 控制面

- 生产旧会话曾把“等一下”识别为“等下。”。
- `65d4498` 下该文本进入 `chat`，无 VAD 的小程序 AEC 窄通道不能触发 interrupt。
- `a0308a4` 下“等下。”进入 `interrupt_command`；“等一下”保持原行为，interim/final
  去重后只打断一次，纯控制词不进入 chat。
- `20260726-215154` 进一步拒绝助手正文中的“等一下 / 等下”被 ASR 缩写成“等下”后
  绕过短文本回声保护；助手未说该词时，主人“等下”仍走原纯打断路径。
- 旧红新绿：
  - `65d4498`：目标用例 `1 passed, 2 failed`。
  - `a0308a4`：打断相关 `112 passed`。
  - `20260726-215154` 全量 Python：`1236 passed, 27 skipped`。

### 声学链路

- Router 修复是必要条件，但真实手机整链尚未验收。
- 旧公网 WSS 脚本给网关提供 far-end reverse reference，却向麦克风注入不含物理回声的
  合成近端音频，不符合真实免提双讲。APM 会错误削弱短词首部，FunASR 曾得到“一下。”
  或“你想？”，错位混入模拟回声时还会合并助手残音。
- 当前生产下行捕获在 0.6 秒注入的离线 APM 重放仍能稳定暴露短词 onset 失真；能量或
  voiced 时长可能通过，但词形相关性和 SI-SDR 已明显下降。因此不能只用 RMS/voiced
  断言，也不能把该合成脚本当成真机通过证据。
- 正确的代码测试 seam 是
  `MiniProgramAudioProcessor.observe_downlink/process_uplink`；Router/mock FunASR
  只能覆盖控制面，不能证明 APM 后词形仍可识别。

## 本次真机新增结论

- 会话 `c67bc334-6ba9-428c-a76c-7bc67e86427d` 没有旧 generation 复活或双 TTS；
  gen4 被打断后，相同“你叫什么名字？”再次作为用户话轮提交并创建 gen6。
- sticky `interrupt_then_chat` 遇到只重放上一问题的 endpoint final 时，旧实现会继续
  进入 chat，且 route 没有确认音。当前版本以 `interrupt_replay` 控制意图收口。
- `TurnDetector v1-mini` 负责语义 EOU，不负责 stop/chat 路由。Git 历史没有 LLM
  intent classifier；当前版本没有新增停词，而是在统一 Router 中使用 speech epoch 上下文。
- 真机多次 `end_of_turn_delay=2.200s`，生产旧 env 为 `1.50 / 2.20 / 1.70`；
  代码、模板、生成器和发布精确值门禁现统一为 `0.90 / 1.50 / 1.70`。
- 本地门禁：定向 `188 passed`，Python 全量 `1248 passed, 27 skipped`，Ruff、
  strict mypy 与 `git diff --check` 通过。

## 清理结果

- 当前保留主工作树与 `memoria-interrupt-echo-guard` hotfix worktree；真机验收后再清理后者。
- 临时 tag `20260726-193953`、对应本地 artifact、镜像和服务器候选均已删除。
- 本机与生产 Memoria runtime 镜像均保留 5 个 runtime 版本的四角色 tag，共 20 个；
  真机通过后再删除不再需要的旧版本。
- 此前清理释放约 `42.13 GB / 39.24 GiB`；当前生产根分区使用率 35%，可用约 74 GB。
- Python、pytest、mypy、ruff、npm、前端 build cache 和无引用派生音频已清理。
- H5 删除 12 个零引用旧角色/声音资源、12 张已被最终证据替代的中间 QA 截图，以及
  本地 ignored `_opaque_backup`；`232/232` 测试和 production build 通过。
- 已删除零调用且默认检查旧 `sites-enabled/echolife` 的
  `scripts/validate_nginx_memoria.sh`；生产 systemd、crontab 与常用运维目录均无引用。
- 保留当前诊断所需：
  - `/private/tmp/memoria-artifacts-20260726-133033`
  - `/private/tmp/memoria-artifacts-20260726-181813`
  - `/private/tmp/memoria-artifacts-20260726-215154`
  - `/private/tmp/memoria-artifacts-20260726-233337`
  - `/private/tmp/run_memoria_wss_diag.py`
  - `/private/tmp/memoria-story.pcm`
  - `/private/tmp/memoria-wait.pcm`
  - `/private/tmp/memoria-last-downlink-24k.pcm`
- 未运行 `docker system prune -a`；未删除卷、数据库、WMS 或其他项目镜像。

## 生产取证

- 本轮 root-only 证据目录：
  `/var/backups/memoria/runtime-switch-20260727-120448-from-20260726-233337-20260727-122225/`。
  SQLite 双副本 SHA-256 一致、PostgreSQL custom dump 可由 `pg_restore --list` 读取，四份
  env、前后状态、容器脱敏日志与激活时间均为 `0600`；完整 env/inspect 快照不得复制到普通
  文档、聊天或 issue。
- 本地 source/image/H5 manifest 三件套、服务器二次 verifier、隔离 server smoke、LiveKit、
  FunASR、Qwen、Doubao、`qwen-flash` 五样本 smoke、readiness 与公网 H5/API/WMS 均通过。
- 本轮两次切换共用 root-only 证据目录：
  `/var/backups/memoria/runtime-switch-20260726-233337-from-20260726-215154-20260726-235734/`。
- 第一次候选服务已通过 Provider/readiness，但发布侧 JSON 断言读到空 stdin；
  trap 于 2026-07-26 16:01:22 UTC 自动恢复 `20260726-215154` 和四份旧 env。
- 修正断言后于 2026-07-26 16:03:34 UTC 第二次激活；状态快照确认 runtime 为
  `20260726-233337`、H5 为 `20260723-192611`。
- `rollback.txt`、三份 `*.failed.log`、`activated-at.txt`、`state.after.txt`、
  SQLite/PostgreSQL 备份及哈希均保留在上述目录。完整 env/inspect 快照属于敏感凭据，
  不得复制到普通文档、聊天或 issue。
- 本次切换状态、四份 root-only env 与回滚记录：
  `/var/backups/memoria/runtime-switch-20260726-215154-from-20260726-133033-20260726-221931/`
- 本次从 `20260726-133033` 切换到 `20260726-181813` 的回滚状态与四份 root-only env：
  `/var/backups/memoria/runtime-switch-20260726-181813-from-20260726-133033-20260726-201520/`
- root-only 脱敏日志：
  - `/var/backups/memoria/20260726-181813-wss-49f1c94b-*.log`
  - `/var/backups/memoria/20260726-193953-wss-9ca3d2ea-*.log`
  - `/var/backups/memoria/20260726-181813-wss-af0c31fb-*.log`
- 测试账号均已永久删除。
- 回滚会删除旧 Compose 容器及其 `json-file` 日志。再次真机测试时，必须在回滚前导出
  Agent/Gateway 脱敏日志，否则会再次丢失目标会话证据。
- 本轮只读复核曾触达含完整容器 env 的 root-only 状态快照；稳妥起见，后续需安排
  capability/provider/LiveKit 凭据轮换，并把未来状态快照改成脱敏摘要。

## 未闭环与下一步

1. 在新体验版完全退出并重新打开后，AI 播放约 0.6 秒时依次测试：“等一下”、“停一下，你叫什么名字？”、
   引用助手原话的追问、纯噪声/误触发和分类超时恢复。
2. 验收 0–700 ms 内静音、纯控制只确认一次且不进 chat、真实问题不丢失、“继续”恢复原回答，
   并补测正常短句、长句、慢语速和 1–2 秒句中停顿。
3. 任一结果失败，先冻结同一会话的 Agent/Gateway/Control 脱敏日志，再恢复
   `20260726-233337` 及本轮四份旧 env。
4. 真机通过后清理 hotfix worktree、多余旧镜像和不再需要的本地 artifacts；不运行
   `docker system prune -a`，不删除卷或其他项目镜像。

## 用户工作区边界

- 保留且不要擅自删除：
  - `.workbuddy/`
  - `apps/miniprogram/assets/bg/aurora-light.webp`
  - `apps/miniprogram/assets/mascot-alpha.webp`
  - `apps/miniprogram/design-preview/`
