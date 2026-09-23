# HANDOFF 历史归档：2026-09-20 及更早

主交接已在 2026-09-23 将永久运维材料与历史流水拆分。本文件保存移出的 2026-09-20 发布、设备窗口、F2 和待命复现证据；当前 HANDOFF 没有独立的 2026-09-20 之前日期节。删除范围与 seal 契约的完整原文已迁入 [docs/compliance/delete-domains.md](compliance/delete-domains.md)，本归档保留该链接以避免重复维护。

## 2026-09-20 发布与模拟音频验收（F1/F2 上线）

- 发布 tag `20260920-f1f2-owner-silence-and-barge`（commit `d61d486`，tag 已随仓库推送）。依赖输入变更（`pyproject.toml`、`infra/Dockerfile.agent`）使组件快车道与 `delta_build_images.sh` 按设计拒绝，本次走本地 linux/amd64 全量构建 + 按生产既有机制切流（`component-releases/<tag>/*.override.yml` + `docker compose -p memoria … --profile media-runtime up -d --no-deps --no-build`）。
  - agent / voice-core-media-bridge：`memoria-agent:20260920-f1f2-owner-silence-and-barge`，收据 `/opt/memoria/component-releases/20260920-f1f2-owner-silence-and-barge/CUTOVER_RESULT.txt`（含 sha256），回滚镜像 `memoria-agent:20260916-livekit-181-v1`。
  - media-edge：`memoria-media-edge:20260920-f1f2-owner-silence-and-barge`，收据 `MEDIA_EDGE_CUTOVER_RESULT.txt`（含 sha256），回滚镜像 `memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`。
  - 两次切流后全栈复核：agent/bridge/media-edge/control-api/device-media-gateway 全 healthy；其它服务镜像未改动。
- 操作事实（此前未记录，易踩）：agent 心跳上报的 `release_tag` 必须等于 control-api 自身的 `MEMORIA_RELEASE_TAG`；control-api 的组件 override 把它钉在 `20260901-0945-wake-word-whitelist`（commit `7ca3d4ec`）。故 agent 的 override 必须钉同一值，否则心跳 409 `agent release tag does not match config`、容器 healthcheck 恒 unhealthy。本次切流修正了该既存漂移，回滚 override 同样钉值，保证失败路径不劣化。
- 身份口径（勿误读）：切流后镜像自身的 OCI label 是本次候选（`version=20260920-f1f2-owner-silence-and-barge`、`revision=d61d486`、`role` 按组件），但 agent/bridge 容器**上报**的身份是 Control 既有校验要求的 `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist` / commit `7ca3d4ec`。这是为通过 `readiness.py` 的 tag 相等校验做的**身份对齐**，不等于候选 tag/commit 全链路一致；候选的真实身份以镜像 label 与切流收据为准。
- 后续项（发布治理）：收敛 stack/组件身份——让 control-api 的期望 tag 随真实发布 tag 走，而不是让 agent 冒充历史冻结 tag。当前做法保留为临时对齐手段，列入 P1-01 与发布工具输入。
- 模拟音频验收（电脑扬声器放合成 TTS + 麦克风收音，`scripts/wake_word_matrix.py`，`uv run --with pyserial`，`--trials 1 --warmup-s 50 --unmute --output-volume 80`）：近距档 1/1、远距档 1/1 唤醒，tv/small_talk/quiet 误唤醒 0；设备控制台同批证据为 `Wake word detected: 茉莉 (state: 3) -> idle->connecting -> connecting->listening -> listening->speaking -> speaking->listening`，即唤醒、开会话、机器人出声应答并回到待听，且运行在本轮新发布的 agent/media-edge 上。运行后系统音量已还原（31/muted=true）。
- 未验证边界：F1 的"播后重新给足 30s 窗口"与 F2 的"禁止源 barge 只拒交接、不清播放窗口"**尚未**在设备上做行为复验，需要问答+打断序列。`outputs/design/auto-audio-20260915/auto_audio_session.py` 在本机被自身自检挡住：①live 门禁受 ffmpeg 8 的 WAV 缓冲影响（文件约 10s 才刷到 256KB，默认 10s 窗口刚好失败；`--ffmpeg-start-timeout-s 25` 可绕过）；②`blocked_reference_match`——三块归一化波形相关（NCC≥0.20）在本机扬声器→麦克风路径不成立，而同一录音的播放段电平（峰值 -1.34 dBFS、margin 36 dB）证明链路本身可用。该判据调整前，问答扫频需人工说话或修工具。
- 发布工具缺口（P1-01 输入）：`deploy_agent_component.sh` 只支持"服务器上薄镜像"，依赖输入变更即拒绝，且无预构建镜像入口；全量制品清单（5 角色）不含 media-edge，也没有对应的切流脚本。建议补 `--target-image` 预构建路径（校验 revision/arch/role 后复用其门禁与回滚）。

## 2026-09-20 设备窗口复验（F1，跑在新发布的 agent/media-edge 上）

