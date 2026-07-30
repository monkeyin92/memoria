# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

## 吉祥物形象与动效

- H5 主形象保持正面朝向；设计母版位于 `design/mascot-v2/`，运行时机身位图位于 `public/assets/companions/`。
- 运行时使用“3D 位图机身 + 内联 SVG 表情骨架 + CSS 微动效”：位图保留绒毛、硅胶和光照质感，SVG 负责眼睛、眉毛和嘴形；不要为简单表情切换引入 Rive、Live2D 或 SVG 路径 morph。
- 用户情绪只响应当前会话中已被权威用户终稿确认的 `emotion_observation`；负面声学标签统一表现为关切，不机械镜像愤怒或厌恶。助手说话时则只消费 Agent 发布、受 `session_id / turn_id / generation_id / tool_epoch` 约束的 `assistant_expression`；不要根据助手字幕在浏览器端猜关键词，非 speaking、断线或中断时必须清除该表情。
- 眨眼、呼吸、说话嘴形和胸灯应保持克制，并完整尊重 `prefers-reduced-motion`。

## 陪伴伙伴 onboarding

- 称呼只在新账号注册时设置：字段为“怎么称呼你？”，示例为“朋友、主人、小明”；“我的”不再编辑称呼或“想让伙伴怎样陪你”。兼容的 profile 字段不等于可继续暴露的资料编辑入口。
- 新注册用户必须先从星澜、桃喜、绵绵、阿序、玄墨中选择伙伴；使用原生 `scroll-snap` 滑卡，同时保留可见箭头、分页点和键盘操作。
- 选角页应同时提供性格简介、四种表情演示和设计音色试听；确认角色后再进入自然、轻声、带笑、认真四种说话状态的声纹录制，按顺序解锁，步骤切换必须回到滚动顶部。“我的”需保留可重复录取入口；同一版本内各段作为独立声线原型参与匹配，不能重新退化为单中心平均模板。
- 声纹授权必须明确、可撤销；当前登记只生成 shadow 档案，不得在 UI 或文案中宣称已启用主人识别，也不得与声音克隆混为一谈。
- 当前豆包主链的伙伴音色优先级为：所选伙伴的豆包设计音色 > 全局默认音色。历史 CosyVoice 复刻档案保留可见、可评估和可撤销，但不得在 H5 宣称已应用或提供激活入口；待运行时重新具备兼容能力后再恢复。
- 浏览器和 Control API 只传稳定目录键，供应商 `voice_id` 只能由 Agent 的本地批准 registry 解析。试听 WAV 使用包含供应商和版本号的新路径，禁止覆盖已发布的旧资源。
