# 项目交接

## 当前生产增量（2026-08-24，天气停播/失联双端修复已切流并刷板；待两轮复验）

- 真实会话 `1c56c70a-4115-4254-8b21-2f968d0290d4` 第一代天气回复已完成 Actual Heard；第二代播放时，
  无 AEC/reference 的 Memoria simplex 板把自身 TTS 识别成唤醒词并 `Abort speaking`。随后 Go Edge stop 漏
  `session_epoch`、Agent 用旧代发失败 CANCEL、迟到旧代音频先撞新代序列连续性，形成
  `stale_client_event`、cancel fence mismatch 与 first-frame 0/0 错误，解释了“说到一半停，之后再说无响应”。
- 主线提交/tag `fadda434c9ea081ba1331ec81bc029fc74516bf6` /
  `20260824-1318-playback-fence-recovery` 已推送。Agent/Bridge 组件投影 tag
  `20260824-1320-playback-fence-agent-component` 运行镜像 `sha256:5771074693cf…64b0`；Media Edge 运行
  `memoria-media-edge:20260824-1318-playback-fence-recovery`，镜像 `sha256:de28a74efc95…e930`。三容器均
  healthy，Edge health failing streak=0，切流后目标错误日志为 0。
- Go Edge 现在完整转发 stop epoch、只接受同 epoch successor cancel，并在序列连续性前先按完整 fence 丢弃
  迟到旧代帧；Agent 播放错误/超时先推进权威 successor generation 再发 CANCEL。Agent Media Session 全量、
  Ruff、Go 全量/针对性回归通过；QwenRealtimeSearch、Doubao、FunASR、DeepSeek 与 InterruptSemantic 生产
  smoke 全部 PASS。
- 官方 DTLN 未被薄发布覆盖：当前 Agent 与 Bridge 均完成 ONNX checksum/contract 初始化和 640 字节 PCM
  实推，输出 `DTLN_SMOKE True 640`。服务器普通制品已按当前+一个可运行回滚收敛，根盘 59%→54%；未删除
  数据卷、数据库、安全或合规备份。
- 板端已把 ES8388 输入增益从 12 dB 调到 18 dB，上一候选曾 app-only 安全刷写并确认身份区刷前/刷后
  SHA-256 同为 `b7a717fa…084846`；真实上行峰值/RMS 已到 `2373 / 141.227`。最终固件进一步在 Memoria
  speaking 期间关闭 KWS、忽略迟到的 speaking-state wake event，idle 恢复 KWS，BOOT 仍是本地硬停止。
  app SHA-256 `416af6d2…e59d`、merged `09058a6a…4d69`、overlay `b7edb277…eba2`，clean build 通过。
- 最终固件已在板卡重新枚举为 `/dev/cu.usbmodem1101` 后 app-only 写入 0x20000；刷前身份区与安全备份
  逐字节一致，刷后仍为 `b7a717fa…084846`。串口确认 app 2.4.2、18 dB、Wi-Fi、Activation Manifest v2、
  idle、1MIC/0 playback AFE 与 KWS 初始化，无 brownout 或重启循环。
- 当前层级：服务端 `code=complete / wired=complete / enabled=true / verified=production runtime`；最终固件
  `code=complete / wired=complete / enabled=true / verified=identity-safe flash + board boot/activation`。尚未做
  “天气自然结束 + 第二轮仍响应”真机媒体验收，所以 `direct_real_device_verified=false`、
  `full_duplex_verified=false`、T1–T14 不重计；Actual Heard 必须由用户确认。

## 当前生产增量（2026-08-24，BOOT 服务链与官方 DTLN 已投产；待板卡重新枚举）

- 板端连接失败的连续根因已收口：恢复原始身份分区后，Activation Manifest v2 签名验证通过；Control API
  会话目录从不可解析的普通 Redis 改为共享 mTLS `device-state-redis:6379/1`，真实 challenge/session 从 503
  恢复；Bridge 首个 VAD 的 `logger` NameError 进一步追到平行 `ListeningStateManager` 控制面。该平行门禁、
  ASR 水位后的 PCM 丢弃及不存在的 `stop_speaking()` 调用已删除。
- 官方 DTLN 上游固定在 `breizhn/DTLN` commit `1de1f15…f7fbc`，成对模型摘要为
  `22b91cae…d4ae3` / `e20c92f9…2e639`。每个 media session 独立持有 recurrent/overlap state，只共享只读
  ONNX sessions；仅 ASR 消费降噪 PCM，声纹仍消费原始 PCM。生产对 2 秒 16 kHz PCM 实跑约 0.127 秒
  （RTF 0.064），长度保持、reset 确定性和模型 contract 均通过。未安装的 RNNoise/频谱回退已关闭。
- 热修前首个真实 BOOT session `3f4e8213…ced4` 已完成 session policy 并在生产日志中确认 DTLN 初始化；
  第 100 帧统计日志随后读取 `process()` 返回值中不存在的 `stage2_available`，导致 ingress pump `KeyError`。
  最终提交/tag `9a71e446a7df1943044a81fe841427a5648633d5` /
  `20260824-120459-dtln-stats-contract-fix` 已补齐统计契约和回归断言，CI run `32688703120` 通过。Agent 与
  Voice Core Bridge 运行 `memoria-agent:20260824-120459-dtln-stats-contract-fix`，image ID
  `sha256:9e9a7b7e…8f9ecf`，均 healthy、
  restart=0；Bridge gRPC、Control readiness 和模型实推通过。整栈 runtime authority 保持
  `20260823-210222-voice-fix`。生产容器 100 帧 ingress probe 已跨过原失败边界，`stage2_available=true`。
- 发布器现按内容而非历史路径核验 Compose 权威；旧父镜像已 prune 时，从运行容器重建 Agent 源码回滚镜像
  并做全树哈希比对。前两次切流分别在 Compose 路径、缺失父镜像和 heartbeat 栈版本门禁处 fail closed，
  其中一次已自动回滚成功；最终回滚点为 `rollback-20260824-120459-dtln-stats-contract-fix-pre-agent/-pre-bridge`。
  同日失败候选和旧回滚标签已清理，仅保留当前版与可运行回滚 `20260824-115603-dtln-boot-chain-fix`。
- 当前层级为 `code=complete / wired=complete / enabled=true / verified=production runtime + pre-hotfix real-session
  DTLN initialization`。热修后 macOS 当前无 `/dev/cu.usbmodem*` 且 USB 树不再枚举 Espressif，故尚未重做
  本候选的稳定 BOOT/WSS/ASR/播放验收。`direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 仍为
  `0 pass / 14 blocked / 0 failed`；待重新插线/RESET 后继续，Actual Heard 必须由用户本人确认。

## 当前生产增量（2026-08-21，播放回执完整 fence 已切流并刷板；待修复后真机复验）

- 根因已收敛并修复：ESP32 `playback.*` / `button.stop` 回执此前遗漏 `session_epoch`，而 Go Media Edge
  已按非零完整 generation fence 校验；因此回执进入 Voice Core 前会被判为旧代，表现为首帧后
  `speaking → recovering`、`output_timeout` 和 WSS 断开。公共 JSON 契约、Go Edge、ESP32 解析/发送和回归测试
  已统一到 `session_epoch + turn_id + generation_id + tool_epoch`。
- 源提交/tag 为 `da08f5b7061a6e44d45db1e516b904a8a79e057a` /
  `20260821-194700-device-playback-fence-contract`；Media Edge 只切换该组件，运行镜像
  `memoria-media-edge:20260821-194700-device-playback-fence-contract`，image ID
  `sha256:5721272a35feffc7c472635da551c1a5156152cdd1513d17f3b1d2e044174d42`，amd64、OCI revision/tag/role
  正确，healthy、restart count=0。Agent、Voice Core Media Bridge、Control API 容器未重建；Control readiness=200。
- 本地/固件门禁：Go `go test ./...`、针对性固件源码回归、ESP-IDF 6.0.2 clean build、merge-bin、overlay
  gate、`git diff --check` 均通过。固件 overlay SHA-256 `263c6b79…a7b7`，merged SHA-256
  `ff68c629…665c`，app SHA-256 `dffa4a21…9f56`；已刷入 `/dev/cu.usbmodem1101`。身份区回读
  `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`，与刷前备份逐字节一致；串口确认
  目标 SKU、ES8388/I2S、Wi‑Fi、Activation Manifest v2、`starting → activating → idle`，无重启循环。
- 对照证据：Edge 切流后、刷板前旧固件仍产生 `handler rejected kind=text` 与 `1006 unexpected EOF`；这与缺失
  `session_epoch` 的旧回执一致。该对照不代表新固件已完成真机验收。
- 证据目录 `/opt/memoria/direct-canaries/20260821-194700-device-playback-fence-contract/`：
  `CUTOVER_RESULT.txt` SHA-256 `9500e06cfa10d643050b855cc23ca5520fca0df1394375c0b06083cc46b017b9`，
  `POST_CUTOVER_STATE.txt` `ad133a2b351ecf376293e0ba9cff667003c41a061421f8baf07b4f0cdbbce09e`，
  `CONTROL_READY.json` `bb5f116ff3c3d686e71a83cf9d44791be3b8e3859471b09ad6c1c3ca983d801f`；回滚 override
  保留旧 Edge `memoria-media-edge:20260821-171500-device-wss-diagnostics`。
- 当前层级：服务端和固件为 `code=complete / wired=complete / enabled=true / verified=production runtime
  + board boot/identity`；仍不是 `real-device media verified`。`direct_real_device_verified=false`、
  `full_duplex_verified=false`、T1–T14 仍为 `0 pass / 14 blocked / 0 failed`。
- 下一步：串口已保持监视，用户只说一句简单话且播放期间不再按键；按同一 session/generation 检查
  `media_asr_boundary`、`first_frame_sent`、`playback.started/progress/ended/error`、Actual Heard 和
  WSS close cause。只有形成完整回执闭环后，才推进 T5–T8；不把启动级证据外推为全双工。

## 当前生产增量（2026-08-21，ASR 重连 task fence 修复已切流；待真机复验）

- 根因修复提交为主线 `d2898c14ebf6d5e45e0b756fb617f34977d4e69b`；生产 Agent 组件投影提交/tag 为 `eca4346a4abadbcbc449e2488a88eb52bbf8400c` / `20260821-192100-asr-task-fence-agent-component`。投影提交只包含 Agent 侧 4 个变更文件，文件内容与主线提交逐字一致；主线中已上线的 Media Edge 诊断历史不被重复带入 Agent overlay。
- Agent 与 Voice Core Media Bridge 当前运行 `memoria-agent:20260821-192100-asr-task-fence-agent-component`，image ID `sha256:038aa6cd2d93ac25c7bef5f64e143dccb4edc6860f45bc624611cefd47147e54`，两个容器 healthy、restart count=0，Bridge gRPC socket PASS。Media Edge 保持 `memoria-media-edge:20260821-171500-device-wss-diagnostics`，Control API 保持 `20260821-121418-reply-delivery-projection`；其余服务、固件、数据库未切换。
- 修复内容：transport epoch 变更后由 Voice Core 把最新 ASR task floor 传给 provider；provider 在 live ASR 被清空的恢复窗口保留最后导出的全局 task epoch，新的 FunASR task 不再从 1 倒退，避免 `media provider ASR task epoch moved backwards` 反复打崩 ingress pump。新增两组 reconnect/floor 回归测试；Agent 两个单测文件全过、Ruff、strict MyPy、git diff check 全过。
- 发布证据：服务器 `/opt/memoria/component-releases/20260821-192100-asr-task-fence-agent-component/`；source archive SHA-256 `ff3c0ef56a9898f376bec3c8863dd7605511d87384717927781166d9bf9a7bac`，`CUTOVER_RESULT.txt` SHA-256 `6f1f48f99f01e93d2f1d507d789ecf4d4398916d6c8a790934d40cf93124f106`，`BUILD_RESULT.txt` SHA-256 `790523e2796c5dc51270434af31c471e7f41e8e4366a3357e0f563ee3a7a3590`。回滚点为 `rollback-20260821-192100-asr-task-fence-agent-component-pre-agent/-pre-bridge`。
- 生产 readiness 复核为 `ready`，Agent/Bridge/Edge/Control 均 healthy，目标日志最近 3 分钟无 `traceback/error/failed/epoch moved backwards`。这仍只是服务器运行证据；`direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 仍为 `0 pass / 14 blocked / 0 failed`。
- 下一步：保持串口监视，让用户对板说一轮且不在播放中再次按键；随后按同一 session/generation 检查 `funasr_ws_trace`、`media_asr_boundary`、`first_frame_sent`、WSS close cause、`playback.started/progress/ended/error` 和 Actual Heard。若仍是首帧后 recovering，再依据新的 Edge `close_code/phase` 证据处理下行生命周期；不把本次服务端修复外推为真机播放通过。

## 上一候选（2026-08-21，服务器已切流并刷板；首帧到板但播放终态验收阻塞）