- 方法：电脑扬声器放合成 TTS，唤醒复用已验刺激 `outputs/acceptance/run-20260920-wake-matrix-v3/stimuli/wake.wav`，问句按 `say`+`afconvert`+0.25s 前导/0.4s 尾随静音重建；输出 80% 且 readback 验证（结束还原 31/muted=true）。串口与服务端日志由 `scripts/voice_session_capture.py --server-logs` 采集成 `outputs/acceptance/run-20260920-f1-device-window-v2/`（ignored）。
- 设备控制台时间线（+08:00）：21:38:26 standby → +52s 一次唤醒成功（`Wake word detected: 茉莉`）→ `idle->connecting->listening` → 欢迎语 `listening->speaking->listening`（21:39:22–25）→ 续问答对三轮（21:39:34 / 21:39:38 / 21:40:16 进入 speaking）→ 播放“再见”后**同一秒** `speaking->idle`（21:40:21.609）。
- 观察（**不是**判据通过，此前表述已降级）：会话从 21:39:20 连续服务到 21:40:21（约 61s），期间多次 `listening->speaking` 输出；即**上一轮输出结束后跨约 8.2s / 11.0s 仍有后续输出**——旧语义只沿用剩余预算（本例 ~4.4s）时难以维持这么久。
- **可归因的部分**（按刺激起点换算，脚本打印是 `afplay` 播完之后，故以起点计）：q3 起点 ≈21:39:29–32 → 话轮 21:39:34.054（距欢迎语结束 21:39:25.227 约 **+8.8s**）；q8 起点 ≈21:40:09 → 话轮 21:40:16.475（距上一轮应答结束 21:40:01.035 约 **+15.5s**）。二者共同支持“上一轮输出结束后跨 ≥8s 仍有新一轮输出”。
- **未证实/已撤回**：① 撤回“+3/+5/+8 三格全通过”与“F1 判据通过”；② **q5 未验证**——其音频起点 ≈21:39:41.7，而 21:39:38.075 已开始的话轮（无对应刺激）一直持续到 21:40:01.035，q5 的音频落在**既有话轮内部**、未产生新话轮，故 q5 的“被接受”无证据；③ 审计提到的 `turn_id=2 / tool_waiting->thinking_silent` 判读**未能证实**（采集的 `agent.log` 为空文件；生产 `docker logs memoria-agent-1` 在该窗口无匹配行），本轮采用其结论但按“独立时序论据”标注为未证实；④ “告别立即待命”为单次观察（bye 起点 ≈21:40:19.8 → `speaking->idle` 21:40:21.609 ≈ +1.8s，早于旧预算到期时刻，无对照）。
- 佐证（Voice Core 侧，`run-20260920-f1-device-window-v2/bridge.log`）：`media ASR result rejected … reason=interval_conflict`（13:39:25）、`reason=cross_sentence_overlap`（13:39:26、13:39:32）、`media duplicate media turn skipped`（13:39:33）——按 16kHz 换算，被拒样本窗为 0.66–4.25s 与 0.66–11.0s，确证输入跨句/重叠、话轮与我的播放**不构成一一对应**，进一步支持上述降级。
- 仍可直接归因的一项：**待命后立即再唤醒**（21:49:30.350 `listening->idle` → 21:49:33.846 `Wake word detected` → 21:49:36 新会话开启），判据来自设备端 wake 行与新会话本身。
- 负向核查：串口无 `barge_source_forbidden`、无 session error、无 retryable；唯一 error 为无关的 BMI2 I2C 传感器超时。
- 未覆盖：F2 打断语义（禁止源 barge 只拒交接；播放窗口只由设备本地 flush 的 `button.stop` 撤销）需要**按压设备按键**产生真实打断，脚本无法替代；“待命后立即再唤醒不弹错”与 +3s/+5s 极短格本轮未单独取值（脚本实际延迟为 +6.8/+8.2/+11s）。
- 本机工具结论：`auto_audio_session.py` 的参考匹配在本机是边缘值（三块 NCC 0.11–0.28，含空段的那块低于 0.20 阈值），且 ffmpeg 8 约每 8s 才整块落盘（`-flush_packets`/`-avioflags direct` 均无效，live 门禁需 `--ffmpeg-start-timeout-s 25`）。因此本机问答扫频改用“设备控制台为时间源”的方式，未放宽工具判据。
- 自检匹配器诊断（一次性、无代码改动，2026-09-20）：用更长且无明显重复音节的 selftest 文本（`--selftest-text "请确认扬声器到麦克风的通路工作正常一二三四五六七八"`，参考 5.90s、录音 20.40s）复算工具判据，结果仍为 `blocked_reference_match`（fail-closed 保持）。逐块读数（工具规定 ±150ms 窗口内）：chunk0=0.249（通过）、chunk1=0.407 残差 -0.148s、**chunk2=0.144（低于 0.20）残差 -0.147s**（无约束峰值 0.267 落在窗口外 -0.17s）。两块残差稳定在 ≈ -0.15s，属**固定时序偏移**而非随机噪声；不改 NCC/skew 阈值、不改匹配器。结论：本机问答扫频继续走“设备控制台为时间源”的独立路径；匹配器若修，需附该时序偏移的回归样本，并保留唯一候选/每块 NCC/拟合残差证据。

