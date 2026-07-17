# 项目交接

## 目标

交付 Memoria H5：动态情绪吉祥物、LiveKit 实时语音、每日 Qwen 回顾和个人资料，并部署为 `aginice.cn` 的根应用。

## 当前状态

- 正式 runtime release 与 H5 release 均为 `20260717-123551`。
- 当前公网 H5：`https://aginice.cn:8443/`；Control API：`https://aginice.cn:8443/memoria-api/`。兼容入口 `https://aginice.cn:8443/memoria-h5/` 与公网 IP 路径继续可用。
- `/opt/memoria/current` 指向 `releases/20260717-123551`；Control API 与 Agent 两个 Linux/AMD64 容器均为 healthy。
- `/var/www/memoria-h5` 指向 `memoria-releases/20260717-123551`；H5 已在 runtime、Provider 与 readiness 门禁通过后最后原子切换。
- 生产默认链路为自建 LiveKit Server `1.13.3`、FunASR Realtime、百炼 Qwen 和 CosyVoice Realtime；每日回顾 `source=qwen`。
- H5 三页、四种动态情绪、实时会话、停止回答、声音解锁、静音保持、重连恢复、每日回顾和个人资料均已完成。
- H5 使用服务端签发的匿名 Bearer 身份。已保存身份先通过 `/v1/auth/me` 校验；只有 401/403 才换发，网络故障不清除现有身份。
- 消息、个人资料、Agent 上下文和 FunASR 上下文在持久化或发送上游前统一做 PII 脱敏；所有用户数据与会话控制均校验所有权。
- SQLite 持久化消息、回顾、Profile、三个偏好、会话控制和 readiness evidence；同 release 重启后数据与 readiness 保持。
- `aginice.cn` 使用 TrustAsia 域名证书，SAN 为 `aginice.cn`、`www.aginice.cn`，有效至 2026-09-04；公网 IP 兼容入口使用 Let's Encrypt 短期证书，有效至 2026-07-22。snap Certbot timer 负责 IP 证书续期，deploy hook 只在 `nginx -t` 成功后 reload。
- Nginx 已在 443 与 8443 的域名根路径交付 Memoria。8443 由 stream 层复用：TLS 转到本机 `9443` 的 HTTPS server，非 TLS ICE/TCP 转到本机 `8444` 的 LiveKit listener。公网 443 的域名 SNI 仍被上游关闭，因此当前正式入口继续使用 8443。
- 2026-07-16 10:18 CST 已停止 PocketSparks 的 4 个常驻容器并将 restart policy 设为 `no`；`goods-invoice.service` 已停止并 disable。两项目数据、目录、镜像和卷全部保留，原公网路径已撤为 404。
- 2026-07-16 已停止并禁用 `wms.service` 与 `mysql.service`；数据仍保留在 `/opt/wms` 与 `/var/lib/mysql`。
- 自建 LiveKit 位于 `/opt/livekit`，Compose project 为 `memoria-livekit`；浏览器经 `wss://aginice.cn:8443` 信令并用 8443 RTC/TCP 回退，Agent 与 LiveKit 共用 `memoria_default` 网络并走内部 UDP。生产日志级别为 `warn`，避免 INFO 级别信令内容进入日志。
- 自建档案使用 `min_words=0`、FunASR 550ms、endpoint 1.50s、interruption 450ms、误打断恢复 1.70s；思考/播放期临时把 LiveKit `min_words` 提高到 1000，只有新的 speaking epoch 和有效 start/stop anchor 才重开提交，带 metrics 但 anchor 全空的迟到 FINAL fail-closed。1.50s 端点窗口先保持不变，收集干净 timing 样本后按 P95/P99 逐步下调。附和、异常脚本、播放期及播放后助手回声与噪声候选恢复播放；15 秒内最多确认两次快速打断。用户字幕只在话轮门禁通过后发布 final，助手每轮使用一个连续 CosyVoice 流；`7882/UDP` 公网策略未改。
- 输入守卫按 `backchannel`、`non_target_language`、`assistant_echo`、`feedback_circuit_open` 做会话内分类计数并写结构化日志；生产没有启动或对外暴露 Prometheus endpoint。
- Product Design QA 见 `apps/h5/design-qa.md`；当前发布证据见 `docs/releases/20260717-123551.md`，上一自然口语基线见 `docs/releases/20260717-113441.md`，完整 A/B 证据见 `docs/releases/20260717-003211.md` 与 `docs/releases/20260717-001826.md`。

