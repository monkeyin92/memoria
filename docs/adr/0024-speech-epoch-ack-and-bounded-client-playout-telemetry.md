---
status: accepted
date: 2026-07-27
---

# 打断确认语按 speech epoch 幂等，并回传有界客户端播放证据

## Context

真实小程序会话中，同一 `turn_id / generation_id` 在约 4.2 秒内播放了两次打断确认语。
第一次来自播放期中断，第二次来自迟到 ASR 终稿。旧实现只使用 4 秒时间冷却；终稿晚于
冷却窗口时，同一用户话轮可以再次播放确认语。

同时，Gateway 已能记录下行丢帧和客户端 reset/interrupt，但客户端没有上报 WebAudio
underflow、hard reset、conceal 和实际排程 lead。生产日志无法区分重复控制音频、网络断帧
和客户端排程重建。

## Decision

- 打断确认语以当前 `speech_epoch` 作为幂等键；同一 speech epoch 最多播放一次。
- 4 秒冷却继续作为不同 speech epoch 间的防抖，但不再承担同一话轮去重职责。
- 迟到 ASR 终稿若再次路由为纯控制，只恢复 listening，不重复合成或发布确认音频。
- 小程序播放器只上报以下有界事件：
  `first_playback`、`miniprogram_playback_underrun`、
  `miniprogram_playback_hard_reset`、`miniprogram_gap_concealed`。
- 指标只允许非负数值字段：
  `queue_lead_ms`、`pending_audio_ms`、`missing_frames`、
  `scheduled_sources`、`clock_ahead_ms`。
- Gateway 对字段、名称和数值范围做严格校验，再以 `source=miniprogram` 转发到既有
  `voice-agent.telemetry`。事件不能携带转写、音频、账号、token、cookie 或业务命令。
- 播放 underflow 只把下一批音频 rebase 到新的 lead，不再调用 hard clear 停止仍登记的
  source；generation/barrier、大 sequence gap 和严重时钟漂移仍可 hard reset。
- FunASR 增加可选 `vocabulary_id` 与 `speech_noise_threshold` 协议支持，但生产值必须由
  真机录音集校准，默认不启用。

## Consequences

- 同一用户打断不会因 ASR 终稿迟到而重复播放确认语。
- 下一次真机故障可以直接从同一 session 的 Agent/Gateway 日志确认首播 lead、
  underflow、hard reset 和 conceal。
- 遥测是诊断事实，不改变 generation、Agent 状态、权限、记忆或播放控制。
- 这仍不能证明 AEC、ASR 或 TRTC 已完成；真实声学根因仍需定向 AEC 采样和设备矩阵。

## Alternatives considered

1. 把冷却从 4 秒延长：拒绝。ASR 可以更晚，且会压掉另一个真实 speech epoch。
2. 只在 Agent 记录服务端下行：拒绝。服务端不知道 WebAudio 是否 underflow 或重排。
3. 上报任意客户端日志对象：拒绝。会形成文本和敏感数据进入生产日志的旁路。
4. 本轮直接切换 TRTC：拒绝。当前缺少腾讯权限、凭据和 TRTC 到现有 Agent 的媒体桥。
