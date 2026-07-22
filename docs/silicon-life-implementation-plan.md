# Memoria“硅基生命”建设实施计划

> 日期：2026-07-22  
> 状态：IN_PROGRESS  
> 基线：Git `98b04727b5521f9ffdd4061c3bc5a95b538d0bde`；H5-only  
> 分支：`codex/silicon-life-roadmap`

## 1. 目标与边界

目标是把现有“长期记忆 + Persona + 声纹/声音治理 + 全双工伙伴”升级为：

1. 有轻量稳定陪伴方式、但不会污染用户人格的 Companion；
2. 从空白开始、依据本人证据成长、可批准和冻结的 Digital Self；
3. 能区分事实、推断和未知，且回答可追溯的 Self Preview；
4. 能按关系和授权范围交付冻结版本的 Legacy；
5. 最终通过本地、候选服务器、公开路由与真实浏览器/设备验收后发布。

本轮明确不实现：

- 多区域、分布式 worker、Redis 协调或微服务拆分；
- KMS、异地副本、PITR 和跨机房恢复。它们保留在待办，不能因此宣称“永久不丢失”；
- 遗嘱效力、死亡认证、法院/公证流程或自动法律执行；
- 未经官方能力和真实盲测验证的“已复刻本人声音”宣传。

继续复用 PostgreSQL、Control API、Agent、H5、Evidence Ledger、MemoryCatalog、PersonaEngine、VoiceProfile 和现有发布脚本，不新增数据库、中间件或微服务。

## 2. 不变量

1. 陪伴者风格不进入账户主人的 Evidence、Persona 或 DigitalSelf manifest。
2. 只有主人权威表达、真实选择、明确确认、纠错和负面证据可以塑造 Digital Self。
3. 每个对话会话由服务端冻结模式、版本、关系与权限；浏览器不能自行升级。
4. `fact` 只能来自指定 manifest 中的已批准来源；`inference` 必须披露；证据不足必须返回 `unknown`。
5. Self Preview/Legacy 的模拟输出不作为主人新证据，也不能自动改变冻结版本。
6. Legacy 使用“冻结的主人核心 + 每位接收人独立关系外壳”；外壳不能覆盖核心事实、价值边界、声音或安全策略。
7. 账户、说话人、人物、声音和接收人继续使用不同身份空间；声纹不能单独授予高风险权限。
8. 所有新权威表必须纳入账户导出、删除传播、RLS 和审计；容灾演练本轮只记录待办。

## 3. 当前基线

| 能力 | 当前状态 | 本路线处理 |
|---|---|---|
| Evidence / Memory / Persona / Voice 治理 | 已有正式 seam、版本、来源、撤销与测试 | 复用，不复制第二套真相 |
| 五伙伴 onboarding 与批准设计音色 | 已上线 | 增加轻量 Companion Style，并修正文案/状态边界 |
| Archive 页面 | 已有时间线、检索和审核 | 正式定义为只读/审核模式 |
| Digital Self | 只有 Persona 局部版本 | 新增完整版本、manifest、批准与冻结 |
| Self Preview / Legacy | 缺失 | 按依赖顺序新增 |
| Cognitive / Decision / Relationship policy | 局部或缺失 | 新增一等领域模型，来源强制可追溯 |
| approved personal voice runtime | 明确未接入豆包主链 | 官方能力确认和盲测后独立接入 |
| 本地质量基线 | Python/H5 当前全量通过 | 关闭 85% coverage 正式门槛 |
| 当前线上 | `20260721-224804` ready | 最终以可追溯 Git commit 新发布替换 |

## 4. 默认产品决策

用户未另行指定时，本轮采用以下安全默认：

- 五个伙伴只影响温暖程度、直接程度、回复长度、提问频率和访谈深度；不改变事实判断、价值建议和隐私边界。
- Self Preview 首版只模拟“我可能会怎样表达和回答”，不替主人执行决定、工具或高风险动作。
- 系统可自动生成 `draft` DigitalSelfVersion；只有主人明确批准后才可用于 Self Preview，只有 `frozen` 版本才可用于 Legacy。
- 回答在内部按主张/自然短句标记 `fact / inference / unknown`；语音不机械朗读标签，但在推断或未知时自然披露。
- Fidelity Evaluation 的账户主人否决权最高；任何来源修正、边界变化或声音版本变化都会要求重新评测。
- Legacy 首版支持主人在世时预演、指定一个注册接收人、一个冻结版本和一个关系外壳；不自动判断死亡或法律继承。
- 接收人不能看到未授权来源正文；Legacy 审计默认只记录 ID、决策与时间，不记录完整私密回答正文。
- 仓库没有 OpenSpec，沿用现有中文实施计划、`CONTEXT.md` 和 ADR 约定，不为本轮初始化新流程目录。

