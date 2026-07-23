---
status: accepted
date: 2026-07-23
---

# 个人音色只能由不可变 Digital Self 版本绑定并按 generation 证明

## Context

Memoria 的五个陪伴伙伴使用豆包 `seed-tts-2.0` 设计音色；用户本人声音使用豆包
Voice Clone 2.0 / `seed-icl-2.0`。两者属于不同产品身份、资源头和权限域。如果只把
“当前 active VoiceProfile”直接接到 Agent，会产生三个不可接受的问题：

- 日常 Companion 会被个人音色替换，混淆“陪伴者”和“数字分身”；
- profile 在会话中途更新、撤销或过期时，“当前最新值”会替换已批准版本；
- Archive 只能看到 profile ID，无法证明某个 generation 实际用了哪个资源和 speaker。

豆包公开文档还没有给出可由本项目确认的通用删除接口，也没有证明
`custom_speaker_id`、查询响应字段与实时合成 `speaker` 的映射。实现不能猜测这些合同。

## Decision

- Companion 永远使用所选伙伴目录中的 `seed-tts-2.0` 设计音色。个人音色只允许用于
  owner Self Preview；Legacy 是否允许由后续 `LegacyGrant.voice_allowed` 决定。
- 激活 VoiceProfile 不会修改已有 DigitalSelfVersion。编译 manifest v3 时只采用 consent
  有效、主观盲测与服务端质量探针均通过、active、未过期的豆包个人音色，并写入：
  `profile_id + version_number + provider + target_model + resource_id + provider_expires_at +
  speaker_sha256`。其中 digest 由服务端对 synth-ready provider speaker 计算，H5 和
  Archive 都不能获得 raw speaker。
- Self Preview 创建会话时，把上述七个字段与当前 resolution 精确比对并冻结，同时把账户
  当时所选伙伴的 `fallback_voice_profile/provider/model/resource_id` 作为独立声音快照；
  不携带 `companion_style_id` 或陪伴人格能力。任一 personal 字段不同、撤销、过期、缺失
  或状态变化都只能回退该快照，不能读取“最新 profile”或进程默认音色替代。
- Agent 分别维护 `seed-tts-2.0` 与 `seed-icl-2.0` 连接池；resource header 不共享。
  每个 GenerationFence 在播放前冻结实际 profile/resource/kind 与 speaker SHA-256。
  Archive 不保存 raw provider speaker ID，只保存 profile、version、resource、expiry 与小写
  SHA-256；设计音色 digest 由服务端批准目录计算，调用方不能自报。
- 个人音色在首音频前失败时，只允许同一 generation 将同一文本重放一次到会话冻结的
  所选伙伴设计音色；进程启动默认音色只在冻结 fallback 本身无效时作为最后安全兜底。
  已产生音频后失败不得静默重放。旧 generation 的 fallback callback 必须被 fence 拒绝。
- provider speaker 映射默认 `unverified` 并 fail closed；只有授权真实样本 smoke 证明后，
  才可选择 `custom_speaker_id` 或明确响应字段。expiry 响应字段路径和格式也必须由账号
  smoke 明确配置，NULL/过期值不能激活。Control API 使用独立 clone credential，
  不复用 Agent TTS API key 或 access token。
- 本地撤销立即停止解析个人音色。由于删除合同未确认，豆包云端清理记录为
  `pending/manual`；不得谎报 completed。供应商控制台人工清理后，只能由独立
  `voice_cleanup` 能力令牌调用审计化确认入口，记录工单引用与 provider voice digest 后
  收敛为 completed；H5、Agent 和普通账户 Bearer 无此权限。

## Consequences

- “启用个人声音”之后，用户必须重新构建、测试并批准 DigitalSelfVersion；旧版本和当前
  Companion 都不会改变。
- H5 和 API 必须同时识别 manifest v1/v2/v3；v1/v2 canonical bytes 与 digest 保持不变。
- `context_texts` 继续禁用，直到 seed-icl 的真实探针证明字幕时间戳与 PCM 对齐稳定。
- 本地自动化可以证明权限、版本、资源池、fallback 与 provenance，但不能证明真实声音
  相似度、自然度、供应商 speaker 映射或真机听感；这些仍是发布后的明确人工验收门。

## References

- [`docs/research/doubao-personal-voice-runtime-2026-07-23.md`](../research/doubao-personal-voice-runtime-2026-07-23.md)
- [`docs/adr/0012-doubao-bidirectional-tts-and-voice-registry.md`](0012-doubao-bidirectional-tts-and-voice-registry.md)
- [`docs/adr/0016-separate-companion-style-from-digital-self.md`](0016-separate-companion-style-from-digital-self.md)