## 2026-09-20 F2 禁止源 barge：**未验证**（此前结论已撤回）

- 撤回原因：`services/media_edge/device_ws_uplink.go:111 ignoreForbiddenBarge` 在被触发时**必然**打印 `media edge ignored barge from a forbidden source session=… source=…`，而三次采集（`run-20260920-f2-barge-window`、`run-20260920-f2-barge-counter`、`run-20260920-rewake-after-standby`）的 `edge.log` 中该行为 **0**；即我制造的“播放中重放唤醒词”**没有**产生被忽略的禁止源 barge（设备把它当成合法新话轮/新会话）。故原先“语音打断路径通过”的说法不成立。
- 修复契约（代码自证）：`ignoreForbiddenBarge` 只对**签名设备设置 `DeviceSettings.AllowedBargeIn` 未允许的源**生效——`handleVAD`（源 `voice`，仅当 `vad.start` 且 `playbackActive`）、`handleKeyword`（源 `keyword`，与播放无关）、`handleButtonStop`（源 `button`）；被忽略时帧**从不转发**、助手保持话语权、**不发 session error**（epoch/序号/形状违规仍关通道）。`button` 还额外 `clearPlaybackActive("device_button_stop", …)`。
- 计数证据不可得（已定案）：Edge `/metrics` 只在私有监听 `:8081`。直连容器 IP（`172.19.0.12`）发明文请求返回 **400**，响应体原文 `Client sent an HTTP request to an HTTPS server.`；改用 `https://` 则返回 `tlsv13 alert certificate required`，即该口为 **mTLS**、需客户端证书。故计数路线在生产不可用，判据只能取日志。
- 日志判据（已取真值）：`docker logs memoria-media-edge-1 | grep -c "ignored barge"` = **0**，覆盖新镜像上线至今**整个容器生命周期** ⇒ 禁止源 barge 从未被忽略过，F2 该路径**未触发**。
- 第三次尝试（`run-20260920-f2-barge-counter`，播放中打断）同样**未**出现 `ignored barge` 行；该轮 barge 结束后 `speaking -> listening`（距 barge 结束 0.00s），随后 20s 内追问**未被接受**，设备无 error 行。故本轮既未复现禁止源 barge，也未取得“打断后会话继续服务”的正向证据，结论维持**未验证**。
- 待办（复现所需）：读取该设备的签名 `allowed_barge_in`（`services/media_edge/device_ws_auth.go:118`），据此构造会话内、被禁止源的 barge；`button` 源只能在设备上产生（触摸面板/触摸按键，`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_esp_vocat.cc` + `touch_button_sensor.h`，固件 `MemoriaProtocol::SendButtonStop`），**需要人到设备旁**或另建非生产 edge 复现。

## 2026-09-20 待命后再唤醒回归（原始症状）与设备侧异常

- 回归（跑在新发布镜像上，独立路径）：21:48:24 `activating->idle` → +50s 预热 → 21:49:22 一次唤醒成功 → 21:49:24 会话开启 → 21:49:28 欢迎语结束 → 21:49:30 播放“再见”后**同秒** `listening->idle` → **21:49:34 立即再唤醒成功**（`Wake word detected` 21:49:33.846）→ 21:49:36 **新会话开启**。结论：原报告症状“待命后再唤醒被拒”**未复现**；本轮无 `barge_source_forbidden`/error。收据 `outputs/acceptance/run-20260920-rewake-after-standby/`（脚本被操作者提前停止，最终 RESULTS 行未打印，证据为上列控制台时间线）。
- **设备侧异常（与刺激无关，发生于空闲期）**：① `BMI2_ESP32: I2C read reg 0x03 len 24 failed: ESP_ERR_TIMEOUT` 慢性持续（IMU 读失败，短时可 ~10 条/秒）；② 端口复位后两次 `abort() was called at PC 0x4038acd6 on core 0` → `rst:0xc (RTC_SW_CPU_RST)`，每次约 12s 后再起，随后 `MemoriaEspVocat: BMI270 initialized` 并恢复正常运行。两次 abort 均发生在**尚未播放任何刺激**的空闲窗口内，因此不能归因于本轮 F1/F2 测试；需按硬件/固件路径单独排查（刷写需另行授权）。
- 仍未覆盖：**设备本地按键**触发的 `button.stop` 撤销播放窗口（需人手按压）；+3s/+5s 极短续问格未单独取值。



## 已迁出的删除域与 seal 契约

详见 [删除域与 seal 契约](compliance/delete-domains.md)。该文档保留原有 2026-09-20 删除范围结论、17 表约束、读口影响、残余泄漏面和“不可归属”表述纪律。
