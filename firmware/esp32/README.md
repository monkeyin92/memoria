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

锁定值以 [`upstream.lock`](upstream.lock) 为准。`scripts/bootstrap.sh` 会校验远端、commit 和 ESP-IDF 版本，然后把 `overlay/` 应用到本地缓存的 upstream 工作树。

## 目录与工作流

```text
firmware/esp32/
├── overlay/
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

构建中文、关闭唤醒词的首个硬件 bring-up 固件：

```bash
./scripts/build.sh
```

构建产物写入被忽略的 `artifacts/`，其中包含 `merged-binary.bin` 的板型副本；真实 upstream 构建目录位于被忽略的 `.cache/upstream/`。需要从头清理构建中间物时使用：

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
- Wi-Fi 仍使用 upstream Hotspot 配网；长按 5 秒只重置网络配置，不清除 `memoria_identity`，也不解除云端绑定。小程序在缺少经审计的 Protocomm Security 1/Protobuf codec 时继续 fail closed，当前不宣称微信 BLE 配网已完成。
- Flash `0x10000..0x1ffff` 是独立 `memoria_identity` NVS 分区。研发脚本生成/校验 Ed25519 身份并只烧录该分区，私钥产物权限为 `0600`；没有写 Secure Boot、Flash Encryption 或 eFuse。
- 固件启动后直接调用 Memoria Activation，验证 Ed25519 Manifest、配置哈希和单调 ACK；不会把 upstream `xiaozhi.me` 账号或旧 `DeviceRegistry` 当作 Memoria 身份权威。
- 激活后，固件使用一次性 media challenge 和设备签名换取独立 audience/type 的设备票据，再连接 `GET /v1/device/media`。媒体帧采用严格 `MemoriaAudioFrameV1`，上行 Opus mono 16 kHz/20 ms、下行 Opus mono 24 kHz/20 ms。
- 独立 `services/device_media_gateway` 只做设备协议、Opus 转换和 LiveKit 桥接，复用现有 Agent/ASR/LLM/TTS/Policy 控制链，不创建第二套对话权威。`playback.ended` 只在设备真实播放队列排空后上报。
- 小程序已有扫码启用、Claim、Binding、Activation 查询和设备管理入口；首页与 AI 对话暂时保留，长期定位仍是硬件管理和权威对话信息查看端。
- 生产 Device Fleet 在 PostgreSQL/RLS 和托管签名密钥接入前明确返回 503，不回退本地 SQLite 权威。

## 2026-08-11 实板证据与边界

- 用户已用测试账号走通 upstream `xiaozhi.me` 激活并完成真实对话；这只验收上游路径。
- 同一块真实 ESP32-S3 rev 0.2 板卡随后烧入 Memoria 研发身份和 Path 2 固件。串口确认 Wi-Fi 重连、Activation Manifest 验签、`activating -> idle`，并从真机完成 media challenge、media session 和 WSS 握手。
- 本地没有运行 LiveKit，网关在尝试连接 `127.0.0.1:7880` 时按服务端错误关闭。因此当前只验收到“真实设备已进入 Memoria 媒体边界”，不能宣称已经完成 Memoria 真实语音对话。
- 服务不可用路径已连续按键复测两次：均约 1 秒提示并回到待机，持续观察无 panic、看门狗或自动重启，堆余量稳定。
- 已刷真机合并包为 `artifacts/memoria-atk-dnesp32s3-v1-merged.bin`，SHA-256 `29ab9cf4825099aa586a007aa03c97a786c2d16daad54911dfc96b665cca096f`。Path 1 回滚整包为 `artifacts/backups/pre-path2-upstream-working-merged.bin`，SHA-256 `ea3b37904e42c42a8334b9808871e8bebff2f72f9ed02dbc4000ec35fdf1e250`。
- 干净上游重新套用 overlay、clean build 和 merge-bin 已通过；相关 Python 测试 234 项、小程序 193 项、Ruff 和 mypy 均通过。

仍未完成的验收包括：微信 iOS/Android Protocomm 配网；生产 PostgreSQL/RLS、托管制造签发和量产安全；真实 LiveKit/Agent/provider 部署后的 Memoria 对话；LCD 肉眼细节、完整声学半双工/AEC、稳定全双工、唤醒词产品化与声音复刻。以上不能由本地测试或 WSS 握手替代。

## Overlay 约束

`apply-overlay.sh` 只会把 `overlay/files/` 中列出的板级文件复制到 upstream，并按字典序应用 `overlay/patches/`。overlay 变更后应重新执行 `bootstrap.sh --refresh`，让缓存回到锁定 commit 后再应用新 overlay。生成的 `.cache/upstream/` 可以删除并重建；它不是交付内容。

静态校验：

```bash
./scripts/check-overlay.sh
```

该校验会检查 lock、JSON、shell 语法、patch 可应用性、板型身份、摄像头残留和 `git diff --check`。有 ESP-IDF 时还会检查 upstream 的 board variant 列表；没有 ESP-IDF 时仍可完成不依赖工具链的 overlay 校验。