## 5. 阶段与验收

### S0：现状审计、领域合同与计划

状态：COMPLETED（2026-07-22）

交付：

- [x] 核对当前 HEAD、线上 release、已有模块和真实缺口；
- [x] 固化 Companion、Digital Self、四模式、Cognitive、Legacy 等领域词汇；
- [x] ADR-0016 记录混合方案及污染边界；
- [x] 将本计划与阶段门禁同步到 `HANDOFF.md`。

验收：文档只声明已核验能力；PersonaVersion 不冒充 DigitalSelfVersion；Archive 不冒充模拟对话。

### S1：内测可信底座收口

状态：COMPLETED（2026-07-22）

纵向切片：

1. Agent 新鲜 heartbeat：release、boot ID、worker/LiveKit ready、事件循环新鲜度；失鲜后 Compose 和 Control readiness fail closed。
2. 认证会话：10–15 分钟 access token 只驻留 H5 内存；旋转 HttpOnly/Secure/SameSite refresh cookie；服务端 session hash、`jti/sid`、logout、logout-all 和吊销检查。
3. 消息幂等：客户端稳定 `client_message_id`；同账户同 ID 同 payload 重试返回原记录，不同 payload 返回 409。
4. 对象 keyring：按 `encryption_key_version` 解密，新写只使用 active key；旧 key 只读，未知版本 fail closed。
5. 合同与浏览器安全：补全 UI event schema、旧 generation fence、移除 console raw event、CSP、签名样本 URL 不入 access log。
6. 发布可信度：正式发布拒绝 dirty tree；commit、tag、artifact digest 和 release 文档一一绑定；覆盖率达到 85% 后才标记 CI 绿色。

公开 seam：`/v1/auth/*`、`POST /v1/memory/messages`、`/health/ready`/Agent health、`ObjectStore.get(ObjectRef)`、UI event JSON Schema。

阶段门禁：

- [x] 新 H5 不持久化短 access token；旧 bearer 仅在最长 24 小时迁移窗内用于
  `/auth/upgrade`，匿名账号可跨响应丢失/重载恢复；refresh 重放、logout 和 logout-all 有回归；
- [x] Agent 主循环或注册状态失鲜 60 秒内 readiness 变为 503；
- [x] 离线消息在“服务端已提交、响应丢失”场景只产生一条记录；
- [x] key 轮换后旧对象仍可读，新对象只由新 key 读取，active/read-only/跨域材料不得复用；
- [x] 浏览器 console、Nginx/Uvicorn access log 和事件合同不暴露 raw event/token；
- [x] 干净 commit/tag、source、三镜像与 H5 工件由外带 manifest/verifier 摘要绑定；
- [x] `coverage report --fail-under=85` 通过。

验收证据：正式临时 pgvector 环境 796 collected，793 passed、3 skipped、0 failed；总覆盖率
88.41%，orchestration 92%，protocol 91%。H5 全量与生产构建通过；Ruff、strict mypy、
Compose 解析、Shell/JSON、`git diff --check` 通过。Standards 与 Spec 两轮最终复审均无 P0/P1。
3 个 skip 均为需要本机 CAM++ ONNX 与官方授权 WAV 的真实模型用例，不以 mock 冒充。

明确待办：旧对象后台 rewrap、KMS、异地备份/PITR、历史第三方凭据实际轮换证明。

### S2：混合人格与四模式控制壳

状态：COMPLETED（2026-07-22）

实现：

