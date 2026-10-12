# 2026-10-12 电脑多轮对话（一圈 child.json）

用户授权「分支清理后用电脑模拟多轮对话」。本轮在刷回产品 build 24 后，以 Mac 扬声器 + USB `wake` 跑一圈 [`child.json`](../../../scripts/voice_soak_scenarios/child.json)。音量 30。不宣称 N-5 / N-8 / N-10 关闭，也不改三个全局 verified 标志。

## 结果摘要

| 项 | 值 |
|---|---|
| 场景步数 | 44（`deadline_reached=false`） |
| 有回复 | 41 |
| 无回复 | 2（`t036-game-2` 成语接龙；`t044-bye` 告别进 idle，驱动记 no_reply） |
| 说完到开口 p50 | 2.57 s（min −0.08，max 3.46） |
| 场景时长 | 约 12 分 56 秒（23:58:32–00:11:28 CST） |
| 尾窗 | 90.01 s；`final_state=idle`（`serial_transition`，自 00:10:59），但判据为 `evidence_insufficient`（idle 早于观察窗起点），驱动退出码 **4** |
| 停止观察（音量 30，非验收） | 「别说了」v2 `stopped=true`（2.402 s）；「停」`playback_completed`；另有 mid-question / backchannel 自然播完 |

原始证据在本机 gitignored `outputs/acceptance/run-20261012-multiturn/` 与 `outputs/serial/robot-20261012-multiturn.log`。

## 条件

- 设备 MAC `90:e5:b1:d7:83:2c`，产品固件 **build 24** / `ota_0`。开测前板子仍为 #200 bench 镜像；本轮为正式语音先刷回产品（`flash.sh --port /dev/cu.usbmodem101`，无 `--bench`），串口见 `MEMORIA_FIRMWARE_BUILD=24`。
- 生产整栈 `20261010-n8-endpoint-diag-v1`，开测前 readiness `ready` / smokes `passed`，三角色与 media-edge healthy。
- Mac 开测前音量 19、静音；测试音量 30、解除静音；结束后恢复 19 与静音。串口 `/dev/cu.usbmodem101`，常驻 logger；复位后热身唤醒一次再开场景。
- 语音库 `outputs/voice-bank/`（既有 Doubao 素材）。未部署、未改生产 env、未建热词表、未采孩子真声。

## 停止与闲聊边界

音量 30 下停止样本**只作观察**：`t012`「别说了」有 v2 `spoken_stop_and_device_playout`；`t016`「停」自然播完。不能据此关闭 N-10，也不能外推音量 50。成语接龙第二步与「再见」无回复与历史 N-5 收据同类，不单独立缺陷。

尾窗退出码 4：设备已在场景结束前处于 `idle`，90 s 观察窗内没有新的 `-> idle` 转换，故 `idle_observed=false` / `evidence_insufficient`。这不等于设备未 idle。

## 身份摘录

会话 delivery 前缀 `1e8aff64-8098-4143-a2e5-531b7845c102`（如 `…/epoch-1/turn-12/generation-12/tool-0`）。离线停止重算：

```bash
python scripts/voice_soak_evidence.py outputs/acceptance/run-20261012-multiturn
```

`code=既有 voice_soak + 产品 build 24 / wired=生产 bridge 与唯一串口 logger / enabled=20261010-n8-endpoint-diag-v1 / verified=一圈多轮问答 41/44 有回复，idle 尾窗证据不足（退出 4），停止非验收观察项`。
