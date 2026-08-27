# Memoria

Memoria 是以 ESP32-S3 为第一等语音终端的中文陪伴与长期档案系统。当前唯一交互权威链是：

```text
ESP32-S3 -> Go Media Edge -> Python Voice Core / Agent
                                      |
                                      +-> Control API -> PostgreSQL / Redis / MinIO
H5 / 微信小程序 -------------------------> 控制面、档案与设备管理
```

小程序不是实时媒体终端：不采集麦克风、不播放实时 TTS、不建立媒体 WSS、不加入 LiveKit 房间。H5 保留浏览器实时语音能力；ESP32 是机器人产品的实时语音入口。未完成真实 AEC、双讲和连续轮次验收前，不得宣称全双工。

## 文档与权威

仓库长期只保留三份文档：

- `README.md`：产品、架构、开发、协议和固件入口。
- `PROJECT_RULES.md`：长期协作规则、产品约束和工程纪律。
- `HANDOFF.md`：当前线上状态、发布/回滚/备份和待验收事项。

机器可执行事实放在 schema、proto、配置、锁文件和测试中，不另建说明文档；历史发布过程不在仓库累积。

## 技术栈与目录

- Python 3.12、uv、FastAPI、LiveKit Agents、FunASR、百炼兼容 LLM、豆包 Seed-TTS。
- Go Media Edge：设备 WSS、generation fence、gRPC Voice Core bridge、Pion WebRTC。
- PostgreSQL 17 + pgvector、Redis、MinIO。
- H5：`apps/h5`；微信小程序：`apps/miniprogram`；ESP32 overlay：`firmware/esp32`。
- 共享契约：`packages/contracts`；Media Edge/Voice Core proto：`packages/proto`。
- 发布、验收和运维脚本：`scripts`；生产 Compose/Nginx/模型 registry：`infra`。

共享 JSON/schema/proto 是兼容性契约，不是运行时配置。修改契约时必须在同一提交中修改全部生产者、消费者、生成物和跨语言测试。

## 快速开始

前置：Python 3.12、uv、Node 20+、npm；容器开发另需 Docker。

```bash
cp .env.example .env
echo 'OFFLINE_MOCK=true' >> .env  # 没有供应商密钥时
uv sync --all-extras
npm --prefix apps/h5 ci
```

三个终端分别启动：

```bash
uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload
uv run python -m services.agent.src.main dev
npm --prefix apps/h5 run dev -- --host 0.0.0.0
```

本地数据层与自建 LiveKit：

```bash
docker compose up -d postgres redis
docker compose --profile self-hosted up -d redis livekit
```

`devkey/devsecret` 只允许本机开发。生产 API secret 永远只进入 Control API/Agent 的 root-only 环境文件；H5 只接收短期 participant token。

## 质量门

提交前按改动范围运行，发布前运行完整门禁：

```bash
uv run ruff check .
uv run python scripts/check_module_budget.py check
uv run mypy services --strict
uv run pytest
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
npm --prefix apps/miniprogram test
uv run python scripts/run_e2e.py --profile offline
uv run python scripts/provider_smoke_test.py
```

Media Edge：

```bash
./scripts/generate_media_go_proto.sh
cd services/media_edge
go test ./...
go test -race ./...
```

多主体契约：

```bash
uv run python scripts/generate_multi_subject_contracts.py --check
node --test apps/miniprogram/tests/*.test.js
```

只要出现旧 generation/tool epoch 误播、主人数据越权、危机回复被普通提示覆盖或危机事件未进入监护通知 outbox，发布结论必须是 REJECT。

## 产品与数据边界

- 账号身份只证明客户端持有账号凭据，不证明麦克风前的人是账户主人。
- `Speaker Classification` 是 `owner / guest / uncertain` 的概率结论，不是法律身份认证。
- 明确 guest 可以被实验性 Target Speaker Focus 拒绝；ambiguous 不可获得主人历史、私人记忆、工具或敏感权限。
- 只有 Agent 权威终稿中的 `history_eligible=true` 可以进入主人长期历史，且资格必须绑定原话轮 generation fence。
- Evidence Event 是不可变观察；Memory Claim 具有 `candidate / confirmed / disputed / retracted` 生命周期；投影可重建，不能替代原始证据。
- Speaker Profile 用于判断谁在说话；Voice Profile 用于授权合成声音，两者不得混称。
- Companion Mode、Self Preview、Archive Mode、Legacy Mode 是不同权限边界；模拟输出不得反哺主人证据。
- 浏览器 bundle、日志、发布清单和仓库不得包含永久凭据、设备私钥或供应商 secret。

