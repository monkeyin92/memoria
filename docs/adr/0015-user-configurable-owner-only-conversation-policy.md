---
status: accepted
date: 2026-07-21
amends: 0011-target-speaker-focus-and-shadow-boundary
---

# 用户可配置的明显旁人过滤策略

## Context

ADR 0011 为避免未校准的 shadow 声纹误静音主人，曾让 shadow
`guest/ambiguous` 在普通聊天中 fail-open。真实家庭场景表明，这会使系统明明已经得到
`shadow_guest_candidate`，仍接纳孩子或旁人的话并让模型回复；同时 H5 把所有权威字幕
直接保存，访客话轮和对应 AI 回复会进入账户主人的每日回顾。这与“默认只和主人对话”
以及“允许访客聊天也不能污染主人历史”的产品预期不一致。

声纹仍是概率信号，不能把 `shadow_owner_candidate` 提升为正式 owner 权限；仅调 CAM++
阈值也无法表达用户希望允许或拒绝访客的选择。因此需要在身份权限平面之外增加显式、
可撤销的交互策略，并为长期历史建立服务端来源的话轮资格。

## Decision

- Profile 公共字段 `reject_non_owner_voice` 默认 `true`，H5 以“过滤明显旁人（实验）”
  开关展示，避免把概率型声纹误述为可靠的主人识别。
- 设置由 Control API 持久化。Agent 每次通过可信的 session→account 解析执行声纹分类时
  同步读取最新策略；不信任浏览器传入的策略或 LiveKit room metadata。设置成功后，当前
  会话的下一次分类即可生效；缺失或非法策略按 `true` 处理。
- 开启时，formal `guest/owner_mismatch` 与明确的 shadow
  `shadow_guest_candidate` 在普通提交和播放期打断中均被拒绝；已经明确为非主人的短暂
  停止词不能绕过门禁。shadow `shadow_ambiguous_candidate` 与 formal
  `ambiguous_score` 为避免误静音主人仍可普通对话，但保持 non-owner/uncertain，不能获得
  私人记忆、工具、敏感权限或主人历史资格。没有档案、模型/模板不可用或尚无可靠结论时
  同样保持对话 fail-open，但不得表述为已识别主人。
- Shadow guest cutoff 的运行时默认值为 `0.40`，分类时取 Profile 已存阈值与运行时阈值
  的较小值。该阈值只是当前设备实测后的低成本止损，不替代 FAR/FRR/EER 与真实家庭设备
  校准。
- 关闭时，上述访客候选可以正常对话和打断；`SpeakerDecision` 仍保持原来的
  `guest/uncertain` 分类，不能读取主人私人记忆、使用 owner 工具或执行敏感动作。
- Agent 为每个已接受话轮计算 `history_eligible`：只有正式 `owner` 或
  `shadow_owner_candidate` 为 `true`。资格按 `GenerationFence` 冻结，并同时附在该用户
  终稿及其对应的 actual-heard AI 终稿上；后续说话人的分类不得改写旧 generation。
- H5 只保存 `history_eligible=true` 的权威终稿。字段缺失、访客、ambiguous、无档案或
  authority 不可用时均不写入消息表、离线重试队列和自动摘要输入。
- 对非正式 owner 的 LLM 上下文明确说明“身份未确认”，不得因为称呼或对话内容假定
  对方就是账户主人，也不得扮演其父母或其他亲属。

## Consequences

- 默认过滤明显旁人，同时优先避免把主人静音；用户仍可显式允许家人或访客临时聊天。
- “允许聊天”与“允许写主人历史”不再是同一个开关，访客回复不会污染主人回顾。
- Shadow owner 仅获得本次交互与普通对话历史资格，不获得私人记忆或敏感操作权限；
  CAM++ 未完成 FAR/FRR、unknown rejection 与 anti-spoof 验证的边界不变。
- Authority 故障时宁可暂不沉淀回顾，也不把身份未知的话轮写入主人历史；实时对话本身
  仍保持可用。
- Omni 浏览器直连路径没有同等的服务端声纹事实，生产继续禁用；恢复该路径前必须实现
  等价的服务端策略与 `history_eligible` 契约。

## Considered Options

- 只调高/调低 CAM++ 阈值：拒绝。阈值不能表达用户选择，也不能修复历史持久化越权。
- 由 H5 把开关传给 Agent：拒绝。浏览器输入不能成为身份门禁的可信来源。
- 关闭开关时把访客升级成 owner：拒绝。交互便利不能改变私人数据和敏感操作权限。
- 仅在 H5 根据最新说话人分类过滤助手回复：拒绝。回复结束前可能已有新说话人，必须按
  原 generation 冻结归属。
