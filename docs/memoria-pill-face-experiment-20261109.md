# 药丸脸动画实验

日期：2026-11-09

这次实验把 360×360 圆屏的主脸从预栅格化点阵帧切换为实时 RGB565 绘制。当前版本使用固定粉色背景和两只黑色药丸眼；眼睛中心位置和间距固定，删除了连续平移、呼吸缩放和漂移，避免画面卡顿与眼睛持续晃动。说话时只在固定的中性、瞪眼、窄眼三种表情之间每 700 ms 离散切换，其他状态使用固定表情，点阵脸仍保留为回退路径。

## 产物

- C++ 渲染器：`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_pill_face.cc`。
- 渲染数据结构：`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_pill_face.h`。
- 显示开关：`memoria_mascot_display.cc` 中的 `kPillAnimationExperiment`，当前为 `true`。
- 表情参考板：[memoria-pill-face-expression-reference-20261109.png](assets/memoria-pill-face-expression-reference-20261109.png)。
- 连续动画版本的历史预览：[memoria-pill-face-preview-20261109.mp4](assets/memoria-pill-face-preview-20261109.mp4)。当前实现以表情状态和测试为准。

## 固件与备份

上一版实验镜像曾完成构建和写入校验，但当前设备画面仍是原来的蓝色，不能把那次写入记录当成粉色版本的真机验收。

本次固定表情版本已重新构建，并把同一份 app 写入 `ota_0` 和 `ota_1`。实际写入的产品 app 镜像为 `firmware/esp32/artifacts/memoria-esp-vocat-app.bin`，SHA-256 为 `a91c653a881dee80f15b79af3e2c5daf556ea3b82323b6e42ae44b4357c21caf`，大小为 `3381136 bytes`。

刷写前的备份目录是 `devicetest/flash-backups/20261109-display-animation-states/pre-reflash-173629/`。其中保存了两个 app 槽、bootloader/分区表、NVS/OTA/PHY 和 identity 的二进制备份。刷前 `ota_0` 的 app 前缀 SHA-256 为 `be6072dd33dca2462daa61c95e9a6fd26f5b94ea28a4bab4340de2d2ca823c5c`，`ota_1` 仍是旧点阵固件，完整槽 MD5 为 `c46cdf55af5c686f70c242f772f77ebd`。

## 验证边界

主机侧 `uv run --no-project pytest -q firmware/esp32/tests` 通过，进程退出码为 0。渲染器单独用 clang++ 编译并生成了预览。产品构建通过，app 分区仍有约 18% 空间。

本次先把 8192 字节空白 `otadata` 写入 `0xd000`，再把同一 app 写入 `0x20000` 和 `0x410000`。三个写入阶段都报告 `Hash of data verified`，写入退出码为 0。

刷后强制复位的串口日志确认 `MEMORIA_FIRMWARE_BUILD=28; slot=ota_0`，并读到 `MemoriaMascot: face=wake`、`expression=neutral` 和 `face=calm`。日志没有出现回滚或药丸脸初始化失败。设备仍能连接 Wi-Fi 和 HTTPS。

芯片端 MD5 复核结果如下。保护区 `boot_and_ptable`、`nvs`、`phy_init`、`identity` 和 `assets` 与刷前二进制或刷前设备端 MD5 相同。`otadata` 在空白写入后由引导程序恢复为运行记录，这是预期变化。

- `boot_and_ptable`: `9604c30e3bec72b32bff6a8fb8b98554`
- `nvs`: `9d08c986e3aa8c60714a2cb21a9a23b1`
- `otadata`: `11c30a9ae56317cafe1c25ac18da9f41`
- `phy_init`: `6ae59e64850377ee5470c854761551ea`
- `identity`: `db3c2ccded4c3c957c3563bdb6812b64`
- `assets`: `7840e4bce899b8eaacc10f9bc9a29008`

两个 app 槽的前 `3381136` 字节芯片端 MD5 都为 `a76a3e8b5b17afc5e3a73f3772089726`，与当前 app 文件一致。两个完整槽的 MD5 都为 `931ecdeb81b79c6608ac345985e3e22d`。完整槽包含 app 文件之外的槽尾字节，因此以 app 边界 MD5 作为镜像一致性判据。

本次刷写日志保存在 `devicetest/flash-backups/20261109-display-animation-states/pre-reflash-173629/write-both-ota-slots.log`。刷后启动日志保存在 `devicetest/flash-backups/20261109-display-animation-states/pre-reflash-173629/serial-after-forced-reset.log`。MD5 与槽边界核对日志也保存在同一目录。当前仍没有屏幕照片或现场操作录像，因此粉色背景、眼睛静止效果和说话时离散表情的视觉验收待现场观察。

## 声源定位现状

声源定位已暂缓。本轮不改采集链、不切换到双麦加回采的 `MMR`，也不加入能量差或 GCC-PHAT 方向计算。当前 `BoxAudioCodec` 的 `M + R` 逻辑仍按原有 AFE 回采链工作，显示层继续只消费 AFE 后的单路 input level。

所以当前固件不能可靠判断「声音在左边还是右边」。要实现眼睛随声源左右看，需要先确认 ES7210 的实际物理通道映射，把采集链改成两路麦克风加一路回采参考，形成 `MMR`，再在 AFE 前计算双麦能量差或 GCC-PHAT 方向，加入置信度和时间平滑，最后把方向映射到眼睛的水平偏移。这个改动需要单独做通道采集和真机声源测试，不能只在现有单路 RMS 上加一个左右分支。

## 回滚

当前没有自动回滚。若效果不合格，设备端口恢复后，先停止所有串口持有进程，再使用备份目录中的 app 和保护区文件恢复，并重新执行芯片 MD5 核对。当前备份没有保存刷写前 assets 的完整二进制，只保存了刷前 assets 的芯片 MD5，因此完整逐字节回滚仍需要找到对应的旧 assets 镜像；app、bootloader/分区表、NVS/OTA/PHY 和 identity 已有可写回文件。