## 已发布的双工、情绪与 Omni A/B（2026-07-16）

- 已按研究顺序完成首声 trace、duck-first 候选打断、独立 listener cue、FunASR + Qwen3-ASR 情绪旁路、受控 CosyVoice 情绪输出，以及 Qwen3.5-Omni 隔离 A/B。
- H5 新增“现有级联 / Qwen3.5-Omni-Flash”选择：默认级联、刷新后保持选择，连接中和通话中禁止切换。未新增 MiniCPM 或 MiniMax。
- Omni 媒体由 H5 直接通过 WebRTC 与百炼交换；Control API 只代理 SDP 和服务端鉴权，浏览器永远拿不到 `DASHSCOPE_API_KEY`。Omni 会话不签发 LiveKit token，也不派发级联 Agent。
- Omni 固定使用 `qwen3.5-omni-flash-realtime`、`semantic_vad`；麦克风在收到 `session.updated` 前关闭，支持 RTP 音频、字幕、静音、停止、结束和迟到事件隔离。助手输出按 `response_id + item_id` 隔离；用户转写按 `speech_started` 的 `item_id` 绑定 `turn_id/generation_id`，未知项丢弃，A 的迟到 completed 不会覆盖或结束 B。
- Control API 对 Omni SDP 交换校验 Bearer、会话所有者、后端类型、`application/sdp`、64 KiB 上限和每会话两次交换；百炼北京 Workspace 主机及模型由服务端固定，禁用重定向与环境代理，上游错误体不回传客户端。
- 上述变更已随 runtime 与 H5 `20260716-225754` 部署。现有级联仍是首次进入默认项；Omni 仅作为用户主动选择的隔离 A/B。

## 真人对话稳定性优化（2026-07-17）

- H5 release `20260717-001826` 与最终 runtime 候选 `20260717-003211` 修复上轮实测中的三类确定性问题：级联迟到 FINAL 拆轮、Omni 不主动开场、重复远端音轨或迟到 response 导致的重复播放/抢话。
- Omni 在首次 `session.updated` 后只发送一次欢迎 `response.create`；用户开口时取消待创建或正在播放的 response，用户说话期间迟到的 `response.created` 也会立即发 `response.cancel`。
- Transport 按远端 track/stream ID 去重；H5 复用唯一 `<audio>`，断开时清理 `srcObject` 和元素引用。
- 两条链路每 5 秒采集一次脱敏 inbound RTP 数值：`jitter`、收发包、丢包、concealment 与 jitter-buffer delay。Omni 通过所有权校验的 Control API 入口只记录允许的事件名和数值，拒绝文本、音频、SDP、任意字段与凭据。
- 级联在每次 turn commit 前记录 LiveKit 的 speaking、transcription 与 end-of-turn timing，用于判断迟到 FINAL，并为后续降低 1.50s 等待提供依据。
- 08:15 CST 复测中 Omni 4 次真人 speaking 均只对应 1 次 response，stop 到 created 为 68–139ms；级联仅 2 次真人 speaking 却提交 6 轮，其中 4 轮的 speaking/timing anchor 全空，证明是迟到 FINAL 重复话轮，不是 LLM 自主连续生成。
- `20260717-084049` 在 `DuplexVoiceAgent.on_user_turn_completed()` 的真实提交 seam 加入新 speaking epoch 与 start/stop anchor 门禁，思考/播放期同时使用 `min_words=1000` 做预防门禁。FunASR 仅对同 task、同 `sentence_id` 的重复 FINAL 去重，不跨不同 sentence ID 合并合法分句。
- 用户 final 字幕移到门禁通过后发布；两条链路新增总采样数、静音 concealment、jitter-buffer emitted count 及加减速采样数，后续应使用比例/平均值而非累计值判断音质。

## 真人感 DeliveryPlan（2026-07-17）

