# 药丸脸动画实验

日期：2026-11-09

这次实验把 360×360 圆屏的主脸从预栅格化点阵帧切换为实时 RGB565 绘制。当前版本使用固定粉色背景和两只黑色药丸眼。眼睛支持平移、呼吸缩放、上移和眨眼，点阵脸仍保留为回退路径。

## 产物

- C++ 渲染器：`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_pill_face.cc`。
- 渲染数据结构：`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_pill_face.h`。
- 显示开关：`memoria_mascot_display.cc` 中的 `kPillAnimationExperiment`，当前为 `true`。
- 同一份 C++ 渲染器生成的预览：[memoria-pill-face-preview-20261109.mp4](assets/memoria-pill-face-preview-20261109.mp4)。

## 固件与备份

上一版实验镜像曾完成构建和写入校验，但当前设备画面仍是原来的蓝色，不能把那次写入记录当成粉色版本的真机验收。

本次粉色固定背景版本已重新构建并刷入设备。实际写入的产品 app 镜像为 `firmware/esp32/artifacts/memoria-esp-vocat-app.bin`，SHA-256 为 `1c6d453d8384b56b257fae058a5e40ca1232d20844da0d512b0ec509fea0c738`，大小为 `3381200 bytes`。干净提交构建的 app SHA-256 为 `644e177dd2f5b078b01354cce47ae6a2c4b0bae463112077cf97c397eded8477`；本次重试沿用了当前产品构建目录中的镜像。

刷写前的备份目录是 `devicetest/flash-backups/20261109-display-animation-baseline/`。其中保存了刷前 app、bootloader/分区表、NVS/OTA/PHY、identity 二进制和各分区的芯片 MD5。刷前关键 MD5 为：app `c46cdf55af5c686f70c242f772f77ebd`，identity `db3c2ccded4c3c957c3563bdb6812b64`。

## 验证边界

主机侧 `uv run --no-project pytest -q firmware/esp32/tests` 通过，进程退出码为 0。渲染器单独用 clang++ 编译并生成了预览。产品构建通过，app 分区仍有约 18% 空间。

设备侧写入已确认：bootloader、分区表、OTA 数据、assets 和 app 均报告 `Hash of data verified`，刷后 `device namespace: present`。刷后串口已读到 HTTP 连接、静音时段和 `MemoriaPower` 运行日志。当前还没有真机照片，因此粉色背景与动画的视觉验收仍待现场观察。

本次刷写日志保存在 `devicetest/flash-backups/20261109-display-animation-baseline/flash-pink-retry-2.log`，串口日志保存在 `devicetest/flash-backups/20261109-display-animation-baseline/serial-pink-retry.log`。

## 声源定位现状

声源定位已暂缓。本轮不改采集链、不切换到双麦加回采的 `MMR`，也不加入能量差或 GCC-PHAT 方向计算。当前 `BoxAudioCodec` 的 `M + R` 逻辑仍按原有 AFE 回采链工作，显示层继续只消费 AFE 后的单路 input level。

所以当前固件不能可靠判断「声音在左边还是右边」。要实现眼睛随声源左右看，需要先确认 ES7210 的实际物理通道映射，把采集链改成两路麦克风加一路回采参考，形成 `MMR`，再在 AFE 前计算双麦能量差或 GCC-PHAT 方向，加入置信度和时间平滑，最后把方向映射到眼睛的水平偏移。这个改动需要单独做通道采集和真机声源测试，不能只在现有单路 RMS 上加一个左右分支。

## 回滚

当前没有自动回滚。若效果不合格，设备端口恢复后，先停止所有串口持有进程，再使用备份目录中的 app 和保护区文件恢复，并重新执行芯片 MD5 核对。当前备份没有保存刷写前 assets 的完整二进制，只保存了刷前 assets 的芯片 MD5，因此完整逐字节回滚仍需要找到对应的旧 assets 镜像；app、bootloader/分区表、NVS/OTA/PHY 和 identity 已有可写回文件。