学生账号能力以 `services/control_api/app/account_gate.py` 为唯一规则表。任何端点必须在读取私有资源或产生写副作用前完成 capability 检查；未声明能力默认拒绝。

## ESP32 首次启用与安全配网

首次启用使用“二维码确认设备身份、BLE 近场安全配网、HTTPS 设备认领与激活”的单一路径：

1. ESP32 显示签名的 `memoria-bootstrap:v1:` 二维码并广播 `MEM-XXXX` BLE 名称。
2. 小程序把原始二维码交给 Control API introspect，取得一次性 onboarding session 和设备 provisioning 契约。
3. 小程序与设备建立 Protocomm Security 1 会话，使用 X25519、PoP 和 AES-256-CTR；只有会话认证成功后才允许写入 Wi-Fi SSID/密码。
4. 设备联网后自行提交 online-proof；小程序完成 claim、binding 和授权确认。
5. 服务端生成签名 Activation Manifest；设备拉取、验签并 ACK。只有服务端状态达到 `device_acknowledged` 或 `ready_for_conversation`，小程序才显示启用完成。

Wi-Fi 密码只通过加密 BLE 会话进入设备，不经过普通 HTTPS 业务请求或服务端日志。蓝牙本身不是互联网通道，也不会自动把手机蜂窝网络桥接给 ESP32；附近没有路由器时，可以先开启手机热点，再把该热点的 SSID/密码通过上述 BLE 流程交给设备。

绑定完成前设备拉取 Activation Manifest 得到 `409` 属于正常中间态：固件保持二维码/BLE 入口并后台重试，绑定完成后停止配网入口。相同二维码从新页面再次扫码会复用原 onboarding session；安全会话已经释放时必须重新扫码，不能复用旧内存会话发送网络信息。

小程序仍是控制面：手机不采集声纹，不参与机器人实时对话。主人声纹登记由小程序记录明确授权，再由已绑定设备采集有界语音样本并提交 Speaker Authority；`requested`、`pending`、`active` 是服务端权威状态，shadow 档案未激活前不得宣传为主人认证。

当前启用链路的线上、板卡和小程序证据以及仍待完成的真实对话验收见 `HANDOFF.md`。

## 实时话轮状态设计（CPU-only）

本节定义 Memoria 的实时话轮状态层。它借鉴流式 ASR 与 turn-state 同步建模的设计理念，但不引入 X2-Turn 的代码、模型、权重、Tokenizer、vLLM patch、容器或运行时依赖。当前没有 GPU 服务器，本方案只使用既有 16 kHz PCM、20 ms VAD、FunASR partial/final、播放状态、说话人结论和 generation fence，在 Python Voice Core 内以确定性 CPU 规则运行。

