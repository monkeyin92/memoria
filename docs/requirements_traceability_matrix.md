# Requirements Traceability Matrix

| 规范要求 | 实现文件 | 测试文件 | 状态 |
|---|---|---|---|
| 生产默认选型 FunASR/百炼 Qwen/CosyVoice/LiveKit 1.6.5 | `pyproject.toml`, `config.py`, `providers/*`, `agent.py` | `test_versions.py`, `test_config.py` | PASS |
| GenerationFence 全字段比对；旧结果丢弃 | `contracts/ids.py`, `orchestration/generation_fence.py` | `test_generation_fence.py`, `test_interrupt_isolation.py` | PASS |
| 旧 generation 音频不得播放 | `orchestrator.py`, `cosyvoice_tts.py` | `test_interrupt_isolation.py` | PASS |
| 旧 tool_epoch 结果不得播报 | `task_manager.py`, `orchestrator.py` | `test_tool_epoch_isolation.py` | PASS |
| 状态机合法/非法转换 | `orchestration/state_machine.py` | `test_state_machine.py` | PASS |
| 助手历史只含实际已听文本 | `heard_text_tracker.py`, `context_manager.py` | `test_heard_text_tracker.py`, `test_interrupt_isolation.py` | PASS |
| FunASR 协议/字级时间戳 | `funasr_protocol.py`, `funasr_stt.py` | `test_funasr_protocol.py`, `test_funasr_mock.py` | PASS |
| FunASR LiveKit `stt.STT` + `RecognizeStream._run` | `funasr_stt.py` (`FunASRSTT`, `FunASRRecognizeStream`) | `test_livekit_adapters.py` | PASS |
| CosyVoice LiveKit `tts.TTS` + `SynthesizeStream._run` | `cosyvoice_tts.py` (`CosyVoiceTTS`, `CosyVoiceSynthesizeStream`) | `test_livekit_adapters.py` | PASS |
| Agent entry wires Orchestrator/Fence/interrupt | `agent.py`, `duplex_runtime.py` | `test_duplex_runtime_wiring.py` | PASS |
| LLM/TTS 生产路径 GenerationFence 门控 | `DuplexVoiceAgent.llm_node/tts_node` | `test_pipeline_fence_gating.py` | PASS |
| 非预生成 turn 先提交 fence 再启动 LLM；THINKING 可接收真实新轮次 | `agent.py`, `orchestrator.py` | `test_agent_production_wiring.py`, `test_orchestration_edges.py` | PASS |
| interrupt 取消已注册 LLM/TTS task | `orchestrator.set_active_*_task` + `confirm_interruption` | `test_pipeline_fence_gating.py`, `test_duplex_runtime_wiring.py` | PASS |
| FunASR RecognizeStream 真实 mock 出 final | `FunASRRecognizeStream._run` | `test_funasr_recognize_stream.py` | PASS |
| speaking 生命周期 → HeardTextTracker | `duplex_runtime.on_assistant_speaking/on_playback_done` | `test_pipeline_fence_gating.py` | PASS |
| 工具 cancel_event 协同取消 | `task_manager.py` | `test_tool_cancel_event.py` | PASS |
| FunASR 稳定前缀 | `stable_prefix.py` | `test_stable_prefix.py` | PASS |
| CosyVoice 连接池；取消丢连接 | `cosyvoice_tts.py` | `test_cosyvoice_mock.py` | PASS |
| CosyVoice 时间戳缩放/退化 | `cosyvoice_protocol.py` | `test_timestamp_scale.py` | PASS |
| Qwen 默认快/深模型；可选配置不得隐式覆盖默认 provider | `config.py`, `agent.py` | `test_dashscope_qwen_is_the_default_llm`, `test_deepseek_key_does_not_override_qwen_implicitly` | PASS |
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
| 消息、Profile、Agent/FunASR 上下文统一 PII 脱敏 | `services/common/redaction.py`, `routes/memory.py`, `context_manager.py`, `funasr_protocol.py` | `test_memory_api.py`, `test_orchestration_edges.py`, `test_funasr_protocol.py` | PASS |
| SQLite 持久化会话控制与 release-bound readiness evidence | `control_api/app/database.py`, `routes/session.py`, `routes/readiness.py` | `test_session_api.py` | PASS |
| RTC 恢复两端原子前移 generation | `rtc-recovered` route, `agent.py`, `apps/h5/src/hooks/useVoiceSession.js` | Python/H5 recovery tests | PASS |
| legacy Web session store / generation 丢弃 | `apps/web/src/state/sessionStore.ts` | 历史 Web tests | HISTORICAL-NOT-IN-SCOPE |
| legacy Web 远端音频、字幕、设备与重连 | `apps/web/` | 历史 Web tests | HISTORICAL-NOT-IN-SCOPE |
| 原生 iOS 会话与同步播放 | `apps/ios/` | 历史构建记录 | HISTORICAL-NOT-IN-SCOPE |
| H5 首页动态吉祥物与四种情绪 | `apps/h5/src/components/Mascot.jsx`, `apps/h5/public/assets/mascot-*` | `apps/h5/src/components/Mascot.test.jsx`, `apps/h5/design-qa.md` | PASS |
| H5 实时语音、停止回答、声音解锁、静音保持与 10 秒重连恢复 | `apps/h5/src/hooks/useVoiceSession.js`, `apps/h5/src/App.jsx` | `apps/h5/src/hooks/useVoiceSession.test.jsx`, 浏览器交互 QA | PASS |
| 首声全链路 trace 与 CosyVoice 首包超时恢复 | `duplex_runtime.py`, `agent.py`, `cosyvoice_tts.py`, `apps/h5/src/hooks/useVoiceSession.js` | `test_duplex_runtime_wiring.py`, `test_cosyvoice_livekit_stream.py`, H5 hook tests | PASS-LOCAL |
| 候选打断 duck-first，确认后停止或平滑恢复 | `duplex_runtime.py`, `interruption_guard.py`, `apps/h5/src/hooks/useVoiceSession.js` | `test_agent_production_wiring.py`, H5 hook tests | PASS-LOCAL |
| listener cue 独立调度、上限、冷却、禁用场景与独立取消域 | `orchestration/cue_scheduler.py`, `duplex_runtime.py` | `test_cue_scheduler.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| FunASR 主链 + Qwen3-ASR 非阻塞情绪旁路；短 TTL、不持久化 | `funasr_stt.py`, `qwen_emotion_asr.py`, `orchestration/emotion.py`, `duplex_runtime.py` | `test_qwen_emotion_sidecar.py`, `test_emotion_policy.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| 每 generation 的受控 CosyVoice 情绪与 neutral 回退 | `orchestration/prosody.py`, `cosyvoice_tts.py`, `duplex_runtime.py` | `test_emotion_policy.py`, `test_provider_config.py`, `test_duplex_runtime_wiring.py` | PASS-LOCAL |
| 旧 Qwen Omni/Audio 端到端 A/B | 历史代码/发布记录；当前 H5 入口已下线 | 历史 A/B 证据 | HISTORICAL-NOT-IN-SCOPE |
| H5 只持久化权威字幕，避免分段字幕重复落库 | `apps/h5/src/hooks/useVoiceSession.js` | `persists only authoritative transcript_delta events` | PASS |
| H5 每日回顾与北京时间日期 | `apps/h5/src/App.jsx`, `apps/h5/src/api.js`, `apps/h5/src/lib/date.js` | `apps/h5/src/lib/date.test.js`、Control API tests | PASS |
| H5 个人资料与三个偏好持久化 | `apps/h5/src/App.jsx`, `apps/h5/src/api.js`, `services/control_api/app/routes/memory.py` | `apps/h5/src/App.test.jsx`, `test_memory_api.py` | PASS |
| 稳定账号、匿名原地升级与跨账户隔离 | `routes/auth.py`, `database.py`, `AuthScreen.jsx`, `api.js` | `test_account_auth.py`, `App.test.jsx`, `api.test.js` | PASS-LOCAL |
| 证据账本、人物/关系/时间线/知识与混合检索 | `services/archive/*`, `routes/archive.py`, `LifeArchivePanel.jsx` | archive/control/H5 tests | PASS-LOCAL |
| owner/guest/uncertain 三态；guest/uncertain 仍进入不可变证据账本，但不进入主人投影、私人上下文或 Persona 学习 | `services/speaker/*`, `routes/archive.py`, `speaker_authority_client.py`, `utterance_router.py` | speaker/control/agent tests，含 guest/uncertain 入账与投影隔离合同 | PASS-LOCAL |
| 固定 CAM++ ONNX 模型服务；模型 digest、HTTP 鉴权、192 维 embedding、shadow-only 与 anti-spoof fail-closed | `services/speaker_model/*`, `infra/Dockerfile.speaker-model`, `scripts/smoke_campplus_onnx.py`, `docs/adr/0010-campplus-shadow-and-readiness-boundary.md` | speaker-model API/engine、真实 ONNX smoke、Control API shadow 纵向合同、容器 HTTP smoke | PASS-LOCAL |
| Control API readiness 实时探测 speaker-model，拒绝宕机、非 ready 和 model_version 漂移 | `services/control_api/app/routes/readiness.py` | `services/control_api/tests/test_readiness.py` | PASS-LOCAL |
| Persona 证据、反例门禁、版本审核与实时胶囊 | `services/persona/*`, `persona_client.py`, `DigitalSelfPanel.jsx` | persona/control/agent/H5 tests | PASS-LOCAL |
| CosyVoice 3.5 声音授权、登记 saga、盲测、质量门禁与撤销；profile/orphan 外部清理未完成返回 503，H5 可见并幂等重试 | `services/voice_profile/*`, `routes/voice.py`, `DigitalSelfPanel.jsx` | voice profile/control/agent/H5 tests + SQLite/PostgreSQL Provider/orphan contract | PASS-LOCAL |
| 原始主人语音独立授权、同一加密 spool 分流；撤销事务先冻结授权并快照对象，删除成功后按精确键清 manifest；临时失败音频保留重试但不阻塞后续/新转写 | `archive_sink.py`, `routes/archive.py`, `services/archive/*`, `PrivacyDataPanel.jsx` | archive/control/agent/H5 tests + SQLite/PostgreSQL 并发及 HTTP contract | PASS-LOCAL |
| 账户导出、删除 fence、tombstone、外部资产清理与联合恢复 | `services/governance/*`, `session_termination.py`, restore scripts/runbook | governance/control/restore tests | PASS-LOCAL |
| H5 视觉、可访问性与移动端溢出 | `apps/h5/src/styles.css`, `apps/h5/design-qa.md` | 390×844 浏览器 QA、console 检查 | PASS |
| 本地 mock servers | `tests/integration/mock_servers.py` | integration tests | PASS |
| 离线 ASR→LLM→TTS | `scripts/run_e2e.py`, `OfflinePipeline` | `test_offline_pipeline.py` | PASS |
| 当前生产基线 Provider smoke（`20260719-000731`） | `scripts/livekit_smoke_test.py`, `scripts/provider_smoke_test.py`, `docs/releases/20260719-000731.md` | LiveKit、FunASR、Qwen、CosyVoice 实网证据 | PASS-PROD-BASELINE |
| 当前本地候选 Provider smoke/readiness | 同上；候选尚未部署且未注入真实 Provider 凭据 | 未执行 | NOT-RUN-LOCAL |
| 韵律自适应（阶段4） | `orchestration/prosody.py` | `test_prosody.py` | PASS |
| 双部署档案 | `config.py`, `agent.build_turn_handling_config` | `test_config.py` | PASS |
| H5 前端无永久密钥 | `.env.example`, Control API, `apps/h5/` | `apps/h5/src/api.test.js`, production bundle static scan | PASS |
| Docker/Makefile/CI/锁文件 | `infra/*`, `Makefile`, `.github/workflows/ci.yml` | lockfiles present | PASS |
| H5 独立生产路径与 SQLite 持久化 | `docker-compose.production.yml`, `infra/nginx-memoria-*.conf` | Compose 解析、Nginx 语法、AMD64 runtime 与重启持久性 | PASS |
| 公网 IP HTTPS 与短期证书自动续期 | `nginx-memoria-ip-server.conf`, `certbot-memoria-deploy-hook.sh` | IP SAN 证书、renew dry-run、hook `nginx -t` 门禁 | PASS |
| 生产发布、备份与回滚 runbook | `docs/production-deployment.md`, `HANDOFF.md` | 首次发布清单与回滚步骤 | PASS-DOC |
| 可观测性 registry/结构化日志/追踪；输入守卫按原因计数；生产未启动或对外暴露 Prometheus endpoint | `observability/*`, `duplex_runtime.py` | `test_metrics_exporter.py`, `test_duplex_runtime_wiring.py` | PASS-INTERNAL |
| Python 质量门 | `pyproject.toml`, `.github/workflows/ci.yml` | Ruff、mypy strict、全量 pytest | PASS-LOCAL |
| H5 质量门 | `apps/h5/package.json`, `apps/h5/src/**/*.test.*` | 98 项测试、production build、390×844 浏览器与 console 回归 | PASS-LOCAL |
| legacy Web/iOS 质量门 | 历史源码 | 不进入当前 CI/DoD | HISTORICAL-NOT-IN-SCOPE |
| 200 条真实中文录音、AEC 设备矩阵、第 21 章 SLO | 需外部测试数据与设备 | 尚未执行 | NOT-VALIDATED |

## 当前发布结论

当前已发布生产基线为 runtime/H5 `20260719-000731`，默认 provider 为 `qwen`，H5 只暴露 FunASR + Qwen + CosyVoice 3.5 级联。本文新增的稳定账号、终身记忆、Persona、正式声纹、声音档案治理与原始语音授权为 **本地工程完成、未生产发布**；所有 `PASS-LOCAL` 都不得解读为线上已具备。

200 条明确授权真人录音、完整 AEC/噪声/重叠/回放设备矩阵、真人 Persona/声音盲测、生产 PostgreSQL/S3/KMS 联合恢复与真实手机 H5 验收仍为 **NOT-VALIDATED**。原生 iOS 与 legacy Web 不属于当前产品范围。
