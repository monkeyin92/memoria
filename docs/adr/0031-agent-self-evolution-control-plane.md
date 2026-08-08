---
status: accepted
date: 2026-08-08
---

# Agent 自我进化控制面

## 背景

第 8 章把“保存轨迹”和“真正学习”分开：一次运行只能产生不可变证据，只有独立结果/过程/质量验证、跨轨迹诊断、留出评测和可回滚发布之后，后续 Agent 行为才算发生了可证明的改进。Memoria 还必须保持主人、访客和不确定说话人的隐私边界，以及 generation fence 和工具权限边界。

## 决策

1. `services/evolution` 是唯一的进化控制面。`LearningSignal` 绑定任务族、账号范围、说话人快照、session/turn/generation/tool epoch、来源事件、三层 verdict、环境和成本；普通 archive `actual_heard` 事件不会直接变成成功或学习信号。
2. 原始轨迹仍由 archive evidence ledger 持有。独立 validator 先通过 `POST /v1/evolution/trajectory-replay-bundles` 获取服务端按 scope 脱敏的事件对，再通过 `POST /v1/evolution/trajectory-evaluations` 提交有界的结果/过程/质量证据；服务端重新加载事件对、重新计算 scope/fence，并只持久化脱敏后的 signal。owner-private、global-redacted 两种范围不能相互升级。validator 是独立部署/维护的评估器，Control API 不把 LLM judge 放进在线 Agent 请求路径。
3. 睡眠循环只按失败信号跨轨迹聚类并生成 `candidate` manifest。候选不是可执行代码；prompt 之外的 knowledge/skill/harness/parameter 候选必须由独立 runtime adapter 或正常代码发布处理。候选需要至少两条同范围失败轨迹、固定可信根和回归门禁。
4. 生命周期严格为 `candidate -> validated -> canary -> stable`，另有 `rejected/retired`。验证必须包含 failure replay、retention、transfer、safety 且每项有证据；stable 还需要至少三次独立 canary activation，旧 stable 只能由更高版本替换。回滚只能从 supersede 产生的 retired 目标恢复，并追加 lifecycle audit。
5. 运行时只解析可信根匹配、状态为 canary/stable、查询命中且账号/说话人范围匹配的 prompt 候选。新 Agent 通过 `X-Memoria-Evolution-Protocol: v1` 协商该能力；Control API 对实际注入列表签发不含原文的 generation-scoped HMAC receipt，query digest 必须使用与 archive 持久化相同的 PII 脱敏规范化文本，Agent/Archive 在同一 fence 上透传并校验它。这样候选在播放期间退休或 resolver 重启不会改变已签发的话轮快照，也不会让旧 Agent/旧 Archive 在滚动发布期间因新增字段降级。进化文本始终低于安全、隐私、权限、generation fence、工具白名单和当前明确指令。
6. 账号导出/删除覆盖 owner-private evolution signal、candidate 及其 validation/activation/lifecycle；generation receipt 只作为已归档 response provenance 的无原文签名快照，随对应 owner-private archive event 一并导出/删除。global-redacted 和 controller-only control state 保留。PostgreSQL 由独立 `memoria_evolution` 角色和强制 RLS 承载，生产配置禁止回退到 archive app DSN。
7. 发布评估使用固定 `static / append_only / evolving` 三臂，并分开报告 activation、adherence、outcome、transfer、rule replacement、retention、negative transfer、safety、device、token 和 latency。文本/回放评测不能替代真实设备证据；`scripts/evaluate_self_evolution.py --holdout-results ...` 只接受独立 evaluator 产生的无 transcript 结果包。

## 运行流程

1. 通过 `trajectory-replay-bundles` 从 canonical archive pair 生成独立 evaluator 的 bounded 输入（guest/uncertain 不含账号和 transcript），在 evaluator 外部完成结果/过程/质量判定，再提交 `trajectory-evaluations`。
2. 定时 worker 或内部 `POST /v1/evolution/sleep-cycle` 生成候选；查看 `GET /v1/evolution/candidates` 和 lifecycle events。
3. 在隔离 evaluator 中完成 failure replay、retention、transfer、safety 和设备案例，提交 validation；再由 control token 逐级推进 canary。
4. 对每个 canary/stable 终稿提交 activation telemetry。若回归，使用带理由的 `POST /v1/evolution/candidates/{id}/rollback`，并检查 lifecycle audit 和旧版本 provenance。
5. 发布前运行三臂 holdout gate；缺少设备证据、无正迁移、规则替换无改善、隐私/权限失败或保持率退化时禁止 stable。

## 未被本控制面自动推断的事实

- `actual_heard` 只证明音频确实播放，不证明任务成功、事实正确或用户满意。
- LLM/Rubric 只能提供质量维度证据，不能授予 owner 身份、主人历史、私人记忆或敏感工具权限。
- 候选写入、候选被解析和 Agent 遵循候选是三个独立指标；任一缺失都不能宣称“已经学会”。
- 本地控制面评测不代表真实手机声学、弱网、AEC、断线恢复或生产账号验收。