- `InteractionMode = companion | self_preview | legacy | archive` 与服务端 `ModePolicy`；
- Companion 为默认语音模式；Archive 复用现有档案浏览/审核，不运行模拟回答；
- Self Preview/Legacy 在依赖未满足时返回明确 capability 状态，不能只靠前端隐藏；
- 会话创建时冻结 `mode + version_id + relationship_profile_id + grant_id`；
- 五伙伴映射到轻量静态 Companion Style，允许更换且不修改 Digital Self；
- onboarding 文案改为“选择陪伴方式”，分别展示“它怎样陪你”和“数字分身怎样成长”。

- Control 与 Agent 共用冻结的 `ModePolicy` 合同：会话范围字段、能力上限、`history_eligible`、
  formal-owner-only 的 `owner_projection_eligible`、shadow-owner 低敏学习原因码，以及
  assistant 生成的 generation-bound 覆盖值均由服务端 canonicalize。
- 实时语音事件强制走 `/v1/archive/session-events`；通用 `/v1/archive/events` 拒绝
  session-bound 事件，不能绕过说话人/模式门禁触发 Persona。
- H5 增加四模式能力面板和双成长线；Self Preview/Legacy 在依赖未满足时明确显示
  `建设中`，不以隐藏按钮代替服务端阻断。
- 已完成 onboarding 的用户可直接更换陪伴方式，不需重录声纹；伙伴切换只影响下一次
  会话冻结的陪伴风格，当前会话及 Digital Self 来源不变。
- canonical 用户话轮是同 session/turn/generation 的唯一父事件；assistant 已听证据和
  原始音频只能继承或绑定该父事件。缺父事件返回可重试状态，不能创建另一条主人证据。
- Persona 对 owner/uncertain 都要求显式 `persona_eligible=true`；缺失或 false 在
  Control、SQLite 与 PostgreSQL 三层 fail closed。

验收证据：正式临时 pgvector 环境 842 passed、2 skipped、0 failed，总覆盖率
89.40%；关键父事件、中断 generation 与 425 spool 定向再验收 55 passed、1 skipped。
H5 全量 `158/158` 与 production build 通过；Ruff、strict mypy、Compose 合同和
`git diff --check` 通过。
本地 in-app browser 在 390×844 与 667×375 视口均无水平溢出，控制台无 error/warn。
guest/uncertain 不得进入 owner 私有能力；shadow owner 只保留历史与低敏学习，
不获得 private memory、tools 或 owner projection；模拟输出不进入主人学习。

### S3：DigitalSelfVersion 与不可变 manifest

状态：COMPLETED（2026-07-22）

状态机：`draft -> testing -> approved -> frozen -> revoked`。

首个版本只编译已确认 Memory 与当前 PersonaVersion，随后增量接入 Cognitive、Decision、Relationship 和 Voice。manifest 使用规范 JSON、父版本、compiler/policy version、typed entries、来源摘要与 SHA-256；旧版本字节不可修改。

公开 seam：`DigitalSelfRegistry.build/get/list/approve/freeze/revoke/rollback` 与账户隔离 REST API。

实现：

- 新增 SQLite/PostgreSQL 双实现，状态转换与 rollback 都使用 manifest digest 做
  compare-and-set；账户级 advisory lock、FORCE RLS 和不可变触发器共同收口并发与越权；
- manifest 只编译来源为主人 canonical user speech 的 confirmed Memory，以及当前
  PersonaVersion 中全部来源均为主人的 confirmed trait；guest、uncertain、assistant、
  companion 输出和未确认候选全部排除；
- 生命周期审计与版本写入同事务完成，覆盖 build/testing/approve/freeze/revoke/rollback，
  记录 actor、状态、digest、目标版本和新版本；账户导出、删除传播和生命周期目录已纳入；
- H5 提供版本构建、测试、批准、冻结、撤销和 rollback，敏感转换要求当前密码；
  modal 支持 Enter、Escape、焦点恢复和 Tab/Shift+Tab 焦点圈定；
- Self Preview 在已批准版本存在后仍因 `self_preview_runtime` 未实现而 fail closed，
  不把“已批准版本”误报成完整运行时已经可用。

验收证据：

- 正式临时 PostgreSQL 17 + pgvector 环境：861 collected，859 passed、2 skipped、
  0 failed；总覆盖率 89.35%，`coverage report --fail-under=85` 通过；