- 当前生产基线已经前移到 Agent/Voice Core Media Bridge `memoria-agent:20260821-153249-playback-terminal`（revision `711eb0c`）、Control API `20260821-121418-reply-delivery-projection`（revision `caf7027`）和 Media Edge `20260821-153249-playback-terminal`（revision `711eb0c`），均为本轮切流后 healthy、restart count=0。此前真实板会话 `a9ea0f30-5a37-4c75-baa7-65a03e19b76f`、epoch 922 已有三代连续可播放回复，用户确认“AI 能说了”；该证据只覆盖窄主流程，不覆盖本候选的完整播放终态、Actual Heard 或打断。
- 根因已收敛：固件签名回执带 `playback.started/progress/ended/error`，但 Go Edge 转入 media-v1 时压扁成无类型 `PlaybackProgress`；Python 因而可能把覆盖全量水位的 progress 提前当作完成，也可能把 error 当成功终态。另有 Python 回执 fence 漏带 `session_epoch`，会拒绝非零 epoch 的合法回执。Control SQLite 因此只看到 generation 1/2/3 的 `first_frame_sent / provider_completed`，没有 `playback_ended / actual_heard`，即使 Edge 指标已经观测到 playback receipt。
- 本地候选已完成 `code + wired + local verified`，并已完成服务器 `enabled + production runtime verified（零真机会话）`：media-v1 新增显式 `PlaybackEventType` 并贯通 Go/Python；typed progress 必须等合法 `ENDED`，`ERROR` 进入 ReplyDelivery `error` 并取消输出；回执完整携带 `session_epoch`，越界序列/水位和伪造终态 fail closed。物理按钮只要存在 active generation 就立即本地 flush/静音并上报 signed `button.stop`，不再等待首个音频帧。切流 tag `20260821-153249-playback-terminal`，Agent/Bridge image `sha256:90f1f51e…a22535`，Media Edge image `sha256:c612f317e…de860`；Control ready、Bridge mTLS Health、Edge mTLS readyz、Provider smoke 全通过，非目标容器 ID 未变。证据：`/opt/memoria/direct-canaries/20260821-153249-playback-terminal/`。
- ESP32 patch `0017-i2s-tx-eof-exact-playback-watermark.patch` 使用 ES8388 I2S TX EOF，在最后写入后等待完整 DMA descriptor ring 轮转才发送 `approximate=false` 最终水位；流式 progress 仍标 approximate。ESP-IDF 6.0.2 clean build、overlay gate、目标板刷写与启动通过：app `2,951,088` bytes、分区余 29%、SHA-256 `135a714b…bcf`；merged `13,577,086` bytes、SHA-256 `7cd6578c…192`。刷前/刷后身份区逐字节一致，SHA-256 `b7a717fa…4846`，备份目录 `firmware/esp32/artifacts/backups/pre-playback-terminal-20260821/`。该边界证明 I2S 数字数据移出设计边界，不证明 DAC、功放、扬声器或用户声学 Actual Heard。
- 当前层级：服务器 `code=complete / wired=complete / enabled=true / verified=production runtime zero-session`；固件 `code=complete / wired=complete / enabled=true / verified=flash + boot/activation`。串口确认目标 SKU、ES8388/I2S、Wi‑Fi、Activation Manifest v2、`starting → activating → idle`，无重启循环。最新真机尝试会话 `84780027-1f5b-4b83-9583-4cc6eb49e76a`（epoch 933，turn 1，generation 1）已收到首个可播放下行帧，但随后设备反复 `speaking → recovering`；服务器最终 `output_timeout`，且 `provider_completed=false`、`playback_ended=false`、`actual_heard=false`。未捕获可确认的 `button.stop`，不能把问题归因于按键。当前仍保持 `direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 `0 pass / 14 blocked / 0 failed`。
- 下一步：先在保持串口监视的情况下做一次不按第二次键的隔离重试；若仍首帧后 recovering，定位 WSS close/recovery 与 Edge 下行生命周期；若自然结束，再单独验收物理按钮硬停止和同代迟到 PCM 拒绝。
- 后续范围：完成固定 TTS/完整单轮与播放中随机按钮的同代取证，覆盖 `playback.started/progress/ended/error`、Actual Heard 投影、按钮停止 p95 与迟到 PCM 拒绝；自然语音打断仍需后续本地停止词、AEC Reference、双讲与 Router 验收，不能由物理按钮结果外推。

## 当前生产增量（2026-08-21，ReplyDelivery 跨进程投影已启用，待真机话轮取证）

- 源提交/tag `caf70271222c43047c5d661052f3b717348cc209` / `20260821-121418-reply-delivery-projection` 已推送并冻结。Control API、Agent、Voice Core Media Bridge 已协调切流；运行 image ID 分别为 `sha256:4c0d8966a885…` 与 `sha256:9b6260e5fc22…`，均 amd64、OCI provenance 正确、healthy、restart count=0。Media Edge、LiveKit、Gateway、H5、小程序、数据服务与固件未切换；runtime authority tag 保持 Control/Agent=`20260814-231749-direct-canary`、Bridge=`20260816-bridge-liveness-83af813`。
- `reply-delivery-v1` 使用完整 `session_epoch + turn_id + generation_id + tool_epoch` fence，从 Python Voice Core 非阻塞投影到 Control API 幂等 SQLite；不含用户/助手文本、PCM 或账号 ID，也不进入历史/记忆。生产已配置独立 `MEDIA_REPLY_DELIVERY_TOKEN` 与 Agent-only Fernet spool key，env 均 `root:root 0600`；SQLite 与旧 env 已做 root-only 冻结备份。
- 生产门禁通过：内部端点错误 token=401、首次写入=200/inserted、重复=200/not inserted、精确读取=200，合成事件已删除；LiveKit、QwenRealtimeSearch、Doubao、FunASR×6、DeepSeek、InterruptSemantic 与 readiness 全部 PASS。复核时 `media_reply_delivery_events=0`、spool 不存在、Control/Bridge 相关错误计数 0，表示尚无候选真机会话。
- 回滚点 `rollback-20260821-121418-reply-delivery-projection-pre-control/-pre-agent/-pre-bridge` 分别冻结上一健康 Control `sha256:14916cf1…` 与 Agent/Bridge `sha256:f9a45a4d…`。证据目录 `/opt/memoria/direct-canaries/20260821-121418-reply-delivery-projection/`：`CUTOVER_RESULT.txt` `c68692b2…`、`POST_CUTOVER_STATE.json` `97f26552…`、`CONTROL_ENDPOINT_SMOKE.json` `d356ff59…`、`PROVIDER_SMOKE_RESULT.txt` `b820915c…`、`ASSET_CLEANUP.txt` `29c85bfb…`，manifest `4f9bb228…` 校验通过。旧普通镜像标签与 build cache 已定点清理，根盘 43%→40%，未删除数据卷、数据库或备份。
- 当前层级为 `code + wired + enabled + production runtime verified`（零真机会话范围）。下一步由用户本人对板说话，取同一 delivery 的 ASR final、TTS 首帧、设备 Playback/Actual Heard 与终态证据；此前保持 `direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 `0 pass / 14 blocked / 0 failed`。

## 上一生产增量（2026-08-20，Agent 组件级源码薄发布已投产）

- 发布提交 `eaee0bcfcd9d1b3a211486cab47426ff7b6efcf3` 与 tag `20260820-210548-agent-source-overlay-v6` 已推送 `origin/main`。新增 Agent 组件快车道：依赖不变时只从固定健康 Agent 基座叠加 `services/agent` 源码，服务器使用 `--network=none` 离线构建；依赖锁、共享服务或其他运行时越界改动会 fail closed，仍走完整/协调发布。CI 新增 Agent 独立门禁，run `32372208215` 用时 59 秒并通过，Python/H5/Media Edge/小程序按未修改范围跳过。
- 本次源码归档仅 `2,037,760` bytes（SHA-256 `990dfc1e41f2f9ecaa6fe3dc82068d010800ebf0de91e9aae03ca1f277b37444`），Docker build context `2.033MB`；不再上传约 2.37GB 的完整 Agent 镜像。生产 Agent 与 Voice Core Media Bridge 均运行 `memoria-agent:20260820-210548-agent-source-overlay-v6`，image ID `sha256:f9a45a4dfa87fcc502cd0b445c8858033dad3a3c6d66127943c5961fb281b565`，OCI revision/version/role/kind 与提交、tag、`agent`、`agent-source-overlay` 一致；两个容器 healthy、restart count=0，Bridge gRPC socket 通过。
- 组件镜像版本与 runtime authority tag 已解耦：Agent 保持 `20260814-231749-direct-canary`，Bridge 保持 `20260816-bridge-liveness-83af813`；Control API `/health/ready` 返回 200/ready，Agent worker ready、LiveKit ready，切流后零 heartbeat failure。PostgreSQL 与 Redis 容器 ID 在切流前后不变。
- 真实 Provider smoke 通过：QwenRealtimeSearch 强制公网检索、Doubao 五音色/字时间戳/取消、FunASR 六轮 interim+final、DeepSeek 流式和 InterruptSemantic 五类样本全部 PASS。运行容器内 `agent.py`、`open_meteo_weather.py`、`qwen_realtime_search.py` SHA-256 与发布提交逐字节一致。
- 回滚点 `rollback-20260820-210548-agent-source-overlay-v6-pre-agent/-pre-bridge` 均指向上一健康镜像 `sha256:e6d5876aa379…`。证据目录 `/opt/memoria/component-releases/20260820-210548-agent-source-overlay-v6/`：`CUTOVER_RESULT.txt` SHA-256 `2d95bf1dc54a973efd0e8fcdccaea76df5b809e7f4c58d862aaa82dcc58dc39d`、`PROVIDER_SMOKE_RESULT.txt` `5b118c89b4d7171b71577768e8b8a01c6b00bdb9b158c805dea32c3e4640252d`、`POST_CUTOVER_STATE.txt` `f206fdd78d9e79a0fa2ce3e14573476a7884b95eb1a604188bdd7b0e8c843dbe`，校验均通过。失败候选与更早普通回滚制品已删除；服务器仅保留当前 v6、上一健康基座及 v6 回滚 tag，根盘 40%。
- 当前层级为 `code + wired + enabled + production runtime verified`（组件发布、零真机会话范围）。不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`；真机说话、Actual Heard、双讲/打断仍需单独验收。

## 上一生产增量（2026-08-20，FunASR wire trace 诊断埋点 + ASR 静默丢弃可见性，已切流待真机说话取证）

- 源码提交 `65507bede7f6842db3e659b280774db9fdfcda2b` 与 annotated tag `20260820-144144-asr-ws-trace-diagnostics` 已推送 `origin/main`。背景：下行 fence 修复后 TTS 仍无声，新会话（epoch 911）pcm-tap 音频能量健康（语音段 RMS 160–184）、`media_asr_boundary` 11 条全 success、finalize 正常闭环，但 FunASR 零 `result-generated`、零 partial、尾超时丢弃。四层容器内探针（实时回放 / burst / adapter 层 / 150 次高频轮转）同容器同代码同音频同边界全部识别成功，排除 provider、帧节奏、adapter 映射与限流累积——唯一未观测环节是生产 WS 链路本身。内容：`FUNASR_WS_TRACE=true` 开启 provider WebSocket 全量控制消息/服务端事件限频 trace（每 1 秒窗口最多 20 条，超出计数汇总输出）；ASR 决策拒绝（preview/timeline/accept 三阶段）落日志（final 为 WARNING）；ingress stale stream-epoch 丢弃与 adapter 驱逐上下文丢弃不再静默；ASR 尾超时丢弃日志补 partial 在场证据。纯观测，不改门禁/判定/fail-closed 行为。
- 本地门禁：聚焦单测（provider_config/funasr_session_edges/media_session 全过，含 ws trace 限频、env 解析、stale final 拒绝日志新用例）、ruff、strict mypy、模块预算全部通过。
- 生产切换：Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260820-144144-asr-ws-trace-diagnostics`（load 后 image ID `sha256:e6d5876aa379…`，manifest config SHA-256 `bf7abcf3cb0c…` 与本地候选一致，OCI revision 与源提交一致），均 amd64、healthy；bridge 容器 env `FUNASR_WS_TRACE=true` 生效、pcm-tap 保持；容器内 grep 确认埋点在位。回滚点 `rollback-20260820-144144-asr-ws-trace-diagnostics-pre-agent/-pre-bridge` 已冻结（指向 downlink-fence-session-epoch 镜像 `sha256:237d3ca72337…`）。证据目录 `/opt/memoria/direct-canaries/20260820-144144-asr-ws-trace-diagnostics/`：`CUTOVER_RESULT.txt` `bf458d3d403b068839cf7ebb3bd207b171726981fa05ba341aa8be2cc98f7043`、`POST_CUTOVER_STATE.txt` `30c789317543f8e9779b8c01a0bc5485491523d44364c71f25fad6a2007681a7`。制品两版本策略已执行：服务器清理 20260812-173008 至 20260820-122047 的旧镜像与旧回滚 tag，回收 2.9GB（磁盘 41%→33%），仅保留当前版本与紧邻回滚点。
- 下一步：用户本人对板卡说话，随后分析 bridge 日志 `funasr_ws_trace rx/tx` 与 `media ASR result rejected`，定位服务端零回复还是本地静默丢弃，然后实施真正修复。
- 不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`。

## 上一生产增量（2026-08-20，下行 PCM fence 补携 session_epoch 修复 transport_rejected，已切流待真机 TTS 出声验证）

- 源码提交 `2fdc1e721daee6c750aa3852e7235ef8f5380fac` 与 annotated tag `20260820-125356-downlink-fence-session-epoch` 已推送 `origin/main`。根因（埋点候选取证）：生产日志四个 turn 全部命中 `fence_mismatch_or_generation_rejected`，权威 generation controller 的 fence 为 `session_epoch=1`，但 `PCMFrame` 根本没有 session_epoch 字段，`accept_downlink` 重建 fence 恒为 epoch=0，完整 fence 比对必败——会话 epoch 超过 0 后每帧 TTS 都被拒（本地测试全 epoch=0 所以全过）。修复：`PCMFrame` 新增 `session_epoch` 字段（默认 0），输出流构造时以运行时权威 fence 盖戳，状态门禁按完整 fence 比对；诊断日志同时打印双侧 tool_epoch/session_epoch。保留“换 subject/profile 后旧帧不得越界”的 fail-closed 语义；门禁在 gRPC 序列化前，无需改 proto/Go media edge。
- 本地门禁：聚焦单测（contracts/grpc_bridge/media_session 共 160 例，含新增 epoch=1 放行/epoch=0 拒绝回归用例）、ruff、strict mypy、模块预算全部通过。
- 生产切换：Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260820-125356-downlink-fence-session-epoch`（load 后 image ID `sha256:237d3ca72337…`，OCI revision 与源提交一致），均 amd64、healthy；容器内 grep 确认两处修复在位；回滚点 `rollback-20260820-125356-downlink-fence-session-epoch-pre-agent/-pre-bridge` 已冻结（指向诊断埋点镜像）。证据目录 `/opt/memoria/direct-canaries/20260820-125356-downlink-fence-session-epoch/`：`CUTOVER_RESULT.txt` `2938ca406a859d6552b197dc95523873dc6e05744a7cb71e69460937f1a5ce2e`、`POST_CUTOVER_STATE.txt` `9189fddad8847603442204a952462d136e51e9a0127d732e1932e3f27bd150c6`。
- 当前层级 `code + wired + enabled + production runtime verified`；下一步：用户本人对板卡说话验证 TTS 首次出声（预期零 `transport_rejected`、`emitted_audio=True`），若出声则推进 T1–T14 验收；若仍拒，新日志已能直接读出双侧 session_epoch。
- 不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`（待真机出声后重计）。

## 上一生产增量（2026-08-20，下行拒绝路径限频诊断埋点，已切流并完成取证）

- 源码提交 `bc383a030b0fa27c3605e545ef4a837e69381582` 与 annotated tag `20260820-122047-downlink-reject-diagnostics` 已推送 `origin/main`。内容：`media_bridge_server.py` 的 `accept_downlink` 全部七个拒绝分支（identity/generation_active/fence/sequence/source-sample/queue-full）打 5s 限频 WARNING，含 generation gate 与 fence/sequence 状态快照；`grpc_bridge.py` 的 `emit_generation` 四个失败分支打 `media generation control rejected` 日志；`media_session_commit.py` 开始检查 `GENERATION_ACTION_START` 返回值，未接受时打 `media generation START not accepted`。不改变任何门禁判定、fence、权限或 fail-closed 行为。
- 本地门禁：聚焦单测（含 2 个新增限频/门禁状态日志用例）、ruff、strict mypy、模块预算全部通过。
- 生产切换：Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260820-122047-downlink-reject-diagnostics`（load 后 image ID `sha256:79003b31ab50…`，manifest config SHA-256 `a94adfe063e4…` 与本地候选一致，OCI revision 与源提交一致），均 amd64、healthy；其余服务不变。回滚点 `rollback-20260820-122047-downlink-reject-diagnostics-pre-agent/-pre-bridge` 已冻结（指向 ingress-overflow-fix 镜像 `sha256:7d16a2cf07eb…`）。证据目录 `/opt/memoria/direct-canaries/20260820-122047-downlink-reject-diagnostics/`：`CUTOVER_RESULT.txt` `8d6d902c575cfcff8be444c1a6ebbb6a602ce3984adb75c01490e9f33bc9d7a9`、`POST_CUTOVER_STATE.txt` `1a2e8a971fb207dbd80a7f1893d511f1ac2a3c04a1bc723d00da4003b5daccc6`；容器内 grep 确认三处埋点均在位。
- 取证结果（用户真机说话后）：四个 turn 全部 `reason=fence_mismatch_or_generation_rejected`，turn/gen/tool_epoch 与 sequence/队列均正常，唯一分歧是 controller fence `session_epoch=1` vs 帧重建 epoch=0——直接定位新阻塞③根因，修复见顶部增量节。

