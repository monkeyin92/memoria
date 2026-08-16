# 多主体整改：PR-01~PR-18 依赖与风险表

> 依据 ADR-0033 冻结的 D-01~D-08 与 `multi-subject-path-map.md`。
> **门禁标注**：🟢=软件可完成（本仓库内可编码、测试并验收）；
> 🟡=软件可做但依赖外部数据/渠道/评审；🔴=外部或真机门禁（真机、语料、
> AEC、法务/PIA、专业危机评审、生产容灾、发布场景），软件侧不得冒充完成。
> 阶段顺序 A→F 固定；箭头表示硬依赖（前项完成标准通过后才能开始）。

## 依赖总览

```text
PR-01 ──► PR-02 ──► PR-03 ──► PR-05 ──► PR-06 ──► PR-07 ──► PR-08
  │         │         │         │         │         │         │
  │         │         ├──► PR-04        │         │         ├──► PR-13
  │         │         │                  │         │         │
  │         │         │                  ├──► PR-10 ──► PR-12 ──► PR-14
  │         │         │                  │         │
  │         │         │                  └──► PR-09 ──► PR-11
  │         │         │                           │
  │         │         └──► PR-15 ◄────────────────┘
  │         └──► PR-16（依赖 PR-03）      └──► PR-17（依赖 PR-03/05/10/12）
  └──► PR-18（依赖全部 PR 的完成标准，纯门禁）
```

## PR 明细