- `20260717-111241` 先收紧两条链路的 Prompt：思考式开场只用于多步骤、查询或真实推理，直接问题不添加填充词；轻松安全场景允许一次短笑，严肃场景禁止笑和咳嗽。
- `20260717-113441` 将级联从单一 Emotion 映射升级为当前话轮 `delivery_mode`：`direct / deliberative / light_laughter / supportive`。该决策不持久化，日志不记录用户正文。
- “安排十五分钟口语训练”选 `deliberative`：同一 generation 内先说一次 4–12 字短衔接，CosyVoice 整轮速率从 0.98 小幅降到 0.95；“今天星期几”仍为 `direct/0.98`。
- 级联只有在主转写有笑声、Qwen3-ASR 当前声学标签为 `happy` 且文本无严肃信号时才选 `light_laughter/happy`；事故、诈骗、医疗、负向情绪或求助场景优先 `supportive`，禁止笑、咳嗽和轻佻填充词。
- Omni 加入上述两类请求的对比示例，以及“能自然发笑才笑，不能就直接回应，不把‘哈哈’逐字念出来”和非匀速朗读约束；仍明确属于 Prompt 软控制。
- Preamble 与正文保持同一个 LLM generation、同一个 CosyVoice 双向流，不拆成两次回答，不重新引入“说完后又接着说”的取消与衔接风险。

## 音质与 Omni 生命周期修复（2026-07-17）

- `20260717-123551` 将级联 LiveKit Opus 上限提高到 64 kbps，保持 CosyVoice 与 RoomIO 为 24 kHz/mono、endpoint 为 1.50 秒；运行容器已读回该配置。它是码率上限而不是实际发送码率保证。
- Omni 在 `session.updated` 后等待 400 ms 安静观察窗；用户先开口就抑制欢迎，不向未知 pending response 发送 cancel。pending、active、cancelled 分离，同一 ID 最多 cancel 一次，迟到 done 不覆盖新状态。
- 正常 Omni response 完成后启用 500 ms 实验性反馈保护，抑制已观测到的 `response.done` 后 8–10 ms 扬声器尾音反馈轮；该窗口可能吞掉用户立即接话开头，仍需真机验证后再决定是否保留或改为更精确的 AEC/能量门控。
- 用户开口或 response cancel 时只静音唯一 Omni `<audio>`，MediaStream 继续消费；新合法 response 创建后再恢复，防止旧 RTP 缓冲稍后冒出。
- 浏览器支持时设置 `RTCRtpReceiver.jitterBufferTarget=120 ms`，不支持则无损回退；遥测新增逐 response 快照、concealment 比例、平均 jitter delay 与编码码率估算。
- 全网官方资料结论是继续保留 Qwen3.5 Omni 主链。腾讯 FlowTTS 仅列为未来级联 TTS A/B 候选；百度 Pro、豆包 O2.0/SC2.0 只做独立 Spike；讯飞 SuperTTS 先离线试听。本轮未新增供应商。

## 会话就绪协议

- LiveKit transport 连接成功不等于 Agent 可用。H5 初次会话只接受当前 session、当前 generation、Agent participant 在 `voice-agent.ui` topic 发布的显式 `assistant_state: ready`。
- 收到首次 `ready` 前，其他状态和字幕不驱动 UI；45 秒仍未收到 `ready` 时断开房间、清理 session，并返回可重试状态。
- Agent 完成 session、音频输出和 UI publisher 绑定后，先发布并等待 `assistant_state: ready`，再调用 `generate_reply` 生成首次欢迎语。因此欢迎语不会先于首次 ready 门禁。

## 最近正式发布

`20260717-123551` runtime/H5 按以下顺序完成：

