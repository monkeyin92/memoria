# Memoria ESP32 固件交付

本目录是 Memoria 主仓库中的 ESP32 固件入口，采用“固定 upstream + 小型 overlay”方式维护。不会复制 `xiaozhi-esp32` 整仓，也不会把 `managed_components`、ESP-IDF 工具链或生成的二进制提交到主仓库。

## 当前锁定

| 项目 | 值 |
| --- | --- |
| upstream | `78/xiaozhi-esp32` |
| upstream version | `v2.4.2` |
| upstream commit | `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5` |
| ESP-IDF | `v6.0.2` |
| target | `esp32s3` |
| board identity | `memoria-atk-dnesp32s3-v1` |

上游与工具链锁定值以 [`upstream.lock`](upstream.lock) 为准，ESP Component Registry 的传递依赖由 `overlay/files/dependencies.lock` 固定。`scripts/bootstrap.sh` 会校验远端、commit 和 ESP-IDF 版本，然后把 `overlay/` 应用到本地缓存的 upstream 工作树。

## 目录与工作流

```text
firmware/esp32/
├── overlay/
│   ├── files/dependencies.lock
│   ├── files/main/boards/memoria/atk-dnesp32s3-v1/
│   └── patches/
├── scripts/
│   ├── apply-overlay.sh
│   ├── bootstrap.sh
│   ├── build.sh
│   ├── check-overlay.sh
│   ├── common.sh
│   ├── flash.sh
│   └── monitor.sh
├── upstream.lock
└── README.md
```

首次准备（会按需安装 ESP-IDF 6.0.2；不需要时可加 `--no-idf-install`）：

```bash
cd /Users/monkeyin/projects/memoria/firmware/esp32
./scripts/bootstrap.sh
```

只做上游与 overlay 准备：

```bash
./scripts/bootstrap.sh --no-idf-install
```

构建中文产品固件。默认启用本地唤醒词“梅莫里亚”（拼音模型：`mei mo li ya`，显示名：`Memoria`）；短按 BOOT 也可启动会话：

```bash
./scripts/build.sh
```

普通“你好你好”不是唤醒词。待机态先说“梅莫里亚”，听到提示后再说实际内容。只有定位麦克风/媒体问题时才显式构建无唤醒词诊断镜像：

```bash
./scripts/build.sh --wake-word disabled
```

构建产物写入被忽略的 `artifacts/`，包含 Memoria 自有命名的板型合并镜像 `memoria-atk-dnesp32s3-v1-merged.bin`、应用镜像 `memoria-atk-dnesp32s3-v1-app.bin`、bootloader 和分区表；真实 upstream 构建目录位于被忽略的 `.cache/upstream/`，其中的内部应用文件仍由上游命名为 `xiaozhi.bin`，不作为产品交付物。需要从头清理构建中间物时使用：

```bash
./scripts/build.sh --clean
```

列出串口并显式烧录：

```bash
./scripts/flash.sh --list
./scripts/flash.sh --port /dev/cu.usbmodemXXXX
./scripts/flash.sh --port /dev/cu.usbmodemXXXX --monitor
```

只有一个明确的 USB 串口候选（名称包含 `usbmodem`、`usbserial`、`wch`、`SLAB` 或 `UART`）时才可以自动选择：

```bash
./scripts/flash.sh --monitor
```

`auto` 不会把任意唯一的 `/dev/cu.*` 当作开发板；如果没有明确 USB 候选，脚本会失败并要求显式传入 `--port`，避免误选 Bluetooth 串口。

也可以单独打开 monitor：

```bash
./scripts/monitor.sh --port /dev/cu.usbmodemXXXX
```

Mac 下载模式失败时，按住 BOOT，轻按 RESET，松开 RESET，再松开 BOOT，然后重试 `flash.sh`。monitor 使用 `Ctrl+]` 退出。

## 当前真实软件能力

本次交付已落到固定 upstream、可重建 overlay、Control API/Device Fleet 和独立设备媒体网关：

