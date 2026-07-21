# 项目交接

## 当前生产

- 唯一客户端是 H5，入口为 <https://122.51.108.140:8443/>（也可用 <https://aigcnice.com:8443/>）；runtime 当前版为 `20260720-225534`，H5 保持 `20260720-214800`；仓库已移除原生 iOS 客户端源码。
- 旧域名 `aginice.cn` 仍解析到旧服务器 `110.42.235.198`，其 API 为 502 且证书不匹配；不要再把它作为 Memoria 入口。用户从旧 origin 切到新入口后需点“登录已有账号”，浏览器本地 token 不会跨 origin 迁移，服务端账号与历史不受影响。
- 固定级联主链：FunASR Realtime + 百炼 Qwen + CosyVoice 3.5；不向用户暴露 Omni 路径。
- 三个应用容器 `agent / control-api / speaker-model` healthy；新服务器 `/health/ready` 为 200，绑定 release `20260720-225534`、provider `qwen`，9 项 core check 全部 ready。
- 独立同机数据栈为 PostgreSQL 17 + pgvector 0.8.1 与 MinIO；不复用其他项目卷。32 张表全部 FORCE RLS，MinIO 两个 bucket 均启用版本控制。
- H5 直接回滚点为 `20260720-182535`；runtime 直接回滚点为 `20260720-214800`。新服务器旧 release、镜像和 root-only 发布前备份已复核存在；旧服务器保持停止作为回滚点。

## 本轮发布

- 新服务器 `122.51.108.140` 已接管 8443：Memoria H5/API/LiveKit、PostgreSQL、MinIO 与 speaker-model 均切换完成；既有 WMS 的 `/wms/` 与 443 入口保留。
- runtime `20260720-225534` 已上线，H5 不换版；生产 `COSYVOICE_VOLUME` 从 70 原子调整为 45，旧 Agent env 与 SQLite 发布前快照均保留两份 root-only 回滚副本，快照完整性和外键检查通过。
- 新版上线门禁通过：三个应用容器 healthy；LiveKit、FunASR、Qwen、CosyVoice 3.5 smoke 与 readiness 全部 PASS；公网 `/`、`/memoria-api/health/live`、`/memoria-api/health/ready`、`/wms/` 均 200，ready release 为 `20260720-225534`。
- 发布后同文直接 PCM A/B：volume 45 为 peak `-1.0028 dBFS`、削顶 `0/172320`；volume 70 为 peak `-0.0003 dBFS`、削顶 `1405/172320`（`0.815344%`）。运行容器实际 env/config 均为 45，启动日志无 warning/error/traceback。
- 上一版 `20260720-214800` 已按 runtime 先、H5 后顺序上线；其镜像归档、源码与 H5 工件均完成 SHA-256 校验，继续作为 runtime 直接回滚点和当前 H5 版本。
- H5 index 与本机构建 SHA-256 一致；历史 immutable 懒加载分片、5 个伙伴位图和试听音频均经公网返回 200。浏览器注册页 network idle、无 console error/warning、无横向溢出。
- 新机历史 release 联合后，又从旧服务器补齐 `DigitalSelfPanel-De2mgjPN.js`、`LockKey.es-C8KNGFU3.js`、`PrivacyDataPanel-tZIfzuNt.js` 三个旧标签页动态分片；三者经新入口均返回 200。
- 迁移记录：[`docs/releases/20260720-140053.md`](docs/releases/20260720-140053.md)；onboarding 热修：[`docs/releases/20260720-145953.md`](docs/releases/20260720-145953.md)；账号注销 UI：[`docs/releases/20260720-162553.md`](docs/releases/20260720-162553.md)；LiveKit 删除幂等修复：[`docs/releases/20260720-164942.md`](docs/releases/20260720-164942.md)；PCM 声纹录音修复：[`docs/releases/20260720-174545.md`](docs/releases/20260720-174545.md)；重复登记与音色跳变修复：[`docs/releases/20260720-182535.md`](docs/releases/20260720-182535.md)；音质与播放期让话修复：[`docs/releases/20260720-225534.md`](docs/releases/20260720-225534.md)。
- H5 注册与吉祥物 onboarding 已发布；匿名身份同 `user_id` 升级与跨账号 Profile 隔离保持原语义。
- 完整门禁通过：Ruff、format check、strict mypy、Python 全仓 `585 tests collected` 且 exit code 0；H5 沿用上一版已通过的 `121/121` 与 production build。
- 当前 runtime 的本机 `linux/amd64` 镜像归档 SHA-256 为 `4a4b90da96d50886069295010f8fea4ae9b21c37d616fc15b5aadaa659c75b91`；服务器只执行 `docker load` 与 `compose up --no-build`。
- SQLite、PostgreSQL、MinIO、Agent spool 与 root-only 配置已迁移；32/32 表 FORCE RLS，两个 MinIO bucket 已启用版本控制。
- H5 `index / JS / CSS` 公网哈希与本机构建一致；生产浏览器无横向溢出、破图或 console warning/error。

## 本轮产品变更（已发布）