1. 用生产 timing 与 WebRTC stats 定位级联低码率 Opus 伪影、Omni 尾音反馈轮和首轮 concealment，实施 64 kbps、欢迎状态机、反馈保护、播放门控与增强遥测。
2. 本地 260 项 Python、61 项 H5、28 项 Web 测试与 Ruff、mypy strict、两个 production build 全部通过。
3. 验证双份 SQLite 快照、root-only 环境备份和 Linux/AMD64 固定 tag 镜像，先原子切换 runtime。
4. LiveKit、FunASR、Qwen、CosyVoice 实网 smoke 全部 PASS，readiness 绑定 `20260717-123551 / qwen`，两容器 healthy 后最后原子切换 H5。
5. 公网 H5/SPA/API、三份核心资源哈希、三页 UI、模型切换刷新保持、恢复级联和 0 console warning/error 全部通过；新容器错误关键词为 0。
6. 直接回滚点为 runtime/H5 `20260717-113441`；首词、滋滋声、500 ms 后立即接话和真人感留给用户同设备 A/B。

`20260717-113441` runtime/H5 按以下顺序完成：

1. 为级联增加当前话轮 DeliveryPlan 与一次性 LLM 指令；Omni 增加思考开场、直接回答、笑声与韵律的对比示例。
2. 本地 257 项 Python、218 项 Agent、57 项 H5、28 项 Web 测试与 Ruff、mypy strict、两个 production build 全部通过。
3. 验证双份 SQLite 快照、root-only 环境/Compose 备份和 Linux/AMD64 固定 tag 镜像，先原子切换 runtime。
4. LiveKit、FunASR、Qwen、CosyVoice 实网 smoke 全部 PASS，readiness 绑定 `20260717-113441 / qwen`，两容器 healthy 后最后原子切换 H5。
5. 公网 H5/SPA/API、三份核心资源哈希、Omni 选择刷新保持、切回级联和 0 console warning/error 全部通过；新容器错误关键词为 0。
6. 直接回滚点为 runtime/H5 `20260717-111241`；新 DeliveryPlan 的真实笑声与拖音听感留给用户同设备 A/B。

`20260717-111241` runtime/H5 收紧了条件式思考开场、笑声安全语境、CosyVoice 合法情绪/语速与 Qwen3-ASR 多轮声学证据；完整证据见对应 release manifest。

`20260717-084049` runtime/H5 按以下顺序完成：

1. 用真人会话 timing 证据定位级联孤儿 FINAL，实施 speech anchor、pre-EOU 锁、窄范围 FunASR 去重和字幕门禁；Omni 默认回复收紧为一到两句。
2. 本地 243 项 Python、204 项 Agent、57 项 H5、28 项 Web 测试与 Ruff、mypy strict、两个 production build 全部通过。
3. 验证两份 SQLite 快照、root-only 环境/LiveKit 备份和 Linux/AMD64 固定 tag 镜像，然后原子切换 runtime。
4. LiveKit、FunASR、Qwen、CosyVoice 实网 smoke 全部 PASS，readiness 绑定 `20260717-084049 / qwen`，两容器 healthy 后最后原子切换 H5。
5. LiveKit 生产日志收紧为 `warn`；公网 H5/SPA/API 状态、资源哈希、模型切换持久化和 0 console warning/error 验收通过。
6. 直接回滚点为 runtime `20260717-003211` 与 H5 `20260717-001826`；新版同 iPhone 真人首声、抢话、级联单轮性与音质留给用户 A/B 复测。

`20260717-003211` runtime 与 `20260717-001826` H5 按以下顺序完成：

1. 固化级联 1.50s 端点稳定窗口、Omni 单次主动欢迎、抢话取消、音轨/音频元素去重与双链路 WebRTC 音质采样。
2. 创建并验证两轮双份 SQLite 快照和 root-only 环境备份；构建固定 tag、Linux/AMD64 镜像。
3. 两次 runtime 候选均完成 LiveKit、FunASR、Qwen、CosyVoice 实网门禁；最终 readiness 绑定 `20260717-003211 / qwen`。
4. 浏览器真实 Omni 会话主动欢迎只创建一次，远端 `<audio>` 始终为 1 且实际播放；级联欢迎语与音频同样正常，两种模式 console warning/error 为 0。
5. 验收发现并修复 telemetry INFO 被根日志级别过滤的问题；最终日志包含欢迎、response 生命周期、音轨和 inbound RTP 数值，Control API/Agent 错误计数均为 0。
6. 公网根 H5、兼容入口、SPA、API live/ready 与 EchoLife 均为 200，internal 为 404；本次未 reload Nginx，配置检查通过。

`20260716-225754` 按以下顺序完成：