- 新增独立板型 `memoria-atk-dnesp32s3-v1`，不修改 upstream 的 `alientek/atk-dnesp32s3` 身份。板型适配正点原子 ATK-DNESP32S3 V1 的 16 MB Flash、8 MB PSRAM、ES8388、ST7789、XL9555、BOOT(GPIO0) 和 LED(GPIO1)，不包含摄像头初始化。
- Wi-Fi 仍使用 upstream Hotspot 配网；长按 5 秒只重置网络配置，不清除 `memoria_identity`，也不解除云端绑定。小程序配网仍按独立安全门禁推进；当前不宣称微信 iOS/Android 上的 BLE/SoftAP 配网已完成。
- Flash `0x10000..0x1ffff` 是独立 `memoria_identity` NVS 分区。研发脚本生成/校验 Ed25519 身份并只烧录该分区，私钥产物权限为 `0600`；没有写 Secure Boot、Flash Encryption 或 eFuse。
- 固件启动后直接调用 Memoria Activation，验证 Ed25519 Manifest、配置哈希和单调 ACK；不会把 upstream `xiaozhi.me` 账号或旧 `DeviceRegistry` 当作 Memoria 身份权威。
- 产品构建默认启用本地自定义唤醒词“梅莫里亚”（`mei mo li ya`，显示名 `Memoria`）。它与 BOOT 短按共用 `idle -> connecting -> listening` 状态机；唤醒词音频不上传为用户话轮，避免把唤醒词拼进随后 ASR。
- 激活后，固件使用一次性 media challenge 和设备签名换取独立 audience/type 的设备票据，再按协商结果连接：v2 直连 `GET /v1/device/media`（direct Edge 是 v2-only，不接受 v1），v1 由 legacy livekit_compat Gateway 承接（24 kHz/20 ms 下行）。媒体帧采用严格 `MemoriaAudioFrameV1`，上行 Opus mono 16 kHz/20 ms；v2 直连优先协商 16 kHz/20 ms 下行。
- 独立 `services/device_media_gateway` 只做设备协议、Opus 转换和 LiveKit 桥接，复用现有 Agent/ASR/LLM/TTS/Policy 控制链，不创建第二套对话权威。`playback.ended` 只在设备真实播放队列排空后上报。
- 小程序已有扫码启用、Claim、Binding、Activation 查询和设备管理入口；首页已移除实时 AI 对话与手机媒体链，只保留硬件管理、摘要/周报和权威对话信息查看。
- 生产 Device Fleet 在 PostgreSQL/RLS 和托管签名密钥接入前明确返回 503，不回退本地 SQLite 权威。

## 2026-08-11 实板证据与边界

