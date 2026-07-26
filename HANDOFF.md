# 项目交接

## 当前目标

- 收尾仓库、发布工件与 Docker 清理。
- 修复并验收微信小程序在 AI 播放期间说“等一下 / 等下”无法打断的问题。
- 当前不扩展新功能；历史路线、架构决策和发布过程分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与 `docs/releases/`。

## 当前代码

- 当前生产 source commit / annotated tag：
  `7edd751500a14290d24f6cb799a07e426877121c / 20260726-215154`。
- `a0308a4` 在统一 `UtteranceRouter` 中把规范化后完全等于“等下”的文本识别为纯打断，
  同时保持“我等下再说 / 等下我想问……”为普通聊天。
- 本版本在共享回声门禁中补充“助手说等一下 / ASR 缩成等下”的同义拒绝，防止助手回声
  自己触发纯打断。不要把临时 DEBUG 取证重新带入后续候选。

## 当前生产

- runtime：`20260726-215154`，于 2026-07-26 22:21:13 CST 原子切换。
- H5：`20260723-192611`。
- 小程序体验版：`0.8.49`。
- `agent / control-api / speaker-model / miniprogram-gateway` 四个容器均为
  `healthy`，readiness 为 `ready`，Agent 与 9/9 core checks 正常。
- 固定语音链路：
  `小程序 PCM → MiniProgramMediaGateway APM → LiveKit → FunASR → Qwen → Doubao → 小程序`。
- PostgreSQL、MinIO、LiveKit 与 WMS 未在本轮清理或诊断中修改。

## 保留版本与回滚

- 当前版本：`20260726-215154`。
- 直接回滚：`20260726-133033`；`20260726-181813` 保留为 Router 修复证据版本。
- 次级回滚：`20260726-111550`。
- H5 固定保留：`20260723-192611`。
- 上述四套 runtime 的四角色镜像和对应 release/artifact 已保留；其他旧 runtime tag、
  明确过时 release/incoming 和诊断候选 `20260726-193953` 已删除。
- runtime 回滚不自动恢复数据库。详细边界见：
  - `docs/releases/20260726-111550.md`
  - `docs/releases/20260726-133033.md`
  - `docs/releases/20260726-181813.md`

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
  - 本版本全量 Python：`1236 passed, 27 skipped`。

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

## 清理结果

- 当前保留主工作树与 `memoria-interrupt-echo-guard` hotfix worktree；真机验收后再清理后者。
- 临时 tag `20260726-193953`、对应本地 artifact、镜像和服务器候选均已删除。
- 本机 Memoria runtime 镜像保留 4 个 runtime 版本的四角色 tag，共 16 个。
- 生产 Memoria runtime 镜像同样保留 16 个 tag；此前清理释放约
  `42.13 GB / 39.24 GiB`，根分区使用率由 67% 降至 32%，可用约 77 GB。
- Python、pytest、mypy、ruff、npm、前端 build cache 和无引用派生音频已清理。
- H5 删除 12 个零引用旧角色/声音资源、12 张已被最终证据替代的中间 QA 截图，以及
  本地 ignored `_opaque_backup`；`232/232` 测试和 production build 通过。
- 已删除零调用且默认检查旧 `sites-enabled/echolife` 的
  `scripts/validate_nginx_memoria.sh`；生产 systemd、crontab 与常用运维目录均无引用。
- 保留当前诊断所需：
  - `/private/tmp/memoria-artifacts-20260726-133033`
  - `/private/tmp/memoria-artifacts-20260726-181813`
  - `/private/tmp/run_memoria_wss_diag.py`
  - `/private/tmp/memoria-story.pcm`
  - `/private/tmp/memoria-wait.pcm`
  - `/private/tmp/memoria-last-downlink-24k.pcm`
- 未运行 `docker system prune -a`；未删除卷、数据库、WMS 或其他项目镜像。

## 生产取证

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

## 未闭环与下一步

1. 用户在真实手机外放状态、AI 播放约 0.6 秒后说“等一下”和“等下”。
2. 同一会话确认：
   `trusted_unanchored_interrupt_cmd → explicit_interrupt_cmd → interrupted → audio_reset`，
   并以实际扬声器立即停播为最终结果。
3. 任一信号缺失，先导出候选容器日志再回滚到 `20260726-133033`；不要继续放宽 Router
   或把 raw PCM 混回 trusted AEC 通道。
4. 若真机仍失败，在网关 APM seam 增加短 onset 回归：先做时间对齐，再断言
   correlation、SI-SDR、voiced-ms 与纯回声衰减。真人样本只从外部只读路径加载，
   不进仓库、不复制、不发送外部 ASR；采集或使用前先取得用户确认。

## 用户工作区边界

- 保留且不要擅自删除：
  - `.workbuddy/`
  - `apps/miniprogram/assets/bg/aurora-light.webp`
  - `apps/miniprogram/assets/mascot-alpha.webp`
  - `apps/miniprogram/design-preview/`