1. 暂存完整 runtime 与 H5，逐文件 checksum 校验一致，公网软链保持旧版。
2. 创建并验证两份 SQLite 快照，同时备份 `/etc/memoria.env` 到 root-only 目录。
3. 构建固定 tag、Linux/AMD64 的 Control API 与 Agent 镜像。
4. 原子切换 runtime，确认两个容器 healthy；LiveKit、FunASR、Qwen、CosyVoice smoke 全部 PASS，readiness 绑定 `20260716-225754 / qwen`。
5. 通过生产公网完成真实 Omni SDP：鉴权、创建会话、SDP 均为 200，WebRTC connected，收到远端音轨与 `session.updated`。
6. `nginx -t` 通过后最后原子切换 H5；根页面、兼容入口、SPA、API、EchoLife 与公网 IP 路径验收通过。Nginx、LiveKit、端口与防火墙均未切换。

上一 runtime 回声守卫修复为 `20260716-171958`；被审计拒绝的 `20260716-163957` 保持回滚状态。

## 主要交付文件

- `apps/h5/`：移动 H5、动态吉祥物、匿名身份、LiveKit 会话、测试与视觉 QA。
- `services/control_api/`：身份、会话、消息、回顾、Profile、readiness 与 SQLite 持久化 API。
- `services/common/redaction.py`：服务端统一 PII 脱敏。
- `services/agent/`：LiveKit Agent、FunASR/Qwen/CosyVoice 生产链路与 Provider 门禁。
- `docker-compose.production.yml`、`infra/Dockerfile.*`：固定 release tag 的生产 runtime。
- `infra/nginx-aginice-server.conf`、`infra/nginx-memoria-*.conf`：根域名、域名/公网 IP TLS、H5/API 独立路径、限流和 internal 路由隔离。
- `infra/livekit-compose.production.yml`、`infra/livekit.production.yaml.example`：独立 LiveKit 服务、内部共享网络与 RTC 端口。
- `infra/certbot-memoria-deploy-hook.sh`：续期后先检查 Nginx 再 reload。
- `scripts/refresh_readiness.sh`、`infra/memoria-readiness-refresh.*`：绑定 release/provider 的 readiness 证据与 12 小时刷新。
- `docs/production-deployment.md`：发布、证书、验收、备份和回滚 runbook。

## 关键决策

- 当前正式 H5 入口为 `https://aginice.cn:8443/`；`/memoria-h5/` 继续承载静态资源与兼容访问，`/memoria-api/` 继续承载 Control API。Control API 上游只监听 `127.0.0.1:8791`。
- 443 直接由 HTTPS server 处理；8443 先由 Nginx stream 区分 TLS 与原生 ICE/TCP，再分别转发到 HTTPS 与 LiveKit。80 根路径 308 跳转到 8443。EchoLife API 路由保留，PocketSparks 和 Goods Invoice 路由已移除。
- 浏览器只接收匿名访问 token 和短期 LiveKit participant token；任何永久 Provider、LiveKit 或应用认证 secret 都不进入 H5 bundle。
- Control API 固定单 worker，SQLite 固定写入 `/var/lib/memoria/memoria.sqlite3`。
- 用户字幕只展示并持久化 final；助手字幕继续按实际播放进度流式展示。
- H5 与 runtime 使用独立版本目录和原子软链；镜像必须使用非空 release tag，禁止用 `latest` 作为回滚锚点。
- Provider smoke 必须完整 PASS；`SKIP`、只验证变量存在或单独 readiness HTTP 200 都不算生产门禁通过。
- LiveKit production turn 禁用 preemptive generation，保证 turn commit 提升 fence 后才启动本轮 LLM；这是“用户说话后停在 thinking”的根因修复。
- 自建档案基础 `min_words=0`；AI 播报期间临时封锁 LiveKit 原生提交，候选输入由应用守卫二次判定。守卫确认“停”“停一下”“等等”“不是”等真实短打断后立即恢复为 0，再交给 LiveKit EOT 提交。
- `THINKING` 中收到真实新 VAD 可进入 `USER_SPEAKING`。本轮先用 1.50s pre-EOU 窗口吸收已观测的迟到 final；更低延迟的同一物理语音轮次合并仍需 Provider 到 EOU 的 speech epoch，不用脆弱文本去重替代。
- 现有级联继续作为默认与生产主线；Qwen3.5-Omni 仅作为隔离 A/B，不自动继承或宣称等价于级联的 GenerationFence、实际已听文本和工具隔离。
- Omni 助手字幕统一为 `heard=false`，不能凭模型生成或字幕完成事件冒充“用户已经听到”，因此当前不会作为正式助手消息持久化。
- Omni 只接受已由 `speech_started` 登记的用户转写 `item_id`；`speech_stopped` 与 completed 只有命中当前活动项时才能结束用户说话态，防止交错迟到事件放行抢答。
- 情绪观察量只在当前会话短期有效，不写长期画像；输出只允许受控情绪子集，负向情绪不直接镜像，策略异常回退 `neutral`。
- DeliveryPlan 只在当前 generation 生效；严肃语境优先于笑声和思考式开场。级联不使用 SSML 或第二次 TTS 模拟停顿，Omni 不把 Prompt 软行为写成确定性能力。