## 上一固件增量（2026-08-20，ES8388 mic gain 24dB→12dB 底噪整改，真机声学验证通过；新阻塞：下行 transport_rejected）

- 范围：仅固件 overlay（`firmware/esp32/overlay/files/main/boards/memoria/atk-dnesp32s3-v1/memoria_atk_dnesp32s3_v1.cc` 的 `GetAudioCodec()` 在 ES8388 构造后调用 `SetInputGain(12.0f)`），无服务端切流；overlay hash `b0d53334…`。源码已随提交 `ee04810` 入库。
- 本地门禁：ESP-IDF 构建 EXIT=0（merged.bin SHA-256 `92d48e14c80b636e868d4ea3c212f2e3feb36c8c88da75703f80097077e8eb68`、app.bin `5a4f32967b0734a162cfb7e7fff3d8c1fa06bb5420b28454f6f21b8c5cf4d74d`）；`check-overlay.sh` 通过；`firmware/esp32/tests` 78 个全过。
- 刷写与身份保护：`flash.sh` 写 `/dev/cu.usbmodem101` EXIT=0，五段写后 Hash verified；刷前/刷后身份区逐字节一致（SHA-256 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`），备份目录 `firmware/esp32/artifacts/backups/pre-current-candidate-20260820/`。串口启动日志确认 `AudioCodec: Set input gain to 12.0`、Wi-Fi 连接成功、无重启循环。
- 声学验证（用户本人对板卡说话，会话 `1e44323b-bf19-46ae-9368-a58812ef4b3b`，stream_epoch 908，85.2s）：pcm-tap WAV SHA-256 `9ed58e547f751b574d5aca03b072b015aecd6db39a404ad270fbcef044bac23b`；1s 窗口底噪中位 RMS 由修复前 100–150 降至 24（77/85 窗口 <40，最低 19.3），语音段 RMS 峰值 158；`media_asr_boundary` 8 条全部 success，VAD 正常闭合，零 `superseded`——阻塞①（底噪导致 VAD 永不关闭、回复永远被抢占）已消除。
- 新阻塞③（举一反三）：turn 1–3 回复均在第一帧 TTS PCM 即 `status=aborted reason=transport_rejected emitted_audio=False`。`accept_downlink` 门禁拒绝但无打断/取消事件；本地 `test_media_session.py`（94 用例）通过，属生产状态分歧。怀疑方向：turn 提交时 `GENERATION_ACTION_START` 未被消费/generation gate 未激活、或 session fence 与帧 fence 不一致；`media_session_commit.py` 对 START 的返回值未检查。已实施：为 `accept_downlink` 拒绝分支加限频诊断日志埋点候选并切流（见顶部增量节）。
- 阻塞②（speaker authority `subject_capability_forbidden`）维持原判：生产所有账号 `subject_category='unknown'`，`speaker_enrollment` 规则表只允许 `adult`，需产品流程或规则表评审，不擅自改数据。
- 本增量达到固件侧 `code + wired + enabled + verified（声学）`；`direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 保持 `0 pass / 14 blocked / 0 failed`（仍无 TTS 出声）。

## 上一生产增量（2026-08-20，Media ingress 静默丢帧修复 + PCM 诊断 tap，已切流并完成首次 Direct 话轮提交观测）

- 源码提交 `c70577271aae3b59d74936d1753a0716549ec24c` 与 annotated tag `20260820-112031-ingress-overflow-fix` 已推送 `origin/main`，tag 不再移动。内容：Media ingress 队列由 20 帧扩到 256 帧（约 5.1s），溢出改为丢最旧帧保最新语音，overflow/pump-stall 打限频 WARNING；保留 env 门控 PCM 诊断 tap（`MEDIA_PCM_TAP_DIR`，生产 bridge 启用指向 `/data/diagnostics/pcm-tap`）。不改变话轮路由、权限、主体、fence 或 fail-closed 行为。
- 诊断结论（本轮核心）：8-19 的“能量健康但 FunASR 零结果”根因不在固件——tap 抓回的 WAV（会话 `73805bf2`，epoch 907，327.7s）人声能量 RMS 200–470、频谱正常；真正断裂点是 bridge ingress 泵送被 FunASR 阻塞时队列静默丢帧（修复前实收仅准入量的约 3%）。修复后生产零 overflow/pump-stall 日志，每条 `media_asr_boundary` 的 `provider_pcm_samples == send_count×320`，ASR finalize 成功，会话 `73805bf2`（stream_epoch 907）提交话轮 1–4。
- 生产切换：Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260820-112031-ingress-overflow-fix`（image ID `sha256:7d16a2cf07eb…`，OCI revision 与源提交一致），均 amd64、healthy。回滚点 `rollback-20260820-112031-ingress-overflow-fix-pre-agent/-pre-bridge` 已冻结（指向 `20260820-010500-livekit-stack-upgrade`）。证据目录 `/opt/memoria/direct-canaries/20260820-112031-ingress-overflow-fix/`：`CUTOVER_RESULT.txt` `47d9e4f10aaf76393b0f3ef19c275fe6801d570009c83356588ad694bc84eff3`、`POST_CUTOVER_STATE.txt` `39be6d9ce7e4ee1509d771c84267445898d75b5e5943631e3691a55b1c0b1d2c`。
- 端到端仍不通，两个新阻塞点已定位：① 设备上行存在不间断宽带底噪（328s 全程 RMS≈100–150，1–8kHz 均匀分布、无周期性，疑似环境噪声/AFE 残留叠加 24dB mic gain），Provider VAD 几乎永不关闭，每个回复 generation 都在 TTS 出声前被下一个 `vad_start` 抢占（`status=aborted reason=superseded emitted_audio=False`）；② speaker authority 对该设备主体返回 `subject_capability_forbidden`（account gate fail-closed），说话人无法确认为 owner，回复走 `no_verified_runtime_profile` 兜底。另观察到 3 次 projection range/text mismatch 拒提交与 2 次 ASR tail timeout 丢弃，待底噪解决后复评。
- 本增量达到 `code / wired / enabled / production runtime verified`（传输与会话层）；不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`。下一步：固件声学链降噪/增益整改 + 设备主体账号能力修复，之后重做真机会话验收。

## 上一生产增量（2026-08-20，LiveKit 全栈升级：livekit-agents 1.6.10 + LiveKit Server v1.13.5 + livekit-client 2.22.0 + pion 补丁，已切流并验收）

