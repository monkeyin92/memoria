---
status: accepted
date: 2026-07-27
---

# 用小模型复核歧义打断，但由 UtteranceRouter 保持唯一策略权威

## Context

微信小程序真机外放时，用户开口后可以很快触发 barge-in，但下行只降到
`gain=0.25`，实际听感仍要等 ASR interim、说话人确认和播放停止。FunASR final 还可能把
用户控制词与助手尾音交错成一段新文本，例如：

`份停听一下能是据提供的数据和指示来协助。`

确定性规则能可靠处理纯控制词、继续、登记、精确重放和明确的
`interrupt_then_chat`，但不能安全枚举所有同音、漏字和交错回声。继续增加关键词会同时
误伤“停一下，你叫什么名字？”和引用助手原话后的真实追问。

## Decision

- 可信小程序 AEC 会话在 VAD/barge-in 首事件立即发布
  `assistant_audio(action=duck, gain=0.0)`；普通 H5/非可信链路仍使用 `0.25`。静音只改变
  客户端播放增益，不提前确认 generation interruption。
- 高置信纯控制、resume、enroll 和精确 replay 仍先由确定性 `UtteranceRouter` 处理。
  只有当前 speech epoch 已有 sticky `interrupt_then_chat`、且权威 final 仍为该歧义路线时，
  才调用小模型。
- 模型输入只包含权威 final、首个 sticky interrupt 文本和 barge-in 时冻结的助手文本；
  不发送会话历史、记忆、工具、权限或 Persona。
- 模型只能返回严格枚举：
  `CONTROL_ONLY / HAS_USER_CONTENT / UNSURE`。它不能直接停止播放、提交 chat、清理话轮
  或选择确认音；这些副作用仍只由 `UtteranceRouter` 的 route 决定。
- `CONTROL_ONLY` 不进入 LLM，清理当前控制话轮并只确认一次；`HAS_USER_CONTENT` 保持
  `interrupt_then_chat`；`UNSURE`、超时、HTTP 异常或非法输出 fail closed，不把污染文本
  送进 LLM，并提示用户重说。
- 请求与结果绑定 speech epoch、playback epoch 和 generation fence。迟到结果不得作用到
  后续话轮；模型复核与 SpeakerAuthority 并行等待，避免把两段延迟串行相加。
- 首个可替换适配器复用 DashScope OpenAI-compatible 端点和 `qwen-flash`，超时
  `0.6s`，使用会话级长连接。Provider smoke 必须覆盖真实污染、控制+内容、引用助手原话、
  明确问题和控制+回声五类样本。

## Consequences

- 用户一开口，小程序下行先静音，不再等语义确认才获得听感上的停止。
- 模型只补足确定性规则无法可靠表达的声学污染边界，既避免全话轮增加模型延迟，也避免
  形成第二套控制状态机。
- 分类服务不可用时，歧义话轮会要求重说，可能多一次交互，但不会让助手尾音进入 LLM、
  历史、记忆或 Persona。
- `CONTROL_ONLY` 的最终证据可把早期 `interrupt_then_chat` 恢复为可继续的暂停回答；
  真正含用户内容的话轮提交后仍清除旧暂停状态。
- 自动化只能证明路由、fence、确认音和事件协议；0–700 ms 真机静音体感、误触发恢复和
  扬声器双讲仍必须在发布候选上验收。

## Alternatives considered

1. 继续扩充停词和回声相似度规则：拒绝。ASR 交错污染没有稳定词形，会不断产生新碰撞。
2. 每个 final 都调用模型：拒绝。普通聊天和高置信控制不需要额外成本与延迟。
3. 让模型直接返回 `enter_chat/stop/ack`：拒绝。会绕过 Router、权限与 generation fence。
4. `UNSURE` 或超时继续进入 chat：拒绝。真实故障已证明这会把助手回声送入回答和归档。
5. 立即部署本地分类权重：暂缓。当前运行机没有已验证的中文权重和 tokenizer；先积累
   shadow 样本，再以相同严格接口替换适配器。

## References

- [`0013-canonical-user-transcript-after-playback-contamination.md`](0013-canonical-user-transcript-after-playback-contamination.md)
- [`0021-wechat-miniprogram-media-gateway.md`](0021-wechat-miniprogram-media-gateway.md)
- [`../production-deployment.md`](../production-deployment.md)
