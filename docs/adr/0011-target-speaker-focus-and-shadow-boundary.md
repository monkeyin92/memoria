---
status: accepted
date: 2026-07-20
amended_by: 0015-user-configurable-owner-only-conversation-policy
---

# 将目标说话人聚焦与 SpeakerAuthority 权限平面分离

> 2026-07-21：普通聊天一律 fail-open 的产品默认已由 ADR 0015 修订；当前默认只拒绝明确的 formal/shadow guest，ambiguous 为避免误静音主人仍可聊天但不获得权限或历史资格。本 ADR 的权限分离、完整端点分类和统一 Router 原则继续有效。

## Context

正式 `SpeakerAuthority` 的职责是为记忆、Persona 和敏感动作给出
`owner / guest / uncertain` 权限结论。它刻意允许 guest 和 uncertain
进行普通对话；CAM++ 仍为 shadow-only，也不能用于反重放或身份认证。

这无法满足单人陪伴场景的交互预期：旁人、电视或近距离杂声会被 ASR
转写，进而提交为用户话轮，或在播放期间触发 VAD 打断。把 shadow
`owner` 候选直接升级成 `owner` 权限会破坏 ADR 0003 和 ADR 0010 的安全边界；
只调高 VAD 阈值也无法区分主人和旁人。

## Decision

在启用正式 `SpeakerAuthority` 的实时 Agent 会话中，增加
`TargetSpeakerFocus` 作为独立的交互控制面：

- 策略开启时，正式 `guest` / `owner_mismatch` 不能提交话轮或打断播放；用户关闭
  `reject_non_owner_voice` 后可放开普通交互，但权限仍保持 non-owner。
- 明确的 shadow `guest` 与低于 600 ms 的语音不能因普通旁人讲话抢断
  播放；shadow/formal `ambiguous` 普通聊天则 fail-open，仍保持 `uncertain`
  权限且不进入主人历史。
- 最终 ASR 经 `utterance_router` 判为明确暂停/让话控制意图时（例如“等一下”），
  即使 shadow 声纹尚未校准也可以停止当前播放并返回固定让话确认；该语句不进入
  聊天、不读取私人记忆、不写长期记忆，也不授予敏感动作权限。
- 没有声纹档案，或模型/模板暂时不可用时，普通对话保持可用；系统不得把
  这种可用性回退表述为已识别主人。
- 正常话轮先完成说话人分类，再进入 `accept_user_turn`。
- 播放期间保持 LiveKit 的 `min_words` 门禁；VAD 刚起始时不得用不完整 PCM
  抢先做目标说话人分类。普通输入须由最终转写和完整 endpointed 音频的目标
  说话人聚焦共同放行，明确控制意图走同一规则表的窄豁免后才调用底层
  `session.interrupt`。直接收到的 LiveKit interruption 也必须经过同一门禁。
- 页面上的实体停止按钮不属于语音输入，继续可立即停止播放。

规则表位于 `utterance_router`，普通提交和播放期打断共用它，避免出现两套
旁人过滤条件。

## Consequences

- Shadow 候选只能决定“这一句话是否控制陪伴对话”；它仍返回
  `uncertain`，不能读取私人记忆、写入长期记忆或执行敏感动作。
- 一般短促语音仍可能需要说得稍完整才会触发语音控制；明确的暂停/让话短句
  是可恢复的交互控制，不会成为聊天或权限升级。模型不可用时则保留对话可用性，
  而不是伪造身份结论。
- 旁人声音不再触发 VAD 即时 duck/restore，避免连续音量抽动；播放只在
  目标说话人确认后停止。
- 后续若要处理重叠说话人或远场主人声，需要评估 personalized VAD 或
  target-speaker extraction；单次 CAM++ embedding 不足以解决混叠分离。

## Considered Options

- 把 shadow 匹配提升为 `owner`：拒绝。CAM++ 没有活体/反重放结论，也没有
  通过授权样本的 FAR/FRR/EER 指标。
- 在浏览器端继续使用遗留声学 gate：拒绝。它不能安全访问账户声纹模板，且
  不能覆盖服务端最终话轮和 LiveKit interruption。
- 仅提高 VAD 或 ASR 的最短时长：拒绝。可以降低杂音，却无法识别旁人讲话，
  还会伤害主人正常打断体验。