| PR | 阶段 | 依赖 | 主要内容 | 主要风险 | 门禁 |
| --- | --- | --- | --- | --- | --- |
| PR-01 新增 ADR 与统一枚举 | A | 无 | 12 枚举 canonical schema + Python/TS/Go/固件紧凑合同 codegen；D-01~D-08 进 ADR | 枚举值集收敛争议；生成物漂移 | 🟢（本次工作包已落地合同与 ADR；其余服务收敛在 PR-02~PR-10） |
| PR-02 主体默认值与历史识别 | A | PR-01 | `subject_category` 默认 `unknown`；`age_evidence_status`；历史 adult 进待确认；敏感能力 fail-closed；迁移 dry-run 报告 | 历史数据误标 adult 导致权限外溢 | 🟢 软件与迁移 dry-run 可做；🟡 生产库 dry-run 与回滚演练需运维窗口 |
| PR-03 设备绑定领域 | B | PR-01 | `device_binding`/`device_binding_role`；版本化 Binding Manifest；解绑/转移/失效 | 绑定版本与客户端缓存不一致 | 🟢 |
| PR-04 四类首次绑定流程 | B | PR-02, PR-03 | 小程序/App 四分流；ConsentOffer；人格与服务偏好初始化；E2E 与可回放 manifest | 客户端自行推断权限；分流 UI 语义偏差 | 🟢 流程/E2E 软件可做；🔴 小程序/App 真机验收 |
| PR-05 Relationship 与主体权限 | B | PR-03 | guardian/delegate/emergency contact/family member；邀请/接受/撤销/争议；购买者不自动获得读取权 | 关系状态机遗漏撤销/争议路径 | 🟢 |
| PR-06 Subject Resolver 与 unknown_safe | C | PR-02, PR-03, PR-05 | 声纹候选、App 选择、低置信度确认、多说话人检测、未确认模式 | 低置信度被当成成人/设备所有者；声纹服务不可用 | 🟢 确定性降级可做；🟡 真实声纹模型质量需评测 |
| PR-07 Runtime Profile 签发 | C | PR-06 | 会话级短期配置；签名与过期；断网降级；客户端/固件兼容；Profile 版本观测 | 过期 Profile 继续执行敏感副作用 | 🟢 签发/签名/降级可做；🔴 固件离线降级需真机 |
| PR-08 Session Epoch 与换人栅栏 | C | PR-07 | `epoch` 提升；换人清私有上下文；迟到事件拒绝；工具/TTS/记忆/UI 同步失效 | 连续孩子-父母-孩子说话串写/串播 | 🟢 状态机与事件拒绝可做；🔴 多成员连续会话真机验收 |
| PR-09 Robot Persona 与 Digital Self 拆分 | D | PR-01 | Persona Definition/Assignment、relationship stage；Digital Self 独立版本；禁止 Persona 定义权限 | 人格与权限耦合回归 | 🟢 |
| PR-10 Policy Engine V2 | D | PR-01, PR-02, PR-07 | PolicyContext；allow_with_obligations；policy receipt；属性/矩阵测试；敏感端点统一接入 | 规则表漏声明；receipt 丢失 | 🟢 属性测试与矩阵可做；🟡 敏感端点清单需产品确认 |
| PR-11 Persona Renderer 与身份透明事件 | D | PR-09, PR-10 | 移除绝对隐藏 AI 规则；询问/混淆/依赖事件；分龄差异化表达 | 冒充真人；每轮机械自报 | 🟢 渲染与事件可做；🟡 话术需专业评审后上线 |
| PR-12 Memory Scope 与主体归属 | E | PR-10 | private/guardian summary/family shared/legacy；co-subject；unknown speaker 禁写；receipt/evidence 引用 | 历史记忆归属不明被自动开放 | 🟢 代码与隔离测试可做；🟡 历史数据拆分需 dry-run/回滚 |
| PR-13 Tutor 主体化与可信证据 | E | PR-08, PR-12 | 学习进度绑 active subject；服务端评分；换人不串进度；家长只看聚合 | 家长对话计入孩子进度；客户端自评 | 🟢 |
| PR-14 家庭共享记忆确认 | E | PR-05, PR-12 | pending shared memory；共同主体确认；异议与撤回；管理员无无限读取权 | 一人授权全家可见 | 🟢 |
| PR-15 通知状态机与多角色收件人 | F | PR-05, PR-09 | minor guardian/adult emergency contact/senior delegate；送达回执、fallback、dead letter；最小化内容 | 危机通知失败阻塞当轮回应 | 🟢 状态机/outbox 可做；🟡 真实渠道投递回执需外部通道验收 |
| PR-16 Device Fleet 最小闭环 | F | PR-03 | 设备证书；binding version；固件能力清单；OTA 与反回滚；物理静音与隐私灯；SIM/远程失效 | 过旧固件绕过策略；隐私灯软件伪造 | 🟢 服务端清单/证书可做；🔴 固件 OTA/隐私灯/麦克风切断需硬件真机 |
| PR-17 PostgreSQL 权威迁移与真实 RLS | F | PR-03, PR-05, PR-10, PR-12 | account/person/relationship/binding/session/receipt 迁移；API/projector/worker 分角色；session-local actor/subject；跨家庭负向测试 | 迁移破坏 RLS 或绕过审计 | 🟢 代码/负向测试可做；🟡 生产迁移需停机窗口、dry-run、回滚演练 |
| PR-18 真机、容灾与分场景发布门禁 | F | 全部 PR 完成标准 | 小程序/App/硬件真机；多成员连续会话；弱网断网；异地恢复；儿童语料、AEC、通知按发布场景执行门禁 | 把研发验收冒充生产/儿童发布 | 🔴 纯外部门禁（真机、语料、AEC、法务/PIA、专业危机评审、生产容灾），按 R0~R5 分级执行 |

## 2026-08-11 本地实现状态

> 本节是当前工作区的权威状态，不改变上表的目标定义。当前改动未提交、
> 未推送、未部署，也未执行生产迁移。`pytest`、浏览器构建和本地 PostgreSQL
> 只能证明仓库软件行为，不能替代真机、真实渠道、生产容灾或发布验收。

