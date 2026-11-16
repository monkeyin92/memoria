# AGENTS.md — boards/memoria/esp-vocat

## OVERVIEW
memoria 唯一面板（ESP32-S3, 360x360 圆屏 ST77916）：开机入口、mascot 合成/动画、表情（dot/pill）、拍摸、IMU、离线资源。得分 13，独立知识域。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 面板装配 / BLE 配网 / 任务与钩子 | `memoria_esp_vocat.cc` (1735 L) |
| 动画循环 / 脏矩形 chrome / QR | `memoria_mascot_display.cc` |
| 合成器 + actor + 覆盖层 | `memoria_mascot_scene.cc` |
| 表情判定与绘制 | `memoria_dot_face.{h,cc}` / `memoria_pill_face.{h,cc}` |
| 拍摸状态机 / IMU 轮询 | `memoria_pat.h` / `memoria_imu_poll.h` |
| 产品/bench/keep-awake 三份 manifest | `config.json` / `config.bench.json` / `config.keep-awake.json` |

## CONVENTIONS
- 多数 header（pat/imu/mascot_pack/raster/scene/dot/pill）故意无 ESP-IDF/LVGL 依赖，可被 `firmware/esp32/tests/` 在 host 上编译（假时钟/注入分配器）；只有 display/board/bench/diag 引用 IDF。
- 枚举漂移编译期抓：`kPatState*`/`kFaceState*` 与 upstream `DeviceState` 用 `static_assert` 镜像；name 表长度也 `static_assert`。
- 所有 UI 归一个 FreeRTOS task `mascot_anim`（core 1, 6KB 栈），外部通过 mutex/atomic 递输入；帧率 25 fps（`kFrameMs=40`，`memoria::FramePacer` 自适应）。
- 渲染直接画 RGB565 framebuffer + 上报脏矩形（max 6）；LVGL 只画文字/QR。
- 资源全走 `assets/` manifest：`offline_lines/index.json` 与 `stories/index.json` 由 `scripts/build_offline_stories.py` 从脚本内表 + OpusTags TITLE 派生，禁止手改；AIGC 标签（CONTENT_ID/SERVICE_PROVIDER/AI_GENERATED/ARTIST==VOICE_ID/DATE/CONTENT_PRODUCER）每段 ogg 必备。
- companion id 固定表镜像 `services/common/companions.py`，持久化在 NVS key `companion`。

## ANTI-PATTERNS
- `config.h` 编译期 `#error`：设备侧 AEC 不可去掉（`CONFIG_USE_AUDIO_PROCESSOR` + `CONFIG_USE_DEVICE_AEC`，禁 `CONFIG_USE_SERVER_AEC`）；永不退回 AutoStop。
- 产品镜像不得含 bench/keep-awake marker（`memoria_bench_build_marker` / `memoria_keep_awake_build_marker`），发布器据 marker 拒绝；bench 镜像绝不能把 QR 绑定载荷写到 USB 口（`memoria_mascot_bench.cc`）。
- `mascot_*.mmp` 只在 tests fixtures，不进镜像；白色线脸 `memoria_face.cc`、`offline-menu-intro-v2`、根级 `night_soothe.ogg` 不得存在；哄睡分支只进 `EnterTapDeepSleep`。
- 表情规则：危机态永远用 calm 脸；回复中提到 thinking/listening 时绝不能说话中途冻结；眨眼间隔 >= 2s 不双发；低于 y=48（AI badge）不得画。
- 面板必须从设备状态绘制（`FaceForFacts` 不接 display）；屏触摸绝不能停播放；`/dev/secondary` 只在面板文件里读；无 SSID 的 wifi 行名。
- 拍摸 boot-quiet 样本不得武装确认链；crisis 剧本永不打断播放。

## NOTES
- `memoria_dot_face_data.inc`（25K 行）是烘焙产物，生成器不在本仓库，改动需重烤。
- `stories/*.ogg` 占镜像大头；每重录一条要同步更新 `network_prompt_provenance.json` 的 sha256 与前值链。