- 源码提交 `59fbf9969987bd9df13709a1cc1a0051aafc6aa6`（release 文档另随后续提交入库）与 annotated tag `20260820-010500-livekit-stack-upgrade` 已推送 `origin/main`，tag 不再移动。升级内容：Python `livekit-agents/plugins-openai/plugins-silero 1.6.5 -> 1.6.10`（含 rtc 1.1.14、protocol 1.1.22、openai 2.54.0 传递升级）；自建 LiveKit Server `v1.13.3 -> v1.13.5`；H5 `livekit-client 2.20.1 -> 2.22.0`；Go media edge `pion/ice v4.4.1`、`pion/turn v5.0.13`、`pion/transport v4.1.0`。不改变话轮路由、权限、主体、fence 或 fail-closed 行为。
- 生产切换：Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260820-010500-livekit-stack-upgrade`（image ID `sha256:1798c3ab6f93…`），Media Edge 运行 `memoria-media-edge:20260820-010500-livekit-stack-upgrade`（image ID `sha256:9beee50dc6f5…`），均 amd64、OCI version/role 正确；三容器 healthy、restart count=0。心跳代际保持：agent=`20260814-231749-direct-canary`、bridge=`20260816-bridge-liveness-83af813`；Agent 在新 LiveKit Server 重新注册 worker `AW_2p7oFEKqQmE7`，心跳 `ready`。回滚点 `rollback-20260820-010500-livekit-stack-upgrade-pre-agent/-bridge/-edge` 已冻结（分别指向 `20260819-185500-ingress-pcm-tap` 与 `20260818-103228` 同 image ID）。
- LiveKit Server 升级按 runbook 5.1：`/opt/livekit` compose.yml/livekit.yaml 备份至 `/var/backups/memoria/*pre-20260820-livekit-v1.13.5`，容器重建后 `livekit-server version 1.13.5`，`logging.level: warn` 保持；`v1.13.3` 镜像保留为回滚点。候选容器内 Provider smoke（Qwen realtime-search、Doubao、FunASR×6、DeepSeek、InterruptSemantic）与 LiveKit smoke 全通过。
- H5 首次随 runtime 一起切流（此前软链停留在 `20260808-171749`）：候选 `/var/www/memoria-releases/20260820-010500-livekit-stack-upgrade`，immutable union 262 资源无 collision；回滚目录 `rollback-20260820-010500-livekit-stack-upgrade` manifest SHA-256 `2921c4ff7bd0ada7db658fab3aa437dc41e9de7d841d706b7148488ce3fa5311`；软链原子切换后根页、H5、SPA、live、ready 均 200。
- readiness 运维隐患（举一反三）：切流前 `health/ready` 503 `smokes:expired`，根因是既有 canary tag 漂移——`/opt/memoria/current/.env` 仍为 `20260812-173008`，而 Control API 实际运行 `20260814-231749-direct-canary`，`memoria-readiness-refresh.timer` 每 12h 以旧 tag 标记持续被 409 拒绝。本次以运行 tag 覆盖在 Control API 容器内重新标记，ready 恢复 200；后续 runtime 切流必须同步更新 `/opt/memoria/current` 或其 `.env` 的 release tag，否则 readiness 刷新会静默失效。
- 制品清理（保留两个可用版本策略）：删除 `/opt/memoria/incoming` 三个已取代上传包（约 4.3 GB）与 19 个旧镜像 tag；仅保留当前运行与紧邻回滚版本及 livekit 双版本。根盘 47% -> 38%（118G 中 43G 已用）。未删除命名卷、数据库、Redis、MinIO、合规备份与 H5 immutable union。
- 证据目录 `/opt/memoria/direct-canaries/20260820-010500-livekit-stack-upgrade/`：`CUTOVER_RESULT.txt` `9afe0bdd7ad769854d25e3b8f82b8e4ccc673ecbe74d198e68cf2c4557181a71`、`POST_CUTOVER_STATE.txt` `67b92f7f450024ab31014fb7c2bf9b9f4268047bda466fcbd7ecacc161934e3f`、`PROVIDER_SMOKE_RESULT.txt` `c192007ccbaca74af3e84c014123c6a9a832db47b0332b006ef0ce2b1e3a0c54`、`LIVEKIT_UPGRADE_RESULT.txt` `2307e1d82c29b4e703b65b3b07b3d90b1d5b0f83ce9b7d535dc9cace250546e0`、`H5_DEPLOY_RESULT.txt` `99fcff0302c36a2091c5257c9819ede9e5be89f1f039ffdd2e33e467b75c7a30`；`EVIDENCE_MANIFEST.sha256` `55f888d100bc154d63001ed2f546754634cf91afe35133ff2a188fd31c51c5c7`，14/14 校验通过。详见 `docs/releases/20260820-010500-livekit-stack-upgrade.md`。
- 本次升级达到 `code / wired / enabled / production runtime verified`；不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 与 T1–T14 `0 pass / 14 blocked / 0 failed`。

## 上一生产增量（2026-08-19，输出 fence + FunASR task boundary，已切流并完成首次 Direct 上行会话观测）

- 源码提交 `3a3fcdc995efd86544480a5ee8c3beeb351ffd07` 与 annotated tag `20260819-151012-output-fence-funasr` 已推送 `origin/main`，tag 不再移动。已构建并仅切换 Agent 与 Voice Core Media Bridge，运行镜像 `memoria-agent:20260819-151012-output-fence-funasr`（image ID `sha256:589b6f4c993730be98517ffb4ef4481447106358d49f5207dce8bc217eff27ad`，OCI revision 与源提交一致）；Control API、Media Edge、固件、H5、小程序和数据服务保持 `20260818-103228` 及更早版本不变。
- 当前层级为 `code + wired + enabled + production runtime verified`：Bridge 具名 gRPC Health `SERVING`、Edge mTLS readyz `ready`、公网 8443 未认证回顾读 401 fail closed；回滚点 `rollback-20260819-151012-output-fence-funasr-pre-agent/bridge`（`sha256:2afb5420…`）已冻结。证据目录 `/opt/memoria/direct-canaries/20260819-151012-output-fence-funasr/`：`CUTOVER_RESULT.txt` SHA-256 `97f6af5237026c783fa61b79b81561b87c1e4d3f6bae24094f06bd4ef1161e67`，`POST_CUTOVER_STATE.txt` `3447155124a7c3842bb81ae698aded4e85ff8d66caad6d2d501e216e191baf5f`，manifest `c544d0ddeb0d37fd6377fe21fb383d99f72e52bda05d3d49976efb0587a19cee`。
- 首次 Direct 设备会话观测：会话 `8d2eeae4-96a7-4a5a-bb4a-183f14081fc4`（`stream_epoch=902`，`08:28:07Z–08:55:20Z` 约 27 分钟）上行音频到达 Provider，`media_asr_boundary` 共 223 条：143 success、80 failed（均为 `EmptyAudio` 短段低能量 fail-closed）；会话未产生 ASR final、话轮、generation、TTS 或下行播放，不满足 T4/T7，不计入 `direct_real_device_verified`。
- Agent 单次自愈重启归因：`16:52–16:55 CST` 服务器为 family-h5 站点多次 reload nginx，导致 Agent 到 LiveKit 的 WSS 于 `08:55:22Z` 断开；worker 重试 16 次耗尽后优雅退出，compose restart 于 `08:57:33Z` 拉起并在 `08:59:14Z` 重新注册 worker，第二代无连接失败。属外部运维瞬态，非候选缺陷；同时终止了上述设备会话。后续 nginx/证书运维应避开活跃设备会话窗口，并可评估拉长 Agent 对 LiveKit WSS 的重试预算。
- 本增量不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 或 T1–T14 `0 pass / 14 blocked / 0 failed`。下一步：用户本人对板卡说话，按顺序验收 T1/T2 → T4–T7。

## 上一待部署增量（2026-08-19，已合并入上节；保留提交前本地门禁记录）

- 源码提交 `3a3fcdc995efd86544480a5ee8c3beeb351ffd07` 与 annotated tag `20260819-151012-output-fence-funasr` 已推送 `origin/main`，tag 不再移动。内容：FunASR lazy task handoff、task/WebSocket/epoch fence、失败帧 fail-closed、Provider PCM 统计与失败恢复 backlog drain/discontinuity reset，以及 media session 输出 fence 与 output dispatch 收口。
- 切流前本地层级为 `code + wired + local verified`：Ruff、strict MyPy（399 个源码文件）、模块预算、固件 overlay gate、`firmware/esp32/tests` 与 FunASR/Provider adapter/Media session 聚焦套件全部通过。

## 当前候选 ESP32 实板状态（2026-08-19；非媒体启动已验证）

- 当前候选已由 `firmware/esp32/scripts/flash.sh` 写入真实板卡 `/dev/cu.usbmodem101`，退出码 `0`；bootloader、分区表、OTA data、应用和资源五段写入及写后校验通过并硬复位。写入地址仅为 `0x0/0x8000/0xd000/0x20000/0x800000`，未触碰 `0x10000..0x1ffff` 身份区。合并镜像 SHA-256 为 `7d7b921a81500817fda6a8230cd5c5b5a97814855dd0a440188f3d75e2cee188`（`13,577,086` bytes），应用镜像 SHA-256 为 `6fe8e936d481433f80fd0dcd5c076f2a5f47029132e2b9e21ccb1680fe3d798d`（`2,950,832` bytes），overlay SHA-256 为 `513e14ef346d49fc39e82b9bf96e2c56df0bf25bb1d2f081ee56e5707dd6a969`。
- 刷前/刷后身份区证据目录为 `firmware/esp32/artifacts/backups/pre-current-candidate-20260819/`；两份身份区 SHA-256 均为 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`，逐字节 `cmp` 一致；Wi-Fi NVS 与 OTA data 已备份。真实启动确认 ESP32-S3 rev `0.2`、`8 MB PSRAM`、SKU `memoria-atk-dnesp32s3-v1`、MAC `a4:cb:8f:d6:09:5c`，Wi-Fi 取得 IP，Activation Manifest v2 验签成功，状态为 `starting -> activating -> idle`，ES8388/I2S/AFE/单麦初始化成功。
- 这只是 `code + wired + enabled + verified` 的固件写入及非媒体启动证据；没有电脑播放、音频文件、TTS、程序生成音频或真人语音注入。`direct_real_device_verified=false`、`full_duplex_verified=false`，T1–T14 仍为 `0 pass / 14 blocked / 0 failed`。下一步等待用户本人说话，再按既定顺序进入 T1/T2/T4–T7。

## 当前候选增量（2026-08-18，VAD → ASR Provider PCM 边界可观测性）

- 代码提交 `3e16ee93b0628bfc02be527c3760ea53b943f53b` 与不可移动 annotated tag `20260818-152620-vad-asr-boundary` 已推送 `origin/main`。候选已按服务器最新环境仅部署 Agent 与 Voice Core Media Bridge：`memoria-agent:20260818-152620-vad-asr-boundary`，image ID `sha256:2afb5420f7d90459d1d94f708e97a1ba98588f31b7a144bb1a722aac6bb4c769`，OCI revision `3e16ee93b0628bfc02be527c3760ea53b943f53b`。当前层级为 `code + wired + enabled + production runtime verified`，但 verified 仅限服务器运行与脱敏边界观测。
- Direct Voice Core 在每次 VAD finalize 输出统一 `media_asr_boundary` JSON，包含 VAD start/event/voiced-end、transport watermark、Provider PCM 范围/send count、Provider task epoch 前后与 `success / failed / duplicate / stale / no-provider-finalize` 结果；不改变 VAD 阈值、短段策略、权限、主体或 fail-closed 行为。生产冻结于 `2026-08-18T09:37:29Z`：共 645 条，638 success、7 failed；7 条失败均为 `EmptyAudio`，另有 1 条独立 Provider `CLIENT_ERROR request timeout after 23 seconds`，不归因于边界观测。
- 生产探针：Agent/Bridge 容器 running/healthy、restart count=0；Control API 与 Media Edge 保持 `20260818-103228`；channel generation=2，liveness healthy=1，redial attempts/successes/failures=1/1/0，active device connections=1、active media sessions=0、downlink frames=0。该连接可能是空闲持久连接，不足以证明真实候选板卡、播放或全双工。
- 候选容器内 Qwen realtime-search、Doubao、FunASR Provider smoke 均通过（FunASR 6 次）；证据目录为 `/opt/memoria/direct-canaries/20260818-152620-vad-asr-boundary/`。`POST_CUTOVER_STATE.txt`、`CUTOVER_RESULT.txt`、`PROVIDER_SMOKE_RESULT.txt`、`MEDIA_ASR_BOUNDARY_RESULT.txt` 的 SHA-256 分别为 `ea9d953e969711f1d4043c6016b8202a3d8bba9f6ee06fe82207eccdce7578f5`、`c41355fb662574863ec20c0d73cbb7f36b86885bc4b5e0e1b51783ecb58f916d`、`65f85c4d6a23188f679ea74418abbbffadaa41f578f45f610a4802ac504cc1fd`、`ea32078314c3f43bbd02e2776e156a5a22bef9eb1f3f8ca4f01beabe18dda7e6`；`EVIDENCE_MANIFEST.sha256` 为 `9ae753278568bd8496af1e4d87f123751df1c3113ef9ad151a32b4103231be07`，18/18 校验通过。
- 失效脚本清理于 `2026-08-18T09:27:15Z` 完成：删除历史目录中的 `rollback-control-api.sh` 与其唯一依赖的 `cutover-control-api.sh`。root-only 回执 `/opt/memoria/direct-canaries/20260818-152620-vad-asr-boundary/ASSET_CLEANUP_20260818T092715Z_OBSOLETE_CONTROL_SCRIPTS.txt` SHA-256 为 `481c7fb40a1e00e221f7c478c744a34734fad45b329267744cd34e8bd19a7426`。历史 `20260817-171013` Compose/证据目录保留；当前有效紧邻回滚链 `rollback-20260818-103228-pre-runtime-profile*` 保留。
- 本增量不刷写 ESP32，不改变 `direct_real_device_verified=false`、`full_duplex_verified=false` 或 T1–T14 `0 pass / 14 blocked / 0 failed`。

## 上一生产增量（2026-08-18，Runtime Profile 生产切流与制品清理已完成）

- 源代码提交 `fb45fad59165ec819ac5b03148cba5c537b7eb6e` 与 annotated tag `20260818-103228` 已推送 `origin/main`；tag 不再移动。授权切换范围仅为 Control API（含 Session Runtime）、Agent、Voice Core Media Bridge 与 Go Media Edge；ESP32 固件、Nginx、H5、小程序、PostgreSQL、Redis、MinIO、LiveKit、Speaker Model 与两个兼容 Gateway 均未切换。
- 当前 Control API 运行 `memoria-control-api:20260818-103228`，image ID `sha256:14916cf1d00e1c10a8a8f52b5aad64eb301949f5e2319a02125484d3ed11e891`；Agent 与 Voice Core Media Bridge 运行 `memoria-agent:20260818-103228`，image ID `sha256:549f5f29176b6ad3361290726abecff748b804b5cb3473fd7ea6fc5e37784d65`；Go Media Edge 运行 `memoria-media-edge:20260818-103228`，image ID `sha256:d512fd348f9424cbeaccf091b9deeb50962dc83e5f3c598675dd2b5081697261`。四个目标容器均 running/healthy、restart count=0，OCI revision 均为源提交。
- 分层 runtime fence 保持：Control API/Agent 为 `20260814-231749-direct-canary`，Voice Core Media Bridge 为 `20260816-bridge-liveness-83af813`。公网 live/ready 均为 200；未认证回顾读与记忆确认写均为 401；Bridge 具名 gRPC Health 为 `memoria.media.v1.VoiceMediaBridge=SERVING`；Edge mTLS `/readyz` 为 200/ready。
- 切流后 metrics 为 `active_device_connections=0`、`active_media_sessions=0`、channel generation=1、liveness healthy=1、probe successes=4060、failures=0、redial attempts/successes/failures=0/0/0。该证据范围是服务器零活跃会话，不是真机语音闭环。
- 生产证据：`/opt/memoria/direct-canaries/20260818-103228/POST_CUTOVER_STATE.txt` SHA-256 为 `41060c9838f1c1db0b38e52c906292f9b14bd025e13df9539dc3bd6e9fa98bc8`；`CUTOVER_RESULT.txt` SHA-256 为 `37da7f571630c0585ab350464b789196cd3075998758a3c43d611ab240b53848`。即时 rollback 点与切流前快照均保留，annotated tag 不移动。
- 首轮制品清理于 `2026-08-18T02:57:28Z` 完成：删除历史目录 55 个、旧 `memoria-*` 镜像标签 90 个，失败 0，Docker build cache=0；该轮清理后根盘为 118G 总量 / 50G 已用 / 65G 可用 / 44%。
- 后续资产清理于 `2026-08-18T05:55:13Z–05:55:25Z` 完成：删除已退出诊断/一次性 provision 容器 `eloquent_brattain`、`focused_euler`、`affectionate_bardeen`、`memoria-data-minio-provision-1`；旧 incoming `20260817-135129`、两个临时 staging、两个旧 Agent canary、一个旧 Media Edge canary；以及 5 个未被容器引用的旧镜像标签。root-only 回执为 `/opt/memoria/direct-canaries/20260818-103228/ASSET_CLEANUP_20260818T060000Z.txt`，SHA-256 `68e0e72c4bedaf49a919bb1ede901f07eedc25453e29188a753568c6ff8582cf`；删除后 Docker 为 29 images / 12 active、14 containers / 14 active、5 volumes / 4 active、build cache=0，根盘为 118G 总量 / 44G 已用 / 70G 可用 / 39%。未删除命名卷、数据库、Redis、MinIO、LiveKit、合规备份或当前/紧邻回滚链。
- 第三轮定点清理于 `2026-08-18T07:16:42Z` 完成：确认无 Compose、容器、软链、systemd、cron、runtime manifest 或可运行回滚链引用后，删除旧源码检出 `/opt/memoria/direct-canaries/20260814-231749-direct-canary`，释放 `43,723,036` bytes。root-only 回执为 `/opt/memoria/direct-canaries/20260818-103228/ASSET_CLEANUP_20260818T071642Z_ROUND3.txt`，SHA-256 `327061dd42e369cf2d93cb45b6174f89b122892231d1d82bcd8604fae144c9bc`；删除后路径不存在，当前 runtime 与紧邻可运行 rollback 均保留，14/14 容器 active，根盘为 118G 总量 / 45G 已用 / 69G 可用 / 40%。回执中的 Docker images=22 按唯一 image ID 统计，与第二轮 29 个 tag/list 项口径不同。
- 当前仍保留 `20260818-103228` Control API/Media Edge 运行版本、`20260817-171013` 证据、正在运行的 legacy Gateway/Speaker Model 镜像、当前/回滚 incoming 与 Compose 证据；Agent/Bridge 已由顶部 VAD→ASR 候选接替。两份失效一次性脚本的删除记录与回执见顶部。PR-23 与 Direct T1–T14 不因制品清理而完成。
- 层级严格记录为：本 release 目标范围 `code / wired / enabled / production runtime verified`；`direct_real_device_verified=false`、`full_duplex_verified=false`，T1–T14 仍为 `0 pass / 14 blocked / 0 failed`。没有当前候选固件刷写、候选设备音频会话、Exact DAC、AEC Reference、Double-talk、精确 Actual Heard、100/500 轮或微信 iOS/Android T14 证据。

## 上一生产增量（2026-08-17，已提交、推送并完成 Agent/Bridge canary）

- 发布代码提交与 tag 均为 `91f179069055720795fadafc1839045c1cc61f31` / `20260817-135129`；代码提交已推送 `origin/main`，发布 tag 不再移动。固定 amd64 工件已上传并校验，实际只切换 `agent` 与 `voice-core-media-bridge`；Control API、Media Edge、Speaker Model、两个 Gateway、数据层、LiveKit、Nginx 与 H5 均未切换。
- Agent 与 Bridge 当前均运行 `memoria-agent:20260817-135129`，image ID `sha256:163c4d534fa06a4b796a5217fcaead32ea97162a108bef1c4c6237f94238cb4e`，OCI revision 与发布提交一致。Agent 继续报告 Control 接受的 runtime tag `20260814-231749-direct-canary`；Bridge 保留 `20260816-bridge-liveness-83af813`，避免分层 canary 的 release fence 被破坏。
- 第一次切流于 `2026-08-17T06:20:54Z` 因公网 readiness 的既有 `smokes:not_run` 返回 503 而自动回滚；候选容器健康、具名 gRPC Health、Edge redial 与 Provider smoke 当时均已通过，未定位到候选代码故障。该过程实际验证了冻结 Agent/Bridge rollback 镜像可恢复。补跑 LiveKit、Provider、`verify_env` 和 readiness mark 后，第二次切流自 `06:25:41Z` 成功，Edge 于 `06:26:12Z` 无重启自动重拨。
- `2026-08-17T06:34:16Z` 延迟复核：Agent、Bridge、Edge 均 healthy 且 restart count 为 0；Bridge 具名 `memoria.media.v1.VoiceMediaBridge=SERVING`，Edge mTLS `/readyz` 为 ready。Edge redial attempts/successes 为 `3/3`、liveness healthy 为 `1`、channel generation 为 `4`；active device connections/media sessions 为 `0/0`。自 `06:26:12Z` 起三者的目标错误计数均为 0。
- 公网 8443 的域名与 IP 根页、H5、SPA、API live/ready 和 WMS 均为 200，三个保护路由按预期为 404。2026-08-17T06:56:47Z 只读复核确认：真实源站 IP 的 443 不带 SNI 时 WMS 为 200，但带域名 SNI 在 ClientHello 后 EOF（curl `SSL_ERROR_SYSCALL`、openssl `unexpected eof`），所以标准域名 443 WMS 仍未验收；该既有边界未由本 release 修改，正式入口继续使用 8443。域名证书有效至 `2026-10-18`；IP 证书有效至 `2026-08-21`，certbot renewal timer 当前 enabled/active。生产 runtime/H5 软链仍分别指向 `20260812-173008` 与 `20260808-171749`。
- 回滚目录为 `/var/backups/memoria/canary-20260817-135129`；Agent rollback image ID 为 `sha256:467b431bc80c5d6de00ffa2c578e20f0843e523ad971808654ea77bdf7d26acd`，Bridge rollback image ID 为 `sha256:d0be30a1db2126840d2af886d99a038d2303bb5a705da14af420311d8e9a1bde`。服务器证据位于 `/opt/memoria/direct-canaries/20260817-135129/CUTOVER_RESULT.txt`，SHA-256 为 `afc6a8aacf3c7fa9c6e4c21fe53d65e98b12c22779903253ad1e71d7211fc04c`。当前磁盘约 19 GiB 可用；不要清理正在使用的分层 canary、即时回滚镜像及本次 incoming/runtime/证据目录。
- Speaker Authority、generation 委派输出 claim、Media Turn fence/retry、空帧 fail-closed、Runtime Profile 低基数指标与 dev 依赖的本地门禁仍为：Agent `1558`、Control API `586`、聚焦 `165/126/4/251`、strict MyPy `399 source files`、Ruff、Go 全量、模块预算和 `git diff --check` 通过。Control、Go Edge 与 ESP32 未随本次 Agent/Bridge 切流更新，不能把跨语言本地通过写成这些组件已部署。
- GitHub Actions run `32003128393` 未通过；H5/小程序通过，Python collection、Media Edge lint 与 Media Edge image Trivy 失败，且同一失败签名在此前提交持续存在。远端 CI 当前不是绿色门禁，不影响已完成的 Agent/Bridge 零会话 canary 事实。
- 当前层级严格为 `code=true`、`wired=true`、`enabled=true`（仅 Agent/Bridge 候选）、`production_runtime_verified=true`（零活跃会话服务器范围）；`direct_real_device_verified=false`、`full_duplex_verified=false`。T1–T14 继续为 `0 pass / 14 blocked / 0 failed`，Exact DAC、AEC Reference、双讲、精确 Actual Heard、100/500 轮和 T14 微信独立性没有新增证据。本轮没有刷写 ESP32，也没有触发设备或电脑播放。

## PR-16 本地回归收口（2026-08-17；不扩展生产验收范围）

- Tool cancel 已达到 `code / wired / enabled / local verified`：真实 interrupt、媒体抢占和 identity epoch rotation 都沿旧 generation 调用 `TaskManager.cancel_cancellable`；迟到 tool 结果同时受 cancelled 状态、generation/task/context fence 和 one-shot output gate 约束，不能形成新代语音输出。证据为 `test_cancelled_late_tool_result_cannot_form_new_generation_output`。
- heard text cutoff 已达到 `code / wired / enabled / local verified`：provider timed transcript 与 Playback Ledger 在中断时只保留已生成、已确认的时间范围；未播放尾部不会进入 Actual Heard 候选。证据为 `test_existing_provider_adapter_cuts_unplayed_timed_text_on_interrupt` 及 media-session interruption/playback-ledger 回归。
- 以上是当前 Agent/Bridge canary 范围内的本地回归和接线证据，不是有活跃设备会话的生产行为证据。Exact DAC watermark、真实设备 Actual Heard、AEC Reference、Double-talk、自然 Barge-in 与 T1–T14 仍保持未通过；`direct_real_device_verified` 和 `full_duplex_verified` 继续为 `false`。

## PR-19 对话回顾投影（2026-08-17；本地闭环，不扩展真机验收）

- `GET /v1/archive/conversation-review` 已成为小程序回顾页的窄投影权威，返回 `actual_heard`、`memory_candidates`、`confirmed_memories` 三个互斥分区；Actual Heard 只接受带 session/turn/generation fence 且同时满足 history/owner eligibility 的 `assistant.playout_stopped`，未明确 `approximate=false` 的记录默认按近似展示。
- `POST /v1/archive/memories/{claim_id}/review` 继续作为唯一候选确认写路径；conversation review capability 在 Archive/Catalog 读取或写入前执行，双账号以及 adult/minor/unknown/missing 主体矩阵已覆盖。
- 小程序 MemoryRecallPrivate 门禁、authEpoch 晚到保护、游客清理和确认后服务端权威重拉已接线；聚焦 23/23、小程序全量 150/150、Control API archive 测试 2/2 通过，Ruff 通过。当前层级为 `code + wired + local verified`。
- 这不是精确 DAC watermark、真实设备 Actual Heard、AEC、Double-talk 或 `full_duplex_verified` 证据；`direct_real_device_verified=false`、`full_duplex_verified=false`、T1–T14 仍保持原状态。

## 当前状态（2026-08-16，Edge Bridge Health + supervisor 已 production enabled + runtime/chaos verified）

- 当前未刷板固件候选已于 `2026-08-16T16:14:08Z` 从锁定 upstream
  `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5` 完成 ESP-IDF 6.0.2 clean build 与 overlay
  gate。内容加相对路径的 overlay SHA-256 为
  `513e14ef346d49fc39e82b9bf96e2c56df0bf25bb1d2f081ee56e5707dd6a969`；Memoria 应用
  `2,950,832` bytes、SHA-256 `6fe8e936d481433f80fd0dcd5c076f2a5f47029132e2b9e21ccb1680fe3d798d`，
  应用分区余 `1,177,936` bytes（29%）；合并镜像 `13,577,086` bytes、SHA-256
  `7d7b921a81500817fda6a8230cd5c5b5a97814855dd0a440188f3d75e2cee188`。`flasher_args.json`
  只含 `0x0/0x8000/0xd000/0x20000/0x800000`，不含 `0x10000` 身份区。该候选没有刷板：
  `flashed=false`、身份保持和 boot/activation 均未验证，Direct 真机与全双工仍为 false。
- Python `MediaBridgeGrpcServer` 已注册标准 gRPC Health；只有
  `memoria.media.v1.VoiceMediaBridge=SERVING` 才可被 Edge 接受。Supervisor 的启动、
  readiness 和 replacement candidate 都执行 Health RPC，因此旧 gRPC transport 即使仍为
  `READY` 也会 fail closed；并新增 liveness 成功/失败/健康指标。
- 本轮聚焦门禁：`services/media_edge` Go 全量测试、`go test -race -count=1 ./...`、`vet` 与
  三次 supervisor 聚焦 race 全部通过；Python Ruff、strict MyPy 和 Bridge Health 定向 pytest
  `21 passed` 通过。测试覆盖启动 fail-closed、READY 但 NOT_SERVING、grace、mTLS
  replacement、有界退避、旧 channel 不重放、shutdown/redial 竞态及指标；T1–T14 本轮复跑仍为
  `0 pass / 14 blocked / 0 failed`，Exact DAC、AEC Reference、双讲、精确 Actual Heard、
  100/500 轮与 T14 微信独立性没有新增证据。
- 生产 Direct 单 Edge 已配置并启用 Device State Redis URL 与 Redis mTLS 四件套，Media Edge
  healthy；生产 Direct 模式缺少 Redis 会 fail-closed 启动。该证据只证明单 Edge 共享权威已启用，
  不证明真实票据重放、跨实例接管、Redis HA、故障转移或混沌。
- Edge Bridge supervisor 生产候选为 tag 20260816-bridge-liveness-83af813、revision
  83af81318083b1977521a1fbdca8d9bbe51927bd；只重建 Voice Core Bridge 和 Go Media Edge。Bridge
  Docker health 为 healthy，具名 gRPC Health memoria.media.v1.VoiceMediaBridge 为 SERVING；Edge
  Docker health 为 healthy，私有 mTLS readyz 返回 200 ready。随后私有 metrics 的 liveness probe
  successes 由 6 增至 9、failures=0，active_device_connections=0、active_media_sessions=0。
- 受保护的生产混沌演练只停止 Voice Core Bridge：停止期间 Edge mTLS readyz=503、liveness=0；恢复
  Bridge 后 readyz=200、liveness=1、redial attempts/successes 均由 0 增至 1、channel generation
  由 1 增至 2，且没有会话重放。回滚链按候选 Edge+Bridge → 旧 Edge+候选 Bridge → 旧 Edge+旧
  Bridge 完成，再反向恢复候选；所有检查均为零活跃设备/媒体会话范围。
- 本轮没有重建 Agent、Control API、Mini Program Gateway、Device Media Gateway、PostgreSQL、Redis、
  Nginx、LiveKit 或 H5；被保护容器启动时间保持不变。以上是服务器运行与混沌证据，不是 Direct
  真机、AEC、双讲、DAC、Actual Heard 或 full-duplex 验收。生产制品已按当前候选加即时可用回滚保留，
  并删除无引用的旧 20260816-170800 Media Edge 候选、其 tar 和已复制的临时上传目录，磁盘可用空间
  从约 26 GiB 增至约 27 GiB。

### Media Turn retry canary（2026-08-16）

- Direct Voice Core 本轮把音频入口串行排空、VAD endpoint/tail、ASR final 缺失、Provider turn prepare
  瞬态失败重试、输出生成超时、重连 epoch 与 Playback/Projection fence 收敛到同一话轮生命周期。
  prepare 重试沿用原 `stream_epoch + endpoint_sample`，新 VAD 才能显式 supersede；耗尽时消费到
  transport retire watermark 后 fail closed 丢弃，不把旧话轮合并进下一话轮。17 个生产运行文件由
  `MEDIA_TURN_RETRY_FILE_HASHES.sha256` 冻结；`media_agent_factory.py` 没有被候选覆盖，因为生产基线
  已有更新的 authority-unavailable fail-closed 保护。
- 本地门禁已通过：Agent 全量测试；`test_media_session.py` 78 项；`test_speech_timeline.py` 5 项；
  硬件验收器 157 项；Ruff；strict MyPy（399 个源码文件）；模块预算；`git diff --check`。
- 生产候选为 `memoria-agent:20260816-214225-media-turn-retry-canary`，revision
  `9be3d18ed7f8ec1702cc52d674c813a549f48f71`，image ID
  `sha256:467b431bc80c5d6de00ffa2c578e20f0843e523ad971808654ea77bdf7d26acd`。第二次切换窗口为
  `2026-08-16 22:19:13–22:20:01 CST`，只重建 `agent` 与 `voice-core-media-bridge`；两个容器
  17/17 运行文件哈希一致，继承的生产 `media_agent_factory.py` SHA-256 均为
  `c4c6004cbbd68665354743d51699faf801568266e7684c169538c47922307410`。Dockerfile SHA-256 为
  `463a921111d17dc8db78e7a6fff6ff1477bd0873f2f7db66cbb12eba69648b6e`，运行 manifest SHA-256
  为 `0d44a70b9b64f10cdc0134c95285c5958e8dacca9442f2f49d4ee4c5b74f46a2`。
- 当前关键容器：Agent `02b557181037…`、Media Bridge `50ca13e22248…`、Media Edge
  `8a157c3d5da4…`，均 healthy。Edge 使用原容器执行同 ID restart，没有重建；启动时间为
  `2026-08-16T14:19:40.076649486Z`。Control、Media Edge、LiveKit、PostgreSQL、两个 Redis、
  MinIO、Device Media Gateway、Mini Program Gateway、Speaker Model 共 10 个保留容器 ID
  均未变化。运行环境仍是 `MEMORIA_RELEASE_TAG=20260814-231749-direct-canary`，
  `/opt/memoria/current -> /opt/memoria/releases/20260812-173008`。
- 第一次切换从 `22:09:45 CST` 开始：Agent/Bridge 已 healthy，但 Edge readiness 持续 503，
  约 `22:10:55 CST` 自动回滚到冻结镜像
  `memoria-agent:rollback-20260816-214225-pre-media-turn-retry`（image ID
  `sha256:0453fa47c495e913ccede09ac3ab0d73caf46c4363c018e23019a51cddcada44`）并恢复 healthy。
  诊断已证明新旧 Bridge 均可经 TCP 与 mTLS gRPC 连接；实际故障是旧 Edge 进程持有的长生命周期
  gRPC channel 在 Bridge 容器 IP 改变后没有自动重解析/重拨。`22:14:59 CST` 重启同一 Edge 容器
  后立即恢复 readiness；第二次切换及对应回滚流程因此都纳入“目标服务 healthy 后，同 ID 重启
  Edge 完成重拨”。这是当前受控发布步骤，不是长期自愈能力。
- `2026-08-16 22:28 CST` 延迟复核：三个关键容器仍 healthy，Agent/Bridge 仍运行上述候选同一
  image ID，Edge `/readyz` 返回 `ready`；自 `22:19:13 CST` 起 Traceback、
  `stale core event sequence`、ASR tail timeout、prepare retry exhausted、output timeout、ERROR、
  closed client、speaker 403 计数均为 0。Edge 指标仍为 `active_device_connections=0`、
  `active_media_sessions=0`，所以只可记录 `production_runtime_verified=true`，不能写成设备恢复、
  Direct 真机通过或用户 Actual Heard。
- 生产证据目录为
  `/opt/memoria/direct-canaries/20260816-214225-media-turn-retry-canary`，其中保留
  `CUTOVER_RESULT.txt`、`PRE_CUTOVER_STATE.txt`、scope/manifest、生产 Compose 与 rollback Compose。
  当前候选和冻结 rollback 镜像必须保留；仍被 Control/Media Edge Compose 元数据引用的旧 canary
  目录也不得删除。
- 当前状态严格为：`code=true`、`wired=true`、`enabled=true`、
  `production_runtime_verified=true`、`direct_real_device_verified=false`、
  `full_duplex_verified=false`。T1–T14 仍是 `0 pass / 14 blocked / 0 failed`；Exact DAC、AEC
  Reference、双讲、精确 Actual Heard、100/500 轮与 T14 微信独立性没有新增证据。
- Edge Bridge supervisor（受监督的 channel 重建、有界退避、指标）已于 2026-08-16 完成生产部署、
  应用层 Health fail-closed、Bridge 停止/恢复自动 redial 与双向回滚演练；本地 Go 全量、-race、vet 与
  Python Bridge Health 门禁仍为其代码证据。尚未验证带活跃会话恢复、Bridge IP 变化、Edge 重启、告警
  送达或完整混沌矩阵，因此该项不外推为 Direct 真机或全双工通过。

### Direct event-sequence canary（2026-08-15）

- 真实服务器日志已定位本轮“板子说你好你好无响应”的直接断点：Media Edge 关闭设备流并报
  `stale core event sequence`。根因是 Voice Core 在生产者入队时预分配序号，而 critical
  事件经优先级队列插队后先发送较大序号，随后发送的较小 reliable 序号被 Edge 的严格
  stale fence 拒绝。修复没有放宽 Edge fence，而是把 Generation/Transcript/State/Client/
  Shadow/Effect 的 wire sequence 统一移到实际发送边界；被合并或丢弃的 shadow 不消耗序号。
- 候选 `memoria-agent:20260815-110524-event-sequence-canary`（revision
  `7287fcf6bbbff26ba42223ccda690e01b54dc20b`）已于 `2026-08-15 11:11 CST` 只重建服务器
  `agent` 与 `voice-core-media-bridge`。两个容器均 healthy，运行文件 `grpc_bridge.py`
  SHA-256 为 `1dd38c58f3e0fcc9c66140136331a06b45e3370da22c4f81555c9535a2dd5f62`；Control、
  Media Edge、LiveKit、PostgreSQL 容器 ID 均保持不变。运行环境 release tag 继续使用
  `20260814-231749-direct-canary`，避免 Control 心跳代际不兼容。
- 切换后新鲜日志未再出现 `stale core event sequence`、ASR tail timeout 或 Agent/Voice Core
  ERROR。Edge 在重建时仅记录一次旧 gRPC 流 `Cancelling all calls`，属于切换窗口回收。板子随后
  成功恢复 Direct v2 会话并回到 idle；但截至本记录尚未捕获切换后的新 VAD/uplink/FunASR
  final/LLM/TTS/playback 与用户 Actual Heard，所以当前层级是 `code + wired + enabled`，不是
  `verified`，`full_duplex_verified` 继续关闭。
- 硬件验收器新增 `collect` 后经主 Agent 收紧：T5–T7 要求同一 generation 的因果链，跨代
  playback ACK 不得放行；复制到 evidence 的 ASR/用户/助手文本与错误消息默认脱敏。聚焦测试
  `122 passed`，Ruff 和 `git diff --check` 通过。输入 trace 仍由操作者提供，因此生成的是候选
  回执，必须与并发串口、服务器日志和用户 Actual Heard 共同验收。

### 本轮整改复核（2026-08-14）

- Direct v2 本轮代码修复已闭合：长生命周期 Voice Core gRPC stream 不再复用有界握手 timeout context；目标板同时声明 `16000/24000` 时，Direct 下行优先协商原生 `24 kHz`，下行帧为 20 ms/480 samples，sample clock 连续递增。新增/同步的 Go 集成测试验证握手 timeout 取消后仍可发送 uplink，runtime close 仍能关闭 stream。
- 本轮本地门禁：Ruff 通过；strict MyPy 通过（399 个源码文件）；Python 相关服务与合同测试 `2200 passed, 1 skipped`；固件协议/源码测试 `151 passed`；小程序 `142 passed`（含 `no-realtime-media-gate` 与真实编译 upload dry-run）；验收器测试 `100 passed`；Go `test`、`test -race`、`vet` 通过；`git diff --check` 通过。
- 固件 JSON 边界解析已收口：`uint32/uint64` 字段拒绝非数字、NaN、Infinity、小数、负数和越界值，`uint64` 在 `2^64` 独占上界检查后才转换；对应源码门禁、overlay 检查与 ESP-IDF 6.0.2 clean build 于 2026-08-14 通过。该句记录的是 `2026-08-14` 当时 Direct v2 尚未部署、未启用、未验证的状态；`2026-08-16` 当前层级以上方状态摘要和 `architecture-status.yaml` 为准，Direct 真机验证仍未通过。
- `scripts/hardware_realtime_acceptance.py run` 在本机安全执行后报告 `T1–T14 = 0 pass / 14 blocked / 0 failed`，因为该工具要求真实设备/微信/生产安全的结构化回执；这不是代码失败。串口 `/dev/cu.usbmodem2101` 存在，但本机没有生产 `.env` 或运行中的 Memoria production stack，不能据此证明 Direct 已重新部署或启用。
- 因此当前状态严格保持：`code = complete`；`wired = 候选 bundle/config complete`；`enabled = 本轮未重新核验`；`verified = Direct T4–T7 未完成`。不得把本地通过写成 Direct 实板通过，不得开启 `full_duplex_verified`。

### ESP32 一等语音终端与小程序纯控制面

- 已按 ADR-0035 将目标链路收敛为“ESP32-S3 → Go Hardware Media Edge WSS →
  mTLS gRPC media-v1 → Python Voice Core”，交互权威仍是 Python。Control API 以服务端
  开关在 `direct_voice_core` 与 `livekit_compat` 回滚路径之间选择，设备不能自行切换；生产
  仍保持 `python_authoritative + livekit`。本轮只发布 legacy Agent ASR task 轮换与确定性当前日期
  响应，没有切换 Direct、Control 或 Edge。
- 新增严格 Device Media v2：Ed25519/JWKS 短票据、一次性 JTI、device/client/binding/
  subject/profile/epoch 全量绑定、direct v2 与 legacy v1 路由隔离、上行 Opus 16 kHz/下行 24 kHz、单连接租约、单调预留
  `stream_epoch`、sequence/sample/generation 校验、80–200 ms 背压、P0 控制队列和 Edge →
  Voice Core 双向桥。短票据只用于建连，不再把 120 秒凭证 TTL 错当会话 TTL。
- Generation 直接从 1 开始，新路径不做 N+1 映射；按钮同步清空本地 decoder，Edge/Core
  关闭 generation gate 并丢弃迟到帧。`generation.completed` 是同代音频之后的有序屏障。
  当前板没有 DAC 样本计数，因此 playback receipt 明确为 output-commit 上界、
  `approximate=true`，Actual Heard 继续 fail-closed，不能宣称 exact。
- Runtime Profile/设备设置闭环已补齐：设置与 profile 版本在同一 SQLite 事务提交；
  Control 通过独立 HTTPS/mTLS + 32 字符以上控制令牌通知 Edge；设备在真实应用
  `session.accepted` 后上报 `runtime_profile.applied(profile_version, settings_version)`；Edge
  只读状态接口给 Control/小程序展示协商后的实际音频模式，页面明确区分期望值与实际值。
- 会话生命周期闭环已补齐：owner DELETE 与 `runtime_profile_invalidated` 才进入
  `session_closed` PostgreSQL 权威事件并原子推进 `session_epoch + generation/turn/tool`
  fence；Edge 网络断开/替代/关机只结束当前 transport epoch，记录断线诊断并保留同一
  Session 的恢复资格。Edge 回报使用与
  Control→Edge 控制令牌分离的 token，并逐项核对 device/account/stream epoch；过期 Profile
  或已撤销 binding 仍允许执行终态清理，不会把 Session 永久卡在 active。
- 设备端同 Session 传输恢复已补齐：Wi-Fi 或仅 WSS 断开进入显式 `RECOVERING`，最多 5 次
  按 1/2/4/8 秒退避申请同 Session、更高 `stream_epoch`；每个 WSS attempt 是独立权限
  fence，被动断开先退休旧 attempt，再通知上层。旧连接排队中的音频、TTS/STT、字幕、表情和
  close 回调不能修改新 epoch；用户主动结束、服务端终态、恢复耗尽均清除 resume 身份。
- 生产 direct bundle 已补齐独立 device WSS listener、精确 Nginx WSS 路由、Ed25519/JWKS、
  Control→Edge mTLS、专用 healthcheck client identity 与 Edge→Control 关闭回报；生成器从
  direct 回退 `livekit_compat` 时清除全部 direct-only key，避免半配置启动。截至 `2026-08-16`，
  当前单设备 Direct canary 的精确路由、mTLS/JWKS、回滚路径与运行时健康已启用并有生产证据；
  证书轮换、托管私钥、更大范围发布及完整混沌仍属外部门禁。
- Direct Device WSS 的一次性 JTI 与设备连接租约已从单进程 map 抽象为生产强制 Redis
  原子共享状态：JTI 使用带票据 `exp` 的 `SET NX`；租约绑定
  `device_id + stream_epoch + owner_id + conn_id`，更高 epoch 通过 Lua 原子接管，Pub/Sub
  立即关闭旧主机 socket，compare-and-refresh 作为消息丢失后备，compare-and-delete 阻止旧连接
  删除新租约。跨两个独立 Edge 实例的真实 Redis 进程测试已覆盖票据重放、conn_id 跨主机碰撞、
  旧 epoch、旧 release、Redis 丢失时 readiness/活动连接 fail closed；Go 全量 test/race/vet 通过。
  截至 `2026-08-16`，生产 Direct 单 Edge 已显式提供独立
  `MEDIA_EDGE_DEVICE_STATE_REDIS_URL` 与 Redis mTLS 四件套并保持 healthy；缺少 Redis 时生产
  Direct 会 fail-closed 启动。该生产证据不替代真实票据重放、跨实例接管、Redis HA、故障转移和
  容量混沌验证。
  设备协议的 `stream_epoch`、Runtime Profile/Settings/声学证明版本已在 Control 签票、Edge
  验票和关闭回报边界统一限制为固件真实可表示的 `uint32`，达到上限显式拒绝，不能静默截断；
  `session.accepted` 只在握手状态 CAS 成功后入队，跨主机接管期间的旧握手不能短暂复活。
- 当前生产 Control API 仍是 `20260812-173008` 代际，只接受 legacy v1 媒体会话请求，并以严格
  422 拒绝新增协商字段。固件已增加仅匹配 FastAPI `extra_forbidden` 且字段集合精确一致时的
  单次滚动发布回退：新会话删除 `supported_protocol_versions` 后复用尚未消费的签名 challenge；
  v2 恢复遇到服务端回滚时，在会话 fence 下清除 resume 身份并重新建立 fresh v1。401、5xx、
  其他 422 或迟到结果仍 fail closed。该兼容代码已通过评审、源码测试、干净构建和本轮最终固件
  的 legacy v1 实板连续两轮验证。该句记录的是 `2026-08-14` 当时 direct v2 尚未部署、未启用、
  未验证的状态；截至 `2026-08-16`，Direct 单设备 canary 已启用并完成服务器运行复核，但没有
  活跃设备会话或 Direct v2 真机闭环，`direct_real_device_verified` 仍为 `false`。
- 旧候选的真实会话已证明 legacy v1 链路能完成首轮“你好你好”：设备 VAD、FunASR final、LLM、
  TTS 和扬声器均实际消费，但生产日志显示从最后一帧用户音频到 ASR final 约 `20.5 s`，LLM 首 token
  仅约 `1.25 s`，因此首轮长等待的主因是 ASR 断句。紧接着“今天星期几”有正常 VAD 和用户音频，
  却始终没有 ASR final，LLM/TTS 未启动。根因是 legacy LiveKit 会话把多个 VAD 话段长期复用为一个
  FunASR task，且 heartbeat 包持续重置接收空闲计时，噪声下既不强制断句也不超时退出。
- Agent 本地候选已在每个权威 `vad.end` 同步 flush 当前 STT stream，执行
  `finish-task -> task-finished -> 新 task_id 的 run-task`，同一 WebSocket 复用但 task/segment fence
  隔离；task boundary 使用单一绝对 deadline，heartbeat 不能续期，已结束 task 的迟到事件也不能污染
  下一话段。除 legacy LiveKit STT stream 外，Direct Device v2 的 provider-neutral Voice Core 路径也在
  VAD 尾帧排空后轮换 FunASR task，并先验收旧 task 的终稿再公布新 task epoch；空 heartbeat 不进入
  ASR 语义时间线。完整 Agent unit/integration `1493/1493`、strict mypy、Ruff 均通过。
  `2026-08-14 17:06 CST` 已从生产基线 `memoria-agent:20260812-173008` 离线增量构建并启用首个
  Agent-only canary `memoria-agent:20260814-165401-agent-canary`，候选 commit
  `4726f99032b3c8ff1f18dc483189db470560d2f5`；运行容器源码哈希、amd64 架构与 OCI 标签均核对。
  首次实板复验已证明第二个 FunASR task 可正常产出 final、turn/generation 连续推进，不再出现第二问
  永久无响应；同时暴露两个独立问题：设备 VAD 使用 `VAD_MODE_2` 时每轮由 `20 s` 硬上限关段，且
  `unknown_safe` 模式把“今天星期几”交给 LLM 后错误回答 2025 年。
  `2026-08-14 17:28 CST` 已增量启用当前 Agent-only canary
  `memoria-agent:20260814-172823-agent-canary`，候选 commit
  `c6c54d4ade09025eb24d88c896ef9e65c6568a82`；当前日期/星期由 Agent 时钟生成规范固定回复，跨模式
  优先于 LLM。运行时 release tag 仍报告 `20260812-173008` 以保持 Control 心跳代际一致，当前 boot ID
  为 `c76aa5d9-9cd5-463b-9077-d9d33e252687`，health/readiness 全绿且其余四个应用容器 ID 未变化。
  首个 canary 与原生产 Agent 镜像均保留回滚标签；尚未执行真实回滚演练。
- 当前 Agent 与最终固件的重新实板复验完成连续两轮：第一轮“你好你好”提交
  `turn 1 / generation 1`，最后用户音频到 FunASR final 约 `163 ms`；第二轮“今天星期几”提交
  `turn 2 / generation 2`，对应延迟约 `181 ms`，Agent 命中 `direct_text=true`，设备显示
  “今天是2026年8月14日，星期五。”并回到 listening。串口未再出现 `20 s` overlong VAD；设备报告
  两轮 playback start，用户已当场确认两句均实际听清、体感可用，仅有“说完后等待 AI 回答”的
  轻微延迟。因此本次连续两轮场景可记 Actual Heard 通过，但不外推为 DAC 精确采样、双讲或完整
  T1–T14 声学验收。服务端从最后用户音频到 playback start：普通问候约 `2.53 s`（其中 LLM
  request 到首 token 约 `1.36 s`），确定性日期回复约 `0.55 s`；口腔停声到设备 VAD end 的尾窗还会
  叠加在用户体感上，后续应以端到端 P95 优化，不宜仅凭一次样本继续压低 VAD 阈值。
- 本次成功复验窗口内 Agent 无 ERROR、无 overlong VAD。仍有两个相互独立的既有告警：生产
  Speaker Authority 调用返回 HTTP 403，Voice Profile 因 authority/token 不可用而禁用，会话因此按
  `unknown_safe` 降级；LiveKit 另记录一次 transcript-after-commit，但本轮 canonical 文本、turn 与
  generation 均正确。只读核对确认本次测试账号权威 `subject_category=unknown`，而统一能力规则只
  允许 adult 使用 `speaker_enrollment`，因此 403 是预期 fail-closed，不是 token 配错；扩大 canary
  前应补齐账号类别证据，或让 Agent 对明确无资格会话降低预期告警噪声，不能放宽生物特征门禁。
  transcript 时序仍需独立消警。二者均不改变本次 ASR 轮换结论，不能与“第二问不响应”混为同一根因。
- 旧固件还在 legacy v1 上错误套用了 v2 主动 Ping/Pong 判死，并曾在设备 uptime 约 `392.5 s`
  误退健康 transport。最新固件仅对 v1 关闭这条客户端主动判死，由 TCP/WSS 断开、发送失败和网关
  close 负责故障权威；v2 的 `30 s Ping / 10 s Pong` 仍严格保留。该修复已上板完成真实 legacy v1
  媒体会话与连续两轮对话；超过旧故障点 `392.5 s` 的长连接稳定性仍待单独持续验证。
- 当前板固件会应用音量与亮度；ATK-DNESP32S3 的背光是二值门控（0 关闭，1–100 开启），
  不是 PWM 精确亮度。设备能力诚实声明无 AEC Reference、无 simultaneous capture/playback、
  无本地停止词/duck；服务端无声学验收记录时只开放 `half_duplex_safe`，绝不自报
  `full_duplex_verified`。
- Espressif VAD 模式语义已纠正：数值越大越容易触发语音，当前无 AEC Reference 的目标板使用
  `VAD_MODE_0` 并保留 `vad_min_noise_ms=900`。以下为 `2026-08-14` 历史实板候选，overlay 哈希为
  `3d967764a623d13e0983f87de494465c051c6278efa1b2b862280c0c2bd33217`；ESP-IDF 6.0.2
  clean build 生成 `xiaozhi.bin` `2,964,720` bytes、SHA-256
  `1a91babd4a36b4e30ae6bb5e0463bae0fd099ec8778c71d4c7e5dcfc1411deb4`，应用分区余
  `1,164,048` bytes（28%）。合并镜像 `9,873,069` bytes，SHA-256
  `cc155a344ea948deb373aa440f5c85ebd71cad0c602960188c7ff07d136eca81`。
  最新候选仅在 `0x20000` 写应用并完成独立 flash verify，未重写 bootloader、分区表、OTA 数据、
  资源或 `0x10000..0x1ffff` 身份区；身份区刷前/刷后 `65,536` bytes 逐字节一致，SHA-256
  `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`，旧应用回滚镜像 SHA-256
  `04b3c915663700687efc5b9cb4d4193b6ade469aed9d67ac3663b50f9d9955d8`。真实重启已验证 8 MB
  PSRAM、目标板 SKU、Wi-Fi 重连、Activation Manifest v2 验签、`starting -> activating -> idle`
  和 16 kHz 单麦 VAD；启动噪声话段在约 `930 ms` 自动结束，证明 `900 ms` 尾窗生效。串口连续观察
  至设备 uptime 约 `802 s`，无 panic/watchdog/restart，free SRAM 稳定在约 `145.6 KB`；这不是活动媒体
  会话，也不替代 100/500 轮稳定性。以上是刷写、启动与两轮功能证据，不替代 DAC 精确采样、打断、
  重连或 T1–T14 全套声学回执，也不得归属于 `2026-08-16T16:14:08Z` 的当前未刷板候选。
- 小程序已删除首页实时语音/文字会话、RecorderManager、PCM player、媒体 WSS、LiveKit
  房间和手机声纹录取，首页改为设备/摘要 Dashboard；设备页保留版本化设置、服务端声学能力、
  实际 Edge 模式和脱敏诊断。`design-preview` 与 tests 明确排除出微信包，静态门禁阻止实时媒体
  重新进入生产源码。
- 本地最新门禁：Control `581/581` 与 Agent `1493/1493` 全量测试、Session Runtime PostgreSQL `41/41`、common/
  Device Fleet/Device Gateway、strict mypy（`399` 个源码文件）、Ruff 与
  `git diff --check`；Go `test`/`race`/`vet`；H5 `372/372`（Node 24.16.0）及 production
  build；小程序 `142/142`（含真实编译 upload dry-run）；Device v2、固件源码与验收合同门禁
  均通过。T1–T14 编排结果仍为 `0 pass / 14 blocked / 0 failed`；虽有历史真实开发板证据，当前仍缺
  结构化声学/微信/生产安全回执，因此继续按外部 Gate 退出。ESP32 overlay
  已从锁定 upstream `e8d8a401...` 重新克隆并通过重放校验、ESP-IDF clean build 与
  merge-bin；当前 overlay 哈希为
  `513e14ef346d49fc39e82b9bf96e2c56df0bf25bb1d2f081ee56e5707dd6a969`。
  产品应用 `memoria.bin` 为 `2,950,832` bytes（`0x2d06b0`），`ota_0/ota_1` 各 `0x3f0000`，
  余 `1,177,936` bytes（`0x11f950`，29%）。合并镜像为 `13,577,086` bytes，SHA-256
  `7d7b921a81500817fda6a8230cd5c5b5a97814855dd0a440188f3d75e2cee188`，位于
  `firmware/esp32/artifacts/memoria-atk-dnesp32s3-v1-merged.bin`；当前候选尚未刷板、启动或激活，
  历史候选的身份保持和连续两轮功能验收不能继承。
- 外部 Gate 仍未完成：T1–T13 真实 ESP32/DAC/麦克风/Provider/AEC/停止词/双讲/100+500
  轮稳定性，T14 微信 iOS+Android 独立性，OTA/断电、证书轮换/托管签名密钥、真实 Direct
  设备话轮、多实例接管、Redis HA/故障转移、Edge supervisor 生产部署及容量/混沌。单设备 canary、
  失败自动回滚和当前 mTLS/JWKS 运行证据不替代这些 Gate。未通过这些 Gate 前不得宣传“全双工”，不得启用
  `full_duplex_verified`，不得把本地构建当作生产或真机证据。
- Direct Canary 当前已由生产单 Edge 使用 Redis 原子共享 ownership，并启用 Redis mTLS；该状态只
  证明共享权威的生产运行接线，不证明多实例接管或 Redis HA。扩容必须先完成真实双实例票据重放/
  租约接管、Redis 故障转移、Edge 重启与
  容量混沌。实板还必须验证 WSS 服务器 CA 信任锚/证书校验，不得仅因 URL 为 `wss://` 就视为
  TLS 身份已验收。Direct 默认切换必须和兼容路径共享 HS256 设备票据退役绑定为同一发布 Gate。

## 当前状态（2026-08-12）

### 小程序硬件管理与 ESP32-S3 Path 2（代码已提交并进入生产真机验收）

- 小程序保留首页与 AI 对话，主导航调整为“陪伴 / 设备 / 回顾 / 我的”；新增独立扫码启用页，
  串起二维码校验、真实 `wx` BLE 生命周期、Wi-Fi 表单、Claim、首次多主体初始化、Binding、
  Activation 查询和失败恢复。设备页成为硬件管理入口，绑定页只接受服务端确认的
  `claim_id + onboarding_session_id`，生产默认拒绝旧 `device_claim_token`。
- Wi-Fi 密码只存在页面内存和可清零缓冲区，隐藏、卸载或提交后清除，不进入 Storage、日志、
  二维码或后端。因小程序侧尚无经审计的 Protocomm Security 1/Protobuf codec，默认配网
  transport 在写凭据前 fail-closed；当前不能宣称微信真机 BLE 配网已完成。
- Device Fleet 新增独立 Bootstrap/Claim/Activation 子域和版本化合同：二维码及设备在线证明使用
  Ed25519，一次性 Claim 与 Identity 权威 Binding 通过可恢复 Saga 衔接，Activation Manifest
  按版本和设备单调计数 ACK。新增一次性 device media challenge/session，设备专用票据使用独立
  `aud/typ/secret` 并冻结 device、client、binding、cascade session 与 stream epoch；生产在
  PostgreSQL/RLS、托管签名密钥接入前显式 503，不回退 SQLite 或旧 DeviceRegistry 权威。
- `firmware/esp32` 固定 `xiaozhi-esp32 v2.4.2` commit
  `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5` 与 ESP-IDF `v6.0.2`，新增独立
  `memoria-atk-dnesp32s3-v1` 板型，保留 ES8388/ST7789/XL9555/按键并关闭 OV2640。新增独立
  `memoria_identity` NVS 分区、Ed25519 研发身份注入、Activation Manifest 验签/ACK、设备
  challenge/ticket/WSS 客户端和严格 `MemoriaAudioFrameV1`；上行 Opus 16 kHz/20 ms，下行
  24 kHz/20 ms。身份脚本只写 `0x10000..0x1ffff`，不会写 Secure Boot、Flash Encryption 或 eFuse。
- 新增独立 `services/device_media_gateway`，只负责设备票据、帧协议、Opus 与 LiveKit 桥接，复用
  现有 Mini Program LiveKit Bridge 和 Agent/ASR/LLM/TTS/Policy 控制链，不创建第二套媒体或身份
  权威。设备只有在真实播放队列排空后才发送 `playback.ended`；服务器生成/发送 TTS 不算已听到。
- `2026-08-11 18:29 CST` 已在真实正点原子板卡完成首次烧录：esptool 识别为 ESP32-S3
  rev `v0.2`、8 MB PSRAM、USB-Serial/JTAG，Bootloader、分区表、OTA 数据、资源和主应用全部
  写入并逐段通过 Hash 校验。自动复位后真实启动到 `wifi_configuring`，串口确认板型 SKU、8 MB
  PSRAM、LVGL/ST7789、ES8388、24 kHz I2S 和 SoftAP 均初始化成功；连续观察到 50 秒无 panic、
  看门狗或重启循环，也无摄像头初始化错误。串口监视已正常退出，未执行不可逆安全配置。
- 用户随后通过上游热点完成 Wi-Fi 配置，并用测试账号走通 `xiaozhi.me` 激活和真实硬件对话；
  这是 Path 1 的上游验收，不作为 Memoria 闭环证据。
- `2026-08-11 22:32 CST` 已把 Path 2 最终固件写入同一实板并逐段通过 Hash 校验。真机重启后从
  独立 NVS 读取研发身份，Activation Manifest 验签成功并进入 `idle`；短按后真实完成 Control API
  media challenge、media session 和 WSS 握手。网关随后因本机没有 LiveKit（`127.0.0.1:7880`
  refused）以服务器错误关闭，所以只验收到真实设备进入 Memoria 媒体边界，尚未完成 Memoria 对话。
- WSS 关闭回调曾暴露高/低优先级任务销毁竞态并造成 `StoreProhibited`；已按连接生命周期修复，
  最终固件连续短按两次均约 1 秒提示“设备媒体服务不可用”并回到待机，持续观察无 panic、重启或
  堆继续下降。已刷合并包 `9,873,069` bytes，SHA-256
  `29ab9cf4825099aa586a007aa03c97a786c2d16daad54911dfc96b665cca096f`。
- `2026-08-12` 已将五个 Path 2 运行时服务部署到生产。实板 Ed25519 身份、media challenge、
  media session、TURN 身份、设备 WSS `session.ready` 和 Agent `unknown_safe` 策略验签均已通过；
  未确认说话人只允许普通对话，主人称呼、私人记忆、历史、学习、工具和个性化声线保持关闭。
- 真实欢迎语暴露出内部 Agent/小程序 `generation_id=0` 与设备协议“0 表示尚无播放代次”的边界
  冲突。修复统一放在设备媒体网关：所有内部代次映射为设备代次 `N+1`，设备播放回执再映射回
  `N`；没有改 Agent 的全局 generation fence，也没有为欢迎语增加旁路特判。
- `20260812-123053` 已修复已登记但无需下发硬件的 Agent UI 遥测导致 WSS 4400；生产容器与
  readiness 已验收。后续仿真确认连接不再被 UI 事件断开，但欢迎语 PCM 因固定 `session.say`
  未先发布 generation-bound `speaking` 而被共享桥接安全丢弃。该状态合同修复随下一 superseding
  release 发布；在设备签名公网仿真和实板麦克风/扬声器闭环通过前，不把 Path 2 记为语音验收完成。
- `20260812-134635` 已上线统一 fixed-speech 状态合同，公网签名设备仿真通过 challenge、session、
  WSS、speaking、下行 Opus、listening 与播放回执；实板也收到欢迎语并把真实麦克风音频送到
  FunASR。真机“你好”终稿随后暴露出设备输入未被 LiveKit Silero VAD 建立 speech epoch，Agent
  仍按 fence 正确拒绝 orphan final。后续版本不放宽 `missing_speech_epoch`：改由固件 AFE 发布
  sample-clock-bound `vad.start/end`，设备网关校验 stream/单调时钟/状态交替后，通过可靠 LiveKit
  data 投影到 AgentSession 的统一 user-state seam；发布失败即断开会话，避免无边界音频继续运行。
- `20260812-154923` 已上线上述设备 VAD 投影，完整 provider/readiness 与连续 `8/8` 公网稳定性
  通过；签名设备仿真和真实开发板均在生产 Agent 留下 `cause=vad_start` 的实际消费证据。新固件
  五段写入及回读 Hash 通过，身份分区刷前刷后逐字节一致，原 Wi-Fi 与 Activation v2 均保留。
  实板麦克风的“你好，请简单介绍一下你自己”已得到 FunASR 终稿且不再触发
  `missing_speech_epoch`，但真实话轮提交又暴露 LiveKit interrupt 返回 Future、epoch-drain 仅接受
  coroutine 的既有合同缺口，故本版本只完成 VAD/ASR 实板验收，尚未完成回复声学闭环。
- `20260812-163054` 已上线统一 epoch-drain barrier：所有 Awaitable 均经自有 coroutine 包装后进入
  同一超时/取消/fail-closed 路径，不在 LiveKit 调用方加特判。完整 provider/readiness 与连续
  `8/8` 公网稳定性通过；同一实板再次完成 VAD、FunASR final 和话轮提交，旧 Future `TypeError`
  已消失。该真机话轮随后在生成前暴露 `unknown_safe` 公共基线音色被旧 designed-voice 合同拒绝；
  因此本版本证明 epoch-drain 修复真实生效，但仍未完成 LLM/TTS/扬声器闭环。
- `20260812-173008` 候选把生成音色、响应计划和上下文边界收敛为统一 generation output policy：
  `unknown_safe` 只有在 conversation=true 且 history/memory/persona/tools/learning/personal voice 全关闭时，
  才可匿名使用批准的公共基线音色并仅依据当前话轮回答；任何身份引用、未知 speaker hash 或敏感能力
  均 fail closed。Agent `1463` 项、定向 `182` 项、Ruff、strict mypy、模块预算和 diff 校验通过；
  待生产切流并用同一实板完成 LLM、Doubao、下行 Opus 与实际扬声器听感验收。
- 当前这块研发板的生产 authority 身份与绑定是人工受控投影；后续新设备的“小程序扫码 → Claim
  → Binding → Activation”自动 Saga 尚未完成微信真机和生产批量验收，不能据此宣称新设备已能
  零人工自动接入。
- 回滚保留在被忽略目录：Path 1 整包
  `firmware/esp32/artifacts/backups/pre-path2-upstream-working-merged.bin`，SHA-256
  `ea3b37904e42c42a8334b9808871e8bebff2f72f9ed02dbc4000ec35fdf1e250`；切换前启动/NVS/OTA 区备份
  SHA-256 `956c727accb33f1718be569a01275fc4f67627a77d8d258bfcb8816d2b13f77e`。
- 最终本地证据：相关 Python 纵切 `234` 项、小程序 `193/193`、Ruff、mypy、干净 upstream
  overlay 重放、ESP-IDF clean build/merge-bin 通过。仍缺微信 iOS/Android Protocomm、生产
  PostgreSQL/RLS/托管签名密钥、真实 LiveKit/Agent/provider 部署后的 Memoria 对话，以及完整
  LCD/麦克风/扬声器声学、AEC/全双工和量产安全验收；本轮临时 Control API/设备网关已在验收后
  正常关闭，本地 SQLite 数据不是生产发布。

### 多主体整改本地工作区（未提交、未推送、未部署）

- 《Memoria 多用户场景产品策略与架构开发调整方案》PR-01~PR-17 的主体、
  绑定、关系、Runtime Profile、Session epoch、Policy V2、Memory Scope、Tutor、
  Notification、Device Fleet 与 PostgreSQL RLS 软件主体已落入当前 dirty worktree。
- 本轮完成 Identity FORCE RLS、Guardian 核心表 actor/subject RLS、Policy
  nullable-subject receipt scope、Session action subject fence、Notification 写入 actor
  防伪与主体本人读取语义、Device Fleet actor fence，以及 Agent action-policy
  装配；Agent 策略装配、固定播报与 generation output policy 已从 `agent.py` 抽离，模块预算收紧到
  `3410`。
- PR-10/12/14 的仓库软件闭环已补齐：Agent 通过生产 HTTP
  `TransactionalToolEffectCommitPort` 提交/对账 Session Runtime 持久化 intent/outbox；
  Memory capture 由 Control 生产 authority 装配和 Archive canonical evidence projector
  驱动同事务写入；家庭共享由生产 executor 完成不同主体 propose/confirm/promotion、
  object/withdraw 与回滚。缺配置或 authority 时仍 fail-closed，不回退 legacy 写入。
- 本地证据：全部服务测试目录与所有真实 PostgreSQL 合约文件通过（pgvector 专用
  文件使用 pgvector 0.8.1/PostgreSQL 17，其余使用 PostgreSQL 16）；strict mypy
  `380` 个源码文件通过；H5 `372` 项测试和 production build、小程序 `180` 项测试和
  JS 语法检查通过；scripts 测试、canonical 合同、模块预算、Ruff、compileall 与
  `git diff --check` 通过。以上均不是生产迁移、外部副作用送达、真实通知或真机证据。
- 详细状态以 `docs/architecture/multi-subject-pr-plan.md` 的
  “2026-08-11 本地实现状态”为准；不再新建平行会话文档。

### 学生线本地工作区（未提交、未部署、未发布）

- 学生线 P0、P1 仓库能力、P2 导师域和 P3 本地软件链路已实现；持续状态收敛在本节与
  `docs/architecture/multi-subject-pr-plan.md`，不再维护一次性实施计划或复核文档。外部/生产
  门禁仍保持未完成，不能据此创建对外学生体验版。
- 未成年账号能力由 `account_gate.py` 单点 fail-closed；guardian 绑定/孩子确认/分项同意/撤销、
  tutor focus/Router/进度投影、周报、危机固定回复 + Evidence/outbox、授权儿童语料限额/到期删除均已接线。
- guardian/tutor PostgreSQL FORCE RLS schema 与 forward-only 升级脚本、WAL/base backup、异地对象镜像
  和独立恢复演练脚本已进入仓库；这些只证明可执行能力，不证明生产已安装、远端持续上传或完成恢复。
- CI 等价本地 PostgreSQL 环境为 `2108 passed, 2 skipped`（两项为需显式 CAM++ 模型/官方 WAV 的
  真实 ONNX 测试），总覆盖率 `87.0985%`，编排层 `90%`、
  provider 协议层 `97%`；ruff、strict mypy、module budget、tutor E2E、H5 304 项、小程序 90 项与
  Node 24 的 87 文件 upload dry-run 均通过。
- 仍阻塞发布：PIA/法务/算法备案确认、危机话术专业评审、真实微信订阅消息、生产 guardian 升级、
  真实异地备份与独立恢复报告、iOS/Android 完整语音链、ESP32/AEC、200 条真实授权儿童语料。

### 历史生产基线（已被 2026-08-18 Runtime Profile release supersede）

- 历史生产 runtime 为 `20260812-163054`，源码 commit
  `fef36f30556970e9244103f901a1c2ac03fb954a`；H5 有意保持 `20260808-171749`，本轮硬件修复不
  切 H5。直接 runtime 回滚目标为 `20260812-154923`。完整证据见
  `docs/releases/20260812-163054.md`；`20260812-173008` 为待切流候选。
- Agent、Control API、Speaker Model、小程序 Gateway、Device Media Gateway 五个应用容器以及
  PostgreSQL/Redis/MinIO 均 healthy；readiness 已绑定 runtime tag，LiveKit/Agent 权威语音链保持
  复用。Runtime Profile 验签键已在生产 Agent 以 root-only 最小权限临时接通，正式生成器修复随
  `20260812-114447` 已由正式环境生成器逻辑和最小权限拆分测试固化。
- PostgreSQL 已 forward-only 安装独立 `memoria_evolution` 角色、8 张表、8/8 FORCE RLS 与 8/8
  controller policy。不要为代码回滚删除这些对象；旧 runtime 可与 additive schema 共存。
- H5 已最后切流；240 个 immutable URL 全部 HTTPS 200，历史 4776 条资源引用均可用。公网正向路由
  为 200，internal/PocketSparks/Goods Invoice 为 404；390x844 与 667x375 浏览器无横向溢出、
  console warning/error 为 0。
- 小程序 `0.8.66` 已于 `2026-08-08 19:21 CST` 经产品负责人授权使用微信开发者工具官方 CLI
  上传成功；AppID `wx20a3a044b52fcbb7`，包体 `656904 bytes（641.5 KB）`，说明为
  “绑定助手表情完整话轮 fence”。只创建开发/体验版，未提审、未正式发布。
- 项目内 `upload:test` 仍是长期可复现链路：Node 25/Web Storage 问题已 fail-fast，Node 24.16
  compile-only 通过；真实 CI 上传仍需把 `112.20.18.77` 加入微信代码上传 IP 白名单。本次 DevTools
  上传来自干净远端一致 HEAD `05a933f`，小程序业务源码相对 runtime tag `9812fac` 无变化。

## 回滚与备份

- 可执行回滚 receipt：`/var/backups/memoria/rollback-20260808-171749.env`，`root:root 0600`；它由
  上线前冻结的 legacy receipt 安全转换，未从切流后的 `current` 反推旧版本。原
  `release-state-pre-20260808-171749` 继续保留。
- H5 回滚 snapshot：`/var/www/memoria-releases/rollback-20260808-171749`，使用旧入口/provenance
  与当前 240 个 immutable assets；manifest SHA-256：
  `4f37ea9952463ef556aaf28e241c9d5244acf6a1236a46f49a5b968702890169`，逐文件验签与
  `www-data` 可读门禁通过。
- SQLite 双副本 SHA-256：`6443bde9695697296413ed9d7486b744bc52bc52162670d158529bbf4d1577af`。
- PostgreSQL dump SHA-256：`209b09028fdc31be2f2d5d8763b3fe456d34a625c8c43777827c815875cb3fdf`；
  MinIO 对象清单 SHA-256：`c7e273671460428a5fa9183841134a2eae6ded9814c0aeffab3fb6d5ee6155ff`。
- 旧 PostgreSQL/Control/Agent/Speaker/Gateway env 均已 root-only 备份。Runtime 故障先恢复旧 data
  Compose 与全部 env，再切旧软链和四应用；H5-only 故障只切 H5。

## 仍需完成

- Edge Bridge supervisor 已在生产启用，并完成应用层 Health fail-closed、Bridge 停止/恢复后的
  自动 redial 与零会话双向回滚演练。仍需补 Bridge IP 变化、Edge 重启、Redis/网络故障、告警送达
  和带活跃会话恢复的完整混沌矩阵；在这些证据形成前，不得把该项外推为 Direct 真机或全双工通过。
- 重新接入真实 ESP32 后，用同一 generation 的串口、Edge/Core/Provider 日志和用户 Actual Heard
  回执完成 Direct T4–T7；没有活跃设备指标和 Exact DAC 水位时继续保持
  `direct_real_device_verified=false`、`full_duplex_verified=false`。
- 多主体能力上线前必须先做生产备份和 forward-only schema dry-run，配置并验证
  Policy/Session/Memory 内部 token、Redis/outbox、角色权限与 readiness，再执行回滚演练；
  本次仅部署 Voice Core Bridge 与 Go Media Edge，不改变该独立上线门禁。
- `TransactionalToolEffectCommitPort` 已保证本地持久化、幂等和 worker 领取合同，但仓库
  当前没有具体第三方业务工具/投递 worker；只有在明确业务动作和供应商后才能实现并验收
  外部 delivery，不能把 outbox completed 或单测通过表述为第三方副作用已发生。
- 先完成学生线全部外部门禁并把证据写入 `docs/acceptance/` / `docs/releases/`；当前危机通知只有本地
  outbox 与家长页提醒，不能宣称微信订阅消息已送达，也不能用自动化代替真实 iOS/Android 声学验收。
- 在任何学生数据进入生产前，先执行 guardian forward-only 升级，配置真实远端备份 endpoint，验证
  WAL/base backup 与关键 bucket 持续同步，并在独立主机完成恢复演练；仓库脚本通过不是生产证据。
- 后续为恢复项目脚本真实上传，可把 `112.20.18.77` 加入微信代码上传 IP 白名单；这不再阻塞
  `0.8.66` 体验版交付。
- `0.8.66` 仍需在真实 iOS/Android 上覆盖麦克风、扬声器/AEC、弱网、前后台与蓝牙；H5 仍需真实
  登录后语音验收。现有单测、浏览器、WSS、Provider smoke 或上传成功都不能替代。
- 成功上传并确认回滚工件后，精确清理旧 incoming 大归档；保留当前 `20260808-171749` basis、
  `20260807-163916` runtime/H5/镜像以及全部备份。