| 范围 | 当前结论 | 尚未完成 / 明确边界 |
| --- | --- | --- |
| PR-01~PR-09、PR-11、PR-13、PR-15、PR-16 | 🟢 本地软件链路已实现；canonical 合同生成物一致，绑定/关系/主体解析/Profile/epoch/Persona/Tutor/Notification/Device Fleet 均有对应测试 | 声纹质量、固件离线/OTA/物理隐私、真实通知渠道和真机连续会话仍按各 PR 外部门禁执行 |
| PR-10 Policy Engine V2 | 🟢 仓库软件链路已实现：Policy V2、不可变 receipt、同事务 authority fence、Agent action-policy/义务执行，以及生产 HTTP `TransactionalToolEffectCommitPort`；Session Runtime 持久化 intent/outbox，并提供幂等 commit、reconcile、worker claim/complete | 当前没有可注册的通用外部业务工具，也没有第三方供应商投递 worker；持久化 intent/outbox 不能冒充外部副作用已送达。生产 token、迁移和真实业务适配器仍需按具体工具上线 |
| PR-12 Memory Scope | 🟢 仓库软件链路已实现：Control 注入真实 Policy/Identity/Session/Device/Consent/capture authority、Redis outbox dispatcher 与共享敏感写 executor；Archive 只投影服务端 canonical capture evidence，真实 PostgreSQL E2E 证明 evidence→memory item/receipt/status/outbox/audit 同事务及撤销回滚 | 尚未执行生产 schema 升级、Redis/密钥配置、线上 readiness 和历史数据拆分 dry-run；本地真实 PostgreSQL 证据不等于生产已切换 |
| PR-14 家庭共享记忆 | 🟢 仓库软件链路已实现：Control 注入生产 executor，PostgreSQL 窄函数覆盖 propose→不同主体逐人确认→自动 promotion、object/freeze、withdraw/revoke、幂等/回滚和越权负向路径 | 尚未执行生产 schema 升级、配置和真实家庭试点；管理员仍不因角色获得无限读取权 |
| PR-17 PostgreSQL/RLS | 🟢 仓库软件侧已覆盖 Identity FORCE RLS、Session subject fence、Policy actor/nullable-subject scope、Guardian/Notification/Device actor-subject scope及跨主体负向测试；新增 Archive replay audit 也进入导出、删除与跨账号隔离生命周期 | 生产备份、迁移 dry-run、停机窗口、回滚演练与线上 readiness 尚未执行 |
| PR-18 发布门禁 | 🔴 未完成 | 真机、弱网、AEC、物理断麦/隐私灯、儿童语料、法务/PIA、专业危机评审、真实通知、异地恢复均不得由本地测试替代 |

本轮关键本地证据：全部 `services/*/tests` 目录通过；所有声明真实 PostgreSQL
合同的测试文件逐文件串行通过，其中 pgvector 基准、Memory Catalog 与恢复演练在
`pgvector/pgvector:0.8.1-pg17-bookworm` 中通过，其余在 PostgreSQL 16 中通过；
strict mypy 覆盖 `380` 个源码文件。H5 `372` 项测试及 production build、小程序
`180` 项测试及 JavaScript 语法检查通过；scripts 测试、模块预算、canonical 合同
复现、Ruff、compileall 与 `git diff --check` 通过。

## 发布分级（§13.7）与软件工作包边界

| 发布级别 | 允许用户 | 本仓库软件前置 | 外部/真机前置 |
| --- | --- | --- | --- |
| R0 研发 | 内部成人开发者 | 单元/合同测试、基础隐私、测试数据 | 无 |
| R1 成人内部沙箱 | 明确知情成年人 | 成人绑定、人格/模式解耦、删除与导出 | 知情同意流程 |
| R2 受控成人试点 | 小规模成人/老人 | 设备管理、基础恢复 | 真机、真实通知、弱网 |
| R3 受控家庭试点 | 经批准家庭 | 多主体隔离、监护/代理、家庭共享 | 弱网与恢复、家庭流程评审 |
| R4 儿童正式试点 | 未成年人 | P1 白名单/门禁矩阵（ADR-0032） | 法务/PIA、专业危机评审、儿童真机/AEC/语料、真实通知、时长与现实提醒 |
| R5 规模化 | 多地区多设备 | 容量与监控 | 异地容灾、供应链、持续安全评估 |

## 验收红线（§13 属性测试，进入各 PR 完成标准）

1. `subject_category=unknown` 不能获得任何成人专属能力；
2. `active_subject` 变化使旧 policy receipt 失效；
3. `speaker_state=unconfirmed` 不能读取私人记忆；
4. guardian 关系不自动等于原始聊天读取权；
5. 任何 `persona_id` 不改变同一 PolicyContext 的权限结果；
6. 过期 Runtime Profile 不能继续执行敏感副作用；
7. 全部结果在代码层确定性成立，不依赖 LLM“自觉遵守”。
