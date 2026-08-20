# Requirements Traceability Matrix

| 规范要求 | 实现文件 | 测试文件 | 状态 |
|---|---|---|---|
| 代码默认选型 FunASR/百炼 DeepSeek-v4-flash/豆包 Seed-TTS 2.0 双向流式/LiveKit 1.6.10 | `pyproject.toml`, `config.py`, `providers/*`, `agent.py` | `test_versions.py`, `test_config.py`, `test_agent_production_wiring.py` | PASS-LOCAL |
| GenerationFence 全字段比对；旧结果丢弃 | `contracts/ids.py`, `orchestration/generation_fence.py` | `test_generation_fence.py`, `test_interrupt_isolation.py` | PASS |
| 旧 generation 音频不得播放 | `orchestrator.py`, `doubao_tts.py` | `test_interrupt_isolation.py`, `test_doubao_mock.py` | PASS-LOCAL |
| 旧 tool_epoch 结果不得播报 | `task_manager.py`, `orchestrator.py` | `test_tool_epoch_isolation.py` | PASS |
| 状态机合法/非法转换 | `orchestration/state_machine.py` | `test_state_machine.py` | PASS |
| 助手历史只含实际已听文本 | `heard_text_tracker.py`, `context_manager.py` | `test_heard_text_tracker.py`, `test_interrupt_isolation.py` | PASS |
| FunASR 协议/字级时间戳 | `funasr_protocol.py`, `funasr_stt.py` | `test_funasr_protocol.py`, `test_funasr_mock.py` | PASS |
| FunASR LiveKit `stt.STT` + `RecognizeStream._run` | `funasr_stt.py` (`FunASRSTT`, `FunASRRecognizeStream`) | `test_livekit_adapters.py` | PASS |
| 播放期无 VAD 锚 final/interim 不进入权威用户话轮；发现污染后只提交对应 speech epoch 的 accepted finals，FIFO snapshot 隔离排队回调，迟到控制回调不能清下一轮，空结果不建话轮 | `interruption_guard.py`, `duplex_runtime.py`, `agent.py` | `test_interruption_guard.py`, `test_agent_production_wiring.py`, `test_duplex_runtime_wiring.py` | PASS-PROD |
| FunASR 历史对话 context 默认关闭，旧 user/assistant 内容不得泄漏进当前识别 | `funasr_stt.py`, `config.py`, `split_production_env.py` | `test_funasr_session_edges.py`, `test_funasr_recognize_stream.py`, `test_production_compose.py` | PASS-PROD |
| 豆包 LiveKit `tts.TTS` + 双向增量 `SynthesizeStream._run` | `doubao_tts.py` (`DoubaoTTS`, `DoubaoSynthesizeStream`) | `test_doubao_mock.py`, `test_agent_production_wiring.py` | PASS-LOCAL |
| Agent entry wires Orchestrator/Fence/interrupt | `agent.py`, `duplex_runtime.py` | `test_duplex_runtime_wiring.py` | PASS |
| LLM/TTS 生产路径 GenerationFence 门控 | `DuplexVoiceAgent.llm_node/tts_node` | `test_pipeline_fence_gating.py` | PASS |
| 非预生成 turn 先提交 fence 再启动 LLM；THINKING 可接收真实新轮次 | `agent.py`, `orchestrator.py` | `test_agent_production_wiring.py`, `test_orchestration_edges.py` | PASS |
| interrupt 取消已注册 LLM/TTS task | `orchestrator.set_active_*_task` + `confirm_interruption` | `test_pipeline_fence_gating.py`, `test_duplex_runtime_wiring.py` | PASS |
| FunASR RecognizeStream 真实 mock 出 final | `FunASRRecognizeStream._run` | `test_funasr_recognize_stream.py` | PASS |
| speaking 生命周期 → HeardTextTracker | `duplex_runtime.on_assistant_speaking/on_playback_done` | `test_pipeline_fence_gating.py` | PASS |
| 工具 cancel_event 协同取消 | `task_manager.py` | `test_tool_cancel_event.py` | PASS |
| FunASR 稳定前缀 | `stable_prefix.py` | `test_stable_prefix.py` | PASS |
| 豆包连接池复用；取消发送 `CancelSession` 并丢连接 | `doubao_tts.py` | `test_doubao_mock.py` | PASS-LOCAL |
| 豆包字级字幕按完整 PCM 对齐；缩放后时间戳才进入 LiveKit | `doubao_protocol.py`, `doubao_tts.py` | `test_doubao_protocol.py`, `test_doubao_mock.py` | PASS-LOCAL |
| 百炼 DeepSeek-v4-flash 默认快/深模型；可选配置不得隐式覆盖默认 provider | `config.py`, `agent.py` | `test_bailian_deepseek_is_the_default_llm`, `test_direct_deepseek_key_does_not_override_bailian_deepseek_implicitly` | PASS |
| 中文口语分段器 | `phrase_segmenter.py` | `test_phrase_segmenter.py` | PASS |
| 附和/打断规则；播放期及播放后英文回声、异常脚本与快速打断熔断；按四类原因归档 | `interruption_guard.py` | `test_interruption_guard.py`, `test_agent_production_wiring.py` | PASS |
| 播放期候选暂停；真打断推进 generation，假打断恢复播放；基础 `min_words=0`，播放期临时封锁后恢复 | `duplex_runtime.py`, `orchestrator.py`, `agent.py` | `test_duplex_runtime_wiring.py`, `test_agent_production_wiring.py` | PASS |
| 普通话/英语语言锁与语音回复 3 句/96 字预算，单个超长首段也截断 | `prompts.py`, `agent.py` | `test_agent_production_wiring.py` | PASS |
| 原子打断取消流程 | `orchestrator.confirm_interruption` | `test_interrupt_isolation.py` | PASS |
| 打断只提交实际已听文本；无双重 generation | `agent.py`, `duplex_runtime.py`, `orchestrator.py` | `test_agent_production_wiring.py`, `test_pipeline_fence_gating.py` | PASS |
| 助手说话时麦克风持续开启 | `orchestrator.mic_open`, `apps/h5/src/hooks/useVoiceSession.js` | `test_interrupt_isolation.py`, H5 hook tests | PASS |
| 配置校验 | `config.py`, `scripts/verify_env.py` | `test_config.py` | PASS |
| 控制 API session/stop/health | `control_api/app/*` | `test_session_api.py` | PASS |
| 匿名 Bearer 身份；已存 token 先经 `/v1/auth/me` 校验 | `control_api/app/routes/auth.py`, `apps/h5/src/api.js` | `test_session_api.py`, `apps/h5/src/api.test.js` | PASS |
| 缺少认证与跨用户 memory/session 隔离 | `control_api/app/security.py`, `routes/memory.py`, `routes/session.py` | `test_memory_api.py`, `test_session_api.py` | PASS |
| 消息、Profile 与 Agent 上下文统一 PII 脱敏；FunASR 历史 context 仅保留显式实验入口且生产默认关闭 | `services/common/redaction.py`, `routes/memory.py`, `context_manager.py`, `funasr_protocol.py`, `funasr_stt.py` | `test_memory_api.py`, `test_orchestration_edges.py`, `test_funasr_protocol.py`, `test_funasr_recognize_stream.py` | PASS |
| SQLite 持久化会话控制与 release-bound readiness evidence | `control_api/app/database.py`, `routes/session.py`, `routes/readiness.py` | `test_session_api.py` | PASS |
| RTC 恢复两端原子前移 generation | `rtc-recovered` route, `agent.py`, `apps/h5/src/hooks/useVoiceSession.js` | Python/H5 recovery tests | PASS |
| legacy Web 客户端 | 源码已移除；历史行为保留在 Git 与 release 记录 | 无当前门禁 | REMOVED |
| 原生 iOS 会话与同步播放 | 已从仓库移除 | 历史构建记录 | REMOVED |
| H5 五伙伴正面机身、四种 SVG 表情与权威语音情绪驱动 | `apps/h5/src/components/Mascot.jsx`, `apps/h5/src/lib/companions.js`, `apps/h5/public/assets/companions/` | `apps/h5/src/components/Mascot.test.jsx`, `apps/h5/src/hooks/useVoiceSession.test.jsx`, 浏览器交互 QA | PASS-LOCAL |
| 注册后选择陪伴方式、表情/设计音色试听与三段 shadow 声纹登记；陪伴方式只影响助手工作风格，不定义数字分身 | `CompanionOnboarding.jsx`, `companions.js`, `services/common/companions.py`, `routes/memory.py`, `routes/speaker.py`, `routes/voice.py` | `CompanionOnboarding.test.jsx`, `App.test.jsx`, `test_memory_api.py`, `test_voice_profile_api.py`；生产一次性账号验证混合方案与双视口 | PASS-PROD-BROWSER / VOICE-ENROLLMENT-NOT-EXECUTED |
| `companion / self_preview / legacy / archive` 四模式由服务端 `ModePolicy` 约束；依赖未满足时 Self Preview/Legacy 明确 blocked，Archive 不创建语音会话 | `services/control_api/app/mode_policy.py`, `routes/interaction.py`, `routes/session.py`, `apps/h5/src/components/InteractionModePanel.jsx` | `test_interaction_api.py`, `DigitalSelfPanel.test.jsx`, `api.test.js`；生产能力令牌负向门禁 | DEPLOYED / PASS-LOCAL / PROD-NEGATIVE-GATES |
| 会话冻结 mode/policy/Companion Style/未来 Digital Self、Relationship、Legacy 引用；伙伴切换仅影响下一会话 | `services/control_api/app/database.py`, `routes/session.py`, `services/agent/src/mode_policy_client.py`, `agent.py`, `InteractionModePanel.jsx` | `test_interaction_api.py`, `test_interaction_mode_agent.py`, `test_mode_policy_client.py`, H5 interaction-mode tests | DEPLOYED / PASS-LOCAL |
| S3 不可变 `DigitalSelfVersion`：服务端仅从确认的主人来源构建 manifest；`POST/GET /v1/digital-self/versions`、`GET /{id}`，以及 testing/approve/freeze/revoke/rollback 状态机。跨账户统一 404；testing 要求 manifest digest，后四项额外要求当前账户密码 step-up；Self Preview/Legacy 仍不启用 | `services/digital_self/*`, `services/control_api/app/routes/digital_self.py`, `services/control_api/app/main.py` | `services/digital_self/tests/test_registry.py`, `services/control_api/tests/test_digital_self_api.py` | DEPLOYED / PASS-LOCAL |
| S4 成长地图从现有 Evidence/Memory/Persona/manifest 派生七个定性维度；展示来源、拒绝原因、冲突、最近变化和版本就绪度；四类事件溯源培育任务；`prompt_kind` 分级权重；“不像我/我不会这样说”同时使地图冲突并排除下一版来源 | `services/common/evidence_policy.py`, `services/growth/*`, `services/control_api/app/routes/growth.py`, `services/control_api/app/routes/archive.py`, `services/control_api/app/routes/session.py`, `services/persona/*`, `apps/h5/src/components/GrowthMapPanel.jsx` | `services/common/tests/*`, `services/growth/tests/*`, `services/control_api/tests/test_growth_api.py`, `test_archive_api.py`, `services/persona/tests/*`, `apps/h5/src/components/GrowthMapPanel.test.jsx`, `App.test.jsx`, `api.test.js`, `useVoiceSession.test.jsx` | DEPLOYED / PASS-LOCAL |
| 实时证据只走 session-bound archive contract；Control canonicalize flat/nested interaction，owner/guest/ambiguous/shadow 资格不能由调用方升级；assistant 与 raw audio 只能绑定同 session/turn/generation 的 canonical 用户父话轮，425 可重试且不阻塞父事件 | `routes/archive.py`, `mode_policy.py`, `duplex_runtime.py`, `archive_sink.py`, `services/archive/{life_archive,postgres_archive}.py` | `test_archive_api.py`, `test_interaction_mode_runtime.py`, `test_archive_evidence_wiring.py`, `test_archive_sink.py`, archive adapter tests | DEPLOYED / PASS-LOCAL |
| H5 实时语音、停止回答、声音解锁、静音保持与 10 秒重连恢复 | `apps/h5/src/hooks/useVoiceSession.js`, `apps/h5/src/App.jsx` | `apps/h5/src/hooks/useVoiceSession.test.jsx`, 浏览器交互 QA | PASS |
| 首声全链路 trace 与豆包首包超时恢复；首包计时不包含 LLM 首 token 等待 | `duplex_runtime.py`, `agent.py`, `doubao_tts.py`, `apps/h5/src/hooks/useVoiceSession.js` | `test_duplex_runtime_wiring.py`, `test_doubao_mock.py`, H5 hook tests | PASS-LOCAL |
| 候选打断 duck-first，确认后停止或平滑恢复 | `duplex_runtime.py`, `interruption_guard.py`, `apps/h5/src/hooks/useVoiceSession.js` | `test_agent_production_wiring.py`, H5 hook tests | PASS-LOCAL |
| 可信小程序 barge-in 首事件静音；仅对 sticky `interrupt_then_chat` 歧义 final 用 `deepseek-v4-flash` 提供严格三态证据，Router 保持唯一副作用入口；超时/非法/迟到 fail closed | `utterance_router.py`, `interrupt_semantic_classifier.py`, `duplex_runtime.py`, `agent.py`, ADR-0022 | Router/classifier/runtime/Agent 生产路径回归，Provider smoke 五类样本，小程序 gain 测试 | PASS-LOCAL / REAL-DEVICE-PENDING |
| listener cue 独立调度、上限、冷却、禁用场景与独立取消域 | `orchestration/cue_scheduler.py`, `duplex_runtime.py` | `test_cue_scheduler.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| FunASR 主链 + Qwen3-ASR 非阻塞情绪旁路；短 TTL、不持久化 | `funasr_stt.py`, `qwen_emotion_asr.py`, `orchestration/emotion.py`, `duplex_runtime.py` | `test_qwen_emotion_sidecar.py`, `test_emotion_policy.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| 每 generation 的豆包受控语速/响度/音高与 native timbre 回退；为时间戳安全不发送 `context_texts` | `orchestration/prosody.py`, `doubao_tts.py`, `duplex_runtime.py` | `test_emotion_policy.py`, `test_provider_config.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| 旧 Qwen Omni/Audio 端到端 A/B | 历史代码/发布记录；当前 H5 入口已下线 | 历史 A/B 证据 | HISTORICAL-NOT-IN-SCOPE |
| H5 只消费 Agent canonical final，不自行拼接/清洗，并避免分段字幕重复落库 | `agent.py`, `duplex_runtime.py`, `apps/h5/src/hooks/useVoiceSession.js` | Agent canonical 回归、`persists only authoritative transcript_delta events` | PASS |
| H5 每日回顾与北京时间日期 | `apps/h5/src/App.jsx`, `apps/h5/src/api.js`, `apps/h5/src/lib/date.js` | `apps/h5/src/lib/date.test.js`、Control API tests | PASS |
| H5 个人资料与四个偏好持久化（含默认开启的“过滤明显旁人（实验）”） | `apps/h5/src/App.jsx`, `apps/h5/src/api.js`, `services/control_api/app/routes/memory.py` | `apps/h5/src/App.test.jsx`, `test_memory_api.py` | PASS |
| 稳定账号、匿名原地升级与跨账户隔离 | `routes/auth.py`, `database.py`, `AuthScreen.jsx`, `api.js` | `test_account_auth.py`, `App.test.jsx`, `api.test.js` | PASS-LOCAL |
| 证据账本、人物/关系/时间线/知识与混合检索 | `services/archive/*`, `routes/archive.py`, `LifeArchivePanel.jsx` | archive/control/H5 tests | PASS-LOCAL |
| owner/guest/uncertain 三态；guest/uncertain 仍进入不可变证据账本且不进入主人记忆投影；只有注册、授权、active session、同一可信 `shadow_owner_candidate` profile 的合格文本可按 6 次/3 会话自动学习低敏 Persona | `services/speaker/*`, `routes/archive.py`, `services/persona/*`, `speaker_authority_client.py`, `utterance_router.py` | speaker/control/agent/persona tests，含 guest/anonymous/direct/ambiguous/no-audio/低质量/跨 profile/控制话轮隔离 | PASS-PROD |
| `reject_non_owner_voice=true` 默认拒绝 formal guest/owner mismatch 与明确的 shadow guest；shadow/formal ambiguous 普通聊天 fail-open 但 `history_eligible=false`、无私人权限；关闭后只放开交互，不升级 owner、记忆、工具或敏感操作权限；shadow cutoff 为 0.40 | `routes/speaker.py`, `speaker_authority_client.py`, `utterance_router.py`, `duplex_runtime.py`, `DigitalSelfPanel.jsx` | `test_speaker_api.py`, `test_speaker_authority.py`, `test_target_speaker_focus.py`, `test_utterance_router.py`, `DigitalSelfPanel.test.jsx`，含真实 score matrix 主人 4/4 放行、孩子 6/7 拒绝 | PASS-PROD |
| 主人历史只消费按 `(turn_id, generation_id)` 冻结且显式 `history_eligible=true` 的 Agent 权威终稿；访客、ambiguous、无档案、authority 不可用及缺字段的话轮和对应 AI 回复均 fail-closed | `duplex_runtime.py`, `useVoiceSession.js`, `App.jsx`, `api.js` | Agent fence/history tests，H5 Hook/App/API/cache tests | PASS-LOCAL |
| 固定 CAM++ ONNX 模型服务；模型 digest、HTTP 鉴权、192 维 embedding、shadow-only 与 anti-spoof fail-closed | `services/speaker_model/*`, `infra/Dockerfile.speaker-model`, `scripts/smoke_campplus_onnx.py`, `docs/adr/0010-campplus-shadow-and-readiness-boundary.md` | speaker-model API/engine、真实 ONNX smoke、Control API shadow 纵向合同、容器 HTTP smoke | PASS-LOCAL |
| Control API readiness 实时探测 speaker-model，拒绝宕机、非 ready 和 model_version 漂移 | `services/control_api/app/routes/readiness.py` | `services/control_api/tests/test_readiness.py` | PASS-LOCAL |
| Persona 低敏特征跨会话自动发布、shadow profile 独立 lane、互斥 bucket 2:1 且单一生效、candidate 默认不下发客户、disabled sticky、consent 原子门禁和当前话轮刷新；owner/uncertain 均须显式 `persona_eligible=true`；shadow-only uncertain 只读固定安全描述白名单并清空自由文本/证据，仍隔离价值/决策、私人记忆、旧话轮与工具 | `services/persona/*`, `routes/persona.py`, `routes/archive.py`, `persona_client.py`, `agent.py`, `context_assembler.py`, `DigitalSelfPanel.jsx` | persona/control/agent/H5 tests + SQLite/PostgreSQL fail-closed 合同 | DEPLOYED / PASS-LOCAL |
| 历史 CosyVoice 3.5 复刻资产继续支持查看、评估与撤销，但不进入豆包播放主链；会话按所选伙伴回退已批准豆包原生音色 | `services/voice_profile/*`, `routes/voice.py`, `voice_profile_client.py`, `DigitalSelfPanel.jsx` | voice profile/control/agent/H5 tests + SQLite/PostgreSQL Provider/orphan contract | PASS-LOCAL |
| Doubao 个人音色只在 exact manifest v3 + owner Self Preview 中生效；session 冻结 profile/version/provider/model/resource/expiry/speaker digest，并独立冻结所选伙伴 fallback；Companion 永不加载 personal；seed-tts/seed-icl 分池，首音频前一次回退；复评/expiry fail closed；Archive 精确核验 personal version/expiry，并从批准目录重算 designed digest；人工云端删除仅由独立 cleanup capability 审计收敛 | `services/voice_profile/doubao_voice_clone.py`, `services/digital_self/*`, `routes/{session,interaction,voice,archive}.py`, `mode_policy_client.py`, `doubao_tts.py`, `agent.py`, `DigitalSelfPanel.jsx`, `api.js`, ADR-0019 | Doubao clone/manager/registry/control/agent runtime/archive/H5 tests；全量 1079 passed、3 skipped，H5 219 passed；390×844 / 667×375 浏览器 | DEPLOYED / REAL-PROVIDER-NOT-VALIDATED |
| S9 传承模式：actor/owner/speaker subject 分离；grant 固定 exact frozen version/manifest、approved relationship、allowlist、声音权限与时效；owner 在世预演，active grantee 独立 shell；actual-heard 不反哺 owner core；response plan/voice/archive 每 generation 重验；最小 ID 审计；private/unknown scope fail closed | `services/legacy/*`, `routes/{legacy,session,interaction,voice,archive}.py`, `mode_policy.py`, `agent.py`, `mode_policy_client.py`, `response_planner_client.py`, `LegacyPanel.jsx`, ADR-0020 | Legacy SQLite/PostgreSQL/FORCE RLS/API/Agent/Archive/Governance/H5 tests；全量 1175 passed、3 skipped，H5 232 passed；390×844 / 667×375 浏览器；生产 capability 负向门禁 | DEPLOYED / PROD-POSITIVE-FIXTURE-NOT-VALIDATED |
| 五伙伴豆包原生音色、服务端 registry 白名单与版本化真实试听 | `services/common/companions.py`, `doubao_voice_catalog.py`, `infra/voices/doubao_voice_ids.json`, `apps/h5/public/assets/voices/*-doubao-v1.wav` | `test_doubao_voice_catalog.py`, `test_voice_profile_api.py`, H5 onboarding tests | PASS-LOCAL |
| 原始主人语音独立授权、同一加密 spool 分流；撤销事务先冻结授权并快照对象，删除成功后按精确键清 manifest；临时失败音频保留重试但不阻塞后续/新转写 | `archive_sink.py`, `routes/archive.py`, `services/archive/*`, `PrivacyDataPanel.jsx` | archive/control/agent/H5 tests + SQLite/PostgreSQL 并发及 HTTP contract | PASS-LOCAL |
| 账户导出、删除 fence、tombstone、外部资产清理与联合恢复 | `services/governance/*`, `session_termination.py`, restore scripts/runbook | governance/control/restore tests | PASS-LOCAL |
| H5 视觉、可访问性与移动端溢出 | `apps/h5/src/styles.css`, `apps/h5/AGENTS.md` | 390×844、375×667、667×375 浏览器 QA、reduced-motion、console 检查 | PASS-LOCAL |
| 本地 mock servers | `tests/integration/mock_servers.py` | integration tests | PASS |
| 离线 ASR→LLM→TTS | `scripts/run_e2e.py`, `OfflinePipeline` | `test_offline_pipeline.py` | PASS |
| 当前生产基线 Provider smoke（runtime `20260723-192611`） | `scripts/livekit_smoke_test.py`, `scripts/provider_smoke_test.py`, `docs/releases/20260723-192611.md` | LiveKit、FunASR、Qwen、豆包 PCM/字幕/取消、Agent 新鲜 heartbeat、9 项 core readiness 实网证据 | PASS-PROD-CURRENT |
| 当前 release canonical/Persona 发布门禁 | `scripts/provider_smoke_test.py`, `scripts/mark_readiness.py`, `scripts/refresh_readiness.sh` | 新 release 绑定 FunASR、Qwen、豆包、core readiness、H5 与 WMS 无回归 | PASS-PROD |
| 韵律自适应（阶段4） | `orchestration/prosody.py` | `test_prosody.py` | PASS |
| 双部署档案 | `config.py`, `agent.build_turn_handling_config` | `test_config.py` | PASS |
| H5 前端无永久密钥 | `.env.example`, Control API, `apps/h5/` | `apps/h5/src/api.test.js`, production bundle static scan | PASS |
| Docker/Makefile/CI/锁文件 | `infra/*`, `Makefile`, `.github/workflows/ci.yml` | lockfiles present | PASS |
| H5 独立生产路径与 SQLite 持久化 | `docker-compose.production.yml`, `infra/nginx-memoria-*.conf` | Compose 解析、Nginx 语法、AMD64 runtime 与重启持久性 | PASS |
| 公网 IP HTTPS 与短期证书自动续期 | `nginx-memoria-ip-server.conf`, `certbot-memoria-deploy-hook.sh` | IP SAN 证书、renew dry-run、hook `nginx -t` 门禁 | PASS |
| 生产发布、备份与回滚 runbook | `docs/production-deployment.md`, `HANDOFF.md`, `docs/releases/20260723-192611.md` | SQLite/PG/env/Nginx 回滚点、runtime-first/H5-last、真实 Agent heartbeat、公网/浏览器/15 分钟观察 | PASS-PROD |
| 可观测性 registry/结构化日志/追踪；输入守卫按原因计数；生产未启动或对外暴露 Prometheus endpoint | `observability/*`, `duplex_runtime.py` | `test_metrics_exporter.py`, `test_duplex_runtime_wiring.py` | PASS-INTERNAL |
| Python 质量门 | `pyproject.toml`, `.github/workflows/ci.yml` | Ruff、mypy strict、全量 pytest | PASS-LOCAL |
| H5 质量门 | `apps/h5/package.json`, `apps/h5/src/**/*.test.*` | 全量测试、production build、移动端浏览器与 console 回归 | PASS-LOCAL |
| legacy Web/iOS 质量门 | 两端源码均已移除 | 无当前构建门 | REMOVED |
| 200 条真实中文录音、AEC 设备矩阵、第 21 章 SLO | 需外部测试数据与设备 | 尚未执行 | NOT-VALIDATED |

## 当前发布结论

当前已发布生产基线为 runtime `20260802-142257`、H5 `20260802-142257`，默认主链为 FunASR + 百炼 DeepSeek-v4-flash +
豆包 Seed-TTS 2.0 双向流式。S1–S9 的四模式、DigitalSelfVersion、成长地图、
认知/关系、统一回答规划、Self Preview、个人声音安全门禁与 Legacy runtime 均已部署；
生产 Provider/readiness、能力令牌负向门禁、混合陪伴方案浏览器、证书、WMS 和
15 分钟观察通过。真实家庭关系正向 Legacy 会话、本人声音样本/盲测与真实设备矩阵
仍未执行，不能用部署成功代替这些外部验收。

200 条明确授权真人录音、完整 AEC/噪声/重叠/回放设备矩阵、真人 Persona/声音盲测、生产 PostgreSQL/S3/KMS 联合恢复与真实手机 H5 验收仍为 **NOT-VALIDATED**。原生 iOS 与 legacy Web 客户端源码均已移除。
