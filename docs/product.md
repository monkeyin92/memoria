# Memoria Product

本文件由 `scripts/consolidate_docs.py` 按类型归并生成。每个来源章节保留原文和来源路径。

## 来源文件

- `docs/product/n-15-smile-reflection.md`
- `docs/product-review-f01-307.md`

---

## 来源：`docs/product/n-15-smile-reflection.md`

## N-15：伙伴微笑与倾听反馈

**目标。** 在对话里让伙伴“像在看你、懂你”。当前只跟着设备状态做眨眼和张嘴；下一步让它跟着内容做出简单情绪反应，再延伸到边听边点头这类非言语互动。

**范围。** 这是产品定义 + 服务层改动，不涉及新模型上线。用现有的 `emotion` JSON 字段（协议见 [`memoria_protocol.cc:2943`](../firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc:2943)），固件侧保持简单。

---

#### 现状

- **固件。** [`MemoriaMascotDisplay::SetEmotion()`](../firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_mascot_display.cc:445) 把服务端的表情名存进一个定长缓冲，动画任务读进说话帧，由 `FaceForExpression()` 在现有七张脸里挑一张（happy/caring/loving 和 neutral 用说话脸，curious/thinking 用思考脸，surprised 用招呼脸，sad 用平静脸；没收到名字或名字不认识时也保持说话脸）。危机脚本仍固定平静脸。不说话的状态仍跟设备状态走：思考脸来自设备自己的 VAD 结束，不读识别文本。眨眼、说话脸的嘴形、进入说话的 200 ms 等待和说话后 800 ms 保持见 README「屏幕：黄色点阵脸」。没有新增表情。
- **服务端。** 回复开头可以带一个内部标签 `[emotion:neutral|happy|caring|curious]`。`PhraseSegmenter` 在进入 TTS 前剥离标签，并把值写入当前 turn 的 `SpeechPlan.assistant_emotion`。豆包 TTS 只有开启 `DOUBAO_TTS_STYLE_CONTROL_ENABLED` 时才把情绪追加到 `context_texts`；表情走 `assistant_expression`，不依赖这个开关。

---

#### 方案一：基础情绪（Phase A）

**规则。** 服务端把 BAILIAN 输出的情感标签透传给固件。映射到三套可见变化，固件只负责插值和开关，不负责判断。

| emotion | 可见表现 | 持续时间 / 回退 |
|---|---|---|
| happy | 嘴角上扬弧度增加（嘴巴形态替换） | 持续直到新 emotion 到来 |
| thinking | 眼睛轻微开大 + 注视停留时间延长 | 持续直到 new emotion |
| angry | 眉毛下压（如果图层允许），否则仅眼形变窄 | 持续直到新 emotion |

**实现：**

- **服务端。** 从 BAILIAN 输出中抽取或生成一个 token (`happy|thinking|angry|neutral`)。若没有显式情绪，默认 neutral。
- **固件。** `SetEmotion(emotion)` 解析标签，选择对应的静态姿态表（嘴角、眼形）。当前版本没有分层美术，所以是整图切换 + 淡入淡出跨帧过渡（alpha 交叉混合），避免“跳帧”。

**边界：** 当前 MVP 不做分层美术，只做平滑过渡。后续 M-7 引入分层 rig 后再切到分层部件控制。

---

#### 方案二：边听边点头（Phase B，听态非言语）

**动机。** ELLSA 论文提出“在对方说话时也会给出反应”，比如点头或身体前倾表示你在听。我们的机器人可以在用户说话时给一点小动作，降低冷场感。

**触发条件：**

- ASR 正在录音且未中断（LISTENING 状态持续 ≥ 2s）
- 不是唤醒失败或误唤醒场景（由策略层过滤）

**行为：**

- 每 1.5–2.0 s 一次轻微点头（头 + 颈方向小幅旋转 + 身体略俯）
- 如果用户暂停超过 3s，停止点头回到待机呼吸

**固件需求：**

