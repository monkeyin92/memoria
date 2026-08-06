# 项目交接

## 2026-08-06：review.md 完成项归档（未提交、未发布）

- F0 的当前工作区 PostgreSQL/pgvector 子门禁已关闭：使用 CI 同镜像
  `pgvector/pgvector:0.8.1-pg17-bookworm` 得到全量 Python `1856 passed, 2 skipped`，总 coverage
  `87.70%`、orchestration `90%`、provider protocol `97%`。Ruff、strict mypy、Python/Go proto
  生成摘要、Go `gofmt/vet/test -race`、media smoke/replay、offline E2E 均通过。`buf lint`、
  相对 `HEAD` 的 `buf breaking`、GolangCI `v2.12.2`（`0 issues`）、当前媒体 Edge 镜像构建与
  Trivy `v0.70.0`（HIGH/CRITICAL 为 0）也已补跑；Trivy 发现的 `golang.org/x/crypto v0.51.0`
  已升级到 `v0.52.0`。远端最新 CI 是 2026-08-04 的
  [`#30889467167`](https://github.com/monkeyin92/memoria/actions/runs/30889467167)：Python job 因
  `funasr_stt.py` 导入未显式导出的 `ASRResult` 失败；当前工作区已修复该错误，但尚未提交并取得
  后续全绿 run，因此 F0 整体继续为部分完成。
- H5 当前 `288 passed` 且 production build 通过；in-app browser `390x844` smoke 中主容器无横向
  溢出，语音/文字入口可用且 console 无 error。该结果只证明本地 H5 壳可用，不能替代真实
  StreamCore TURN/provider E2E。
- 本轮再次在 in-app browser 验证 `390x844` 与 `1440x900`：首页、文字入口与“我的”导航均可用，
  页面没有横向溢出或 console error。`npm test -- --run` 仍为 `25 files / 288 passed`，
  `npm run build` 通过；Vite 仅提示首个 JavaScript chunk 大于 500 kB，尚未构成功能阻塞。
- 本轮 media smoke、replay 与 offline E2E 均通过（旧 generation PCM 为 0）；真实 provider smoke
  未运行：去除 `OFFLINE_MOCK` 后脚本明确报告缺少 `DASHSCOPE_API_KEY` 与 Doubao TTS 认证，未发起
  外部请求，R2 保持开放。
- `docs/media-runtime-acceptance-runbook.md` 已明确 required-mode 的真实 provider 命令，以及
  `DEEP_RESULT` 在 resolver 开始后发生新话轮/Stop 时必须证明零下行 PCM/ACK 的取证要求；offline
  skip 不再可被误读为验收通过。
- `provider_smoke_test.py` 的 required-mode 已补入隔离的 `QwenRealtimeSearch` forced-search
  source，`refresh_readiness.sh` 也同步要求该 PASS 标识；单测、Ruff 和 shell 语法通过。没有
  `DASHSCOPE_API_KEY`/Doubao 认证时仍 fail closed，尚无真实执行证据。
- StreamCore 物理 PCM 执行面现在显式只允许 `CONVERSATION_REPLY`、
  `FAST_ACKNOWLEDGEMENT`、`DEEP_RESULT`；`TOOL_RESULT`、backchannel/reminder/notification
  保留 wire/shadow 兼容但会在执行边界 fail closed。新增
  `test_reserved_output_kind_is_rejected_at_streamcore_execution_boundary`；相关定向套件
  `191 passed`。
- `review.md` 已将 C1 正式 `FloorEffect`、C2 单路 realtime-search、C3 typed
  `FAST_ACKNOWLEDGEMENT` 收入“仓内完成”归档，不再作为 A6A/R2 的开放问题；A6A 只保留实际输出
  来源与真实 Qwen/Doubao provider smoke。
- 修复 H5 语音询问日期后重复回答的仓内根因：播放结束后的回声防护原先放行短 ASR 片段
  `星期四`，导致它被提交为新话轮；`PlaybackInputGuard` 现在只对与当前助手日期回答匹配的
  `星期/周/礼拜` 片段判定 `assistant_echo`，普通“星期四我有安排”等新内容仍放行。新增
  `test_post_playback_weekday_echo_cannot_start_a_follow_up_turn` 与 guard 单测，相关 Agent/Runtime
  定向文件、公共日期回复、Ruff 和 strict mypy 均通过。
- 新增 `test_slow_media_delegation_plays_typed_fast_ack_then_deep_result`：慢 delegation 的 ACK
  必须先持有 playback，`DEEP_RESULT` 仅排队；ACK 的 `PlaybackProgress` 完成后才开始深度结果。
  相关 Agent/MediaSession/Provider/Delegation/gRPC 定向套件为 `190 passed`，Ruff 与
  `git diff --check` 通过。

## 2026-08-05：review.md 复核收口（未提交、未发布）

- 结论：架构升级判断和 A0–A9 计划已经写完，但实现/验收没有全部完成。F1–F3 已完成；
  F0 仍是部分完成：历史同 CI 版本 PostgreSQL/pgvector 基线为
  `1816 passed, 2 skipped`、coverage `87.59% >= 85%`，但当前新增代码尚未在该条件重跑；strict mypy 为
  `223 source files, 0 errors`。本次未启动数据库的全量测试为 `1810 passed, 29 skipped`，coverage
  `81.88%` 未达门槛，不能替代上述数据库条件证据；远端 CI 尚未复跑。
- A0 authority 契约已接入真实握手：Python bridge 选择并在 `SessionAccepted` 返回实际
  `interaction_authority`，Go 保存该值并通过 `session.ready` 提供给 H5。默认保持
  `python_authoritative`；`go_shadow` 要求 Edge/Core 双方显式启用；A6 前
  `go_authoritative` 一律回退或拒绝。
- 修复 A8 指标真实性：Doubao 多 pool 与 FunASR 多 session 使用进程级增减计数；断线、
  重连失败、已关闭 socket 和重复 close 不再覆盖或泄漏 `provider_ws_active`。
- 修复 A7 设备真实性：Linux renderer 只有明确返回绝对 `rendered_sample_end` 才上报
  `approximate=false`，返回 `None` 不再冒充 actual-heard；button/network 事件携带当前
  `(turn_id, generation_id, tool_epoch)`。Go `playout_buffer_ms` 在 generation 变化时重置。
- 生产 StreamCore 已改用仓内
  `services.agent.src.media_agent_factory:build_production_media_session_factory`：每个 session
  原子获得共享的 Runtime/Provider/ModePolicy/ResponsePlanner/speaker/voice authority/context
  与关闭生命周期，删除 production compose 对缺失外部 Runtime/LLM factory 的依赖。H5
  收到 `media_runtime=streamcore` 时会选择该 transport 代码路径；本地开发 API 代理以及浏览器
  注册、Profile、伙伴选择与声纹录取页 smoke 已通过，真实 provider + 浏览器媒体整链联调尚未
  完成。默认仍保持 LiveKit/0%，等待真实外部门槛。
- 修复 StreamCore 正式说话人时序：首次 range-stamped VAD start 建立一个逻辑 speaker
  fence，PCM 在 commit 前完成分类并把 owner/guest/uncertain 证据冻结到 Projection；短暂停顿
  与恢复仍由 sample-clock 合并为同一话轮，owner 历史/私人上下文/工具不再永久失去资格。
- archive spool replay 不再同步阻塞首个 session；`run_media_bridge` 在 start/wait/stop 异常
  下始终关闭共享 factory，并保留主异常。
- Registry 的 fast path 与锁内二次命中都已在执行 `_reuse_session` 前释放新 session 创建锁；
  新 session factory 创建仍串行化，不能把它当作完全无锁容量优化。Projection 的 provisional
  ID 在 discard/backchannel 后不复用，revision 在 session 内保持单调，H5 可继续显示并提交下一话轮。
- 本轮又补上 Voice Core 的提交临界区：provider preparation 成功且 stream epoch 仍有效后才
  消费 Projection provisional；prepare 失败会保留 provisional，重连期间不会把旧 epoch 的
  `turn.committed` 发布到新连接。旧 epoch provider callback 在 speaker/shadow 路径前后重验并
  fail-closed；factory 接线失败会关闭尚未发布的 Runtime/Provider 资源。
- Shadow resync 只有在 actor 接受 observation 后才推进 sequence/清除 resync；mailbox 拒绝时
  保留待恢复状态，避免 comparison ledger 因丢失 resync after-state 持续失真。
- H5 最新 StreamCore 路径已通过回归测试验证：generic client-event 先以 envelope 的 session/fence 覆盖 payload，
  合法 expression/emotion/trace 可进入既有解析器，伪造 payload fence 被拒绝；完整 speaking fence
  激活表情，listening/interrupted/closed/flush 清除。当前 `286 passed` 且 production build 通过；Go `test`、
  `test-race`、`vet` 通过；小程序 `86 passed`。小程序本轮按产品决定暂缓，现有
  speaking/expression 路径已经使用完整 `(turn_id, generation_id, tool_epoch)` fence；工作区
  另有体验版上传依赖/脚本，未纳入本轮实现或验收。
- H5 本地开发认证代理已把后端 `Path=/v1/auth` cookie 重写为
  `Path=/memoria-api/v1/auth`，浏览器整页刷新可以恢复登录；Control API 缺少 LiveKit 凭据时
  现在返回结构化 `503 livekit_credentials_missing`，H5 显示安全中文提示。前后端回归和真实
  Vite 代理请求均已通过，服务端没有未捕获 traceback。
- 全仓 Ruff lint、Go `gofmt -l` 和 `git diff --check` 通过；Python/Go proto 生成物复现已通过。
  本机未安装 `buf`/`golangci-lint`，因此 `buf lint/breaking` 与 GolangCI 未运行；镜像/Trivy
  和远端 CI 也尚未复跑。
- 复核修复：Projection 的 canonical preview 也会结束 fresh-speech admission，避免 provider
  prepare/commit 事务改造后主回复永久停在 `THINKING`；全量 Python `pytest --no-cov` 已重跑通过。
  儿童授权录音 schema、replay gate 和回归测试现统一为至少 200 条。A7 的 `ClientEvent`
  task/context version 已完成 additive 透传，严格 envelope 会保留这两个字段；Go 使用进程单调时钟
  填写 `server_monotonic_ms`。剩余 A7 缺口是实际设备/浏览器媒体、trace、AEC 与端到端 SLO 证据。
- A6A 当前边界：actor 级有界 SpeechTimeline、candidate-only OutputArbiter、typed Floor shadow 及生产
  bridge 的 `shadow_observation` 代码路径已接通；task/segment/commit/context/真实 OutputIntent admission 会在
  单 mailbox event 内 apply/compare。规范化 watermark tail 与 lossy gap 分域 resync 已有
  Speech/Output/Floor 独立恢复回归；Python/Go 现使用一致的
  `(domain rank, priority, created_at, intent_id)` winner 排序，未知/未指定 kind 在 Python 权威入口
  fail closed；完整 active set 每个固定 rank 最多保留 4 个候选，支持同域和跨域 expiry fallback；
  wire 保留旧 `candidate` winner，并用 `active_candidates_complete` 区分完整空集合与旧 sender，
  结构合法 rejected admission 记录双侧 reason；带合法 after-state 的结构非法 rejection 也会
  进入 Go candidate-only ledger。普通 observation 现在由 Go 独立 apply 后比较，不再先覆盖出
  人为 parity；只有 transport gap resync 才原子恢复 Python 完整 after-state。
  `CONVERSATION_REPLY` 和 consumed after-state 已在 Python/Go 两端完成 apply/compare；只有 winner
  返回执行许可。多 kind OutputIntent 仍是 candidate/admission 元数据，但 normal reply、
  `tts_source`、`pcm_s16le` 已统一经 `OutputWork` 取得唯一物理 PCM owner；异步 speaking transition
  后重验 generation/owner，取消先撤 lease 并排空本地 task，再做 best-effort provider 清理；正常
  完成等待全部 PCM 与文本 span 获得 playback ACK 后再消费。queued candidate 会在旧 owner ACK 后
  唤醒，高优先级 work 会取消旧 owner、flush generation，并从 sequence/sample `0` 重启；无 span
  ACK 与 provider COMPLETE 时序回归也已关闭。Go retained queued intent 不再误计 dropped，只有容量
  trim 掉的当前 intent 才记录 superseded。最小 Python-authoritative `RealtimeEffect` 已通过
  `CoreToMedia`、Go identity/sequence/fence gate 和 WebRTC/H5 duck/restore/flush 接通；candidate、
  stale/非法 payload 与 A6B 前的 `go_authoritative` 均 fail closed，duck gain 保持原语义。
  `TOOL_RESULT`、backchannel/reminder/notification 等尚无真实生产来源；逐状态迁移和 A6B 权威启用
  仍未完成。
- 正式 `FloorEffect` 已仓内接通：Python 在 session ready/reconnect/assistant phase 发出带 identity、
  fence、单调 epoch 与 TTL 的 snapshot；Go bridge/runtime 复验 candidate、旧 epoch、TTL 和 A6B gate，
  再由 WHIP DataChannel 发布 H5 `floor.state`。这不改变 Python floor 写权，也不能提前启用 A6B。
- StreamCore 的实时查询已收口为 Registry-owned `media_deep_response -> DEEP_RESULT -> OutputWork`：
  已注册 delegation 的实时查询不再进入普通 `generate_reply`，因此每轮只调用一次 resolver；空结果使用
  既有安全回复，迟到结果仍按 fence 丢弃。普通非实时回复维持原路径。
- 慢于 `20ms` 的 StreamCore realtime delegation 现在会创建 allowlist typed
  `FAST_ACKNOWLEDGEMENT` 并进入同一 OutputWork 队列；快速结果不播 ACK，深度结果在 ACK 完成后接替。
- 本轮新增的 `ShadowFloorDecision` 为 additive typed shadow 证据；Python bridge 只在
  `go_shadow` 会话投递，Go 只记录 parity，不执行 floor effect。新增 actor 过期 fallback、
  Python bridge 和 Go typed-floor 回归均已通过。
- StreamCore Registry 的所有 `OutputWork` 不再只依赖 `reply_lock`；normal reply、`tts_source`、
  `pcm_s16le` 通过同一 OutputIntent winner 和 session owner lease 控制 provider PCM，并在
  cancel/ACK 路径释放。`TOOL_RESULT`、backchannel/reminder/notification 等尚无真实生产 provider
  矩阵，不能据此
  外推为 A6A/A6B 已整体验收。
  A7 仍缺真实浏览器/设备 playout、硬件 AEC 和端到端 P95；A8 四个指标已有本地数据源，
  但仍缺生产长会话容量、SLO 与无副作用 shadow；A9 仍缺儿童授权语料、多实例、chaos/load、
  灰度和回滚。A2 只有仓内实现与 loopback，真实 TURN/provider/跨主机验收仍缺。

## 2026-08-04：A7/A8 本地客户端 fence 与指标接线（未提交、未发布）

- A7 客户端侧补齐 turn/generation/tool fence 消费：H5 `assistant_state` 现在要求 `tool_epoch` 并以
  `(turn_id, generation_id, tool_epoch)` 单调门禁；`assistantStateFenceRef` 保存完整
  fence，`assistant_expression` 只在与当前 speaking fence 完全一致时激活；
  StreamCore 状态回调消费 envelope 的完整 fence；provisional UI 保留 envelope 的
  generation/tool fence。旧 tool epoch、同 generation 新 tool epoch、旧事件拒绝均有回归
  测试（`useVoiceSession.test.jsx` 新增 2 条）。
- H5 provisional 卡片消费 floor state：`user_holds_floor` 显示“你 · 正在说”，
  `uncertain` 显示“你 · 正在确认”，其余保持“你 · 正在听”（`App.jsx` 新增标签映射与
  App 级测试）。`packages/contracts/events.schema.json` 把 `assistant_state.tool_epoch`
  列入 required（Python 权威发布方本已携带）。
- 小程序对齐完整 fence：`_setStatus` 的 speaking fence 保存 `toolEpoch`，离开 speaking
  清空 fence 与 pending 表情；`_onAssistantExpression` 按
  `(turn, generation, tool_epoch)` 拒绝过期表达式，并新增 1 条回归测试。
- Linux 设备客户端新增设备事件上报缝：`client.device.event` 的 `button` 与
  `network_status`（按键与网络 RTT/transport）经 gRPC bridge 的现有
  `on_client_event` 透出，测试覆盖完整往返；`SessionHello.traceparent` 现由设备客户端
  传入并在 `MediaBridgeSession` 保留，为分布式 trace 关联提供本地接缝。设备按键/网络
  真实采集、硬件接线与 trace 后端消费仍待真机/生产。
- A8 指标接线（仅真实数据源）：`provider_ws_active`（Doubao TTS 连接池 `_all` 与 FunASR
  session 连接状态）与 `provider_ws_reconnect_total`（TTS refill 重开、FunASR `_recover`
  重连）接入 `MetricsRegistry`；原有 `asr_reconnects_total` 同时计入
  `provider_ws_reconnect_total{provider="asr"}`。media-edge 已有
  `active_media_sessions / audio_frame_deadline_miss_total|ratio / ingress_queue_age_ms /
  egress_queue_age_ms / floor_decision_latency_ms / generation_cancel_latency_ms /
  playout_buffer_ms / actor_mailbox_age_ms / session_duration_ms /
  shadow_decision_mismatch_total`。
- A8 后续已补齐本地数据源：`asr_send_lag_ms / asr_partial_age_ms /
  tts_frame_age_ms / playout_underrun_total` 分别由 provider send/partial/TTS frame 时间戳和
  Go playout 空转状态产生；仍缺真实生产容量、SLO 和长会话数据。
- 顺手修复 A3 遗留回归：`test_interaction_mode_agent` 的 owner 工具用例改为注册真实
  TaskManager handler 并验证协调包装后的工具通过（原断言用裸字符串，不再符合
  DelegationCoordinator 的 registered-handler 门禁）。
- 本轮验证：H5 `280 passed` + production build；小程序 `86 passed`；Python unit 全量
  `passed`；Ruff + format；strict mypy（改动的核心源文件）。A7/A8 的真实
  PeerConnection/设备 playout、AEC、网络指标和容量验收仍须外部环境完成。

## 2026-08-04：review.md F0–A5 架构升级（未提交、未发布）

- F1–F3 已关闭；F0 的原 mypy 问题已修复，但总 coverage 门禁仍未关闭。A0 已新增
  ADR-0029、三态 `interaction_authority`、单写者/effect
  ownership、additive media-v1 契约、Python/Go wire/旧 binding 兼容与可重复生成门禁。
- A1 已实现 `ConversationProjection`：ASR partial/revision 聚合 provisional patch，Router
  只提交 commit evidence，只有 committed + eligible 才进入权威字幕/历史；H5 单条原位
  patch，commit/discard/reconnect/epoch 清理，访客/ambiguous/backchannel 规则不被绕过。
- A2 已在现有 `services/media_edge` 内安装真实 Pion terminator，不新增网络服务：短期 JWT
  全身份绑定、WHIP POST/DELETE、带 ETag 的 Memoria full-SDP restart、ICE/DTLS/SRTP/RTP、
  上行 Opus→16 kHz PCM/energy VAD、丢包补静音/乱序丢弃、下行 24 kHz PCM→48 kHz
  Opus、DataChannel event/playout/stop relay、generation-scoped sender、immediate
  `playback.flush`、connected 后原子 epoch 替换、旧 epoch 错误隔离。production readiness
  实际 gather candidate，TURN-only 必须获得 relay candidate。
- A2 独立双轴审查发现并已修复：并发 epoch 反向覆盖、resource 鉴权字段不完整、restart
  半失败存活、VAD end 错误吞掉、满队列淘汰 flush、重复 peer lookup，以及 PC failure
  callback/cleanup 锁竞争造成失败 peer 覆盖旧 epoch。`closed` 在任何失败 cleanup 前同步
  发布，cutover 同时校验 Pion 原生状态仍为 connected；新 epoch 现在保留旧链到 ICE/DTLS
  connected 且 sender/Voice Core bridge 全部成功。
- A3 已实现统一 `InteractionPlane` 与 `DelegationCoordinator` 的内部接缝：普通监听和播放期
  transcript 共用决策面，VAD/partial 可提前预取和 warmup；测试覆盖 search/tool/deep 的
  task/generation/context/relevance/expiry gate。生产 session factory 目前没有真实
  `realtime_search_resolver`；adapter 只有条件代理 seam，生产 `_SessionLanguageModel` 未实现
  `start_delegation/accept_output_intent`，所以 `supports_delegation=false`。因此 A3 只能记为部分完成，不能把测试 fake 写成
  StreamCore deep source 已生产接线。
- A4 已实现不可变 `ContextSnapshotManager`、每 generation 固定 `context_version`、后台
  builder、超时/取消和 CAS 激活；superseded partial 会立即取消旧 builder。非 owner
  snapshot 在统一构建边界剥离 owner turns、Memory、Persona、summary 与 tool permission，
  builder 失败或说话人切换不能复用主人私密上下文。
- A5 已实现 Go `LiveSessionActor` shadow：每 session 有界 mailbox、音频 reserve、20ms
  deadline、floor/generation/playout/output candidate、Python-vs-Go comparison ledger；
  `assistant_state` 携带完整 tool epoch，全部 interaction phase 有映射，mismatch 按
  scenario/contract version 导出。`GET /v1/media/sessions/{id}/shadow` 是带完整 session JWT
  的只读证据入口，candidate 解析/队列失败不影响用户路径。
- A5 指标与生命周期按进程单调累计：mailbox reject、deadline、close drain 均计入一致
  denominator；reconnect、显式 close、WHIP replacement 和 server shutdown 会原子转移
  actor counters。`Draining + openMu` 串行 create/reconnect/WHIP/shutdown，所有
  `NewSession` 失败路径都会关闭 runtime 并回收 actor。
- 媒体镜像已安装 `libopus` 构建/运行依赖并实际构建、启动，distroless `/healthz` 通过。
  本地真实 Pion loopback 覆盖双向媒体、DataChannel transcript/projection/state/error、stop、
  cancel 后旧 Core PCM 拒绝、playout progress、ICE restart、RTP 丢包/乱序和 epoch reconnect。
- 本轮验证：Ruff；strict mypy（218 files）；全仓 `1692 passed, 29 skipped`；H5 `277
  passed` + production build；Go `test-race/vet/gofmt`；最终 epoch failure 竞态用 race detector
  连跑 30 次；Docker build/runtime health；`git diff --check`。本地没有安装 golangci-lint，
  CI action仍保留该门禁。
- A3–A5 最新门禁：Python/Control API 扩展回归 404 项通过；相关 30 个 Python 文件 Ruff
  与 format check 通过；13 个核心源文件 strict mypy 通过；Go `test`、`test -race`、`vet`、
  `gofmt` 与 `git diff --check` 通过。当时的双轴复审结论已由本文件顶部最新复核取代。
- 尚不得宣称生产验收：真实浏览器/TURN/跨主机 playout、停止词/普通插话 P95、硬件 AEC、
  儿童授权语料、多实例、真实 chaos/load、Shadow 和回滚发布证据仍缺。一次已开始的
  WebRTC write 无法撤回；后续旧 PCM 会被 gate 拒绝且客户端立即 flush，最终可听停止必须
  在 A7 以真实 playout 测量。LiveKit 默认/fallback 未切换。
- A6 仍不得启用：缺少真实固定评测集 parity、真实长会话容量/SLO、无副作用 shadow 与
  回滚证据，当前 authority 保持 Python，Go 只产生 candidate。下一步继续 A7/A8 可本地
  实现的客户端/指标/证据面；真实设备、儿童授权样本、生产 shadow、灰度和 A9 发布验收
  必须在对应外部环境完成，不能包装成本地已验收。

## 2026-08-04：已提交 `6ee6350`，review.md 复评问题跟进

- `6ee6350` 已提交 ASR 版本统一为
  `ASRLogicalVersion(task_epoch, provider_revision)`；跨 committed watermark 的结果由
  `ASRAcceptDecision` 规范化，缺少可靠词时序时 fail-closed；provider adapter 直接透传
  统一映射结果。
- 该提交同时包含 review.md 复评问题的修复：canonical `ASRResult` 导入、同 task 起点
  修订 revision、stream-scoped 旧 task fence、可靠 timing evidence、Registry timeline
  rollback 与回归测试；上述修复均已提交，不再标记为“工作区未提交”。
- ASR 区间/重放/修正规则收敛为单一决策点：`provider_adapter.py` 退化为纯 provider
  映射（删除 `_final_sentence_ids`/`_accepted_final_ranges`/`_provider_final_ranges`/
  `_max_final_end_by_epoch` 及裁尾逻辑，final 携带完整绝对区间与全文），
  `ASRStreamSupervisor.accept_result` 成为唯一区间权威；supervisor 新增同区间同文本
  高 revision 重放的 fail-closed 去重（transport duplicate 不再重复发布）。由此满足
  “最高 revision 成为权威结果”：`你好`→`你好世界`→`你号世界` 最终 canonical 为
  `你号世界`（P1）。
- pending 字幕合法落后 PCM 时保留已安全确认 heard 前缀：`_provider_timed_text_spans`
  对 `alignment=="pending"` 不再做“最后字幕词 vs 全部 PCM 末尾”的 300ms 差距检查，只
  信任已落在已生成音频范围内的 span；无/未知 alignment 仍 fail-closed（P2）。
- `on_playback_progress` 空 span ACK 仍执行播放完成判定：`acknowledged == ()` 只表示
  无新可发布文本，不再提前 return；`provider_complete + is_fully_acknowledged` 照常
  结束 SPEAKING，heard 文本发布仍以新 span 为条件（P1）。
- doubao mock `scaled_ts` 偏差 0.5s→0.2s（落在 120–300ms scaled 带），新增
  `degraded_ts`（>300ms）集成测试，修复远端 CI `test_doubao_mock.py` 红（P1）。
- `6ee6350` 已验证 agent 单测、全量 pytest（含预期跳过）、Ruff、strict mypy、media
  smoke/replay、Go `test` 与 `git diff --check`。当前工作区未提交内容为 F0–A5 与
  A7/A8 的后续实现（见本文件顶部），与本提交无关。
- 未闭环（外部证据，未伪造）：P0 生产 media-runtime 仍无真实 `DownlinkSenderFactory`
  ——真实 WHIP/RTP/DTLS/SRTP/Opus 终结器需按
  `docs/media-runtime-acceptance-runbook.md` 外部接入并安全审查；端到端打断 P95、
  真实媒体链联调、儿童语料/AEC/多实例/chaos/load/回滚演练仍缺；fail-closed 保持。

## 2026-08-04：最新评审修复（未提交）

- 跟进 `3d05407` 后续评审：gRPC 下行队列满时先原子替换为 terminal
  `CANCEL`，再异步取消 runtime/provider；writer 只在关闭且队列已清空时退出，避免
  terminal 留在无人消费的队列中。
- 豆包字幕仅在误差 ≤120ms 时原样入账、120–300ms 缩放；>300ms 仅保留 telemetry，
  不生成精确 actual-heard span。流式字幕到达时保留快照，打断前只登记已验证、且已由
  ACK 覆盖的前缀；空/非法字幕时音频 ACK 仍能结束 SPEAKING，但不会写入已听历史。
- ASR 重连扩展保留 provider 的完整原始区间；后续同区间高 revision correction 会更新
  已发出的尾段，不再因扩展时裁尾而静默丢失。生产 sender readiness 增加
  `DownlinkReadyProbe`，factory 存在但终结器不健康时 `/readyz`、建连与重连均 fail-closed。
- 已验证：定向 Python 73 项、Ruff、所改 Python 模块 strict mypy、Media Edge
  `go vet`、`go test` 与 `go test -race`、`git diff --check` 均通过。

- Go Media Edge 升级为 Go `1.26.5`、gRPC `1.83.0`、`x/net 0.55.0` 与
  `x/text 0.39.0`；同步 CI 与镜像构建版本，修复上一轮 Trivy 报告的 Go 标准库、
  gRPC 和 `x/*` 高危漏洞，不降低扫描门槛。CI 的 `golangci-lint-action` 同步升级至
  `v9` / linter `v2.12.2`（旧 `v1.63.4` 不支持 Go 1.26）；按新门禁修正 Close 错误
  处理、错误文案和回调等待测试。
- 生产 ready 不再接受 `MEDIA_EDGE_EXTERNAL_DOWNLINK_SENDER_READY` 这类布尔
  声明。真实终结器必须把 `DownlinkSenderFactory` 注入 `Server`，并将 factory
  生成的 sender 直接传给每条 `VoiceCoreMediaRuntime`；当前参考 binary 没有该
  集成，因此生产继续 fail-closed，不能创建假可用 session。
- `DeliverDownlink` 现以 generation-scoped context 调用 sender，调用不再占用
  session mutex；取消先关闭 context 和 generation gate。慢/阻塞 sender 不再拖住
  hard-stop，合规 sender 不能在 context 取消后写入旧 PCM。Reconnect 同步重置
  uplink/downlink 的 sequence 与 sample 基线，避免新 epoch 的首个下行帧被误判为 stale。
- ASR 扩展重放裁出的尾段保留 provider sentence id，但拥有独立 timeline segment
  id，`0..320 “你好” + 320..640 “世界”` 不会再被同 ID revision 覆盖为只剩尾段。
- 流式 TTS 只以供应商 `TTS_SUBTITLE` 字级时间戳登记 Playback Ledger；时间戳晚到
  时按已有 ACK 立即结算。无对齐时间戳时不登记 actual-heard 文本，拒绝用 LLM
  announcement 或 PCM 到达时机猜短语边界。gRPC 下行队列满会同步取消 registry
  runtime、provider 与旧 reply task。
- `interrupt_stop` 不再跨 Edge/Core 主机相减墙钟；Core 只记录本进程
  `interrupt_core_stop`。端到端 SLO 在分布式 trace/时钟同步与真实媒体验收前
  继续缺失并保持 rollout fail-closed。
- 已验证：全量 `uv run pytest -q`、Ruff、strict mypy（216 源文件）、H5 `275 passed`
  及 production build、Go `vet/test/test-race`、CI 同版本 golangci-lint、镜像构建、
  Trivy `HIGH/CRITICAL=0`、media runtime smoke、synthetic replay/chaos/load 和
  `git diff --check` 均通过。
- 仍需外部验收：真实 WHIP/WebRTC/RTP 终结器及其 `DownlinkSenderFactory` 集成、真实
  浏览器/硬件 ACK、Provider/硬件与分布式 trace/端到端 SLO、儿童语料、Redis/coturn
  多实例、真实 chaos/load、灰度与回滚演练。`media-runtime` profile 不可上线，默认
  LiveKit 路径未切换。

## 2026-08-03：双轴评审二轮整改（已提交，未发布）

- CI 门禁：buf lint 命名例外（media-v1 既有契约，避免破坏性改名）、Trivy action
  `v0.36.0`、golangci 清除弃用 gRPC API 与未使用字段；ASR 改为区间集合去重
  （乱序不重叠 final 接受、同区间修正、跨 task 重放与歧义重叠 fail-closed）；
  流式 TTS 按短语增量登记 playback span，打断不再丢失已完成短语；
  INTERRUPTION_PENDING 下按钮/KWS stop 走同一 finalize；interrupt SLO 用
  Edge 墙钟检测时间戳覆盖 detect→gate→网络→cancel；Go
  `DeliverDownlink` 在单锁内完成 gate+sender+出队，队列溢出以 terminal
  CANCEL 收尾、重连不再收到陈旧 RESUME；H5 HTTP stop fallback 携带完整
  expected fence；`MEDIA_EDGE_EXTERNAL_DOWNLINK_SENDER_READY` 显式接线
  production ready 探针（默认 fail-closed，不伪造链路）。
- 验证：全量 pytest 通过（本机 coverage `81.08%`，`85%` 门槛仍差约 4pp，缺口为
  历史遗留 postgres/ONNX 真实依赖模块，未降级）、Ruff、strict mypy（216 源文件）、
  H5 `275 passed` + production build、Go `test/vet/test-race/golangci`、buf lint、
  proto 生成无 diff、bridge smoke 与 replay/chaos/load、`git diff --check` 均通过。
- 仍未关闭且必须外部验收：真实 WHIP/RTP/DTLS/SRTP/Opus 终结器与音频级 KWS
  producer、完整 Agent orchestrated provider factory、真实浏览器/硬件播放 ACK、
  监护人授权儿童语料、Linux 硬件 AEC、Redis/coturn 多实例、真实 chaos/load/SLO
  与回滚演练；`media-runtime` profile 不可上线，默认链路不变。

## 2026-08-03：最新双轴评审整改（未发布）

- 话轮入口不再把每个 ASR final 当作用户轮结束：`VAD_EVENT_SPEECH_END` 显式区分
  含 hangover 的 `sample_position` 与尾静音前的 `voiced_end_sample`；
  `MediaVoiceCoreRegistry` 只在 ASR final 覆盖后一边界且 900ms 静默窗口稳定后提交；500--800ms
  句中停顿、超时后迟到 final、多 final 单话轮均有回归。canonical text 截止前一
  声学边界，提交后再用 `sample_position` 推进 transport retire watermark，尾静音区间
  的迟到/重放结果不会串入下一轮。FunASR 重连若返回跨 task
  扩展区间，只接收能由连续区间和文本前缀证明的新后缀，歧义重叠 fail-closed。
- Go Edge 的按钮停止和 `hard_stop=true && confidence>=0.8` KWS 都先在本地原子关闭
  generation gate，再通知 Core；旧 PCM 立即拒绝，失败重试复用同一事件/fence，下一
  generation 可继续下行。生产缺少真实 downlink sender 时 readiness/session creation
  fail-closed；gRPC 输出队列溢出会唤醒 writer 并结束连接。
- H5 播放 ACK 改为按完整 fence 的每代 `currentTime` 基线计算；flush/seek/静音/重连
  重置基线，停止后更高权威 fence 会恢复同一 remote track。事件必须携带完整版本、
  session/epoch/sequence/turn/generation/tool/server monotonic 字段。Control API 请求不再
  全局硬限 10 秒；仅 heartbeat/reconnect/WHIP 等短请求显式超时，120 秒声纹录取不被误杀。
- ASR supervisor 现在按 `task_epoch + sentence_id` 管理 revision，并拒绝旧 task 回写更新
  segment；Media session 在送入 provider 前记录绝对 sample watermark。VAD endpoint 只允许
  单调前进，重连后的 stop 幂等键按 stream epoch 隔离，Edge/Voice Core session 的显式关闭
  会清理 bridge registry。流式 TTS 的字幕发布改为同一 fence 下的累计文本 revision，避免
  后续短语覆盖前文；播放 ledger 仍对无 provider 对齐信息的整代范围 fail-closed。
- Provider 多短句不再漏文本或 ledger：生产 TTS 在一个 generation 内只建一条双向流，
  首短句完成即推入 TTS，不等待下一短句或 LLM EOS；无 provider 分句时间戳时采用保守的
  整代 actual-heard span，避免中断时多记。generation/ASR 元数据均有界。
- 删除了 media bridge 中“固定系统提示 + 当前文本”的简化生产 LLM 入口。生产
  `build_production_provider_factory` 必须通过
  `MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY` 注入既有完整 Agent 响应链（记忆、Persona、
  权限、工具、安全规划），否则启动失败。仓库目前没有把这条外部注入冒充为已完成，
  `media-runtime` profile 仍不可上线。
- Control API `/stop-response` 先由 Edge/Core 返回完整权威 fence，再单调观察到 Session
  Directory；相同 idempotency key 在“Edge 已成功、响应/目录更新失败”后仍复用原取消。
  HTTP stop 不再关闭 session，后续 uplink/下一话轮 TTS 的 Go 回归已覆盖。
- 本轮验证：Python 功能测试 `1641 passed, 29 skipped`，Ruff 与 strict mypy（216 个源文件）
  通过；H5 `275 passed` 与 production build 通过；Go `vet/test/test-race` 通过；Proto
  生成无 diff，media bridge smoke、synthetic replay/chaos/load、offline E2E、
  `git diff --check` 通过。全仓本地 coverage 为 `81.00%`，仍低于既有 `85%` 门槛；本地
  没有 CI PostgreSQL，29 个数据库/真实模型条件测试被跳过，本轮没有降低门槛或排除代码。
- 未完成且不得包装成“全部生产验收”：真实 Pion/WHIP/RTP/DTLS/SRTP/Opus 终结器、完整
  Agent orchestrated handler 注入、真实 Provider/浏览器/硬件 ACK、监护人授权儿童录音、
  Redis/coturn 多实例、真实 chaos/load、灰度/回滚 SLO 证据。当前修复已提交为
  `c3b6c19` 并推送 `main`，但仍未发布、未切换 LiveKit；小程序素材、营销/研究文档和上传脚本等
  用户原有改动未触碰。

## 2026-08-03：全双工整改边界收口（未发布）

- H5 `StreamCoreTransport` 现在等待真正 `connectionState=connected` 才报告 ready，
  丢弃未来/跨 session/错误版本事件；断线与 heartbeat 只通知一次，hook 使用单飞恢复，
  重连把期望 `stream_epoch` 作为 CAS 发给 Control API；支持 `playback.flush`、session
  状态、assistant audio 状态、error/ping/pong。
- Go `VoiceCoreSession.SendStop` 使用单调 client sequence；Media Edge 入口对 Core gRPC
  使用有界阻塞拨号，生产 `/readyz` 检查 gRPC connectivity，JWT 强制 secret/issuer/audience，
  生产 bridge client 拒绝明文。Go 1.22 CI 兼容性已移除测试中的 `t.Context()`。
- `split_production_env.py`/`prepare_production_upgrade_env.py` 新增独立
  `/etc/memoria-media-edge.env` 输出，统一 `/etc/memoria-media-runtime` mTLS 路径；SLO
  stale counter 拒绝小数并 fail-closed，Linux device client 在 production 拒绝 plaintext。
- 定向与全量验证：Python `pytest` 全通过（含既有弃用 warning/预期 skips），H5 `264 passed`
  与 production build，Go `go test ./...`、`go test -race ./...`、`go vet ./...`，Ruff、strict
  mypy、production env tests、`git diff --check` 均通过。仍未发布、未切换 LiveKit；真实
  WebRTC/WHIP/RTP/DTLS/SRTP/Opus、provider/hardware/Redis/coturn 外部证据仍是启用条件。
- Device challenge bootstrap 增加每设备最多 3 个未消费 nonce 的持久化上限，超限返回 429；
  challenge 仍故意保持无 bearer 的首次引导入口，但不会无限堆积数据库状态。
- 新增 `media-fallback` 的 Session Directory CAS transition：H5 初次/重连失败切回
  LiveKit 后先把 route 从 `streamcore` 切成 `livekit`，避免后续 Stop 仍走 StreamCore
  generation-only 分支而无法投递 LiveKit room；目录与 Control API 均有回归测试。

## 2026-08-03：Voice Core registry、设备 client 与 SLO reporter（未发布）

- `services/agent/src/voice_core/media_session.py` 新增 `MediaVoiceCoreRegistry`：每条
  media-v1 gRPC session 绑定独立 `DuplexRuntime`、`ASRStreamSupervisor`、Generation
  Fence 和 `PlaybackLedger`；显式 sample range commit 后才创建用户话轮，旧 fence 在
  Media Edge 与 Voice Core 两端都拒绝。`test_media_session.py` 用真实本地 asyncio gRPC
  stream + fake provider 验证 ASR transcript、generation START/COMPLETE、PCM downlink、
  playback ACK 实际听见和 stop/cancel；`provider_adapter.py` 可显式复用现有
  FunASRSession、LLM handler 和 Doubao TTS handler，不在 bridge 内复制 provider stack。
- `grpc_bridge.py` 增加 session-close、PlaybackProgress、generation-aware transcript
  回调；`orchestrator.py`/`duplex_runtime.py` 增加 authoritative external generation
  fence 接口，停止命令不依赖清空队列保证正确性。启动脚本仍 provider-neutral，生产必须
  显式注入已有 FunASR/Qwen/Doubao adapter；没有把第三方媒体源码复制进仓库。
- 新增 `LinuxMediaDeviceClient`：bounded capture/reconnect、sample-clock、NLMS AEC
  playback reference、本地 mute、device command TTL/allowlist/ACK、exact playback
  progress；新增 `media-slo-reporter` sidecar，从 Agent Prometheus endpoint 读取后只向
  Control API 上报 allowlist 聚合指标，缺失字段/过期报告 fail-closed。真实 ALSA/I2S/DMA、硬件静音、WebRTC/RTP/DTLS/SRTP/
  Opus、生产 provider wiring 和外部 SLO 仍未验收。
- 文档审计已同步到当前证据：`docs/media-runtime-plan-audit.md`、
  `docs/media-runtime-foundation.md`、`docs/media-runtime-slo.md`、
  `docs/media-runtime-license-boundary.md`。
- 最后验证：全量 `pytest` 通过（含 skips）、Ruff、strict mypy；H5 `260 passed` 与
  production build；Go `test`/`test -race`；Proto 重新生成、descriptor、bridge smoke、
  replay/chaos/load、Compose config 和 `git diff --check` 均通过。仅保留 FastAPI/httpx
  的既有弃用 warning。
- 之后的边界 hardening 还通过了 Control API directory/media tests、Go race test：gRPC
  输出事件带 server sequence，Media Edge JWT 绑定当前 stream epoch，Redis generation
  bump 使用连续 CAS；这些修改未改变 LiveKit 默认或小程序路径。

## 2026-08-03：gRPC bridge、设备 registry 与 SLO gate（未发布）

- `packages/proto` 增加 `buf.yaml`/`buf.gen.yaml` 和可重复的
  `scripts/generate_media_proto.py`；Python generated bindings 已纳入 CI diff gate，
  `grpcio`/`grpcio-tools` 为显式依赖，没有复制第三方媒体源码。
- `services/agent/src/voice_core/grpc_bridge.py` 现在提供真实双向 asyncio gRPC
  `VoiceMediaBridge.Connect`、bounded downlink、stream epoch/generation/identity fence、
  TLS/mTLS material loader，以及 audio/VAD/KWS callback seam；`scripts/run_media_bridge.py`
  可独立启动，生产配置启用时强制 mTLS 和证书路径。它仍是 media boundary：尚未接入现有
  `DuplexRuntime`/FunASR/LLM/TTS 的生产 session registry，不能视作真实 provider E2E。
- Control API 新增持久化 `DeviceRegistry`：公钥注册、单次签名 challenge、revoke 和
  生产 media session signed proof；账户删除会清理设备身份。新增 `MediaSLOGate` 与
  token/TTL 内部报告接口，StreamCore 灰度在 gate 开启但没有新鲜报告时 fail-closed 到
  LiveKit。Agent/Media Edge 自动 SLO reporter、真实硬件/WebRTC/coturn/Redis 多实例仍待外部验收。
- 本轮新增/改动已通过全量 Python `pytest`（含 skips）、H5 `npm test`（260 passed）、H5
  production build、Go `go test ./...` 与 `go test -race ./...`、Ruff、strict mypy、协议
  generated diff、`protoc` descriptor、bridge smoke、synthetic replay/chaos/load 和
  `git diff --check`。未发布、未切换 LiveKit、未提交/推送；工作树中原有用户素材和文档
  改动保持不动。

## 2026-08-03：Go Media Edge ↔ Voice Core gRPC adapter（未发布）

- `services/media_edge/bridge.go` 新增自有 `media-v1` Go 双向 gRPC client：默认要求
  Voice Core mTLS（仅显式 development opt-in 才允许明文），支持 PCM audio/VAD/KWS、
  playback ACK、幂等 stop envelope，并在客户端对 identity、事件 sequence、sample range、
  downlink sequence 和完整 generation 再做一遍 gate。`scripts/generate_media_go_proto.sh`
  从仓库自己的 proto 生成 Go bindings，没有引入 StreamCore/Pion/LiveKit 源码。
- `bridge_test.go` 使用本地 fake gRPC server 验证 hello/accept、generation 后播放和重复
  downlink 拒绝；`go test ./...`、`go test -race ./...` 通过。真实 RTP/DTLS/SRTP/Opus/WHIP
  终结器仍需独立审核和接入，当前没有宣称真实网络媒体 E2E。

## 2026-08-03：A/B OTA 控制状态机（未发布）

- `services/agent/src/voice_core/ota.py` 在已有 Ed25519 manifest/digest 验证之上补充
  inactive-slot staging、bootloader/版本反回滚、bounded boot attempts、健康确认和
  confirmed-slot fallback；`test_ota.py` 覆盖签名/篡改/旧版本/bootloader 以及失败回退。
- 这是可持久化的 boot metadata 合同，不冒充真实 ALSA/I2S、Secure Boot、Flash Encryption
  或硬件断电恢复；真实设备接入时必须把 snapshot 写入 bootloader 的冗余元数据区并做断电演练。

## 2026-08-03：生产验收 runbook（未发布）

- 新增 `docs/media-runtime-acceptance-runbook.md`，把仓内门禁、真实 WebRTC/Provider/
  浏览器/硬件/儿童语料证据、灰度和 LiveKit 回滚步骤分开；未把 synthetic/fake 测试
  包装成生产 SLO，也没有记录 secret 或原始音频。

## 2026-08-03：媒体运行面与安全边界（未发布）

- 在 2026-08-02 基础之上补齐了自有 Session Directory（内存/Redis、TTL、drain、
  reconnect epoch）、coturn REST/HMAC 短期凭证、`/v1/media/sessions` 与设备媒体会话
  façade；Control API 在没有 Redis 生命周期的 ASGI 测试客户端中也保持内存实现，生产
  Redis 故障显式 fail-closed，不回退到本地路由；Redis reconnect/renew 使用 stream
  epoch Lua CAS，HTTP stop 的 generation bump 额外要求当前 generation 连续递增，重复
  claim 只接受完全相同的幂等投影。
- 新增 `services/media_edge/` 自有 Go 参考状态机：session/generation/sample-range
  gate、重连、bounded queue、JWT claim 校验、健康/ready/Prometheus HTTP 控制面和
  race 测试。它没有复制 Pion/StreamCore/LiveKit 的 RTP/DTLS/SRTP/Opus 代码；真实
  WebRTC 终结仍必须由经过审核的适配器提供，故 `MEDIA_RUNTIME_DEFAULT=livekit` 不变。
- 新增 `voice_core` 的 Linux SBC 参考 NLMS AEC、设备 Ed25519 challenge、签名 OTA
  manifest、设备命令解析、媒体 telemetry/OTel bridge、SLO/自动回滚判定，以及 synthetic
  child-speech replay/chaos/load harness；H5 StreamCore 会按 Control API heartbeat 续租
  media route，FunASR/Timeline 对同 revision final 和跨 task final 做 fail-closed 去重，
  Go edge 默认显式鉴权且拒绝 sample gap/discontinuity。真实儿童录音、硬件声学、TURN
  relay、生产 Redis 多实例和回滚演练仍不能用合成测试替代。
- 验证：全量 Python `pytest`（通过）、H5 `npm test`（260 passed）、H5 `npm run build`、
  Go `go test ./...` 与 `go test -race ./...`、定向 Control/voice_core 测试、Ruff、strict
  mypy、Proto descriptor 编译和 `git diff --check` 均通过。曾误用 H5 不支持的
  `--runInBand`，该命令失败不代表测试失败，随后已用项目原生命令重跑通过。
- 计划逐项审计见 `docs/media-runtime-plan-audit.md`。该审计明确：`services/media_edge`
  是自有状态机/协议参考入口，并未重写 DTLS/SRTP/Opus/RTP；在真实 WebRTC、Voice Core
  bridge、Linux AEC、150--300 条监护人授权儿童语料、coturn/Redis 多实例和回滚演练完成
  前，`MEDIA_RUNTIME_DEFAULT=livekit` 不变。
- 本轮未发布、未切换 LiveKit、未提交/推送；工作树内原有小程序素材、营销文档和脚本改动
  保持不动。

## 2026-08-02：全双工整改基础（未发布）

- 已按 `memoria_streamcore_full_duplex_remediation_plan.md` 落地自有媒体契约基础：
  Sample Clock `SpeechTimeline`、FunASR task/sample watermark、严格 generation/response
  lease、Playback ACK ledger、adaptive energy VAD、中文控制词 KWS、media-v1 JSON/Proto
  契约和有界 in-process Media Bridge；实现位于 `services/agent/src/voice_core/`。
- H5 新增浏览器原生 `StreamCoreTransport` 与 factory，Control API 增加 server-owned
  `media_runtime`/LiveKit fallback/短期媒体 JWT；默认 `livekit`，小程序不参与迁移。
- 本轮没有引入 StreamCore/Pion 代码，也没有切换生产链路。Go Media Edge、coturn、Redis
  Session Directory、Linux AEC、设备 OTA/证书和儿童真实音频 SLO 仍须独立阶段验收；详见
  `docs/media-runtime-foundation.md`。
- 已验证：全量 Python `pytest`、全量 H5 Vitest（258 passed）、H5 production build、
  Control API 会话/媒体测试、Ruff、strict mypy、`git diff --check` 和三份 Proto 的
  `protoc` descriptor 编译。

## 当前状态

- `20260802-142257` 已提交、推送并于 `2026-08-02T14:30Z` 原子切换 runtime，H5 随后切换至同 tag；默认主 LLM、打断语义、Control API 日回顾、记忆/Persona 提取均使用百炼 `bailian_deepseek / deepseek-v4-flash`，复用 `DASHSCOPE_API_KEY` 与 OpenAI-compatible endpoint。百炼请求使用 `enable_thinking=false`，实时检索仍无已验证 resolver 时 fail-closed 为“我不知道。”显式 `qwen` 与直连 `deepseek` 仅作兼容覆盖，FunASR/Qwen 情绪 ASR/Omni 实验未误当作主 LLM。
- 首个候选 `20260802-135917` 在真实 smoke 中发现历史 `0.6s` InterruptSemantic 门限不足（DeepSeek 实测约 `0.77–0.88s`），readiness 未写入，已安全回滚至 `20260731-114616`；随后以 `1.2s` 门限修复并重新发布。最终四容器 healthy/restart=0、LiveKit、DeepSeek、InterruptSemantic 五类、FunASR 六段、Doubao 五音色/取消、9/9 core readiness、Nginx 与公网 H5/API 均通过。直接 runtime 回滚点为 `20260731-114616`，H5 回滚点为 `20260730-233824`，root-only 备份和完整 SHA 见 `docs/releases/20260802-142257.md`。
- `20260731-114616` 已提交、推送并于 `2026-07-31T03:58Z` 原子切换 runtime：生产复盘确认
  `qwen-turbo` 当前不支持联网搜索，原先的 `enable_search=true` 没有实际查询能力，且“不能查询”
  一类拒答被完成态误判而清掉待办。现在仅在已识别的实时问题上调用独立的 `qwen-plus` 强制搜索，
  请求体只含固定安全提示和本轮公开 query；普通对话仍用 `qwen-turbo`，不把历史、主人资料、记忆、
  工具或会话 ID 送去联网。Provider 失败或拒答安全降级为“我不知道。”且保留同 scope 相邻待办，
  “你不能帮我查吗”也会恢复原公开查询。精确红绿回归、完整 Agent unit、Ruff、strict mypy、
  `git diff --check` 均通过；服务器 source/images/H5/manifest/verifier 验签、隔离 smoke、真实
  LiveKit/Provider/readiness、容器 healthy/restart=0、Nginx 与公网 H5/API 均通过。新 Agent 容器
  内 `qwen-plus + forced_search` 公开天气 canary 返回可用结果；H5 保持 `20260730-233824`，小程序
  未动。直接 runtime 回滚点为 `20260731-102620`，root-only 备份见
  `docs/releases/20260731-114616.md`。首次备份在旧 `postgres` 角色假设处、切流前停止；最终改用
  已验证的 `memoria_admin` custom dump。PostgreSQL WAL archive 既有权限问题仍不在本次范围，可用
  保护继续是 SQLite 一致快照与 `memoria` custom dump；真实用户语音/连续追问体验待产品负责人复测。
- `20260731-102620` 已提交、推送并于 `2026-07-31T02:42:15Z` 原子切换 runtime：已按生产链路
  复现“南京天气 → 我查一下 → 人呢”的断接。
  原生 Qwen 联网流可正常结束在过渡语，原先没有完成态或待办请求，因此追问被当作全新闲聊。
  现在实时问题只在获得完整结果后播报；桥接语、联网失败或空答统一为“我不知道。”并保留
  同一 public scope、相邻 generation fence 的待办。仅相邻的“人呢／查到了吗”等催办可恢复
  原问题，owner/public 边界、迟到 generation 和新话题均会丢弃待办，不带入主人历史或私有上下文。
  realtime buffer 同时受既有语音字数/句数上限约束，超过上限会关闭 provider stream，避免无限等待。
  本地完整 Python、该回归用例、Ruff、strict mypy（180 个源文件）、H5 `253/253`、production
  build 和 `git diff --check` 通过；候选 smoke、真实 LiveKit/Qwen/FunASR/豆包/Interrupt Semantic、
  9/9 core readiness、四容器 `healthy/restart=0` 及公网 8443 H5/API 均通过。H5 保持
  `20260730-233824`，小程序未动；直接 runtime 回滚点为 `20260730-224522`，root-only 备份见
  `docs/releases/20260731-102620.md`。真实用户会话的天气与催办体验待产品负责人复测。PostgreSQL
  WAL archive 仍因既有目录权限失败，不能当作 PITR；本次可用回滚保护是已验证的 SQLite 快照与
  `memoria` custom dump，需另行授权修复 WAL archive。
- `20260730-233824` 已提交、推送并于 `2026-07-30T15:42:40Z` 切换 H5：普通竖屏和低高度
  横屏的对话卡均由卡片自身纵向滚动，长回答不再溢出卡片或贴入固定底栏。其余单条当前发言、
  乐观用户展示、助手流式替换、single-flight、权威终稿和隐私边界不变。首次切换后的静态
  探针误用了根路径，门禁自动回滚；改用实际 `/memoria-h5/` 资源路径后，同一工件完整通过。
  runtime 保持 `20260730-224522`，直接 H5 回滚点为 `20260730-232440`。线上 390×844、
  360×740、760×390 均无横向溢出，退出按钮为 44px，长回答卡 `overflow-y: auto`；当前
  JavaScript 只有 info 日志。小程序 `0.8.65` 已从同一干净提交上传体验版，任务
  `confirmation_upload_1b7f3850-31b1-487c-bcc3-69fefb7060d4` 返回
  `success / execution_success`，包体 `656,661` 字节；未提审、未正式发布。发布记录见
  `docs/releases/20260730-233824.md`。
- `20260730-232440` 已提交、推送并于 `2026-07-30T15:36:10Z` 切换 H5：
  H5“退出文字对话”恢复为明确按钮；H5 与小程序对话区
  只展示当前说话对象，用户内容会在助手真实流式字幕开始时被替换。小程序流式字幕仅用于文字
  模式临时展示，权威终稿仍是历史与持久化唯一来源，语音模式继续忽略原始网关字幕。H5
  与小程序均限制同一时间只提交一个文字话轮，旧回包不能覆盖更新的本地用户内容。H5
  `253/253`、小程序 `85/85`、production build、JavaScript syntax 与 `git diff --check`
  已通过。首次两次验收脚本误判均自动回滚，第三次按真实 readiness 和稳定容器字段通过；
  runtime 保持 `20260730-224522`。线上单条替换与按钮通过，但截图发现长文本卡片溢出，已由
  `20260730-233824` 候选收口。完整结果见 `docs/releases/20260730-232440.md`。
- `20260730-224522` 已提交、推送并于 `2026-07-30T14:56:32Z` 原子切换 runtime，H5 于
  `2026-07-30T14:58:10Z` 切换。新增 `SpeechEpochAssembler`，以独立 ASR final segment
  关联 LiveKit committed turn，不再假设“一个 VAD epoch 等于一个逻辑话轮”；新增严格白名单的
  服务端 `audio_trace` 和 H5 `LiveKitAudioTelemetry`，不记录文本、设备 ID、标签或原始音频。
  H5 短打断门槛固定为 `0.35s`，候选插话立即静音助手，首页恢复不结束会话的“停止回答”。
  第一次 Provider 门禁因 Interrupt Semantic 瞬时超时自动回滚，旧版验收通过后以同一工件重试
  成功；9/9 core、全部 Provider、四容器 healthy/restart=0、公网 8443 H5/API、1,820 次历史资源
  和 WSS `4401` 均通过。回滚点与工件摘要见 `docs/releases/20260730-224522.md`。儿童真机识别率、
  短命令召回、含停顿话轮和外放听感仍须用同一组 iPhone/Android 语料 A/B，不能把自动门禁写成
  声学验收。
- 小程序 `0.8.64` 已从干净提交 `782fac90db71901e1c6c4a3ae62da4b7c6bd751e`
  上传体验版；任务 `confirmation_upload_8e16a0d0-a9ac-4f26-af32-09e63d6b6192`
  返回 `success / execution_success`，包体 `655,523` 字节。上传内容不包含本地未提交的 CI
  脚本、设计预览或素材草稿；尚未提审、未正式发布。
- `20260730-204546` 已于 `2026-07-30T12:49:39Z` 完成 runtime 热修切换，H5 保持已于
  `2026-07-30T12:34Z` 切换的 `20260730-202610`。小程序首页现在以动态吉祥物为焦点，语音只
  保留开始/结束一个主按钮，并增加完全不录音、不播放声音的文字对话入口。
- H5 `lk.chat` 与小程序 WSS `text_turn` 两条公网真实链路均已通过 Agent 的 owner policy、
  ResponsePlan、伙伴个性和 generation fence，助手终稿为
  `text_delivered=true / heard=false / history_eligible=true`；验收临时账号已删除。真实 SDK smoke
  还发现并修复了 Gateway 漏 `await send_text()` 与文字话轮错误要求 TTS 音色 provenance
  两层问题。
- 当前四容器 healthy/restart=0、9/9 core ready，真实 LiveKit/Qwen/FunASR/豆包五音色/
  Interrupt Semantic、隔离 server smoke、公网 H5/API/WMS、209 个 H5 资源和 WSS `4401`
  均通过；严重错误日志为 0。直接 runtime/H5 回滚点为
  `20260730-203837 / 20260730-202610`，root-only 备份见
  `/var/backups/memoria/runtime-switch-20260730-204546-from-20260730-203837-20260730T124912Z/`。
- `20260730-184554` 已于 `2026-07-30T10:54:21Z` 原子切换 runtime；H5 保持
  `20260730-171321`，小程序体验版保持 `0.8.63`，本轮未重新发布客户端。日期、星期与当前时间
  现在由服务端按 `Asia/Shanghai` 确定性直答；天气缺城市先追问，有城市才走 Qwen 原生联网搜索，
  无可靠结果不猜。Control 失败时 Agent 本地降级仍复用同一规则。
- 五个伙伴现在各有独立首句、具体正文措辞规则和 TTS 默认节奏；真实五音色映射不变。危机/安全、
  用户明确语音命令和当轮语义情绪优先于伙伴默认风格。四容器 healthy/restart=0、9/9 core ready、
  真实 LiveKit/Qwen/FunASR/豆包五音色/打断语义、联网天气、Nginx、公网 API/H5/WMS 与 WSS
  `4401` 均通过；真机主观个性与听感仍待下一次对话确认。直接 runtime 回滚点为
  `20260730-171321`，备份见 `docs/releases/20260730-184554.md`。
- `20260730-171321` 已提交、推送并于 `2026-07-30T09:23:46Z` 切换 runtime，H5 于
  `2026-07-30T09:25:17Z` 原子切换；小程序 `0.8.63` 体验版上传成功（`653,554` 字节），
  未提审、未正式发布。shadow 短纯控制现在只停止播放、不进入聊天或权限面；旧/无快照
  speech epoch 的迟到终稿不会重复播确认音或串入下一话轮；自建 endpoint 为 `1.50 / 2.20` 秒。
- H5 与小程序“我的 → 主人声纹”均可按自然、轻声、带笑、认真四种状态顺序录取；同一加密
  声纹版本保留四个独立原型并取最高 cosine similarity，旧单中心模板继续兼容。登记仍只进入
  shadow，不绕过 anti-spoof、200 条评估与 FAR/FRR/EER 门禁。
- 四容器 healthy/restart=0、9/9 core readiness、真实 LiveKit/Qwen/FunASR/豆包五音色/打断语义、
  公网 H5/API/WMS、WSS `4401`、TLS、Nginx、1,038 次历史资源检查与零严重错误日志均通过。
  直接 runtime/H5 回滚点为 `20260730-092236 / 20260730-153343`；root-only 备份位于
  `/var/backups/memoria/runtime-switch-20260730-171321-from-20260730-092236-20260730T092241Z/`。
- `20260729-193333` 已于 `2026-07-29T12:28:16Z` 原子切换 runtime：未确认说话人现在可承接
  本次会话末尾连续的 public 工作上下文，但主人私人历史、记忆、Persona、工具与
  `history_eligible` 仍隔离；Qwen 多段情绪按 PCM sample 区间、provider `audio_start_ms`
  与 `item_id` 绑定原话轮；自然表达和豆包情绪/方言/语气/语速/音调/引用上文控制已启用。
- required Provider/readiness 两轮均遍历五个批准音色，六段 FunASR 回识别、Qwen、LiveKit、
  Interrupt Semantic 与取消门禁全过；未出现 `degraded`。四容器均 healthy/restart=0，公网
  8443 H5/API/WMS、Nginx、SQLite 完整性和 9 项 core readiness 通过。H5 未切换，仍为
  `20260729-113831`；真机自然度、方言准确度、情绪强度和真实笑声仍需主观声学验收。
- `20260729-171002` 已于 `2026-07-29T09:35:05Z` 原子切换 runtime：陪伴模式只以当前选定
  机器人名称和对应风格对外回应；身份/模型追问与显式违禁请求在 Control API 或其 Agent
  降级路径直接返回固定短句，不读取私人记忆、persona 或调用 LLM。Qwen 兼容接口启用原生
  `enable_search`，DeepSeek 路径不发送该参数；小程序普通回答继续硬限为 3 句/120 字，只有
  用户明确要求故事、朗读、详细、完整、长一点或继续时放宽。
- 发布前完整 Python、Ruff、strict mypy、离线 E2E、H5 `242/242` 与 production build、
  小程序 `80/80`、JS syntax 以及 `git diff --check` 已通过。H5 全量测试第一次有一项异步
  断言波动，单文件和全量重跑均通过；线上四容器均 `running / restart=0`，公网 H5/API 为
  200，Agent 与 Control API 容器内固定回复断言、真实 Qwen `enable_search` 请求均通过。
  未接入需额外开通的外部语义内容审核，未命中词表的违禁改写仍依赖模型安全提示；真机语音时长
  与完整声学矩阵仍待验收。
- 记忆架构 P0–P4 已随 `20260729-093337` 提交、推送并部署生产：类型化投影、13 场景评测、
  EpisodeConsolidator、Skill Domain、Mem0 影子、pgvector HNSW/基准和 TurboVec 硬门禁均已落地。
  权威证据账本不变，工作记忆仍只按话轮动态组装。
- GitHub Actions 已在 `f6a9580` 的 run `30416225947` 全绿：Python job 运行真实
  PostgreSQL/pgvector 合同测试，总覆盖率恢复至 `89%`，未降低既有 `85%` 门槛。
- 当前已部署源码基线：`0fca7584e0737cca431367e230366f0d08fa6898`，annotated tag
  `20260730-184554` 精确指向该提交；`origin/main` 已包含源码提交，发布证据提交见本轮后续记录。
- 微信小程序开发测试版：`0.8.59` 已上传成功（`636,385` 字节），但真机已确认欢迎语首播后
  因遥测契约不兼容断开；修复后的 `0.8.60` 已通过 CLI 上传开发测试版（`637,083` 字节），
  开 VPN 可完整聊天。诊断版 `0.8.61` 已上传（`637,237` 字节）；真机和 Safari 均确认标准 443
  在当前无 VPN 网络不可达，生产已按用户决定回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`。443 精确路由保留但不下发。
  用户已确认回切后页面与语音连接恢复可用。小程序未提审或正式发布；完整 iPhone/Android、
  音频路由、弱网和 AEC A/B 真机矩阵仍待完成。
- 本轮开发测试版均通过已登录的微信开发者工具 `upload` 完成，只上传开发版本，不提审、不正式发布。
  本机未跟踪的上传私钥、辅助脚本和 lockfile 不属于仓库交付，路径和值不得写入本文或提交。
- 新的 `0.8.62` 体验版已通过微信开发者工具上传成功（`640,786` 字节）；
  任务 `confirmation_upload_d0446177-4d6e-4007-8852-fa0a2e10c6fc` 返回
  `success / execution_success`。正式提审/发布仍不在当前 CLI 能力内，且完整
  iPhone/Android 声学、弱网和 AEC A/B 真机矩阵仍待完成。
- `0.8.63` 先按项目 `upload:test` 走 `miniprogram-ci`：dry-run 通过，Node 24 完整编译 32 个代码
  文件并确认新声纹页 JS/WXML/WXSS 通过，但实际上传因当前公网 IP 不在微信 CI 白名单而被拒；
  经用户确认后改用已登录微信开发者工具上传，任务
  `confirmation_upload_082e65b6-5a92-4560-a3a5-143ee334f477` 返回
  `success / execution_success`，包体 `653,554` 字节。默认发布策略仍优先项目脚本；本次成功
  通道是微信开发者工具，不得写成 `miniprogram-ci` 上传成功。
- 当前交付客户端为 `apps/h5` 与 `apps/miniprogram`；legacy Web 与原生 iOS 源码已移除。
- 历史路线、架构决策和发布证据分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与
  `docs/releases/20260728-170236.md`。

## 2026-07-30：实时信息与伙伴个性（已提交、推送并部署）

- 根因不是已接时间工具返回错误：生产没有日期/时间/天气 function tool，只有 Qwen
  `enable_search=true`；响应计划也没有权威当前时间。现在日期、星期和时间走确定性直答，天气
  缺城市先追问，城市天气及其他实时信息获得权威北京时间与联网/不猜规则；Control 不可用的
  Agent fallback 同样覆盖。
- 首句原先在 `agent.py` 固定为“嗨，我在呢”，普通 `SpeechPlan` 也完全不消费
  `companion_id`。现在 `services/common/companions.py` 的单一定义同时驱动五套欢迎语、正文规则、
  设计音色和默认 TTS 情绪/语速；优先级保持安全与用户明确命令更高。
- 本地 Ruff、strict mypy `178 source files`、全量 pytest（`1533 tests collected`）、离线 E2E、
  H5 `244/244` + build、小程序 `82/82` 和发布门禁通过。线上切换、工件、备份与验收见
  `docs/releases/20260730-184554.md`；H5/小程序源码未变，因此没有客户端发版。

## 2026-07-30：H5 与小程序主人声纹多原型录取（已提交、推送、部署并上传体验版）

- “我的”新增独立“主人声纹”入口，复用既有现场 PCM recorder 与登记 API；四段按
  “自然声线 → 轻声说话 → 带点笑意 → 认真表达”顺序解锁，显示 `0–4` 进度、重录、授权、
  忙碌与可恢复错误。录取期间隐藏底栏；返回、卸载或异常会 cancel recorder、停止 track。
  当前有语音会话时入口禁用，避免会话上行与声纹录取争抢同一麦克风。
- SpeakerAuthority 不再把多段 embedding 平均成一个中心；新版本将每段归一化后作为独立
  prototype 加密进既有 `template_ciphertext`，分类取最高 cosine similarity。无数据库迁移，
  SQLite/PostgreSQL 共用同一 codec，旧 JSON 单中心密文按单 prototype 继续读取。
- 仍保持安全边界：登记结果只创建新版 `shadow`；已有 active 时仍优先正式模板，新版不会
  直接获得主人历史、私人记忆或权限。正式激活继续要求 anti-spoof 可用、至少 200 条评估样本
  以及 FAR/FRR/EER/unknown-rejection 门禁。
- 本地门禁：全量 Python `1499 passed, 29 skipped`；Ruff、strict mypy `177 source files`、
  H5 `244/244`、小程序 `82/82`、production build、`git diff --check` 通过。现有 Chrome 用隔离本地 API 验证
  “我的 → 主人声纹 → 返回我的”；`375×812` 与 `812×375` 均无横向溢出，录音按钮
  `44×44`，深色授权文字与横屏居中已实看修正，console warning/error 为空。未自动接受真实
  麦克风权限，真人四种声线的 FAR/FRR、噪声与真机录音质量仍是发布前开放验收项。
- source commit / annotated tag 为
  `79f0cb89e1444e9fbdbb014bd66e1d5d58f5626c / 20260730-171321`；runtime、H5 已切换，
  小程序 `0.8.63` 体验版已上传。工件、备份与线上证据见
  `docs/releases/20260730-171321.md`。

## 2026-07-30：当轮语义角色与危机支持（已提交、推送并部署）

- 没有新增会话级角色状态或第二套路由。`DigitalSelfResponsePlanner` 的 canonical
  instructions 现携带统一当轮策略：危机支持最高优先；外语学习先复用已知目标、水平与场景，
  缺信息时最多追问一到两个关键问题，随后给短计划并立即开练；知识学习先给线索或思路框架，
  用户尝试或明确索要结论后再逐步给答案。每轮重新按语义选择，不继承上一轮角色。
- `fixed_companion_reply` 将明确本人轻生意图、已行动和自伤方法请求从普通违禁词中拆出，直接返回
  不依赖 LLM/私人记忆的危机支持：承认痛苦、确认当前危险、远离手段和危险地点、联系可信的人，
  已行动或即将行动时联系当地急救或报警。一般预防知识和替他人求助继续交给当轮策略，避免把
  用户本人误判为危机；危险行为实施请求仍固定回答“我不知道”。
- 明确危机文本同时进入 `supportive` SpeechPlan、禁止笑声和副语言，并关闭“嗯/你继续”等固定
  listener cue；Control 不可用时 Agent 本地安全降级仍复用同一策略和固定危机回复。
- 本地验证：Python 全量 `1488 passed, 29 skipped`；Ruff、strict mypy `177 source files`、
  `git diff --check`、离线 E2E、H5 `242/242` 与 production build 通过。生产固定危机路由和
  Provider/readiness 已验收；真实模型对开放式长尾语义的判断、真机危机话术听感仍需
  人工验收。本轮只切 runtime，不需要 H5 或小程序发版。

## 2026-07-30：小程序视觉同步到 H5（已提交并部署）

- 以小程序运行时 WXML/WXSS 为视觉基准，H5 新增 `src/miniprogram-theme.css`：全局切换为
  `#070b1a` 深空背景、电青/幻紫/品红主色、半透明深色卡片、霓虹状态与暗色底栏；登录、伙伴
  onboarding、回顾、我的、伙伴切换、数字心智、隐私和 H5 独有深层状态统一使用同一视觉壳。
- H5 首页重排为“问候标题 → 当前伙伴 chip → 光环舞台 → 状态 → 显式语音 CTA/控制 → 当前发言”，
  仍复用现有 `useVoiceSession`、Mascot 四表情、事件 fence 与全部语音 handler。小程序和 H5
  实际都只展示最新一条权威发言，本轮没有扩成会话历史，也没有改 API、协议或后端。
- H5 直接打包小程序已跟踪的 `assets/companions/alpha/*.webp` 与 `night-stars.webp`，避免白底机身
  再次漂移；Vite 开发服务器只把相邻 `apps/` 加入素材读取范围。生产构建会生成独立哈希资产，
  不依赖线上小程序目录。
- “我的”新增可见的当前伙伴卡和更换入口；从“我的”进入时返回“我的”，从数字心智进入时仍
  返回数字心智。高风险确认、声纹、声音、数字分身和传承能力均保留，没有为了视觉一致删功能。
- 本地门禁：H5 `242/242`、production build、`git diff --check -- apps/h5` 通过；小程序
  `80/80` 通过。应用内浏览器连接隔离临时 Control API，已核验注册、伙伴 onboarding、首页、
  回顾、我的、伙伴切换、隐私与数字心智；390×844、360×740、760×390 均无横向溢出，横屏
  CTA/对话卡与底栏不重叠，可见按钮均不小于 44px，console warning/error 为空。
- source commit / annotated tag 为
  `3d97a603598e28fb189560120db2c702a7aa6189 / 20260730-153343`；commit-bound H5 工件
  SHA-256 为 `73d7de646445951e4ddea30c36e418a9f846b7ff1fbf2b754684a62deae90285`。
- 生产 H5 已于 `2026-07-30T07:39:41Z` 原子切换至 `20260730-153343`，runtime 保持
  `20260730-092236`，四容器启动时间未变化，直接 H5 回滚点为 `20260729-113831`。候选
  123 个资源与 6 组历史 release 无 immutable collision，追加合并后 199 个资源；切换后
  839 次历史资源公网检查通过。
- 公网根 H5、兼容路径、SPA、API live/ready 与 WMS 均为 200，三个隔离路径为 404；线上
  index/主 JS/主 CSS 哈希与服务器候选一致，缓存策略、Nginx、域名 TLS 和 9/9 core readiness
  通过。应用内浏览器在 390×844、360×740、760×390 验收线上未登录首屏：无横向溢出，窄屏
  可滚动，可见控件不小于 44px，console warning/error 为空。小程序未上传、提审或发布。

## 2026-07-30：发布包增量上传

- 新增 `scripts/upload_release_artifacts.sh`。生产发布仍在本机构建完整、不可变、commit-bound 的
  `images.tar`，服务器仍只 `docker load` 和 `compose up --no-build`；没有把依赖安装或镜像构建迁到
  3.6 GiB 生产机，也没有改变 manifest/verifier、SHA 或回滚边界。
- 上传时优先把上一健康 release 的服务器 `images.tar` 作为只读 rsync basis，只传变化块；相邻两次
  现有归档实测约 `99.34%` payload 可复用，新增约 `15.8 MB`。basis 缺失时明确输出 full 并回退完整
  上传，不会跳过最终 SHA 校验。
- 脚本支持 `--dry-run`，只上传 8 个允许的发布文件，远端文件固定 `root:root 0600`；上传完成后复验
  新归档，seeded 模式还会复验旧 basis 未被修改。下一次发布前需保留当前 runtime incoming 的
  `images.tar + images.tar.sha256`，新版本激活后即可精确删除更旧 incoming。
- 同轮加固 `delta_build_images.sh`：四个基础镜像必须共享同一 commit/tag/role/amd64；
  依赖锁、完整 Dockerfile、Speaker Model requirements/patch/exporter 或 `.dockerignore` 变化时
  fail closed 走完整构建。增量 Agent 补齐 KWS 词表，Speaker Model 补齐服务代码，四个构建均
  `--network=none --pull=false`。

## 2026-07-29：短期上下文与自然表达修复（已提交、推送并部署）

- 生产证据确认，同一语音会话内“讲笑话 → 好冷啊”和“学英语 → 咖啡店场景 → 如何点咖啡”
  的权威 ASR 均正确，但说话人是 `uncertain / no_active_profile`；旧 `ContextAssembler`
  因非 owner 只保留当前用户一句，导致模型把追问当成独立问题。
- `ContextManager` 现为用户与 actual-heard 助手消息绑定 `owner / public` scope。
  `guest / uncertain` 可消费末尾连续的公开工作记忆；遇到任何 owner scope 立即截断，
  仍不能读取主人历史、私人记忆、Persona、工具或获得 `history_eligible`。两条用户原始场景
  与 owner→public 隔离边界均有回归测试。
- Qwen 情绪 sidecar 的同一话轮多段结果不再逐条覆盖。Agent 最多缓存 8 段、按 turn
  聚合后只在提交时发布一个 fenced `emotion_observation`；单一稳定非中性标签可控制本轮
  `supportive / happy / curious` 表达，但不升级为持久情绪事实，迟到结果不能污染下一轮。
- Qwen sidecar 的 PCM 队列现携带采集时的话轮 epoch，provider 的迟到 `speech_started`
  按已发送 PCM 顺序绑定，不再读取最新 active turn。助手 actual-heard scope、`SpeechPlan`、
  TTS 引用上文与 session config 也按原始 turn/generation 冻结；迟到控制确认音不会覆盖新回答。
- Provider-neutral `SpeechPlan` 已合并进唯一的 LLM/TTS 控制面：默认使用熟人对话式自然
  口语，关切、轻松、思考场景采用有界语速差异；明确的用户命令可选择悲伤、生气、四川话、
  北京话、撒娇、暧昧、争辩、夹子音、快慢与音调高低。命令解析只消费白名单结构，叙述
  “她在撒娇/他们在吵架”不会误改助手声音。
- 豆包 `context_texts` 已按 2026-07-29 官方双向 WS 文档重新接线，但受
  `DOUBAO_TTS_STYLE_CONTROL_ENABLED=false` 默认门禁保护：仅 `seed-tts-2.0` 预置音色可用，
  个人复刻无条件清空。语速走 `audio_params.speech_rate`，音调走 `post_process.pitch`；
  风格和上文合并成一条短 `context_texts`，真实朗读内容仍只在 `TaskRequest.text`。生产
  `/etc/memoria-agent.env` 已在通过五音色 canary 后显式设为 `true`。
- 引用上文只取当前 `owner/public` scope 尾部连续的已提交用户 final 与 actual-heard 助手文本，
  当前用户 final 先脱敏且始终保留；遇到 scope 边界立即停止。不启用 provider `section_id`，
  不发送 SSML 或未公开的 `[laughter]`/`[laugh]` 标签。真实笑声仍是软能力，不能把文本
  “呵，”称为确定性真笑。
- 开关开启时 Provider smoke 会额外验证风格+引用上文，并硬要求原始
  时间戳为现有运行时支持的 `ok/scaled`，继续拒绝 `degraded`；required smoke 会遍历五个
  批准音色，并分别用 FunASR 回识别目标正文、拒绝引用上文关键词。2026-07-29 候选对照
  证实无 `context_texts` 时部分音色也会出现 `scaled`，因此 raw-only 不能作为风格回归判据。
  开关缺失或关闭时门禁 fail closed。
  完整 `pytest -q`、Ruff、strict mypy `176 source files`、离线 E2E、H5 `242/242` 与
  production build 已通过；首个候选 `20260729-185111` 因 raw-only canary 误判未切流。
  修正后的 `20260729-193333` 已通过切流前和 readiness 两轮真实五音色 canary，并已部署；
  回滚点为 `20260729-171002`。

## 2026-07-29：注册称呼与语义表情（已提交、推送、部署与体验版上传）

- H5 与小程序的首次注册改为填写“怎么称呼你？”（示例：朋友、主人、小明）；Control API 将规范化后的称呼写入 profile。个人资料不再展示或写入“称呼 / 想让伙伴怎样陪你”，旧 `bio` 字段只保留 API/数据库兼容。
- Control API 仅经内部 session policy 将称呼交给 Agent；Agent 仅在当前说话人已确认是 `owner` 时把它作为不可执行数据加入上下文，`guest / uncertain` 不会得到称呼。
- Agent 在实际播放开始前按 delivery plan 与回复语义发布一次带
  `session_id / turn_id / generation_id / tool_epoch` 的 `assistant_expression`
  （`neutral / happy / curious / caring`）。H5 与小程序都缓存乱序事件、只在匹配的 speaking fence 内显示，并在结束、断线或系统中断时清除；小程序 Gateway 已有定向转发回归。
- H5 伙伴切换列表和小程序五个陪伴方式缩略图都使用表情骨架，避免只有无脸机身。H5 本地预览已验证注册、选角、首页称呼与伙伴切换；微信开发者工具已编译 auth/home/profile WXML/WXSS，首页和“我的”模拟器画面正常、console 的 error/warn/fail/exception 过滤为空。
- 本地门禁：H5 `242/242`、production build；小程序 `80/80`、相关 JS syntax、
  `git diff --check`；Control API/Agent/Gateway 定向 pytest `217 passed`。commit-bound
  source/H5/四个 `linux/amd64` 镜像/manifest 在本机和服务器均复验通过。
- 生产 `20260729-113831` 已通过隔离 H5/SPA/API/SQLite restart smoke、LiveKit、Doubao、
  FunASR、Qwen、Interrupt Semantic、readiness 与 Nginx 门禁；四个 runtime 容器 healthy，
  公网 H5/API 均为 200。现有 Chrome 已验证注册页实际渲染“怎么称呼你？”及示例占位符，
  console 无 warning/error。
- 切换前 SQLite 一致快照、PostgreSQL custom dump 和四份 root-only env 备份位于
  `/var/backups/memoria/runtime-switch-20260729-113831-from-20260729-093337-20260729T040304Z/`；
  不自动回滚数据。

## 最新实现

- 长期记忆新增独立的 `memory_kind / domain_category / item_kind`，搜索投影携带实体、
  有效期、观察时间、稳定度、重要度、敏感度、冲突状态、检索分数和全部来源。
- EpisodeConsolidator 可跨会话合并同一现实事件，同时保留独立 timeline/evidence；
  canonical key 按主题领域隔离，明确不相交实体与不同事件保守分开。
- 程序技能具备候选、版本、主人审批、单次运行确认、严格 JSON Schema 子集、工具白名单、
  顺序步骤、完整运行审计和逆序补偿。Skill definition/version/run 是持久业务状态；
  只有统一搜索文档可重建。尚未自动接入实时 Agent 工具链。
- Mem0 只运行隔离影子评测，TurboVec 只作为可选实验索引；二者均不写权威记忆。
  pgvector 按 embedding model/dimensions 隔离并建立 partial HNSW，维度漂移 fail closed。
- 账户导出、删除、PostgreSQL dump/restore 与投影重建已覆盖 Skill 状态和搜索文档。
  设计与边界见 `docs/adr/0028-typed-memory-projections-and-derived-experiments.md` 和
  `docs/implementation-plan-20260728-memory-architecture.md`。
- `UtteranceRouter` 仍是 enroll、纯打断、打断后继续提问和普通聊天的唯一副作用入口。
- 小程序改为受控话轮：AI 处于 thinking、tool waiting、speaking、recovering 等响应状态时
  暂停 `RecorderManager` 上行；Agent 回到 listening 后仍等待本地 pending PCM 和全部
  WebAudio source 结束，才恢复录音。
- 用户主动静音优先，回答结束不会擅自重新开麦；小程序播放期录音、口头打断和旧的
  `interruptPlayback` 话轮入口已删除。当前本地候选保留独立“停止播放”按钮，但它不会
  同时开麦或恢复全双工。
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

## 2026-07-28：语音架构最终收口（仓库 checkpoint，runtime/H5 未部署）

- 小程序新增默认 150 ms、可配置的播放尾音保护；播放 lead 在 100–180 ms 间自适应；
  `input_policy/policy_epoch` 成为服务端录音权威，用户静音和本地播放器仍是客户端安全门。
- Gateway AEC 支持 `off/on/alternating`，正常半双工默认关闭；A/B 分组按 session 稳定，
  `ready.aec` 返回 variant 与实际 APM 状态。真机结果记录模板见
  `docs/acceptance/miniprogram-half-duplex-device-matrix.md`，当前全部仍为待验收。
- 小程序保留独立“停止播放”按钮：本地平滑清空后调用既有 stop-response 推进 generation；
  不恢复播放期录音或语音打断。普通半双工回答限制为 3 句/120 字，明确长内容请求保留
  长回复预算。
- H5 已抽出 `VoiceTransport`、`LiveKitCascadeTransport` 与 `voiceSessionReducer`；
  Omni 移入 `voice/experimental/`；回顾页、资料页、偏好行和 14 个领域 API 模块已拆出，
  旧 `api.js` 只保留兼容导出。
- H5 与 Cascade 共用 `packages/contracts/h5-interruption-corpus.json`，句首控制意图不会再
  误伤“我等一下再说”“这个站不是终点”“不是所有人……”。
- 后端新增包装现有 `GenerationFence` 的不可变 `CancellationContext`、provider Handler
  seam、单调 `turn_revision` 和最小 Realtime-style facade；facade 只支持文本映射与取消，
  音频仍由 LiveKit/MiniProgramMediaGateway 承载，没有第二套 runtime。
- ASR 保持 LiveKit `STT` adapter，并由统一构造 seam 隔离供应商初始化；编排层直接调用的
  LLM/TTS 使用窄 Handler/Protocol。首个用户权威 final 或助手 actual-heard final 会关闭
  对应 fenced turn 的修订流，避免 UI 终稿和长期历史分叉。
- HF `speech-to-speech` 只作为 Handler、取消、revision 和协议设计参考；不引入其 runtime、
  本地 STT/TTS 模型或 WebRTC。决策见
  `docs/adr/0027-hf-speech-to-speech-as-design-reference.md`。
- 本地门禁：Python `1345 passed, 27 skipped`；Ruff；165 个 strict mypy source；
  H5 `241/241` 与 production build；小程序 `76/76`、JavaScript syntax、共享 JSON；
  离线 E2E 与 `git diff --check` 全部通过。
- 现有 Chrome 已验证首页、回顾页、个人页、编辑弹层和偏好保存/恢复，console 无
  warning/error；未触发真实麦克风权限或设备语音链路。
- 微信开发者工具 skill `0.3.5` 与当前工具版本一致、登录有效；小程序首页 WXML/WXSS
  编译成功，`pages/home/index` 整页编译打开，console 的 error/warn/fail/exception
  过滤为空，模拟器截图无白屏、遮挡或明显布局回归。
- `0.8.59`、`0.8.60` 与 `0.8.61` 均由已登录的微信开发者工具官方 CLI 上传开发测试版；
  最新 `0.8.61` task 为 `confirmation_upload_3e652991-38c2-4c75-ae65-d16433d1f3a5`，
  返回 `status=success / execution_success`，总包 `637,237` 字节。未提审或正式发布。
- 三阶段代码已提交并推送仓库；生产 runtime/H5 仍是本文件“当前状态”所列版本。

## 2026-07-28：`0.8.59` 首播后断开与向后兼容修复

- 真机点击开始语音后，生产 Gateway 均先完成 `ack_sent` 与 `ready_sent`，随后分别在约
  `1503 ms`、`1558 ms` 进入 `protocol_error`；Agent 已加入房间并开始欢迎语，因此不是公网、
  ticket、LiveKit 或 Provider 启动失败。
- 根因是 `0.8.59` 的 `first_playback` 遥测新增 `target_lead_ms`、`underflow_count`，而生产
  `20260728-170236` 仍使用旧的严格字段白名单。将真实首播形状送入该生产版本校验器可稳定复现
  `invalid client audio trace`；去掉两个新字段后立即通过。
- 客户端遥测出口现默认使用 v1 名称与字段；只有 Gateway 在 `ready` 中明确广告
  `client_audio_trace_version >= 2`，才发送 `miniprogram_playback_lead_adjusted` 及两个新字段。
  新 Gateway 广告 v2，未来版本仍可保留完整自适应播放指标；旧 Gateway 无需先部署即可兼容新客户端。
- 修复后跨版本反馈环已验证：当前客户端生成的 `first_playback` payload 可被生产
  `20260728-170236` 校验器接受。小程序 `75/75`、Gateway 全套测试、共享契约测试、Ruff、
  strict mypy 165 个 source、JavaScript syntax、JSON 与 `git diff --check` 通过。
- `0.8.60` 上传 `--dry-run` 已通过；随后经用户确认，由微信开发者工具 CLI `upload` 成功上传，
  task `confirmation_upload_64566f4f-070a-4d1f-a788-5da39349889b` 返回
  `status=success / execution_success`，总包 `637,083` 字节。未提审或正式发布；立即恢复真机连接
  不要求先切换生产 Gateway。

## 2026-07-28：无 VPN 网络诊断、标准 443 尝试与回切 8443

- 同一 iPhone 的 `0.8.60` 开 VPN 后，session `9477a320-7a46-495f-b263-629859105ea0`
  完成 Gateway `ack_sent/ready_sent`、欢迎语、用户 VAD/ASR、LLM、TTS、首播遥测和按钮停止，
  证明客户端协议修复与完整语音链路有效。
- 关 VPN 后，多次点击仍能通过 HTTPS 创建 session 并重取 gateway ticket，例如
  `2568674a-4544-4bab-bf2d-5e05eb2ce602`、`23636276-2b7d-4f12-b758-94f3a91eb2f4`、
  `47d7e7f9-f949-459c-b570-3da96c706fd4`，但 Gateway 完全没有 WebSocket
  `connection open/ack_sent`。
- 针对手机公网来源 `222.94.122.53` 的双向抓包确认：到达服务器的普通 8443 HTTPS
  请求均完成 TCP 三次握手、TLS 双向传输和正常 FIN，服务器没有 RST、限流或防火墙拒绝；
  这些连接与 Nginx 中的 profile/session/ticket API 一一对应，没有独立
  `wx.connectSocket` Upgrade。阿里、腾讯、Cloudflare、Google DNS 均只返回同一 IPv4
  `122.51.108.140`，没有 AAAA 分流。
- 结论：connection refused 发生在手机当前 Wi-Fi/无 VPN 的非标准端口 Socket 直连路径，
  不是 Gateway、Nginx、TLS、DNS、ticket 或应用协议错误。切换前生产 443 的同一 Upgrade
  探针稳定返回 `404`，形成可执行的红色反馈环。
- 本地候选新增单一共享 `infra/nginx-memoria-miniprogram-media.conf`，只包含精确媒体 WSS
  location；8443 Memoria server 与未来 WMS 443 server 复用该 snippet。Control API 示例 URL
  改为 `wss://aigcnice.com/memoria-mini-media/v1/mini-program/media`。没有迁移 H5、Control API、
  LiveKit 或 WMS 根路径。
- 生产配置/升级环境测试 `39/39`、Ruff、Bash syntax 与 `git diff --check` 通过。生产已安装
  443 snippet、更新 Control API env、reload Nginx，并只重建 Control API；首次 readiness
  `503` 明确为等待 Agent 新心跳，14 秒后自然恢复 `ready / 9/9 core`。
- 用户授权暂时停止 WMS 并将资源留给 Memoria：`wms.service` 当前为 `inactive / enabled`，
  8090 已关闭，WMS 数据和配置未删除。root-only 回滚目录为
  `/var/backups/memoria/miniprogram-443-20260728-214525`；WMS 配置按真实软链解引用备份。
- 443 与 8443 的无效票据 Upgrade 均返回 `101`，443 根仍为 `302`、未知路径仍为 `404`；
  独立公网主机已直连 443 获得 `101`。同一 iPhone 关闭 VPN 的
  `ack_sent → ready_sent → first_playback → listening` 仍是最终验收。
- 真机首次切到 443 后页面显示“证书校验失败”，但 443/8443 实际下发同一张
  `aigcnice.com` 证书和三证书链；独立公网 OpenSSL 与 macOS Apple 信任库均验证成功，
  SAN、CT 和 Certum 交叉签发链正常。根因是客户端把任何包含 `handshake` 的原始错误都误分类为
  certificate，当前文案不能证明 TLS 证书失败。
- `0.8.61` 已将 SSL/TLS/certificate 与普通 WebSocket handshake 分开，后者保留
  截断后的原始 `errMsg`；回归测试先红后绿，小程序 `76/76`、JavaScript syntax、
  `git diff --check` 和上传 `--dry-run` 均通过。用户确认后由微信开发者工具上传成功，
  task `confirmation_upload_3e652991-38c2-4c75-ae65-d16433d1f3a5` 返回
  `status=success / execution_success`，总包 `637,237` 字节；未提审或正式发布。
- `0.8.61` 真机在 443 仍返回 SSL/TLS 错误；同期抓包显示手机完成 TCP 三次握手并发送约
  517 字节 TLS ClientHello，随后手机或当前网络在确认服务器 TLS 数据前发送 RST，Gateway
  和 Nginx HTTP 层均未收到请求。同一 iPhone 关闭 VPN 后用 Safari 打开 443 也失败，证明问题
  不属于 `wx.connectSocket`、票据或 Gateway。
- 用户决定继续使用 8443。生产 `MINIPROGRAM_MEDIA_GATEWAY_URL` 已回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`，仅重建 Control API；
  readiness 首次因等待 Agent 心跳返回 503，14 秒后恢复 `ready / 9/9 core`。回切前 env 备份为
  `/var/backups/memoria/miniprogram-media-url-8443-20260728-222317`。WMS 继续
  `inactive / enabled`，8090 关闭；443 Nginx 精确路由保留但当前不下发。
- 回切后用户已确认页面与语音连接恢复可用；该确认关闭 8443 主链连通性故障，但不替代
  外放/听筒/蓝牙、弱网、前后台、系统录音中断和 AEC A/B 的完整声学矩阵。

## 2026-07-28：游客浏览与微信身份（已发布，真机验收待完成）

- 三个主 Tab 未登录可浏览，不再启动即跳登录。游客态不创建服务端匿名账号；回顾页不读取
  或展示历史，“我的”页不读取资料和统计。
- 对话、生成回顾、修改资料、数字分身与语音授权统一经过 `utils/auth-gate.js`，并保留登录
  返回路径。登录页使用 `wx.login`、手机号授权、微信昵称、微信头像和平台隐私保护指引。
- 退出或清除身份会广播到隐藏的陪伴 Tab，立即作废仍在等待中的语音启动、关闭媒体并清空
  当前字幕；首次手机号登录前的 openid 探测命中 `phone_authorization_required` 后不在登录页
  重复请求。
- 认证状态使用单调 `authEpoch` 约束所有私有异步回调；退出、401 或重新登录后，迟到的回顾、
  资料、统计、隐私授权和数字分身响应不得重新写回游客页。
- 短期 access token、到期时间和最小身份快照可本地恢复；token 失效后，已存在的 openid
  身份通过 `wx.login` 静默换取新会话，首次身份才要求手机号；微信平台
  `40014/42001` 会清除服务端 access token 缓存并只重取一次。
- Control API 新增微信登录、头像上传/读取和微信注销复核。`openid` 沿用 EchoLife 的
  `wx_<sha256(openid)[:24]>`，手机号只保存独立 HMAC 与脱敏值；H5 用户名密码入口保留。
- `scripts/migrate_echolife_users.py` 已支持 EchoLife JSON、备份和 SQLite，支持 dry-run、
  目标 SQLite 在线备份、幂等身份导入和可选本地头像复制；旧故事/时间线不写成 Memoria
  对话。
- 本机真实目录 dry-run：扫描 4 个源文件，发现 1 个旧身份、0 冲突；该身份来自开发备份，
  头像为不可迁的 `wxfile` 路径，4 份旧内容源保持 deferred，因此未正式写入本机或生产。
- EchoLife 旧生产主机是 `110.42.235.198`，Memoria 当前生产主机是
  `122.51.108.140`，两者不得混用。2026-07-25 17:00 UTC 清理旧主机前曾把完整
  `/opt/echolife` 打成 39 MiB 的 root-only 回滚归档；约 4 分钟后该唯一归档也被永久删除。
  当前旧主机、现存服务器备份及 Memoria 冻结快照均不含 EchoLife 数据。若需恢复正式旧用户，
  只能从腾讯云实例 `ins-d5nngvzh` 在删除前的云硬盘快照提取；禁止在仍运行其他生产服务的
  ext4 根盘上直接执行 undelete。
- 当前生产 Memoria SQLite 有 96 个 profile、4 个注册账号、0 个 external identity、0 个 avatar，
  尚未执行旧用户迁移。不能把本机仅含 1 个开发身份的备份冒充正式迁移。
- AppID 已确认是 `wx20a3a044b52fcbb7`；产品负责人已重新提供现有
  `WECHAT_MINIPROGRAM_APPSECRET`，只允许写入 Memoria 生产服务器的 root-only env，不得进入命令
  输出、仓库或文档。该值曾出现在历史自动化命令记录中，体验版闭环后应在微信公众平台重置。
  `MEMORIA_WECHAT_IDENTITY_SECRET` 必须独立生成，不能复用 AppSecret 或其他认证密钥。
- 微信开发者工具已验证陪伴、回顾、“我的”三个游客页可直接渲染，页面数据均为
  `authenticated=false`，且 console 无异常；点击“开始语音陪伴”会进入带返回路径的登录页。
  生产微信登录与头像路由已上线，微信官方 access token 接口验证通过；手机号、昵称、头像、
  静默恢复和登录后语音仍需在 `0.8.58` 真机验收。
- `e723023` / tag `20260728-170236` 已提交、推送并部署；小程序体验版 `0.8.58`
  已经微信开发者工具确认上传，包体 `631,692` 字节，未提审、未正式发布。

## 生产健康

- `agent / control-api / speaker-model / miniprogram-gateway` 四容器均为
  `healthy`、restart 0。
- readiness 为 `ready / 20260728-170236`；9/9 core checks、Agent heartbeat、
  LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过。
- 四个 runtime 容器最近十五分钟未出现 traceback、关键 provider、provenance、
  archive durable/spool 或连接拒绝错误。
- 公网根 H5、兼容 H5、SPA、API live/ready 与 WMS 静态页为 200；
  `/memoria-api/internal/` 为 404。
- 小程序公网 WSS 在标准 443 和回滚 8443 均成功升级；无效 TLS Upgrade header ticket
  按协议关闭为 `4401`，证明请求头已透传至 Gateway。
- Nginx 配置、readiness timer 与 certbot timer 正常；WMS 暂时为 `inactive / enabled`，
  8090 已释放。IP 证书有效至
  `2026-08-03 01:40:00 UTC`，域名证书有效至 `2026-10-18 03:59:59 UTC`。
- PostgreSQL、MinIO、LiveKit、WMS、数据库快照与 Docker 数据卷未在清理中修改。

## 2026-07-28：Gateway 握手验收日志上线（真机无 VPN 复测待完成）

- `33a741b` / tag `20260728-123528` 已推送并原子切换 runtime；本轮只让 Gateway 的脱敏
  `ack_sent` / `ready_sent` 等 `INFO` 日志走 Uvicorn 的 `uvicorn.error` handler，不改协议、
  小程序包、H5、Nginx、数据库或 provider 配置。新增回归测试断言 header 握手日志恰有两条且不含 ticket。
- source、四个 linux/amd64 镜像、H5 artifact 的 manifest/verifier、服务器导入校验与隔离
  H5/API/SQLite restart smoke 均通过；SQLite 双副本 SHA-256 为
  `a285511d8e26413263e792b9312ad4a0304e7f098b3d7d9acf3f2477a309176f`，四份 root-only env
  回滚副本已创建。
- 四容器 healthy，ready 绑定 `20260728-123528`；LiveKit、FunASR、Qwen、Doubao 和 5 条
  InterruptSemantic smoke 最终通过。首次 refresh 的语意分类请求在 0.6 秒上限内瞬时超时，立即重试
  已通过；需继续观察，不把一次超时写成代码回归。
- 发布后用无效 ticket WSS smoke 验证 `close_code=4401`，容器日志可见
  `mini_program_gateway_handshake ... phase=ticket_rejected`，证明新增 `INFO` 输出已经生效。
- 用户关闭 VPN 的实时取证显示 TCP SYN 到达服务器，且其中两次已进入 Gateway；独立宽带的
  `8443` TLS/WSS 也获得 `101`。因此不能把当前现象归因为“8443 全局被封”。443 外部 TLS 尚有
  ClientHello 后断开证据，未迁移入口，避免影响 WMS；下一步只以同一 iPhone 无 VPN 的
  `ack_sent` / `ready_sent` 日志定性。

## 2026-07-28：小程序 handshake acknowledgement 竞态修复（体验版已上传，真机待验收）

- `569e225` / tag `20260728-131612` 已推送。客户端现将有效 `handshake_ack` 视为已认证传输确认：
  取消此前泛化 `SocketTask.onError`，等待严格校验后的 `ready`；ack 后的泛化 error 不再覆盖具体
  close code。
- 新增 10 秒 bridge-ready deadline；超时返回 `gateway_ready_timeout` 并关闭 socket。失败、关闭或显式
  停止后，迟到 `ready` 不得重新启动录音。
- 小程序完整测试 `58/58`、JavaScript syntax check 和 `git diff --check` 已通过；独立复核未发现
  P0/P1。体验版 `0.8.57` 已在微信开发者工具确认后上传成功（约 600 KB），无需重新部署服务端。
- 尚未完成：同一 iPhone 关闭 VPN、完全退出后重新进入 `0.8.57`，单次点击“开始语音陪伴”验证
  Gateway `ack_sent → ready_sent` 和页面 listening；这项真实验收不能由上传成功替代。

## 2026-07-28：小程序 WSS 首包缺失与 header 握手修复

- 体验版 `0.8.54` 已上传成功，但同一 iPhone 仍报“connection refused”。真实抓包已证明：手机
  已完成 `8443` TCP/TLS 和 WebSocket Upgrade，Nginx 已转发至 loopback `8792`；随后约 2.9 秒
  没有任何客户端 WebSocket 数据（未发送首条 JSON `hello`），客户端才发送 close/FIN。网关首包
  超时为 10 秒且没有主动关闭。根因不在网络、证书、Nginx、Agent 或 ticket，而是原生
  `SocketTask.onOpen → hello` 握手路径在真机上未可靠执行。
- 修复：新客户端把同一短期、签名 gateway ticket 与 generation 能力放入 TLS Upgrade header；
  网关在 Upgrade 后直接验证并发送 `ready`。旧客户端的首条 JSON `hello` 仍保持回退，ticket 不进
  URL、access log 或应用日志。纯 `connection refused` 才触发一次 ticket 刷新；混合 timeout/refused
  不再被误归类重试。
- 已完成：网关单测 `6/6`、小程序 `51/51`、Ruff、mypy、JS syntax 与 diff check；runtime
  `20260728-103318` 已先行上线，commit-bound source/images/H5 manifest、隔离 smoke、SQLite
  双副本、四份 env 回滚副本、readiness、Nginx/WMS 和无效 header `4401` WSS smoke 均通过。
  体验版 `0.8.55` 随后上传成功；服务器临时抓包已删除。
- 尚未完成：同一 iPhone 使用 `0.8.55` 实际点击“开始语音陪伴”并确认收到 `ready`、不再显示网络
  拒绝；这项真实设备验收不能由上传成功或无效 ticket smoke 替代。

## 2026-07-28：`0.8.55` 仍显示连接拒绝的二次修复（已上线，真机待验收）

- 同一 iPhone 的后续点击仍显示泛化的“connection refused”。Nginx 时间线同时显示同一设备在短时间内
  重复创建会话，并在第四次 `POST /memoria-api/v1/sessions` 命中 `429`。由于 `8443 → 9443` 的
  stream 转发让内层 HTTP 看见 loopback 地址，旧 `burst=3` 会放大重复启动；这不是唯一根因，不能只靠
  放宽限流掩盖问题。
- 候选修复把 `startVoice()` 改为脱离 `setData` 异步刷新的单飞 Promise，避免一次点击或紧邻点击创建多
  个 session；精确会话路由仅将安全的 `burst` 从 `3` 提到 `6`，保留相同 `10r/m` 限速。没有把未验证的
  `Authorization` header 用作 Nginx 限流身份；那会允许伪造不同 header 绕过桶。真正的按账号限流应在
  Control API 完成 JWT 校验之后再单独实现。
- 新客户端继续发送 TLS Upgrade header，同时在 `SocketTask.onOpen` 发送既有 JSON `hello` 回退；Gateway
  无论 header 或 hello 验证成功，都会先回无敏感数据的 `handshake_ack`，再等待 LiveKit bridge 并发送
  `ready`。旧客户端和旧 Gateway 均保持互操作，ticket 不进入 URL、access log 或应用日志。
- 处理两个真机时序：`ready` 后不再补发 hello；header 认证的服务端只忽略一次精确的迟到 v1 hello，即使
  首个 PCM 已到达。`SocketTask.onError` 先到时会给 `onClose` 100 ms 传递 `4400/4401/1011` 的机会，避免
  有意义的 gateway 拒绝码被泛化成“connection refused”。
- Gateway 只新增脱敏的 `transport=header|hello`、`ack_sent|ready_sent`、耗时日志，用于下一次真机验收；
  不记录 ticket、PCM、用户文本或 cookie。
- 候选本地门禁：小程序 `56/56`；Gateway 与生产 Nginx 契约 Python `81/81`；Ruff、严格 mypy、JS syntax、
  JSON 解析和 `git diff --check` 均通过。Python 开发环境已由锁定的 `uv` 重新创建为 CPython `3.12.13`，
  原 `.venv` 指向已删除的 Homebrew Python 3.12，未使用系统 Python 3.13 代替。
- 已完成：`57eefdc` 与 tag `20260728-114049` 已推送；commit-bound source、四镜像和 H5 artifact
  均在服务器 manifest 校验、隔离 smoke、SQLite 双备份、四份 env 备份后原子切 runtime。新 runtime
  的四容器 healthy，9/9 readiness、LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过；
  Nginx 精确 session `burst=6` 已 reload，公网 H5/API/WMS 为 200，WSS 无效 ticket 为 `101 → 4401`。
- 小程序 `0.8.56` 已于微信开发者工具确认后上传成功（约 599 KB）；真机验收仍必须观察 Gateway
  `ack_sent` 与 `ready_sent`，并确认页面进入 listening。若仍失败，按同一时间窗口取 Gateway 脱敏日志和设备
  原始 `errMsg`，不再猜测网络问题。

## 保留版本与回滚

- 当前 runtime：`20260728-170236`。
- 直接回滚 runtime：`20260728-123528`。
- 固定 H5：`20260723-192611`。
- 本机和生产均只保留当前与直接回滚两套 runtime 的四角色 Docker tag。
- 生产 source release 保留 `20260728-170236` 与 `20260728-123528`；
  H5 release 只保留 `20260723-192611`。
- 本 release 的 SQLite 双备份 SHA-256：
  `6b9fc7f7ead2aca3e47837a97d827de0b407fbd81a1ba784e14f48bf53d6cbd0`；
  四份 env 与 Nginx 回滚副本均为 root-only。
- 上一 runtime release 的 SQLite 双备份 SHA-256：
  `ca973329eb36519494d22076739cf4c32c102ccf2e04d5cca9c5faa423775382`
  （`/var/lib/memoria` 与 root-only `/var/backups/memoria` 一致）；四份 env 与两份 Nginx
  回滚副本均为 root-only。
- 更早 SQLite 快照 SHA-256：
  `9676292067df01121a7568b37b5f9ef5486296b7bd69703f7cbcdf76203ec006`
  （`/var/lib/memoria` 与 root-only `/var/backups/memoria` 两份一致）。
- runtime 回滚不自动恢复数据库；只有数据迁移或数据异常时才使用快照。

## 2026-07-27 清理结果

- 删除非交付 `apps/web`、`infra/Dockerfile.web` 与对应 pnpm workspace 文件，
  约减少 `5,183` 行 tracked 内容。
- 前序阶段已删除三个干净临时 worktree、已合并 hotfix 分支、旧发布工件、构建缓存、
  五套旧 source/runtime/image 和非交付客户端；累计回收约 `32 GiB`。
- 本次发布后已删除服务器 `2.2 GiB` incoming、未激活 H5 候选、过时临时候选 env、旧
  runtime/source/image `20260727-221555`；当前、直接回滚、数据库备份和两份切换证据均保留。
- 当日清理后的历史时点曾仅保留 `20260728-103318` 与 `20260727-235959` 两套 runtime/source/image；
  当前保留集以本文件“保留版本与回滚”和下节为准。
- 本机已删除本轮临时 worktree 与约 `2.1 GiB` 发布工件；未运行 Docker broad prune，也没有删除
  数据卷、数据库、其他项目镜像或用户未跟踪内容。
- 没有运行 `docker system prune -a`；没有删除卷、数据库、其他项目镜像或跨项目构建缓存。

## 2026-07-28：`20260728-123528` 发布后精确清理

- 已删除本机两套已完成发布的临时 artifact/worktree（`20260728-114049`、`20260728-123528`）和
  无容器引用的本机 image tags `20260727-221555`、`20260727-235959`、`20260728-103318`；保留当前
  `20260728-123528` 与直接回滚 `20260728-114049` 的四角色 tag。
- 已删除服务器已导入的两套 incoming/tmp 发布包、未激活 H5 候选、过时 source release
  `20260727-235959`/`20260728-103318` 及其八个 Docker image tags。服务器根分区由约 `37 GiB`
  已用降至 `28 GiB`（约 `86 GiB` 可用）。
- 没有删除当前 runtime、直接回滚、现网 H5、SQLite/环境备份、PostgreSQL/MinIO/Docker 卷或任何用户
  未跟踪文件；没有执行 `docker system prune`。本机其余 `7.0 GiB` Docker volumes 和约 `21.4 GiB`
  build cache 未逐项归属，明确保留，避免影响其他项目。

## 2026-07-28：`20260728-170236` 发布与精确清理

- runtime 已切换至 `20260728-170236`；H5 保持 `20260723-192611`，直接回滚 runtime 为
  `20260728-123528`。四容器、readiness、Provider、公网 API/H5/WMS、微信登录/头像路由及
  WSS 无效 ticket `4401` 验收通过。
- 发布前已创建 SQLite 双备份、四份 env 与 Nginx 配置备份；微信 AppSecret 只写入 root-only
  生产 env，独立身份 HMAC secret 已生成，secret 值未进入仓库或本文。
- 本机与服务器已删除本次临时 artifact/incoming、第三旧版本 source/image tag 和明确无用发布包；
  未运行 broad Docker prune，未删除 volumes、数据库、其他项目镜像或用户未跟踪文件。
- 小程序 `0.8.58` 已上传开发版本；手机号、昵称、头像、静默恢复、退出清理和登录后语音仍待真机验收。

## 2026-07-29：`20260729-093337` 记忆架构发布

- runtime 已切换至 `20260729-093337`，H5 保持 `20260723-192611`，直接回滚 runtime 为
  `20260728-170236`。
- 四容器 healthy/restart 0；9/9 core readiness、LiveKit、FunASR、Qwen、Doubao、
  Interrupt Semantic、公网 API/H5/WMS 与 WSS `4401` 门禁通过。
- 生产 PostgreSQL 已创建 5 张强制 RLS 的 Skill 表、10 个类型化投影字段、多来源表和
  `text-embedding-v4 / 1024` partial HNSW；当前向量表为空，无历史维度迁移。
- 发布前 SQLite/PostgreSQL 与四份 env 回滚副本位于 root-only
  `/var/backups/memoria/runtime-switch-20260729-093337-from-20260728-170236-20260729T014220Z/`。
  固定工件、镜像 ID、备份哈希和保护性首次尝试见
  `docs/releases/20260729-093337.md`。

## 2026-08-03：媒体运行面评审整改（代码完成，外部验收待补）

- 已根据评审线程 `019fc681-3b5b-7e93-b794-44024f7d8901` 收敛媒体控制面：gRPC 队列消费 ACK、generation/stream epoch fence、重连 grace、旧连接竞态去重、ASR final 自动进入 `commit_user_turn → generate_reply`、Router 控制词停止、provider 协作取消和 20 ms/24 kHz 固定 TTS 帧。
- Playback Ledger 现在按已登记的 audio sequence/sample range 验证客户端进度；H5 WHIP/fetch 有超时、事件要求完整 fence、播放进度单调且不会越过已接收音频。Control API 的 HTTP stop 已派发至 Media Edge；Redis CAS 和 Go JWT 增加 generation/account/device/client_type 绑定。
- 修改文件集中在 `services/agent/src/voice_core/`、`services/control_api/app/`、`services/media_edge/`、`apps/h5/src/voice/`、生产 Compose/env 和 media bridge/SLO 脚本；未修改或清理用户工作区未跟踪文件。
- 已验证：Agent unit `1607` 通过；Control API 全测试通过；Go `test`、`vet`、`race` 通过；H5 `269` 测试和 production build 通过；`scripts/media_runtime_smoke.py` 通过；Ruff、严格 mypy 和 diff check 通过。
- 全仓测试结果 `1615 passed, 29 skipped`；现有总覆盖率 `80.95%` 仍低于 `85%` 门槛，因此没有把覆盖率门禁降级。真实 WebRTC/WHIP/RTP/DTLS/SRTP、生产 provider factory、浏览器/硬件播放 ACK、Redis/coturn 多实例和 SLO 演练仍是发布前阻塞项。

## 验证

- Ruff：通过。
- `mypy services --strict`：175 个 source files 无问题。
- Python（含真实 PostgreSQL/pgvector、RLS、Skill、账户治理和恢复演练）：
  `1421 passed, 2 skipped`。
- 固定中文记忆评测集覆盖 13 类场景且无失败。当前规则基线：
  Extraction Precision `0.3947`、Recall `1.0000`、Recall@5/10 `0.1538`、
  nDCG@10 `0.1538`、Temporal Accuracy `1.0000`、Source Attribution `0.9333`、
  Cross-account/Candidate/Contradiction Leakage 均为 `0`。
- H5：`241/241`，production build 通过；现有 Chrome 的首页、回顾、个人、编辑和偏好交互
  已通过，未见 console warning/error。
- 微信小程序：`76/76`，全部 JavaScript syntax check 和共享 JSON 契约解析通过；此前
  开发者工具中的三个游客 Tab 与登录页已打开且无 console 异常；当前候选首页 WXML/WXSS
  及整页编译也已通过。`0.8.59` 已确认存在首播遥测兼容故障；诊断后的 `0.8.61` 已上传，
  生产回切 8443 后用户确认连接恢复。
- `scripts/run_e2e.py --profile offline`：通过。
- Agent Linux/amd64 镜像以 `--require-hashes` 成功构建；`vosk==0.3.45` 和控制词文件
  均进入镜像，官方模型通过宿主机只读挂载。
- `vosk-model-small-cn-0.22` 官方模型页标记 Apache-2.0；归档 SHA-256 为
  `3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba`。
  成品 Linux/amd64 Agent 镜像实测“停一下”命中、“等一下我想问……”拒绝。
  模型未进入仓库、镜像或发布包；生产安装路径为
  `/var/lib/memoria-agent/models/vosk-model-small-cn-0.22`，目录/文件权限为
  `0750/0640`，owner/group 为 `65532:65532`。
- commit-bound manifest/verifier、镜像导入、候选 H5/API/SQLite restart server smoke、SQLite
  双副本、四份 env 回滚、生产 Provider/readiness、公网 H5/API/WMS/TLS/WSS header smoke 均通过。
- 本次工件 SHA-256、镜像 ID、备份和生产验收见
  `docs/releases/20260729-093337.md`。
- GitHub Actions run `30416225947`：Python `1421 passed, 2 skipped`、全 `services`
  覆盖率 `89%`、Agent orchestration `91%`、provider protocols `92%`；H5 与小程序 job
  亦通过。CI 通过 GitHub service container 提供真实 PostgreSQL/pgvector，容器 ID 仅在
  Pytest step 注入，避免 job 级表达式解析失败。

## 未闭环与下一步

1. 当前规则检索的中文同义、人物别名和语义召回仍低，先以固定评测集改进 BM25/实体/向量
   融合，不得因单次演示切换 Mem0 或 TurboVec。Skill 自动执行需先建立安全的跨服务工具注册表，
   统一权限、generation fence、幂等和账户删除门禁。
2. 8443 回切后的主链已获用户确认；继续按
   `docs/acceptance/miniprogram-half-duplex-device-matrix.md` 完成 iPhone/Android、
   外放/听筒/蓝牙、Wi-Fi/移动网络/弱网、前后台和系统录音中断矩阵；上传成功不得冒充
   完整真机通过。后续每次记录 `ack_sent → ready_sent → first_playback → listening`
   的 session/timestamp，并复核游客三页、手机号、昵称/头像、静默恢复、退出清理与登录后语音。
3. 在明确测试窗口将 Gateway 临时设为 `MINIPROGRAM_GATEWAY_AEC_MODE=alternating`，记录
   `ready.aec` 分组、首字丢失、尾音误转写、失真、underflow 与 hard reset；结束后恢复
   `off`，没有真实 A/B 数据前不启用生产 AEC。
4. H5 仍需真实浏览器和设备验证“等等、等一下、停一下、先别说”等语意打断，
   同时覆盖“我等一下再说”等非打断语句，避免误触发。
5. 继续观察小程序 underflow、hard reset、lead 指标；只有需要定位非播放期噪声时才为
   单一 session 开启有界 AEC pre/post 采样，测试后立即关闭。
6. 单独处理全仓覆盖率门槛：优先补齐 PostgreSQL/外部边界测试，不通过降低标准换绿。

## 用户工作区边界

以下未跟踪内容属于用户，必须保留：

- `.workbuddy/`
- `apps/miniprogram/assets/bg/aurora-light.webp`
- `apps/miniprogram/assets/mascot-alpha.webp`
- `apps/miniprogram/design-preview/`
- `apps/miniprogram/package-lock.json`
- `apps/miniprogram/package.json`（仅本机上传辅助命令与依赖）
- `scripts/upload_miniprogram_test.js`