理念参考固定为 [X2-Turn `c3c3a0d6`](https://github.com/X-Square-Robot/X2-Turn/tree/c3c3a0d6d941e7ddfce8db4d6f30333b24a22167) 与 [arXiv:2608.10878](https://arxiv.org/abs/2608.10878)，仅用于说明来源。它不是 Memoria 的供应链依赖，也不进入构建、发布、SBOM、生产网络或回滚链。未来是否采用任何学习型 turn 模型不属于当前方案，必须另行完成架构、资源、隐私、许可证和真实设备评审。

### 目标与非目标

目标：

- 把“检测到声能”“形成了用户语义”“用户可能说完”“只是简短附和”“证据冲突”拆成连续且可观测的状态。
- 降低环境声、回声和短噪声造成的误聆听与误打断，同时缩短有明确语义时的抢话和话轮提交延迟。
- 让 VAD、ASR revision、endpoint、播放状态、说话人结论和 AEC 证据在同一绝对 sample clock 上汇合。
- 复用现有 `InteractionPlane`、`InterruptionPolicy`、`UtteranceRouter`、`MediaTurnEndpointMixin` 和 `GenerationFence`，收敛重复布尔条件，不创建第二个交互控制面。
- 先通过 deterministic replay 和 shadow evidence 证明收益，再改变生产副作用。

非目标：

- 不替换 FunASR，不增加新的 ASR、LLM、分类器或云端调用。
- 不把规则状态包装成“AI 模型置信度”，不伪造说话人、AEC 或语义证据。
- 不改变 `ESP32 -> Go Media Edge -> Python Voice Core / Agent` 权威链，不把话轮决策下放到 Edge、固件、H5 或小程序。
- 不把状态预测等同于 turn commit、generation cancel、Playback ACK、Actual Heard 或全双工验收。
- 不为尚未存在的 GPU/模型路径设计兼容层、自动回退或配置矩阵。

### 单一数据流与职责

```text
16 kHz PCM / 20 ms VAD ───────┐
FunASR partial/final ─────────┤
speaker / AEC / loss evidence ├─> SpeechTimeline + ConversationProjection
PlaybackLedger + current fence┘       （细化 TurnPhase，纯 CPU、无副作用）
                                                 |
                                                 v
                                    ProjectionFrame / phase evidence
                                                 |
                         ┌───────────────────────┴───────────────────────┐
                         v                                               v
              MediaTurnEndpointMixin                         InteractionPlane
              候选端点、ASR 覆盖、提交锁                    + InterruptionPolicy
                         |                                               |
                         └──────────────> DuplexRuntime <────────────────┘
                                      generation / output actions
```

职责必须保持单一：

- 现有 `SpeechTimeline + ConversationProjection` 是唯一连续话轮投影，不新建平行 projector。它只把已有证据投影成 phase、floor 和原因，不调用 provider、不取消 generation、不自行写历史。
- 现有 `FloorState` 继续只表达 `user_holds_floor / assistant_holds_floor / overlap / uncertain / silence`；新增细粒度 `TurnPhase` 表达话轮进展。两者含义不同，但必须由同一个 `ConversationProjection` 原子更新。
- `MediaTurnEndpointMixin` 继续拥有 endpoint grace、ASR sample coverage、bounded tail、提交锁和重试边界。
- `InteractionPlane` 与 `InterruptionPolicy` 继续拥有 duck、continue、backchannel、interrupt、prefetch 等决策；`UtteranceRouter` 继续是 enroll、纯控制词、interrupt+chat 和普通 chat 的唯一语义入口。
- `DuplexRuntime` 只执行已通过当前 fence 校验的决策，不新增按状态散落的 if/else。
- `PlaybackLedger`、`ReplyDeliveryLedger` 和设备回执继续定义输出是否开始、结束、失败或 Actual Heard；输入状态不得改写这些事实。

### 时间基准与窗口

- PCM 与所有话轮证据统一使用 16 kHz 绝对 capture sample clock；不得用回调到达时间替代音频位置。
- 既有 VAD 继续以 20 ms 运行，保证 onset 和本地 duck 不被额外延迟。
- phase 投影按连续四个 20 ms 音频 frame 聚合为一个 80 ms `ProjectionFrame`；VAD start/end、ASR revision 与播放证据按 sample range 落入窗口。80 ms 窗口不启动独立周期 timer，避免事件循环抖动造成漂移。
- 一个 80 ms frame 在 16 kHz 下覆盖 1,280 samples。`frame_index` 只由 `capture_start_sample // 1280` 派生，同一 `stream_epoch` 内不得倒退。
- ASR partial/final 按自身绝对 `capture_start_sample..capture_end_sample` 投影到重叠窗口。迟到 revision 可以替换同一逻辑片段的观察值，但不能回写已提交 sample watermark，也不能跨 `stream_epoch`。
- 物理 BOOT stop、已签名本地 hard-stop KWS 和旧 fence 丢弃不等待 80 ms 聚合，继续走现有即时权威路径。

### 内部状态契约

Memoria 不复制第三方标签作为线上协议，`ConversationProjection` 内部 `TurnPhase` 使用以下稳定语义：

| 状态 | 含义 | 允许的直接效果 |
| --- | --- | --- |
| `IDLE` | 当前窗口没有可采信的近端语音，且没有未决语义证据 | 无 |
| `ACOUSTIC_ONLY` | 有 VAD/能量证据，但尚无稳定用户语义；可能是起音、噪声、回声或很短声音 | 播放时可临时 duck；不得启动 LLM/TTS、commit 或 cancel |
| `SEMANTIC_SPEAKING` | ASR partial/final 已形成非 backchannel 的用户内容，话轮仍可能继续 | 取消 pending endpoint；播放时提交给打断策略评估 |
| `END_CANDIDATE` | 已有用户语义，且声学结束与 ASR 覆盖满足候选条件 | 只唤醒现有 endpoint 协调；不得直接 commit |
| `BACKCHANNEL` | 播放期间出现短确认、附和或不争夺话权的表达 | 继续播放，不进入 chat，不写历史 |
| `UNCERTAIN` | 声学、语义、AEC、说话人或时序证据冲突/缺失 | HOLD；继续收集，超时走现有 fail-closed 边界 |

状态和动作必须分离。`END_CANDIDATE` 不是 `COMMIT`，`SEMANTIC_SPEAKING` 不是 `CANCEL`，`ACOUSTIC_ONLY` 不是“用户正在说话”，`BACKCHANNEL` 不是说话人身份结论。

每个不可变 `ProjectionFrame` 至少携带：

| 字段 | 约束 |
| --- | --- |
| `session_id`、`stream_epoch` | 必须与当前 `SessionIdentity` 一致 |
| `frame_index`、`capture_start_sample`、`capture_end_sample` | 单调、连续、同一 16 kHz 时钟；缺口显式标记 |
| `state`、`reason` | 枚举值；`reason` 只能来自低基数规则表 |
| `vad_active`、`vad_probability` | 缺失保持 `None`，不得默认可信 |
| `asr_task_epoch`、`asr_revision`、`asr_coverage` | 只接受当前 stream/task 的最新合法 revision |
| `semantic_text_present`、`asr_final` | 只记录布尔事实；指标和普通日志不得包含原文 |
| `speaker_class` | `owner / guest / uncertain`；不能由 turn 状态推导 |
| `aec_verified`、`residual_echo_score` | 无硬件证据时保持未验证 |
| `captured_fence` | 当前完整 `session_epoch + turn_id + generation_id + tool_epoch` 快照 |
| `loss_concealed`、`discontinuity` | 任一为真时提高到 `UNCERTAIN`，不能快进提交 |

规则投影不产生伪概率。只有上游真实提供概率时才保留对应字段；规则输出用 `reason` 和 evidence flags 解释。

### 状态转换

| 当前状态 | 新证据 | 下一状态 | 约束 |
| --- | --- | --- | --- |
| `IDLE` | 合法 VAD start / 近端能量 | `ACOUSTIC_ONLY` | 可 prefetch/duck，不取消输出 |
| `ACOUSTIC_ONLY` | 稳定且非 backchannel 的 ASR 内容 | `SEMANTIC_SPEAKING` | 必须绑定当前 stream/task revision |
| `ACOUSTIC_ONLY` | VAD end 且始终无语义 | `IDLE` | 记为无内容候选，不创建 turn |
| `ACOUSTIC_ONLY` | 回声、噪声、时钟缺口或证据冲突 | `UNCERTAIN` | 无取消/提交副作用 |
| `SEMANTIC_SPEAKING` | 新 VAD/partial 延续 | `SEMANTIC_SPEAKING` | 撤销尚未提交的 endpoint candidate |
| `SEMANTIC_SPEAKING` | 播放期短附和且 guard 确认 | `BACKCHANNEL` | 继续当前 generation |
| `SEMANTIC_SPEAKING` | VAD end + ASR 覆盖 endpoint | `END_CANDIDATE` | 只进入现有 endpoint 协调 |
| `SEMANTIC_SPEAKING` | VAD end 但 ASR 未覆盖/存在丢帧 | `UNCERTAIN` | 等 bounded tail 或绝对超时 |
| `END_CANDIDATE` | 新合法 VAD start 或更新 partial | `SEMANTIC_SPEAKING` | 取消 pending timer，endpoint 不得倒退 |
| `END_CANDIDATE` | grace 到期 + 合法 final 覆盖 | `IDLE` | 由现有 commit 路径原子提交后 reset |
| `BACKCHANNEL` | 播放继续且无后续内容 | `IDLE` | 不创建用户历史 turn |
| `BACKCHANNEL` | 后续形成普通内容 | `SEMANTIC_SPEAKING` | 按普通抢话重新评估 |
| `UNCERTAIN` | 证据恢复一致 | 对应确定状态 | 不回放过期副作用 |
| 任意 | stream/fence 过期 | 不转换 | 先丢弃，再做连续性检查 |

`UNCERTAIN` 是保守等待，不是错误恢复入口。到达现有 absolute endpoint timeout 后，只能使用当前 `MediaTurnEndpointMixin` 已定义的 bounded partial/final 规则；不得另加“猜测用户说完”的旁路。

### Endpoint 与儿童停顿

当前儿童语音可能包含 500–800 ms 话中停顿，既有 0.7–1.1 s adaptive grace 与 2.5 s absolute timeout 继续生效。状态层只改善候选质量，不直接缩短这些边界。

端点提交必须同时满足：

1. 已观察到 `SEMANTIC_SPEAKING`，纯声学活动不能产生用户 turn。
2. VAD final 给出不倒退的 `voiced_end_sample`，并形成 `END_CANDIDATE`。
3. 当前合法 ASR final 覆盖 endpoint，或在现有容差与 finalized audio watermark 规则内得到证明。
4. grace 期间没有更新 VAD start、语义 partial 或更高 revision 撤销候选。
5. `turn_commit_lock` 内再次校验 stream/task/fence，提交后才允许持久化、delegation 和回复 generation。

首版不根据标点、文本长度或关键词自行缩短 grace。只有 replay 与真实设备证明某个确定性条件在儿童语音上不截断，才可把它加入同一 endpoint 状态机，并同时补充撤销路径和测试。

### 播放期打断与 backchannel

播放期间按证据强度处理：

1. BOOT 物理 stop 和已签名本地 hard-stop KWS 立即执行，不受状态层、说话人或安全回复阻塞。
2. `ACOUSTIC_ONLY` 只允许可逆 duck 与继续收集；不得取消 generation。没有 AEC reference 时尤其保持保守。
3. `BACKCHANNEL` 继续播放，不创建 chat turn，不进入主人历史。
4. `SEMANTIC_SPEAKING` 只生成 interruption candidate，仍由 `InterruptionPolicy` 结合显式控制语义、持续时长、speaker class、AEC residual、VAD probability 和安全回复规则决定。
5. 真打断只能在完整当前 fence 下先推进 generation，再取消 provider/output；迟到 PCM、TTS、工具和播放回执按旧 generation 丢弃。
6. `UNCERTAIN` 保持 duck/HOLD；超时、无文本、回声相似、未锚定 playback transcript 均 fail closed，不把不确定性升级成 chat。

`UtteranceRouter` 继续区分纯 interrupt、interrupt+chat 和普通 chat。状态层不得维护第二份“停一下/继续/不用了”等词表。

### 环境声、说话人与权限

Turn state 回答“当前话轮发展到哪一步”，speaker classification 回答“可能是谁在说话”，两者必须正交：

- 环境电视或旁人形成 `SEMANTIC_SPEAKING`，也不能因此获得 owner 权限、历史、私人记忆或工具。
- 明确 guest 可以停止公开播放，但后续内容仍经过 Target Speaker 和权限门禁。
- `speaker_class=uncertain` 可以在产品规则允许时普通对话，但保持 non-owner，不写主人历史。
- AEC 未验证时不得把播放期声能标成近端真实语音；缺失字段保持 `None`，策略使用更长持续阈值。
- 环境误聆听率必须由带场景标签的 replay/真实设备证据计算，不能从运行日志中的状态次数推断真实误报率。

### 性能设计

CPU-only 状态层必须满足以下预算；未实测前只称目标，不称已验证：

- 每会话每 80 ms 一次 O(1) 转换；`ConversationProjection` 只保留当前 phase/floor、少量连续 frame 计数、最新合法 ASR revision 和 pending endpoint，不保存无界 frame 列表。
- 不复制 PCM；只消费既有 VAD/ASR metadata 和 sample ranges。
- 不新增线程、进程、WebSocket、RPC、数据库查询、模型加载或 provider 调用。
- 不新增独立 timer loop；复用音频事件和现有 endpoint timer，避免大量会话产生唤醒风暴。
- 单次 `apply_continuous_event()` 增量 CPU p95 目标小于 2 ms，单会话增量内存目标小于 64 KiB，且 ASR task 创建/轮换次数不得增加。
- 只有 `SEMANTIC_SPEAKING` 才允许昂贵 prefetch/warm/delegation 候选；`ACOUSTIC_ONLY` 不启动 LLM/TTS。
- 状态转移保持同步纯函数；异步副作用仍在既有 runtime 执行，从而不扩大 `turn_commit_lock` 持锁时间。

性能收益分开衡量：决策 CPU 成本、VAD-to-duck、ASR-partial-to-interrupt、VAD-end-to-commit、误打断次数和无内容 ASR/provider 工作量。不得用“平均响应更快”掩盖截断率或误触发上升。

### 可观测性与隐私

保留已有 `voice_vad_onset_ms`、`voice_asr_partial_latency_ms`、`voice_asr_final_latency_ms`、`voice_turn_commit_latency_ms`、`voice_interrupt_duck_latency_ms`、`voice_interrupt_stop_latency_ms` 和 false-interrupt 指标。状态层新增指标时必须加入 `telemetry.py` 的固定 allowlist，标签只能使用枚举，不含原文、音频、账号、设备或无界 session ID：

- `voice_turn_state_transition_total{from_state,to_state}`
- `voice_turn_end_candidate_latency_ms`
- `voice_turn_end_candidate_retracted_total{reason}`
- `voice_turn_uncertain_total{reason}`
- `voice_backchannel_filtered_total`
- `voice_acoustic_only_cancel_blocked_total`

OpenTelemetry span 可以记录 `turn.state_changed` 与 `turn.end_candidate`，但只附当前 fence 摘要、sample range、状态、枚举 reason 和 evidence flags。普通日志与指标禁止写 ASR 原文；需要检查文本的 consented replay 证据写入被忽略的 `outputs/`。

状态层不自动扩大 Control API 的聚合 SLO 上报；只有确定了生产门槛的指标才能加入 `slo_reporter.py` allowlist。

### 代码归属与最小实现

首版扩展现有投影，不新增控制模块，也不改 Go/proto/ESP32/H5 契约：

- `services/agent/src/orchestration/conversation_projection.py`：在现有 `FloorState`、`ProvisionalTurn` 和 commit 校验中加入 `TurnPhase`、不可变 `ProjectionFrame` 与有界连续 frame 计数；不导入 provider/runtime 副作用。
- `services/agent/src/voice_core/media_session_state.py`：继续只持有一个 `ConversationProjection`；断线、stream epoch 或身份 epoch 变化时原子 reset phase 与 provisional state。
- `services/agent/src/voice_core/media_session_input.py`：把已接受的 VAD/sample/AEC/speaker evidence 投影进窗口。
- `services/agent/src/voice_core/media_session_commit.py`：把已通过 `ASRStreamSupervisor` 的 partial/final revision 投影进窗口，并把状态作为 `InteractionSnapshot` 的证据，不绕过 Router/Guard。
- `services/agent/src/voice_core/media_session_turns.py`：消费 `END_CANDIDATE` 仅用于调度现有 commit；保留全部 coverage/grace/lock 约束。
- `services/agent/src/voice_core/telemetry.py` 与 `replay_harness.py`：加入低基数指标和确定性状态序列。
- `services/agent/tests/unit/test_conversation_projection.py`：扩展 phase/floor/commit 原子性覆盖；现有 media-session、interruption、speech-timeline 和 replay 测试继续覆盖集成边界。

首版 `TurnPhase` 只在 Python 内部和低基数 telemetry 中使用，不增加新的 `SegmentKind`，也不修改 media-v1 `FloorState` 或 `ProjectionEvent`。只有出现明确的跨进程消费者且收益经过验证时，才允许在同一提交内修改 proto、生成物、Go/Python 消费者与跨语言测试。

不要给每个状态增加环境变量。首版复用现有 VAD、endpoint、backchannel 和 interruption 阈值；只有真实证据证明需要独立调节时，才在一个配置 dataclass 中增加字段。

观察期允许 `ConversationProjection` 的新 phase 与当前行为并行产出证据，但 phase 没有副作用。启用后必须删除被它取代的重复状态分支和测试，不长期维护两套行为或自动回退。

### 实施顺序

| 阶段 | code | wired | enabled | verified |
| --- | --- | --- | --- | --- |
| A. 纯状态机 | 扩展 ConversationProjection 类型、转换表、单元测试 | 未接新行为 | false | CPU/确定性测试 |
| B. Shadow 接线 | VAD/ASR/playback/fence 投影、指标 | 当前 MediaSession | false，无副作用 | replay 对比与资源预算 |
| C. 单一策略切换 | 状态进入现有 Interaction/Endpoint 决策；删除重复分支 | Agent/Bridge 候选 | canary | provider smoke + 集成测试 |
| D. 真实设备 | 不新增模型，仅调既有规则 | 当前 ESP32 Direct 链 | production candidate | 自有语音、环境声、播放回声、backchannel、抢话、连续轮次、Actual Heard |

阶段 B 不改变用户行为。阶段 C 必须是一次可审查的单一权威切换；如果 replay、CPU、provider 或集成门禁失败则不启用，而不是在运行时静默双轨。

### 测试与验收矩阵

单元状态序列至少覆盖：

- `IDLE -> ACOUSTIC_ONLY -> SEMANTIC_SPEAKING -> END_CANDIDATE -> commit/reset`。
- `END_CANDIDATE -> 新 VAD/partial -> SEMANTIC_SPEAKING`，证明儿童停顿不截断。
- 播放期 `ACOUSTIC_ONLY` 不 cancel，`BACKCHANNEL` 继续，普通用户内容进入 interruption policy。
- 未锚定 playback transcript、回声、低 VAD、丢帧和不完整 AEC 进入 `UNCERTAIN` 或现有 guard。
- 旧 stream/task revision、旧 generation/tool/session epoch 先丢弃，不改变状态。
- ASR late final 只能更新合法 pending endpoint，不能覆盖已提交 watermark。
- hard stop 不等待 80 ms；安全回复拒绝语义打断但不阻塞物理 stop。

Replay 使用现有 `adult_clean`、`child_clean`、`child_pause`、`tv_background`、`assistant_playback_echo`、`stop_commands`、`backchannels` 和 `network_reconnect` 场景。每个场景同时比较旧基线与 shadow 状态层：

- 话轮数、endpoint 截断/合并、ASR tail 完整性。
- VAD-end-to-commit、partial-to-duck、partial-to-cancel 延迟。
- false listen、false interrupt、backchannel 错杀和无内容 turn。
- CPU p50/p95/p99、单会话内存、event-loop lag、ASR task churn。

真实设备至少完成：

1. 主人正常距离连续两轮，自然停顿不截断，回答均有同 fence `playback.ended + Actual Heard`。
2. 电视/旁人说话不产生主人权限、不自动进入 chat；公开播放是否停止按既定 guest 策略记录。
3. 助手播放自身回声不取消 generation；短“嗯/对”继续播放。
4. 用户在播放期间说完整新内容，先可逆 duck，再由语义证据确认取消，旧 generation 不再出声，新 turn 可完成 Actual Heard。
5. BOOT 与本地 hard-stop 始终立即工作，断线/重连和迟到事件不复活旧状态。

只有同一候选的 `code / wired / enabled / verified` 与证据日期写入 `HANDOFF.md` 后，才可宣称该状态层上线。Shadow 指标、零会话 readiness、本地测试或首帧均不能证明真实设备误触发、打断或全双工改善。

## UI 产品约束

新账号先选择星澜、桃喜、绵绵、阿序或玄墨。主人声纹由小程序记录授权、机器人端采集，手机不获取录音；当前登记先生成 shadow 档案，只有 Speaker Authority 返回 `active` 后才能作为主人认证，也不得与声音克隆混为一谈。

称呼仅在注册时设置，字段为“怎么称呼你？”；“我的/个人信息”不再提供称呼或陪伴方式编辑入口。伙伴音色只通过服务器批准的稳定目录键解析供应商 `voice_id`。

吉祥物的用户情绪只消费权威 `emotion_observation`；助手说话时只消费 Agent 发布且与 `session_id + turn_id + generation_id + tool_epoch` 匹配的 `assistant_expression`。客户端不得从字幕猜表情，回答结束、断线或中断时必须清除。动效需尊重 `prefers-reduced-motion`。

## ESP32 固件

固件以固定 `78/xiaozhi-esp32` upstream 加小型 overlay 维护。锁定版本、commit、ESP-IDF 和传递依赖分别以 `firmware/esp32/upstream.lock` 与 `overlay/files/dependencies.lock` 为准；缓存、工具链和构建产物不提交。

`MemoriaBootstrap` 负责签名二维码、Protocomm Security 1、Wi-Fi 写入、online-proof 和 Activation 重试。设备未绑定时保持附近配网入口；收到并确认 Activation Manifest 后停止二维码/BLE 配网面并进入正常会话状态。小程序端的对应实现位于 `apps/miniprogram/utils/device-onboarding`，跨端响应结构以 `packages/contracts/device-onboarding-v1.json` 为准。

```bash
cd firmware/esp32
./scripts/bootstrap.sh            # 可加 --no-idf-install
./scripts/build.sh --clean
./scripts/check-overlay.sh
./scripts/flash.sh --list
./scripts/flash.sh --port /dev/cu.usbmodemXXXX --monitor
```

默认启用本地唤醒词“梅莫里亚”（`mei mo li ya`）；普通“你好你好”不是唤醒词。短按 BOOT 可启动会话；播放期间 BOOT 是本地物理硬停止权威。只有排查媒体问题时才构建 `./scripts/build.sh --wake-word disabled`。

Mac 进入下载模式：按住 BOOT，轻按 RESET，松开 RESET，再松开 BOOT，然后重试。monitor 使用 `Ctrl+]` 退出。

### 身份分区安全

Flash `0x10000..0x1ffff` 是独立 `memoria_identity` NVS。普通固件更新不得写入该区：优先 app-only 刷写 `0x20000`，整包刷写前后都必须回读身份区并逐字节比较。任何身份摘要变化、Manifest v2 验签失败或设备 SKU 不符都必须停止验收，禁止靠重置身份绕过。

长按 5 秒只重置网络配置，不清除身份分区，也不解除云端绑定。当前没有启用 Secure Boot、Flash Encryption 或 eFuse，量产安全不可由研发身份验收替代。

### 设备媒体协议

固件请求 `supported_protocol_versions: [2, 1]`，首选 v2：

- direct Edge 是 v2-only；direct WSS 不接受 v1。
- v1 只由 legacy livekit_compat Gateway 承接滚动发布或明确回滚。
- the direct edge is v2-only and never accepts a v1 hello.

v2 的 `session_epoch + turn_id + generation_id + tool_epoch` 完整 fence 贯穿 generation、PCM 和 playback 回执。下行 sequence/sample 时钟按 generation 从 0/0 开始；旧 generation 帧必须在连续性检查前丢弃。`playback.ended` 只能在 completion 到达且真实解码/播放队列排空后上报；网络收到帧不是 Actual Heard。

当前板无 AEC reference、无自然双讲、无精确 DAC 采样计数器。回执水位属于保守播放边界；`full_duplex_verified` 只能由真实声学验收产生。

## 模型与第三方来源

DTLN 降噪固定到 `breizhn/DTLN` commit `1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc`，MIT License。运行契约为 16 kHz mono、512-sample block、128-sample shift、每会话独立 recurrent state。机器可读来源和摘要在 `services/agent/models/dtln/provenance.json`，许可证保留在同目录 `LICENSE`。

第三方模型、声音和上游代码必须带固定来源、版本/commit、摘要和许可证；二进制模型不因位于仓库中而变成 Memoria 自有代码。生成物和预览资产若可由权威源重建，不作为长期文档或发布证据保留。

## 排障顺序

- 无响应：设备状态/票据 -> WSS epoch -> VAD -> ASR final -> Agent generation -> 首个 0/0 下行帧 -> playback terminal。
- 说话距离过近：板端输入增益/codec -> 原始 PCM RMS/peak -> DTLN 输入输出 -> VAD 阈值；不要只调云端识别阈值。
- 回答中断后失联：同一 fence 检查 stop epoch、successor cancel、迟到旧帧、first-frame 连续性和 WSS close cause。
- 打断后仍播旧内容：先推进 generation，再取消 provider/session，并在 Edge、设备和投影处比较完整 fence。
- H5 transport 已连但不可用：必须等当前 Agent 的显式 `assistant_state: ready`，不能把 LiveKit connected 当业务 ready。

当前线上镜像、证据层级、发布与剩余真实设备验收见 `HANDOFF.md`。