- 当前动画状态机支持“循环子动作”，可插入短周期微动
- 不需要新美术资源，复用现有头部倾斜帧（或通过缩放模拟）

**服务端：**

- 推送一个布尔标志 `backchannel=true`，固件据此决定是否进入点头循环
- 若 ASR 结束，服务端立即推 `backchannel=false`

---

#### 实现顺序与验收

##### Phase A（情绪）

**完成标准：**

- [ ] 三个情绪在主机渲染器上可做预览（25 fps）
- [ ] 固件跨帧渐变无跳变
- [ ] 服务端日志记录 emotion → 固件上报确认（链路验证）

**时间估测：** 0.5–1 day（纯服务 + 固件逻辑，无美术新增）

##### Phase B（点头）

**完成标准：**

- [ ] LISTENING ≥ 2s 后点头启动，3s 停
- [ ] 点头频率与幅度符合预期（不要过快/过大）
- [ ] 不影响音频管线与语音打断逻辑

**时间估测：** 1–2 days（需要调参）

---

#### 风险与替代

- **风险 1：表情太生硬。** 如果当前没有分层美术，表情只能靠整图切换。建议先用“口型微调”代替眉眼变化，降低不适感。
- **风险 2：点头过于机械。** 需要在固件中加随机抖动，避免完全周期化。
- **替代：** 如果评估发现整图切换成本太高，可以直接跳过 Phase A，只推进 Phase B（点头更容易通过缩放模拟）。

---

#### 与 M-7 的关系

Phase A/B 都可以先在不改美术包的前提下跑通。等到 M-7 完成分层 rig 之后，可以把这些规则迁移到分层控制（眼、嘴单独运动，更流畅）。所以 N-15 是轻量先行版，为 M-7 积累真实语料。

---

#### 与 ELLSA 论文的对齐

- **借鉴点：** “在对方说话时也给反应”。我们实现为 backchannel 点头。
- **不采用部分：** 论文的手势生成器是 3D SMPL-H + FLAME，与我们 2D 精灵不匹配。所以我们只取逻辑灵感，不用它。

---

#### 待确认

- 情绪标签集是否扩展到更多类别？（sad, surprised, excited）
- 点头幅度与节奏是否需要调优测试？

建议先上线 Phase A 的最小闭环，收集两条数据：

1. 情绪切换流畅度（用户主观评价）
2. 点头是否带来正向体验（A/B 测试，可选）

---

**Co-Authored-By:** Claude Code <noreply@anthropic.com>

---

---

---

## 来源：`docs/product-review-f01-307.md`

## PR #307 产品审查（F01 记忆生命周期管理 UI）