- H5 吉祥物 v2 已改为正面形象：`apps/h5/design/mascot-v2/` 保留 5 张 1254×1254 设计源图，运行时使用正面 3D 位图机身、内联 SVG 四种表情（平静、开心、好奇、关切）及 CSS 眨眼、呼吸、说话嘴形和胸灯动效。
- 表情只消费当前会话、当前已接受用户终稿对应的 `emotion_observation`；旁人、回声、被拒绝话轮、跨会话和迟到旧事件不会驱动表情。负面声学标签统一映射为关切，不用助手字幕关键词推测情绪。
- 新注册用户先通过滑卡选择星澜、桃喜、绵绵、阿序或玄墨，可预览性格、四种表情与对应设计音色；确认后录制三段声纹。声纹只建立 shadow 档案，成功前不进入首页。既有注册用户迁移为星澜，新用户保持空选择进入 onboarding。
- 声纹 onboarding 的现场录音直接采集 16 kHz 单声道 PCM，绕过 Safari `MediaRecorder` 容器解码兼容性；当前不做提示语 ASR 逐字一致性校验，只检查时长、清晰度和声学质量。
- 五个设计音色映射为 `warm_companion / bright_peer / soft_confidante / calm_guide / low_magnetic`；运行时优先级为已激活克隆音色、所选伙伴设计音色、全局默认音色。Control API 只返回目录键，真实供应商 `voice_id` 由 Agent 本地批准 registry 解析。
- H5 onboarding 完成后不再启动遗留的逐会话声纹登记；声音档案在后台刷新期间保持当前伙伴音色，并在下一次 TTS 前完成同步，避免首句后回退到全局基线。生产 Agent `/data` 已改为 UID `65532` 可写。
- 首次欢迎语改为固定单流 CosyVoice 句子，不再对没有真实 user message 的会话调用 Qwen；避免 DashScope 以末条 role 非法拒绝请求而造成“点击机器人后无声”。
- `TargetSpeakerFocus` 现在区分普通聊天与播放期打断：正式 `guest` 仍可被拦截，未经 FAR/FRR 校准的 shadow `guest/ambiguous` 只阻止播放期抢话，普通聊天 fail-open 且仍保持 `uncertain` 的私人记忆/长期写入/敏感操作权限，避免把主人整段静音。
- CosyVoice 3.5 生产音量从 70 降为 45，并移除表达计划将低音量强制抬回 70 的行为；`203120` 已上线的原始 PCM 连续透传保持不变，避免把 WebSocket 分片边界当作音频增益边界。
- 播放期的明确“等一下/等等”等让话意图现在跨 ASR partial/final 近音修订在当前 VAD epoch 内保持，完整端点音频完成目标说话人聚焦后停止播放并简短让话；“等一下+正文”直接进入回答。该窄豁免不授予私人记忆、长期写入或敏感动作权限。
- “我的”页已增加“注销账号”：密码与精确短语二次确认后复用全账户删除治理，成功即清空登录态、实时字幕、回顾与本地缓存并返回注册页；迟到的旧账号任务不能回写新账号。线上一次性账号真实注销后旧登录返回 401，同用户名重新注册由服务端回归锁定为空 Profile/历史并重新进入 onboarding。
- LiveKit 已自然消失的历史房间现在按幂等成功处理，不再阻塞账号删除；真实失败请求在热修部署后由 worker 自动续跑完成。SQLite 账号数据、PostgreSQL 32 张业务表及 MinIO 两个 bucket 均复核无残留，旧用户名登录返回 401。
- 最终门禁：H5 沿用上一版 `121/121` 与 production build；Python 全仓 `585 tests collected` 且 exit code 0，Ruff、format check 与 strict mypy 通过。内置浏览器已覆盖 390×844、375×667、667×375、五个角色、四种表情、WAV 试听、44px 触控、步骤滚动复位、账号注销与 reduced-motion；无横向滚动或 console warning/error。真实手机声纹录音链路、主观听感与动画观感仍需设备验收。
- 真实手机麦克风、声纹录制和全双工主观听感仍需设备验收；内置浏览器不提供麦克风能力，不能以浏览器检查替代。

## 已交付能力

- P0.5：用户名/密码注册登录、稳定 `user_id`、匿名身份原地升级和跨账户隔离。
- P1-P3：追加式证据账本、加密 spool、人物/关系/时间线/人生知识、审核、全文与 pgvector 检索、迁移和恢复。
- P4-P5：证据化 Persona、实时胶囊、CosyVoice 3.5 声音授权、登记 saga、盲测、激活、撤销和供应商补偿。
- P2：`owner / guest / uncertain` 权限、固定 CAM++ ONNX 模型服务、shadow/版本 readiness、声纹治理和私人记忆硬门禁。
- P6：私人记忆实时接入、声音降级与时间戳门禁、导出、删除 fence、tombstone、联合恢复和原始语音独立授权。

## 关键边界

- CAM++ 没有 anti-spoof，当前声纹仍是 shadow-only；不能宣传为已完成生产主人识别。
- 生产未用授权真人样本完成 FAR/FRR/EER、unknown rejection、回放攻击和 CosyVoice 真人盲测，不能自动激活正式声纹模板或复刻声音。
- PostgreSQL、WAL archive、MinIO 与备份目前都在同一台服务器；虽有版本控制和恢复演练，但没有异地副本/KMS/PITR，不能承诺“永不丢失”。
- 真实手机的耳机、扬声器、噪声、重叠、回放和弱网全双工矩阵仍未完成。

## 下一步

1. 用至少 200 条明确授权真人录音完成声纹和抽取/检索指标报告。
2. 完成 CosyVoice 3.5 正式 enrollment、人格 A/B、声音相似度与自然度盲测。
3. 增加异地对象副本、独立备份目标与 PITR，再做跨环境联合恢复演练。
4. 用真实手机完成 H5 全双工听感、权限污染和弱网设备矩阵；原生 iOS 客户端已从仓库移除。
