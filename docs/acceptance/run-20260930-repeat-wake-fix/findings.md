# 重复自动唤醒修复验收

## 结论

已将连接设备刷入 MEMORIA_FIRMWARE_BUILD=17。针对低分回声 mo mo li 的唤醒误判已修复：保留真实“茉莉”召回所需的最低分 0.12，但低于 0.20 时必须同时匹配配置唤醒词 mo li。

## 证据

- 修复前现场日志 outputs/acceptance/run-20261001-repeat-wake/serial-full.log 中两次误唤醒均为 prob=0.138606/0.138899、string=mo mo li。
- 固件构建成功，overlay 检查通过；构建产物 memoria-esp-vocat-merged.bin SHA-256 为 cfd35a7d944df4352c25e2c3a98cbaa514832370b6dd033f78ae91deec4f86e5。
- 使用 firmware/esp32/scripts/flash.sh --port /dev/cu.usbmodem2101 刷写；bootloader、partition table、assets、application 均通过 esptool hash verification，未执行 NVS/身份擦除。
- 启动串口确认 MEMORIA_FIRMWARE_BUILD=17、唤醒词为 mo li/“茉莉”，设备进入 idle。
- 设备静默监听 90 秒：未出现 wake、accepted、Action 或唤醒相关日志。

## 未覆盖边界

- 本次未完成刷写前完整 flash 读回：USB-Serial/JTAG 在只读约 4.7% 时发生数据流中断；刷写本身及每个镜像段校验成功。
- 未注入一次人工“茉莉”语音，因此真实语音召回率仍需下一次有人在设备前说词时确认。
- 启动日志仍有 BMI270 I²C timeout；它不属于本次低分唤醒误判链路，留作独立硬件/总线问题处理。
