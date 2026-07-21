# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

## 吉祥物形象与动效

- H5 主形象保持正面朝向；设计母版位于 `design/mascot-v2/`，运行时机身位图位于 `public/assets/companions/`。
- 运行时使用“3D 位图机身 + 内联 SVG 表情骨架 + CSS 微动效”：位图保留绒毛、硅胶和光照质感，SVG 负责眼睛、眉毛和嘴形；不要为简单表情切换引入 Rive、Live2D 或 SVG 路径 morph。
- 情绪只响应当前会话中已被权威用户终稿确认的 `emotion_observation`；不要用助手字幕关键词反向驱动表情。负面声学标签统一表现为关切，不机械镜像愤怒或厌恶。
- 眨眼、呼吸、说话嘴形和胸灯应保持克制，并完整尊重 `prefers-reduced-motion`。

## 陪伴伙伴 onboarding

- 新注册用户必须先从星澜、桃喜、绵绵、阿序、玄墨中选择伙伴；使用原生 `scroll-snap` 滑卡，同时保留可见箭头、分页点和键盘操作。
- 选角页应同时提供性格简介、四种表情演示和设计音色试听；确认角色后再进入三段声纹录制，步骤切换必须回到滚动顶部。
- 声纹授权必须明确、可撤销；当前登记只生成 shadow 档案，不得在 UI 或文案中宣称已启用主人识别，也不得与声音克隆混为一谈。
- 伙伴音色优先级为：已激活的授权克隆音色 > 所选伙伴的设计音色 > 全局默认音色。浏览器和 Control API 只传稳定目录键，供应商 `voice_id` 只能由 Agent 的本地批准 registry 解析。
