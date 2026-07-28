# 小程序半双工真机验收矩阵

> 本文件只记录真实微信小程序设备结果。模拟器、单元测试、WSS smoke、体验版上传成功均不能标记为通过。

## 已确认的连通性边界

- `0.8.61` 在生产回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 后，用户确认页面与语音
  连接恢复可用。
- 该结果只关闭 8443 主链连通性故障；设备型号、系统版本、session/timestamp、音频路由、
  10 轮稳定性、弱网与 AEC A/B 数据未齐，因此下方声学矩阵仍保持“待验收”。
- 标准 443 候选路由已完成服务端 Upgrade 验证，但同一 iPhone 和 Safari 在当前无 VPN 网络
  发送 TLS ClientHello 后 RST，未进入 Nginx HTTP/Gateway，当前不作为生产下发地址。

## 构建与证据

- 小程序版本：
- Git commit：
- runtime：
- Gateway AEC 模式：`off / on / alternating`
- 测试人：
- 测试日期：
- 服务器日志时间窗口：

每条记录保留：

- 设备型号与系统版本；
- `session_id`；
- `ready.aec.variant` 与 `ready.aec.active`；
- `handshake_ack → ready → listening` 时间线；
- `underflow / hard_reset / lead_adjusted` 计数；
- 首字、尾音、停止按钮、前后台与系统录音中断的实际结果。

## 必测用例

统一测试话术：

1. AI 回答结束后立即说“我想继续问一个问题”，检查首字“我”不被吞。
2. AI 回答播放时点击“停止播放”，检查本地立即淡出，400 ms 内停止且不会开启语音打断。
3. 停止后说“等一下我想问个事”，必须作为下一轮普通内容进入，不得在播放期偷录。
4. 连续完成 10 轮短对话，记录卡顿、滋滋声、underflow 与 hard reset。
5. 切后台再回来、触发一次系统录音中断，确认 `uplink_discontinuity` 后恢复。

| 编号 | 平台 | 音频路由 | 网络 | AEC 组 | 结果 | session_id | 备注 |
|---|---|---|---|---|---|---|---|
| IOS-SPK-WIFI | iPhone | 外放 | Wi-Fi |  | 待验收 |  |  |
| IOS-RCV-WIFI | iPhone | 听筒 | Wi-Fi |  | 待验收 |  |  |
| IOS-BT-WIFI | iPhone | 蓝牙 | Wi-Fi |  | 待验收 |  |  |
| IOS-SPK-CELL | iPhone | 外放 | 4G/5G |  | 待验收 |  |  |
| IOS-SPK-WEAK | iPhone | 外放 | 弱网 |  | 待验收 |  |  |
| AND-SPK-WIFI | Android | 外放 | Wi-Fi |  | 待验收 |  |  |
| AND-RCV-WIFI | Android | 听筒 | Wi-Fi |  | 待验收 |  |  |
| AND-BT-WIFI | Android | 蓝牙 | Wi-Fi |  | 待验收 |  |  |
| AND-SPK-CELL | Android | 外放 | 4G/5G |  | 待验收 |  |  |
| AND-SPK-WEAK | Android | 外放 | 弱网 |  | 待验收 |  |  |

## 单条判定

全部满足才可写“通过”：

- AI 播放期间上行 PCM 为 0；
- 最后一个 WebAudio source 结束后仍等待配置的尾音保护时间；
- 下一轮首字无可感知吞音；
- “停止播放”本地立即淡出，旧 generation 音频不会恢复；
- 用户静音优先，回答结束不会自动开麦；
- 10 轮中无可听 click/pop/持续滋滋声；
- `hard_reset = 0`；underflow 若发生，自适应 lead 上升后不连续复发；
- 前后台、系统录音中断和网络恢复后会话可继续；
- 不出现播放期口头打断或播放期转写进入下一轮。

## AEC A/B 判定

只在 `MINIPROGRAM_GATEWAY_AEC_MODE=alternating` 的明确测试窗口比较，同一设备、路由、
音量、网络和话术至少各完成 10 轮：

| 指标 | control | aec |
|---|---:|---:|
| 首字丢失轮数 |  |  |
| AI 尾音误转写轮数 |  |  |
| 空场误转写轮数 |  |  |
| 可听失真/滋滋声轮数 |  |  |
| underflow 总数 |  |  |
| hard reset 总数 |  |  |

若 AEC 组没有明显降低尾音误转写，或增加首字丢失/失真，则生产保持
`MINIPROGRAM_GATEWAY_AEC_MODE=off`。测试结束后清空单 session WAV 采样配置并删除设备上的
临时录音。
