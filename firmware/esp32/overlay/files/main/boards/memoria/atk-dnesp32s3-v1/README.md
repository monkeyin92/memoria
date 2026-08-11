# Memoria ATK-DNESP32S3 V1

板型身份：`memoria-atk-dnesp32s3-v1`

硬件基线：ESP32-S3、16 MB Flash、8 MB PSRAM、ES8388、320×240 ST7789、XL9555、BOOT(GPIO0)、LED(GPIO1)，当前组合无摄像头。

本板型从 upstream 的 ATK-DNESP32S3 引脚和外设初始化派生，但使用独立的 Memoria board identity。摄像头相关 include、成员、初始化、getter 和 sdkconfig 配置全部不进入本目录。网络、协议和显示状态沿用 upstream 的真实接口；Memoria BLE/Claim/云端媒体功能尚未由本 overlay 声称实现。
