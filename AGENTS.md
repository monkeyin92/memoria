# Agent Working Agreements

Durable instructions for anyone (human or AI) working on Memoria. Update this file when the product owner gives lasting process or design preferences.

## 举一反三（必守）

**触发**：任何 bug、线上异常、产品体验问题，或需要「新思路」的优化。

**要求**：

1. **不要只做点修**。先修当前症状，但同一轮必须多想一层：同类控制路径是否会再次相撞？缺的是阈值、补丁，还是控制面抽象？
2. **主动提出架构级改进**，不要等用户点名。例：用户一路修 enroll / 打断 / 停一下 / 噪声 / 继续 / 纯打断词进 chat，本质是「用户话轮意图」没有统一入口——应主动提出 `UtteranceRouter`（或等价控制平面），而不是等对方问「是不是应该做路由」。
3. **举一反三清单**（修完一个点后快速过一遍）：
   - 同文件、同状态机、同 fence/gate 上是否还有对称分支会踩坑？
   - 成功路径修好后，失败 / 超时 / 空音频 / 误识别 / 迟到事件是否仍坏？
   - 级联与 Omni 两条链路是否同类问题？
   - 是配置/阈值可调，还是规则表 / 状态机 / 单一决策点才能防复发？
   - 磁盘、日志、发布、测试门禁等运维面是否同类隐患？
4. **有更好思路就先说清楚再动手**。用户要的是可讨论的方案深度，不是默默堆 if-else。

**反例（本次会话教训）**：连续修「停一下→我继续」「等等→怎么了」等打断语义时，一直在 `duplex_runtime` / `interruption_guard` 上补丁；用户主动问路由后才收敛到统一话轮路由。以后类似控制路径碰撞，应主动抬升到统一决策层。

## 控制面：UtteranceRouter

- 实现：`services/agent/src/orchestration/utterance_router.py`
- 单测：`services/agent/tests/unit/test_utterance_router.py`
- 接线：`DuplexRuntime.accept_user_turn` 与 `on_real_interrupt` 共用 `route_utterance`（enroll / pure interrupt / interrupt+chat / chat）。
- 用户资料中的 `reject_non_owner_voice` 是 TargetSpeakerFocus 的实验性产品策略开关，默认开启；开启时只拒绝 formal `guest/owner_mismatch` 与明确的 `shadow_guest_candidate`。shadow/formal ambiguous 为避免误静音主人仍可普通对话，但保持 non-owner/uncertain，不能进入主人历史、私人记忆、工具或敏感权限；关闭后访客可以对话，但不得因此升级 `owner` 权限。
- H5 长期历史只能消费 Agent 权威终稿中的 `history_eligible=true`；访客、ambiguous、无档案或 authority 不可用的话轮及其对应 AI 回复不得落入主人的回顾/自动摘要。资格必须按 generation fence 绑定，不能读取“当前最新说话人”代替原话轮归属。
- **修打断/门禁类 bug 时，优先改 Router 规则表 + 单测**，不要在 `duplex_runtime` 再开平行 if。
- 播放期 noise / backchannel / echo 仍由 `PlaybackInputGuard` 处理；若与 Router 意图冲突，再考虑并入（举一反三）。

## 其他

- H5 原型视觉约定见 `apps/h5/AGENTS.md`。
- 发布、回滚、线上状态见 `HANDOFF.md` 与 `docs/releases/`。
- 架构规范见 `full_duplex_voice_agent_architecture_zh.md`。