- 用户已用测试账号走通 upstream `xiaozhi.me` 激活并完成真实对话；这只验收上游路径。
- 同一块真实 ESP32-S3 rev 0.2 板卡随后烧入 Memoria 研发身份和 Path 2 固件。串口确认 Wi-Fi 重连、Activation Manifest 验签、`activating -> idle`，并从真机完成 media challenge、media session 和 WSS 握手。
- 本地没有运行 LiveKit，网关在尝试连接 `127.0.0.1:7880` 时按服务端错误关闭。因此当前只验收到“真实设备已进入 Memoria 媒体边界”，不能宣称已经完成 Memoria 真实语音对话。
- 服务不可用路径已连续按键复测两次：均约 1 秒提示并回到待机，持续观察无 panic、看门狗或自动重启，堆余量稳定。
- 已刷真机合并包为 `artifacts/memoria-atk-dnesp32s3-v1-merged.bin`，SHA-256 `29ab9cf4825099aa586a007aa03c97a786c2d16daad54911dfc96b665cca096f`。Path 1 回滚整包为 `artifacts/backups/pre-path2-upstream-working-merged.bin`，SHA-256 `ea3b37904e42c42a8334b9808871e8bebff2f72f9ed02dbc4000ec35fdf1e250`。
- 上述 2026-08-11 镜像是历史实板基线。2026-08-14 的后续历史实板候选从锁定 upstream `e8d8a401...` 全新重放 overlay（哈希 `a5876c782049060296ab552c77923d6257121157ea6b0e529b3f7b36f97d8119`）并通过 ESP-IDF 6.0.2 clean build/merge-bin；应用为 `2,964,704` bytes，应用分区余 `1,164,064` bytes（28%），合并镜像 SHA-256 为 `3e341acc535af3127a24ffbfe703d3fdb08eeb6c7b028df7b411e2ad3f15c0c4`。该历史候选已把 bootloader、分区表、OTA 数据、应用和资源五段刷入同一实板，写后及独立摘要核验全部通过；`0x10000..0x1ffff` 身份区刷前/刷后 `65,536` bytes 逐字节一致，SHA-256 均为 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`。真实启动已验证 8 MB PSRAM、目标板 SKU、Wi-Fi、Activation Manifest v2 和 `starting -> activating -> idle`；媒体会话、DAC 听感和打断仍需单独验收。
- 当前未刷板候选于 `2026-08-16T16:14:08Z` 从同一锁定 upstream 完成 clean build 与 overlay gate。内容加相对路径的 overlay SHA-256 为 `513e14ef346d49fc39e82b9bf96e2c56df0bf25bb1d2f081ee56e5707dd6a969`；产品应用 `memoria.bin` 为 `2,950,832` bytes、SHA-256 `6fe8e936d481433f80fd0dcd5c076f2a5f47029132e2b9e21ccb1680fe3d798d`，应用分区余 `1,177,936` bytes（29%）；合并镜像为 `13,577,086` bytes、SHA-256 `7d7b921a81500817fda6a8230cd5c5b5a97814855dd0a440188f3d75e2cee188`。`flasher_args.json` 只含 `0x0` bootloader、`0x8000` 分区表、`0xd000` OTA data、`0x20000` 应用与 `0x800000` assets，不含 `0x10000` 身份区。该候选没有刷板；身份保持、boot/activation、Direct 真机和全双工均未验证，不能继承前述历史候选证据。
- 旧候选真实会话已听到首轮 AI 回复，但日志证明首轮最后用户音频到 FunASR final 约 `20.5 s`；第二轮有 VAD/音频而无 ASR final。Agent 本地已改为每个 `vad.end` 显式 `finish-task`，收到 `task-finished` 后在同一 WebSocket 使用新 task ID，并以绝对 deadline 和 task fence 防 heartbeat 假活、重复 sentence ID 和迟到旧事件；完整 Agent `1489/1489` 已通过。该服务端修复尚未部署，因此最新固件的连续两轮真实对话仍待发布后验收。
- legacy v1 网关不向固件网络抽象暴露可用于客户端主动判死的 Pong；旧候选曾因此在 uptime 约 `392.5 s` 误退连接。最新固件只在 v1 跳过主动 Ping/Pong 判死，保留 TCP/WSS close/send failure 权威；v2 仍保持严格的 `30 s Ping / 10 s Pong`。该代码已通过源码门禁、clean build 和安全上板，仍需真实媒体长连接确认。

仍未完成的验收包括：微信 iOS/Android Protocomm 配网；生产 PostgreSQL/RLS、托管制造签发和量产安全；真实 LiveKit/Agent/provider 部署后的 Memoria 对话；LCD 肉眼细节、完整声学半双工/AEC、稳定全双工、唤醒词产品化与声音复刻。以上不能由本地测试或 WSS 握手替代。

## Overlay 约束

`apply-overlay.sh` 会把 `overlay/files/` 中的依赖锁与板级文件复制到 upstream，并按字典序应用 `overlay/patches/`。overlay 变更后应重新执行 `bootstrap.sh --refresh`，让缓存回到锁定 commit 后再应用新 overlay。生成的 `.cache/upstream/` 可以删除并重建；它不是交付内容。

静态校验：

```bash
./scripts/check-overlay.sh
```

该校验会检查 upstream/工具链锁、ESP 组件依赖锁、JSON、shell 语法、patch 可应用性、板型身份、摄像头残留和 `git diff --check`。有 ESP-IDF 时还会检查 upstream 的 board variant 列表；没有 ESP-IDF 时仍可完成不依赖工具链的 overlay 校验。

## 固件协议 v2 与本地硬停（安全第一批）

协议版本由控制面 POST /v1/devices/{id}/media-sessions 协商：固件请求携带 `supported_protocol_versions: [2, 1]`（首选 v2）；Control 端对缺失/空列表的旧固件按缺省 [1] 处理，只有显式支持 2 时才路由到 direct Edge。

- protocol_version=1：由 legacy livekit_compat Gateway 承接（device.hello v1 → session.ready，24 kHz 下行，v1 事件形状），对现有网关保持逐字节兼容。
- protocol_version=2：路由到 v2-only 的 direct Edge（device.hello v2 → session.accepted v2）；direct WSS 不接受 v1。`session.ready` 兼容解析只服务 legacy Gateway 的滚动发布窗口，Control 不会把 direct v2 票据路由到该端点。

滚动发布兼容只覆盖“新固件先于 Control API 上线”的窄窗口。若服务端以 FastAPI
`extra_forbidden` 精确拒绝且唯一字段为 `supported_protocol_versions`，fresh session 会删除该字段并
复用尚未消费的签名 challenge 重试一次；若恢复请求被精确拒绝的字段集合为
`supported_protocol_versions + resume_session_id`，固件会在会话 fence 下清除 resume 身份并只重建
一次 fresh v1。401、5xx、任何其他 422、非法响应或迟到结果均 fail closed，不做静默降级。当前生产
已启用单 Edge Direct v2 canary 并完成服务器运行态验证，legacy v1 仅作为兼容回滚；生产指标仍无活动
设备连接，因此当前未刷板候选尚未完成 Direct 真机或声学验证。

v2 声明与行为：

- 能力诚实声明：无 AEC reference（aec_mode=none、aec_reference=none、aec_reference_verified=false）、无 simultaneous capture/playback、无本地停止词/本地 duck、barge_in_level=0、playback_watermark=approximate。local_vad=true 对应真实运行的 AFE VAD；physical_stop_button=true，因此可接受 half_duplex_safe 或 interrupt_assist；full_duplex_verified 仍 fail closed。
- 下行只声明实际可播采样率：[16000, 24000]（24 kHz 为板级原生，16 kHz 经播放管线重采样）。帧大小随 session.accepted.downlink_sample_rate 校验（320/480 样本）。
- 完整 Generation fence（turn/generation/tool）贯穿 session.accepted.current_fence、generation.* 与所有 v2 playback 回执；generation_id=0 视为无有效播放，帧被丢弃并计数。
- L0 物理硬停：按键路径通过同一个 Generation Gate 原子关闭接收并清空解码/播放队列（可听停止不依赖网络），再发送 button.stop v2（expected fence + local_flush_sample_end + device_monotonic + control_sequence）；被停止的旧 generation 帧丢弃并计数，绝不恢复播放。v1 会话保持 legacy button.event。
- playback started/progress v2 在 AudioService 把解码帧提交到 codec 输出（OutputData）后上报（output-commit 水位），不再以"网络收到帧"代替；ended 只在 generation.completed 到达且真实 decode/playback 队列排空后上报（completed 到达时队列已空则立即上报）。所有水位携带完整 fence、received_sequence、rendered_sample_end 与 approximate=true：本板没有 DAC 采样计数器，任何水位都是边界而非精确听音位置，解码失败上报 playback.error。
- generation.completed 是音频 FIFO 中的有序屏障，只标记 completion pending；播放中途的短暂排空不会触发 ended/tts stop。generation.cancelled / playback.flush 保持 P0 同步 ResetDecoder 与停止，回执以"最后已送入播放管线的帧"为近似水位；cancel 不等同于正常 completed。
- v2 下行 sequence/sample 时钟是 per-generation 的：`generation.started` 与 `playback.flush` 替换把期望时钟重置为 0；Wi-Fi 或 WSS 断线进入显式 `RECOVERING`，最多 5 次以 1/2/4/8 秒退避主动获取同 Session 新票据和更高 `stream_epoch`。网络 EventGroup 只负责唤醒，原子“最新连接观察值”解决 Connected/Disconnected 位合并后的顺序歧义；被动断线至少跨过回调返回后的下一时钟 tick 才能替换 WSS；每个 WSS attempt 有独立 fence，协议错误只关闭产生该错误的连接，旧连接迟到回调不能关闭新 epoch；`session.accepted` 只在服务端仍声明 `current_generation_active` 时恢复播放，否则回到原 listening/idle 语义。新 WSS 对当前 Generation 做纯传输层 0/0 重基，Edge 将设备回执映回 Core 原始序列/样本域并强制按 conservative approximate 处理，不重放旧 WSS 队列。暂停期间到达的当前 generation 帧只推进传输期望时钟，不渲染、不移动回执状态，恢复后不会误判连续性。迟到旧 generation 帧先于连续性检查被丢弃计数，不断会话；用户主动关闭、服务端 `session.close`、不可重试 `session.error` 和恢复耗尽均清除 resume 身份，不能自动复活。
- 同 generation 因 edge 队列丢弃产生的前向空洞，只在帧头带 0x0001 discontinuity 标志且增量一致（sample_delta == sequence_delta × frame_samples，均为 frame_samples 的整数倍）时接受并重基线；未标志、回退或伪造（增量不一致）的空洞仍是协议破坏并断开会话。edge 只能标记它本 lane 实际丢弃的帧，不能替上游自然 gap 加标；真正协议破坏（坏帧、epoch 不符、帧长与协商采样率不符）仍断开。重连以新 stream_epoch 重置全部会话状态，control_sequence 为设备生命周期单调计数。
- `session.accepted.device_settings` 来自 Control 签名短票据，固件应用音量和屏幕亮度；当前板的 XL9555 背光只有开/关门控，因此 `screen_brightness=0` 关屏，1–100 均为开屏，不宣称可调 PWM 亮度。`runtime_profile.invalidated`：`immediate_fail_closed` 立即停止播放并结束会话；`next_safe_point` 在播放排空后结束会话；`next_session` 仅记录（设备不持有 persona，下个会话自然获得新 profile）。
- P0 控制（playback.flush / generation.cancelled / pause / session.error / immediate profile）在 WS 接收路径内同步执行本地 flush，不被音频队列阻塞。

本批未实现、也不声明：AEC、离线停止词 KWS、自然 barge-in、本地 duck。16 kHz 下行与按钮停止的端到端延迟仍属真机验收（T1/T5/T8）；当前候选只完成源码、overlay 门禁和干净构建，没有刷板或启动/激活验证，不能据此宣称 Direct 真机、声学或全双工通过。