## 已验证

- 后端：Ruff、mypy strict 与 260 项完整 pytest 全部通过；迟到空 anchor FINAL、重复 speech anchor、FunASR 去重、实际已听历史对齐、笑声证据与四类 DeliveryPlan 均有确定性回归。
- H5：61 项测试全部通过；Omni 欢迎观察窗、pending/active/cancelled 状态、尾音反馈抑制、迟到 response 隔离、唯一音频元素和增强 WebRTC stats 回归均通过，production build 通过。
- production build 的 `index.html`、主 JS、主 CSS 与服务器版本目录、公网响应三方 SHA-256 完全一致。
- 自建 LiveKit RoomService、FunASR、Qwen 和 CosyVoice 实网 smoke 全部通过。
- 匿名 Bearer、缺少认证、跨用户隔离、PII 入库前脱敏、会话所有权和持久化均有自动化回归覆盖。
- `20260716-150805` 无调试镜像曾连续 3 次完成“欢迎语 → 你好 → 助手回复”实网 E2E，3/3 PASS；用户与助手消息按顺序成对落库。
- 上述旧版三次 E2E 对应 `stale llm token dropped=0`、`IllegalStateTransition=0`、Provider error/failed/timeout `=0`；`20260716-171958` 的生产异常 trace 已由确定性自动化回归覆盖，真实语音效果等待用户复测。
- 公网 H5 与兼容入口为 200；API live/ready 为 200，ready 返回 release `20260717-123551` 和 provider `qwen`；本次 `nginx -t` 通过，未 reload Nginx。
- Nginx 配置检查成功。线上继续使用既有通用 `/memoria-api/` 代理；仓库新增的 `/v1/sessions/` 专用限流块未安装，真实生产 SDP 已通过现网代理。
- 公网证书校验成功，SHA-256 指纹及完整工件证据记录在 release manifest。
- 发布前 SQLite 原始快照保留在 `/var/lib/memoria/memoria-pre-20260717-123551.sqlite3`；保护副本位于 `/var/backups/memoria/memoria-pre-20260717-123551.sqlite3`。
- `/var/backups/memoria` 为 `root:root 0700`，数据库与环境备份为 `root:root 0600`；两份 SQLite SHA-256 均为 `3745e46505fb16d3c1f78e569ed526d24e64b7fd6f4d9daf817adc89cf492c9b`，`integrity_check=ok`、foreign-key violations 为 0。
- 源站域名根路径在 443/8443 均返回 Memoria 200；公网 8443 的 `/`、`/memoria-h5/`、API ready、EchoLife health 均为 200，PocketSparks 与 Goods Invoice 原路径为 404。公网 443 的 SNI 握手仍被上游关闭。
- PocketSparks 无运行容器，4 个常驻容器 restart policy 均为 `no`；Goods Invoice 为 inactive/disabled，8788/18080/19000/15432 监听均消失。
- `wms.service` 与 `mysql.service` 均为 inactive/disabled；LiveKit、Agent 与 Control API 容器运行正常，服务器可用内存约 2.2 GiB。
- 真实浏览器经公网 8443/TCP 建立 RTC：浏览器 participant `connectionType=tcp`、356 ms active；Agent `connectionType=udp`、219 ms active。真实中文语音完成 STT → Qwen → CosyVoice，助手字幕出现，远端音频元素为 `readyState=4`、持续播放。
- `20260716-141337` 公网 TCP 回归：欢迎语播放中注入单字“嗯”未创建第二轮；随后“你好。”只产生 `turn_id=1 / generation_id=1` 的一次回答，`late_stt=0`、`playback_restart=0`、Provider failure=0。
- 内置浏览器通过 8443 完成 390×720 首页、回顾、我的三页及实时语音验收。
- 生产 H5 首次进入默认级联，可切换到 Qwen3.5-Omni-Flash，通话中禁用切换；连接、结束、恢复级联选择均正常，console 无 warning/error；验收通话已结束，测试页面保留在级联选择供用户复测。
- `20260717-123551` 内置浏览器验证了首页、回顾、我的三页、两个模型选项、Omni 选择刷新保持、切回级联和 console warning/error 为 0；本轮未获取浏览器麦克风授权，未宣称新版已完成真人听感验收。
- 生产 DashScope Omni WebRTC 信令已通过：Workspace 环境变量未设置，匿名鉴权、创建会话和 SDP 均为 200，WebRTC connected，收到远端音轨、`session.created` 与 `session.updated`。
- 系统播音被浏览器 AEC 作为回声抑制，未形成真人转写；Omni 真人语音、助手音频、情绪、附和、打断与成本 A/B 仍需同设备人工验收。级联轻笑是安全门禁后的同一回答文本 + `happy` 音色近似，不等同于确定性非语言笑声；咳嗽不实施。