- PostgreSQL 集成覆盖 NOBYPASSRLS 跨账户隔离、CAS 冲突、不可变性、rollback、
  lifecycle audit、账户导出与删除；
- H5 全量 165/165、production build、Ruff、strict mypy、Compose 合同和
  `git diff --check` 通过；
- in-app browser 在 390×844 与 667×375 无水平溢出，console error/warn 为空；
  实测空来源拒绝，以及 draft → testing → approved，密码 modal 的焦点圈定和关闭后
  焦点恢复；
- 同样输入产生同 digest；来源修正只生成新版本；跨账户读取 fail closed；未批准版本
  不能进入 Self Preview，未冻结版本不能进入 Legacy。

### S4：成长地图与学习采集

状态：COMPLETED（2026-07-22）

实现：

- 从 Evidence、Memory、Persona 和 manifest 派生覆盖度，不建立第二份事实；
- 先覆盖人生章节、重要人物、表达方式、决策案例、关系模型、声音和传承设置；
- 展示已采用来源、拒绝原因、冲突、最近变化和版本就绪度；
- 支持自然聊天、人生访谈、情景选择和决策复盘四种采集任务；
- 引导性问题降低证据权重；“不像我/我不会这样说”形成高权重负面证据。

- 成长状态使用 `尚未形成 / 已有支持 / 存在冲突` 等定性结论，不用伪精确百分比；
- 采集任务是事件溯源的短任务，`natural_chat` 在创建语音会话时冻结任务 ID，只有对应
  会话结束后才幂等完成，不能误归属到另一场对话；
- 新证据必须显式具备主人投影资格；已确认但早于新字段落地的历史主人语音，仅在已经
  形成确认投影时按普通权重兼容读取，显式 false、访客、不确定、模拟模式和伙伴输出仍
  fail closed；
- Persona 根据真实 `prompt_kind` 加权：自然表达权重最高，开放式访谈次之，结构化或
  引导性问题较弱；表达风格统计只消费自然表达；
- 负面证据同时作用于显示冲突和下一版 manifest 来源集合，即使同一话轮拆出多条 claim，
  也不能被展示去重掩盖；
- H5 提供七维成长地图、来源/拒绝原因/冲突/最近变化/版本就绪度，以及具有唯一可访问名称
  的“不像我/我不会这样说”纠正动作。

验收证据：

- 正式临时 PostgreSQL 17 + pgvector 环境全量通过，仅 2 个真实模型用例按既有边界跳过；
  总覆盖率 89.09%，`coverage report --fail-under=85` 通过；
- H5 全量 14 files / 180 tests、production build、Ruff、strict mypy 和
  `git diff --check` 通过；
- PostgreSQL 成长地图、Digital Self、Persona 和 RLS 定向集成通过；
- in-app browser 实测版本进入 testing 后批准，成长来源从“版本已就绪”在提交
  “不像我”后同步变为“存在冲突 / 版本待更新”；再次构建因无有效来源而明确拒绝，
  未生成空 manifest；
- 667×375 横屏视口无水平溢出；全新 reload 后 console 无 error/warn。

阶段结论：助手文本不贡献覆盖度；撤销或否定来源会同步降低下一版本资格；成长地图继续
只读现有权威证据，不建立第二份人物事实。

### S5：CognitiveClaim、DecisionCase 与 RelationshipProfile

状态：PENDING

实现顺序：

1. CognitiveClaim：belief/preference/value/decision_rule/red_line/uncertainty/conflict/support，含情境、反例、置信度、状态和多来源；
2. DecisionCase：情境、候选方案、约束、选择、舍弃、结果、反思和当前是否仍认同；
3. RelationshipProfile：复用 `person_entities/relationships`，只新增称呼、语气、建议方式、共享范围、禁区和批准状态。

旧 `decision_habit/value_priority` 和关系事实只能成为 candidate，不自动升级为高敏生效策略。

验收：所有生效项至少一个 owner 来源；高敏项必须本人审核且保留反例；关系画像不能授予访问权或改写人物事实。

### S6：DigitalSelfResponsePlanner 与回答来源

状态：PENDING

唯一规划接口：

```text
plan(mode, actor, version_id, query, relationship_id, speaker_decision)
  -> ResponsePlan(instructions, grounded_items, epistemic_status, voice_target)
```

