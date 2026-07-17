# Memoria H5 Design QA

日期：2026-07-16

## Findings

- 当前正式版没有剩余可执行的 P0、P1 或 P2 设计问题。
- 原始参考图右侧挂环未进入成品，这是用户明确要求的预期差异；主体轮廓、绒球、眼睛、嘴、星点、胸灯、深蓝与暖白材质方向保持一致。
- 当前轮次的应用内浏览器截图接口超时，但不构成视觉证据缺失：已有同一公网 H5 的 390 × 720 浏览器截图；服务器逐项核对确认前一截图版本与正式版 `20260716-004139` 的主 CSS 和四种情绪 PNG/WebP 共 8 个资源哈希完全相同；当前正式版又在 390 × 844 完成了实时 DOM 与交互复验。

## 对比目标与状态

- 原始美术参考：`/Users/monkeyin/.codex/attachments/01a21954-0f9a-4550-8c17-7826a0868ad7/image-1.png`
- 去挂环后的生产视觉目标：`/Users/monkeyin/projects/memoria/apps/h5/public/assets/mascot-neutral.webp`
- 正式首页截图：`/Users/monkeyin/projects/memoria/apps/h5/qa/production-home-final-390x720.png`
- 正式实时会话截图：`/Users/monkeyin/projects/memoria/apps/h5/qa/production-live-connected-390x720.png`
- 正式回顾空状态：`/Users/monkeyin/projects/memoria/apps/h5/qa/production-memory-empty-390x720.jpg`
- 正式个人页：`/Users/monkeyin/projects/memoria/apps/h5/qa/production-profile-390x720.jpg`
- 正式版最新浏览器证据：`/Users/monkeyin/projects/memoria/apps/h5/qa/production-20260716-004139/browser-evidence.md`
- 截图视口：390 × 720；最新正式交互视口：390 × 844。
- 首页状态：中性表情、未开始对话；会话状态：明确 ready 后进入监听；个人页偏好为 `true / true / false`。

## 视觉证据

- 全图同屏对比：`/Users/monkeyin/projects/memoria/apps/h5/qa/source-vs-production-home-final.png`
  - 左侧为原始参考，右侧为生产首页。挂环移除是明确要求，不作为漂移。
  - 页面保留足够呼吸感，吉祥物在移动画布中仍是第一视觉焦点；标题、提示、说明与固定导航没有挤压主体。
- 吉祥物聚焦对比：`/Users/monkeyin/projects/memoria/apps/h5/qa/source-vs-production-mascot-focus.png`
  - 可清楚检查眼睛高光、嘴形、外壳纹理、绒球、星点与胸灯；没有拉伸、透明边缘光晕或压缩破损。
- 情绪资源对比：`/Users/monkeyin/projects/memoria/apps/h5/qa/emotion-assets-contact-sheet.png`
  - neutral、happy、curious、upset 四种资源保留同一角色轮廓、材质与视角，只改变眼、嘴和胸灯色彩，避免表情切换时角色跳变。

## Required Fidelity Surfaces

| 表面 | 结果 | 证据 |
| --- | --- | --- |
| Fonts / typography | 通过 | 本地 `Noto Sans SC Variable` 搭配系统中文回退；标题、正文、标签层级稳定，390 × 720 与 390 × 844 均无异常换行或截断。原始参考不含产品文字样式，因此以独立可读性和层级一致性为准。 |
| Spacing / layout rhythm | 通过 | 首页主体、说明文案和固定导航分区清楚；390 × 844 的 `clientWidth=scrollWidth=390`，三页无横向溢出。 |
| Colors / tokens | 通过 | 深蓝、奶油白、雾蓝与参考美术一致；共享辅助文字色在代表性浅色背景上达到普通文字至少 4.5:1。 |
| Image quality / asset fidelity | 通过 | 使用真实 PNG/WebP 吉祥物资源，不以 CSS、emoji、占位图或手绘 SVG 替代；四种情绪资源清晰且同构，右侧挂环按要求删除。 |
| Copy / content | 通过 | “今天想聊点什么？”、“每天多懂自己一点”、“你的陪伴空间”等文案可独立理解，语气统一，没有提示词或实现说明泄漏。 |
| Icons / controls | 通过 | 可见图标统一来自 Phosphor；主导航、摘要、资料和语音控制保持同一笔画体系与状态语言。 |
| Responsive | 通过 | 390 × 720 视觉截图与 390 × 844 正式 DOM 都无横向溢出；关键控制在较短画布中仍可达。 |
| Accessibility | 通过 | 主导航含当前项语义；偏好使用 `role="switch"` / `aria-checked`；日期使用 `aria-pressed`；吉祥物与图标按钮有可读名称，并支持 `prefers-reduced-motion`。 |