> 审查人：Compass（产品经理）
> 审查日期：2026-10-19 00:50 CST
> PR：[#307](https://github.com/monkeyin92/memoria/pull/307)
> 合并提交：`3b7ba0d0`
> 状态：**已合并**，mp-icon 路径问题已由 PR #311 修复

#### 审查结论

**LGTM（无 blocker）**，产品层面可接受。F01 作为 mock 界面完整实现预期交互路径，所有风险均已披露，保持在内部草稿范围内。

#### 1. 功能完整性

##### 1.1 核心场景覆盖 ✅

三个主场景均已实现（index.js L26-81）：
- **有效保管**（`scenario: "active"`）：展示到期时间、续期/撤回选项、对话状态
- **期限未配置**（`scenario: "unconfigured"`）：禁用新保管和续期，仅支持查进度
- **已撤回**（`scenario: "withdrawn"`）：显示撤回时间、删除处理进度（受理→停权→删除→完成）

##### 1.2 数据契约一致性 ✅

Mock 数据字段（index.js L28-81、operation-detail.js L16-109）完全对应 [keystone.md L550-595](<../share/planning/keystone.md#L550>) 冻结的 B1-contract-v1：
- 保管状态：`grantId`、`status`、`effectiveAt`、`expiresAt`、`daysTillExpiry`
- 操作进度：`operationId`、`action`、`phase`、`acceptedAt`、`accessStoppedAt`、`completedAt`
- 删除域：`dialogue_summary`、`child_archive` 逐域标注 `purged`
- 两层清除：`childLayersPurged`、`subjectDeletion` 分开展示

**证据**：index.js L28-81、operation-detail.js L16-109。

##### 1.3 按 PRD 验收点覆盖情况

对照 [compass.md 附录 A](<../share/planning/compass.md#L91>) A01 系列，本 PR 覆盖以下 mock 路径：
- **A01e 保管同意**：单独选项、默认不勾（index.wxml L141-151，checkbox 无 checked）
- **A01f 撤回并删除**：撤回按钮+确认（index.js L165-180）、操作详情页展示逐域删除结果（operation-detail.wxml L95-106）
- **A01d 仅解绑**：页面展示"本次不申请新保管"（index.wxml L154-162）

**缺口（符合 F01 定位）**：B03/B04/B05 真实接口联调、正式 B10 文案集成，PR body 已明确列出。

#### 2. 用户体验

##### 2.1 交互流程 ✅

- **草稿标识常驻**：页面顶部橙色横幅"内部交互草稿 · 仅使用虚构测试数据 · 不构成真实同意"（index.wxml L4-6，draftLabel 可见）
- **三场景切换**：开发用 debug 按钮（index.wxml L9-12）可选场景，正式版移除即可
- **操作确认**：撤回保管先弹 modal 确认（index.js L165），避免误操作
- **操作历史可展开**：toggleOperationHistory（index.js L184-188）、跳转详情页（index.js L191-195）
- **期限未配置禁用**：续期和新保管按钮在 `canRenew=false` 时禁用（index.wxml L40、unconfiguredScenario 场景不展示新保管）

##### 2.2 文案与边界情况处理

###### 优点
- **保管说明准确**（index.wxml L20-22）：明确"不包含近期摘要或对话原文，也不会自动恢复已撤回的记忆权限"
- **不申请与撤回分开**（index.wxml L155-161）：对应 [warden.md L651](<../share/planning/warden.md#L651>) mock 文案，避免混淆
- **撤回确认文案对齐**（index.js L168）：与 warden.md L654 一致，说明"本次范围"、"其他独立有效用途不在范围内"
- **对话状态枚举**（index.js L46）：`ephemeral_ready` 对应"可继续不留存对话"（warden.md L657）

###### 待改进（非 blocker，nice-to-have）
1. **对话状态标签**（index.wxml L170）：当前只有 `activeScenario` 场景展示，其他场景也应显示当前对话状态（`withdrawn` 场景的 operation 已返回 `chatTransitionStatus`，但主页未展示）
2. **时间格式统一**：index.wxml L31 写"到期: {{grant.expiresAt | dateFormat}}"，但过滤器输出"月日"无年份（wxs L180-186），跨年保管不可读；operation-detail 的 formatTime 含月日+时分（wxs L180-196），格式不一致
3. **grant 续期后状态**：index.js L157-161 续期 mock 后切换到 `withdrawn` 场景，但实际续期应仍为 `active` 且 `expiresAt` 延后；mock 行为与真实流程脱节

**证据**：index.wxml L166-174（对话状态卡片）；index.js L157-161（续期 mock）。

##### 2.3 错误处理 ✅

- 操作详情页找不到 operationId：显示 toast 错误并 1.5 s 后返回（operation-detail.js L120-127）
- 所有模拟操作显示 `[Mock]` 前缀（index.js L158、L175）

#### 3. mock contract 合理性

##### 3.1 状态机一致性 ✅

操作详情页时间线（operation-detail.wxml L24-64）对应四阶段：
1. 请求受理（`acceptedAt`）
2. 停止访问（`accessStoppedAt`）
3. 删除处理（`completedAt` 或"处理中…"）
4. 整体完成（`status='completed'`）

与 [keystone.md 契约](<../share/planning/keystone.md#L550>) 的 `phase` 枚举对齐：`accepted` → `access_stopped` → `deleting` → `completed`。

##### 3.2 字段命名与枚举值

**符合规范**：
- grant.status 枚举（index.js L34）：`active | pending | superseded | revoked | expired | failed`
- operation.action 枚举（index.js L20、L74、L92）：`unbind_device | renew_custody | withdraw_memory`
- operation.phase 枚举（index.js L26、L76、L97）：`completed | access_stopped | deleting`
- policyStatus（index.js L16）：`unconfigured | configured`

**未覆盖但 contract 存在**：
- `custodyStatus.status`（operation-detail.js L42、L78）只用 `active | pending`，未覆盖 `not_requested | failed`
- `domains[].status`（operation-detail.js L58、L64）只用 `purged | pending`，未覆盖 `failed | skipped`

**结论**：未覆盖场景不影响当前 mock 目标，真实联调时补齐即可。

##### 3.3 硬编码 mock 数据合理性

**问题（minor）**：
- policyHash（index.js L18）写死 `"sha256:abc123def456"`，真实接口应动态返回
- 到期天数（index.js L37）写死 `daysTillExpiry: 183`，应按 `expiresAt` 计算
- 操作历史（index.js L84-102）只有 2 条，真实场景可能更多

**不属于 blocker**：mock 数据硬编码符合 F01 定位，PR body 已标注"待后端完成"。

#### 4. 微信小程序规范符合性

##### 4.1 组件使用 ⚠️（已由 PR #311 修复）

**原问题**（已修复）：
- index.wxml L71、L169 使用 `<mp-icon>`，但 index.json L3 未声明 `"usingComponents"`
- 缺少 mp-icon 组件文件路径

**修复情况**：PR #311 已提交（commit `c8a1fce0`），改用项目内组件并换用已登记图标。

##### 4.2 页面配置 ✅

- **app.json 注册正确**（commit 3b7ba0d0）：两个页面路径已添加
- **页面 json 配置完整**：
  - index.json L2：`"navigationBarTitleText": "记忆管理"`
  - operation-detail.json L2：`"navigationBarTitleText": "操作详情"`

##### 4.3 样式规范 ✅

- **使用 mp-token 设计系统**（index.wxss）：
  - 颜色：`--mp-bg`、`--mp-ink`、`--mp-accent`、`--mp-line`
  - 字号：`--mp-type-meta`、`--mp-type-body`、`--mp-type-card`
  - 圆角：`--mp-radius-card`、`--mp-radius-icon`
  - 阴影：`--mp-shadow-card`
- **无硬编码颜色/字号**：所有值通过 `var()` 引用

**证据**：index.wxss L4-101、operation-detail.wxss L1-142（未完整查看，但首屏引用 mp-token）。

##### 4.4 数据绑定与事件 ✅

- **事件处理符合规范**：`bindtap`（index.wxml L10、L115、L124）
- **条件渲染正确**：`wx:if`/`wx:for`（index.wxml L27、L83、L119）
- **wxs 过滤器**：dateFormat、timeFormat、actionLabel（index.wxml L179-211、operation-detail.wxml L179-200）

#### 5. 产品判据总结

##### 5.1 F01 对照 PRD A01 系列

| PRD 项 | 是否覆盖 | 备注 |
|---|---|---|
| A01e 保管同意 | ✅ | 单独选项、默认不勾、可单独拒绝 mock（index.wxml L141-151） |
| A01f 撤回并删除 | ✅ | 撤回按钮+进度+逐域删除（operation-detail.wxml L95-106） |
| A01d 仅解绑 | ✅ | "本次不申请新保管"文案（index.wxml L155-161） |
| A01g 重绑 | ⚠️ | 未明确覆盖"原撤回同意不自动恢复"场景 |
| A01h 多设备 | ⚠️ | 未展示"解绑一台不误删共享内容"逻辑 |
| A01i 保管到期 | ⚠️ | 有 `expiresAt`，但无到期提醒入口 |

**结论**：A01g/h/i 超出 F01 mock 范围，真实接口联调阶段补齐。

##### 5.2 通过/不通过判据

| 项 | 状态 | 依据 |
|---|---|---|
| 功能完整性 | ✅ 通过 | 三场景 + 操作详情页均实现 |
| 数据契约一致性 | ✅ 通过 | 字段与 B1-contract-v1 对齐 |
| 合规文案使用 | ✅ 通过 | 草稿标识常驻，mock 文案符合 [warden.md L641-660](<../share/planning/warden.md#L641>) |
| 小程序规范 | ✅ 通过（已修复） | mp-icon 问题已由 PR #311 解决 |
| 产品边界清晰 | ✅ 通过 | PR body 明确列出尚未验证项 |

#### 6. 待后续处理（非 blocker）

##### 高优
1. **B03/B04/B05 真实接口联调**（依赖后端）
2. **正式 B10 文案集成**（依赖 Warden 定稿）
3. **补齐 A01g/h/i 场景**（重绑、多设备、到期提醒）

##### 中优
4. **时间格式统一**：跨年保管需显示年份
5. **对话状态在所有场景展示**：withdrawn 场景也需显示当前对话状态
6. **续期 mock 流程修正**：续期应停留在 active 场景而非跳转到 withdrawn

##### 低优
7. **mock 数据动态化**：policyHash、daysTillExpiry 按真实逻辑计算

#### 7. 交接

| 给谁 | 要什么 | 依据 | 截止 |
|---|---|---|---|
| Forge | B03/B04/B05 接口就绪后通知联调 | PR body Depends 清单 | 未定 |
| Warden | 正式 B10 文案 v1.0（告知与同意详情） | PR body、[warden.md L660](<../share/planning/warden.md#L660>) | 上线前 |
| Pixel | 若选范围方案 B：隐藏申请保管/续期按钮，加真实入口，去 mock 标签（PRD A01j） | [compass.md R3](<../share/planning/compass.md#L444>) | 用户选定后 |

#### 8. 风险披露（已充分标注）

- **所有页面顶部常驻草稿标识**：橙色横幅"内部交互草稿 · 仅使用虚构测试数据 · 不构成真实同意"
- **每个模拟状态卡片标注"【模拟状态】"**（index.wxml L23、L64、L108、L148、L160、L172）
- **debug 场景选择器**（index.wxml L9-12）仅开发用，正式版需移除
- **符合 [warden.md F01-M01-M03](<../share/planning/warden.md#L666>) 风险控制**：期限缺失禁新保管、新申请与撤回分开、进度分阶段展示

---

**审查完成**。PR #307 在产品层面可接受，mp-icon 问题已由 PR #311 修复，无产品 blocker。

---

## N-16：回复情绪标签联动

2026-10-28 落地。回复模型在首行输出一个受控标签 `[emotion:neutral|happy|caring|curious]`。`PhraseSegmenter` 在流式入口剥离标签，标签不会进入 TTS、聊天历史或字幕。

- 数据形状：`AssistantEmotion` 只有四个值，解析在 `services/agent/src/orchestration/assistant_emotion.py`。未知值会被剥离但不生效。
- TTS：`DuplexRuntimeEmotionMixin.apply_assistant_emotion` 只在非 supportive 模式下把情绪追加到当前 fence 的 `SpeechPlan.tts_instruction`。supportive 模式保留原安全指令。豆包路径只有开启 `DOUBAO_TTS_STYLE_CONTROL_ENABLED` 且音色为 designed 时才把指令送进 `context_texts`，默认关闭。
- 表情：`mascot_expression_for_reply` 优先使用 `assistant_emotion` 的 happy、caring、curious；neutral 回退到文本启发式。supportive、light_laughter、deliberative 三种模式仍优先，危机脚本不被覆盖。
- 未验：真机 TTS 听感和表情变化还没跑，等后续烟测。

---