现有 `ContextAssembler` 收敛为 Planner 内部 adapter，不保留平行 prompt 规则链。每个回答记录完整 generation fence、mode/version、Persona/Memory/Cognitive 来源 ID、Planner policy version、LLM/TTS model、关系与 disclosure 决策，不记录不必要正文。

验收：无来源具体个人事实 <1%；冲突或越权来源稳定进入 unknown；inference 不包装成本人亲口事实；安全规则始终高于风格。

### S7：Self Preview 与 Fidelity Evaluation

状态：PENDING

实现：

- 仅注册主人经过 step-up 后进入，并固定一个 approved DigitalSelfVersion；
- UI 持续显示“数字分身预览，不代表本人”，可展开来源；
- 支持“不像我”、纠正、负面证据、版本比较和孩子/朋友视角预演；
- holdout 评测覆盖事实、决策、关系、幽默、情绪回应、未知和隐私；
- 本人可盲选通用助手/数字分身回答，批准或否决版本。

初始内部门槛：已确认事实来源覆盖 100%、事实准确率 >=95%、无来源事实 <1%、决策一致率 >=75%、未授权泄漏 0、身份披露 100%。这些不是行业标准，需真实用户校准。

### S8：approved Voice Profile 接入实时 TTS

状态：PENDING

先以官方文档确认当前豆包个人音色登记、地域、删除、有效期和实时流兼容能力；不得仅删除现有 409。只有 consent 有效、主观盲测和质量探针均通过、未撤销/未过期且绑定当前版本的声音可解析。

运行时 voice resolution 绑定 generation fence；中途撤销、过期、超时或首包失败立即回退所选伙伴批准音色。补专有词发音、长句、情绪、弱网、打断、取消尾音、水印/反滥用测试。

验收：本人相似度/自然度均 >=4/5、违和感 <=2.5/5；无旧 generation 尾音；撤销下一 generation 生效；真实设备通过后才能标记已应用本人声音。

### S9：LegacyGrant、冻结核心与关系外壳

状态：PENDING

首版闭环：一个 owner、一个注册 grantee、一个 frozen version、一个 approved RelationshipProfile 和一个独立 shell。

- Grant 固定 `version_id + manifest_sha256 + scope + voice_allowed + activation/expiry/revocation`；
- owner 可在世预演、撤销优先；不自动判断死亡；
- 核心不可继续学习，新版本不自动替换；
- shell 只能记录激活后的 grantee 对话和互动偏好，不能写成主人生前记忆；
- 每次授权、来源读取、回答、声音选择、拒绝和撤销产生最小审计；
- 全程披露数字身份，不声称“我就是你的父亲/朋友”。

验收：跨 grantee、过期、撤销、越 scope、未 frozen、voice 未授权均 fail closed；关系外壳无法改变 manifest digest 或主人核心。

### S10：全量门禁、部署与公开验收

状态：PENDING

顺序：schema/迁移预演 -> 离线 E2E -> Provider smoke -> 隔离候选 -> runtime-first -> H5-last -> IP/域名 -> 真实浏览器/设备 -> 15 分钟观察 -> 回滚演练。

共同门禁：

```bash
uv run ruff check .
uv run mypy services --strict
uv run pytest -q
uv run pytest --cov=services --cov-report=term-missing
uv run coverage report --fail-under=85
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
uv run python scripts/run_e2e.py --profile offline
git diff --check
```

最终逐项验证：

- IP 与域名的 root、H5、SPA、live、ready、静态资源和负向 internal 路由；
- Companion/Archive/Self Preview/Legacy 正向与越权负例；
- manifest digest、事实/推断/未知、撤销即时生效、模拟不反哺；
- approved personal voice 的真机主观验收与安全回退；
- release 对应干净 Git commit/tag 与不可变 artifact digest；
- WMS 和同机既有路由不受影响，回滚命令实际可执行。

## 6. 阶段记录规则

每个阶段开始时，把状态改为 `IN_PROGRESS` 并写明 seam、成功标准和最小验证；完成时只勾选有执行证据的项目，更新 `HANDOFF.md` 和 release 文档。mock、配置入口、HTTP 200 或 readiness 不能代替真实设备、真实 Provider 或主观 Fidelity/Voice 验收。