## 正式浏览器交互、Console 与 DOM 证据

- 点击吉祥物后先显示 `正在靠近你…` 和禁用的 `正在连接`，没有把 LiveKit transport 或 participant 占位误当作 ready。
- 收到当前 Agent 显式 `assistant_state: ready` 后才进入 `我在认真听`，并出现静音、停止回答、结束对话三个控制；远端音频元素处于播放状态。
- 静音后按钮变为 `打开麦克风`，恢复后变回 `关闭麦克风`；停止回答没有制造陈旧 speaking/thinking 状态。
- 结束对话后显示 `今天先聊到这里`，控制区消失；3 秒后音频节点数为 0，旧连接没有重新开麦。
- 已验证陪伴、回顾、我的三页导航；回顾空状态、个人资料编辑打开/取消、偏好语义与隐私入口均可达。
- 390 × 844：`documentClientWidth=390`、`documentScrollWidth=390`；console warning 0、console error 0。
- 正式 session 没有用户语音，因此没有强行生成空回顾；Qwen 汇总由生产 Provider smoke 与后端自动化覆盖。

## Comparison History

### Iteration 1 — 初始视觉对比

- 全图与聚焦对比未发现吉祥物、构图、图片质量、颜色方向或文案的 P0/P1/P2 漂移。
- 发现 P2：多处 8–9px 辅助文字使用 2.82:1–3.76:1 的浅灰色，对普通小字号文字不足。
- 发现 P2：个人页偏好开关与回顾页日期选项只有视觉状态，未向辅助技术暴露当前值。
- 发现 P2：`today()` 使用 UTC `toISOString()`，北京时间 00:00–07:59 可能与后端 `Asia/Shanghai` 日期错位。

### Iteration 2 — 修复与复验

- 将浅色表面辅助文字统一到 `--muted: #5f6c83`，代表性背景对比度达到至少 4.5:1。
- 偏好按钮增加 `role="switch"` / `aria-checked`；日期增加命名 group 与 `aria-pressed`。
- 新增 `localDateKey()`，用浏览器本地年月日生成 `YYYY-MM-DD`，并覆盖 UTC 日界线回归。
- 修复后的 390 × 720 浏览器截图、聚焦对比、Console 与无溢出检查均通过，没有引入新的 P0/P1/P2。

### Iteration 3 — 正式版 20260716-004139

- 服务器核对前后两版主 CSS 与四种情绪 PNG/WebP 共 8 个资源哈希完全一致，视觉资产无漂移。
- 在正式 URL 以 390 × 844 重新验证连接门禁、真实 ready、远端音频、静音/恢复、停止回答、结束清理和三页导航。
- 当前正式版 console warning 0、console error 0，横向溢出为 0；没有新的 P0/P1/P2。
- 自动验证：H5 6 个测试文件、38/38 通过；`useVoiceSession` 18/18；production build 通过且公网、本地、服务器关键工件哈希一致。

## Implementation Checklist

- [x] 原图、生产实现、全图对比、聚焦对比与四情绪资源均实际打开检查。
- [x] Typography、spacing、colors、image、copy、responsive 与 accessibility 逐项检查。
- [x] 早期两项无障碍 P2 与一项本地日期 P2 已修复并复验。
- [x] 正式版显式 ready 门禁、远端音频、静音、停止回答、结束清理和三页导航已验证。
- [x] Console、DOM overflow、H5 38/38、hook 18/18 与 production build 已验证。

## Follow-up Polish

- 无阻塞交付的 P3 视觉项。进入规模化上线前仍建议补 200 条真实中文录音与完整 AEC 设备矩阵；它们不改变当前设计 QA 结论。

final result: passed
