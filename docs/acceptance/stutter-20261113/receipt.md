# 2026-11-13 播放卡顿候选 build34

状态：源码及 Host 回归已验证；完整 ESP-IDF 构建及构建后 overlay 检查通过；独立 Gemini 评审运行时无阻塞，门禁依赖问题已修复。已本地签名，未上传、未刷写，真机改善未知。

## 已有设备证据

已部署 build33 的 session `154ab015-e824-43f0-9a01-79aedec0cb9a`，generation 3：服务端 1009 帧、max_gap 26 ms，设备播放 969 帧、解码队列满拒绝 40 帧；供音等待 7 次，最长 1944 ms、累计 3150 ms。969 + 40 = 1009；播放结束回执不能证明连续播放。

新增 build33 基线 session `d3a00fdc-f7d1-49df-a3a9-b6765c4eb590`、stream 3193，generation 2：563 帧完整播放，无队列满日志；供音等待 7 次，最长 304 ms、累计 788 ms。服务端 max_gap 89 ms。串口先确认麦克风及 listening，随后才播放故事查询。两轮动态回答不同，不能据此计算改善比例。

## 候选边界

`0047` overlay 将队列满分支的串口日志替换为原子累计，现有主任务每 10 秒在锁外报告 `media decode queue stats dropped_total=...`。队列仍为 120，原拒绝和停止语义保留。真实函数 Host 回归中，旧实现调用阻塞串口时消费者取不到锁；修复后串口不被调用、消费者可以取锁，拒绝数守恒。

仅目标板、WiFi、WSS 的专属接收任务，每连接 attempt 将基础优先级至少设为 4，保留原来更高的优先级。attempt 检查及设置与 retirement 同锁串行化；先于状态锁采样优先级，避免将临时继承的高优先级误判为基础优先级。Host 继承回归在修前实际得到锁释放后 priority=1，修后为 4。未调整窗口、WiFi RX buffer、起播水位或超时。

队列内持锁日志是已复现缺陷；接收调度是否解释全部供音等待，仍需同一候选真机观察，不能从串口停顿直接断言 TCP 重传。

## 验证

- 相关回归 134 项通过，Ruff、diff 检查通过。
- 完整 `firmware/esp32/tests`：852 passed in 106.74s。
- 完整 ESP-IDF 6.0.2 构建、构建后 overlay 检查通过。首次 overlay 检查因 IDF Python 无 pytest 失败；指定现有项目虚拟环境后完整重跑通过，无跳过构建后检查。
- 独立 Gemini 评审：运行时无阻塞；门禁原先依赖 pytest 的问题已改为标准库独立入口。5 项相关回归及默认 IDF Python 下完整 overlay 检查通过，保留相同的并发、继承和两个板型验证。该修改不影响固件镜像，无需重编。
- 尚未验证设备实际接收优先级、播放连续性及新候选的供音等待/丢包。

原始日志仅留本地，未纳入版本控制：

- `baseline-serial.log`: SHA-256 `0b3f39340a540dba56d68def9de742d1c31a1b74498d85323361c001f0469c71`
- `baseline-bridge.log`: SHA-256 `92e686212a4689ec14c1ece7976908d60abf9f4c66b15cd171112030150cb9a8`
- `firmware-tests.log`: SHA-256 `792c0bee03e05cd3078e6a512642200c8df196a2b0169e60fbd91b5a3db2af11`

## 构建产物

`firmware/esp32/artifacts/stutter-build34/memoria-esp-vocat-app.bin`，3,390,928 bytes，版本 `2.4.2`、build34，SHA-256 `7c73d4738a8a8d8eda8c63ffe6596735d2b9973c8c1203a8846fd0208c65e48e`。发布脚本的真实镜像检查确认 build 标记、产品镜像限制和 OTA 容量通过；已本地签名且通过内置公钥验签；签名产物在 `outputs/firmware-releases/memoria-esp-vocat/34/`，尚未上传。
