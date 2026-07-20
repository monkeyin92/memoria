---
status: accepted
date: 2026-07-18
---

# 分离声纹识别、合成声音与敏感动作授权

Memoria 将 `Speaker Profile`、`Voice Profile` 和 `Action Authority` 作为三种独立资产：声纹档案只提供 `owner / guest / uncertain` 概率信号，声音档案只决定 TTS 使用哪个授权音色，敏感动作仍需账户、设备和必要的二次确认。这样能避免把“声音像主人”误当成“有权读取或操作主人数据”，并允许任一资产独立登记、轮换和撤销。

## Consequences

- 合成输出永远不能回流为主人声纹或人格训练样本。
- 访客可继续普通对话，但默认不能读取或污染账户主人的私人记忆与人格。
- `uncertain` 不静音；普通对话可继续，私密检索和敏感动作升级到设备解锁、Passkey 或主人确认。
- 声纹模板使用独立加密域、密钥、访问审计和删除流程。
- 会话级 log-mel 只保留为远场媒体与微噪声守卫，不能授予 owner 权限，也不能仅因声音不同而静音真实访客。
- 正式 CAM++/3D-Speaker 模板先进入 shadow；只有不少于 200 条授权样本的 FAR/FRR/EER/unknown rejection 报告通过后，内部接口才允许激活。
- Agent 只提交 `session_id` 与当前 PCM，Control API 服务端解析账户归属；模型不可用、低质量、维度错误和超时一律为 uncertain。