## 非阻塞风险与后续运维

- 公网 IP 证书是短期证书。需监控 snap Certbot timer、续期结果和 deploy hook；任何 reload 都必须以 `nginx -t` 成功为前提。
- PocketSparks、Goods Invoice、WMS 与 MySQL 仅停用、未删除。恢复前先确认端口、内存和精确路由；不得删除既有命名卷、`/opt/goods-invoice`、`/opt/wms` 或 `/var/lib/mysql`。
- 聊天渠道曾用于传递生产凭据；交付后应轮换 LiveKit、百炼、服务器登录和应用认证凭据，且继续只在服务器 secret 文件或 secret manager 中保存。
- 本机被 `.gitignore` 排除的 `apps/ios/apps/ios/build/XCBuildData/.../attachments/` 中发现过 `DEEPSEEK_API_KEY` 与 `ANTHROPIC_AUTH_TOKEN` 的真实值形态。它们未进入 authored 源码、长期文档、H5 bundle 或服务器 release，但仍须轮换/吊销这两项 token，清理该 iOS build 目录，并在 unset 对应环境变量后重建、重扫；完成前不要宣称整个本机工作区 `secret-clean`。
- 200 条真实中文录音、完整 AEC 设备矩阵和第 21 章 SLO 属于规模化上线证据，不阻塞当前 H5 成品交付。

## 后续操作

1. 监控 API live/ready、`memoria-readiness-refresh.timer` 与 `snap.certbot.renew.timer`。
2. 在证书首次自动续期后核对 SAN、有效期、deploy hook 和 Nginx reload 日志。
3. 若进入规模化上线，再执行 200 条录音、AEC 设备矩阵和完整 SLO 验收。
4. 当前 runtime/H5 直接回滚点为 `20260717-113441`；环境备份为 `/var/backups/memoria/memoria.env-pre-20260717-123551`，本轮未修改 LiveKit 配置。
5. Omni 已内置模型页给出的空间主机前缀；生产仅在服务端 secret 文件配置 `DASHSCOPE_API_KEY`。`DASHSCOPE_WORKSPACE_ID` 只用于可选覆盖，二者都不得写入 H5 构建参数或浏览器存储。
6. 用户使用真实设备重点复测级联滋滋声、Omni 首词与反馈第二句，并覆盖 AI 结束后 0–700 ms 立即接话、打断、附和、回声、静音、停止、结束和情绪；结果写入下一次调优依据。
