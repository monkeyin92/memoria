# 学生第一站整改与架构优化实施计划

> 日期：2026-08-08
> 状态：IN_PROGRESS（P0、P1 仓库实现、P2/P3 本地软件链路已完成；外部合规、生产容灾、真实微信通知、真机与 P4 仍阻塞对外发布）
> 基线：runtime/H5 `20260808-171749`，源码 commit `9812fac`
> 读者：产品经理（第 1、2、9 章）、架构师（第 3、4 章）、开发（第 5～8 章工作包）
> 本计划遵循仓库既有约定：中文实施计划 + ADR + `HANDOFF.md` 状态同步 + fail-closed 门禁。
> 2026-08-09 勾选口径：`[x]` 只表示该条所写的仓库实现与本地验收已完成；生产运行、第三方送达、法务/专业评审、真机或硬件事项必须有独立证据，不能用本地测试代替。

---

## 1. 背景与结论

产品终极目标：持续陪伴、记录人生轨迹、通过采访回忆逐步"更懂你"，最终形成硅基生命（相似的回忆、说话习惯、语气）。**第一阶段市场为学生群体**（导师+陪伴+心理状况关注），后续拓展到年轻人、老人。

当前代码现实（评审结论，证据见各工作包）：

1. 记忆/人格/证据账本、全双工语音编排、发布治理为存量强项，可直接复用；
2. "学生"域为 **0 行代码**：无教学人格、无学科内容、无监护人同意、无家长端、无未成年人功能白名单；
3. 存量的声音复刻、数字分身、传承（Legacy）、自我进化对未成年账号是**合规负资产**，必须按账号类别硬隔离；
4. "心理监测"作为宣称不可上线；必须降级为"情绪陪伴 + 家长周报 + 危机固定转介"，产品文案与代码边界同步收口；
5. 单机 PostgreSQL/MinIO 无异地副本/PITR，与"记忆永续"承诺冲突，学生数据下风险更高，须前置解决。

**总路线：P0 治理止血 → P1 未成年人合规层（入场券）→ P2 学生导师域 → P3 情绪陪伴与家长端 → P4 硬件最小闭环。P1 未验收前，任何学生功能不得对外。**

---

## 2. 产品边界决策（产品经理必读，开发前冻结）

以下为本计划的**不变量**，写入 ADR 后任何实现不得违反：

### 2.1 措辞与责任边界

- 对外宣传、UI 文案、协议中**禁止**出现"监测/观测/诊断/评估心理健康"。允许的表述："情绪陪伴""成长记录""异常情绪时提醒家长并提供专业求助渠道"。
- 危机响应遵循既有原则（`services/agent/src/prompts.py:9-10` 已有危机优先规则）：不拖延、不提供伤害方法、固定转介话术优先。转介话术库必须经外部心理专业人士书面评审后才能上线（PM 负责取得评审记录，作为发布门禁证据存档于 `docs/acceptance/`）。
- 教学定位第一期收窄为两个语音原生单点：**英语口语陪练**、**作业陪伴督导（讲思路不给答案 + 番茄钟）**。数学/物理讲题等视觉主导场景明确不做（写入 6.1 不做清单）。

### 2.2 未成年账号功能白名单（fail-closed）

| 能力 | 成人账号 | 未成年账号 |
|---|---|---|
| 陪伴对话 / 记忆账本 / 情绪表情 | ✅ | ✅ |
| 导师模式（口语陪练/作业督导） | ✅（可选） | ✅ |
| 声音复刻（Voice Profile enrollment/激活） | ✅ | ❌ 硬禁止 |
| 数字分身 / Self Preview | ✅ | ❌ 硬禁止 |
| 传承 Legacy（授予或接收） | ✅ | ❌ 硬禁止 |
| 自我进化学习信号（account scope） | ✅ | ❌ 硬禁止 |
| 声纹登记 | ✅ shadow | ❌ 第一期禁止（儿童声纹=生物特征敏感信息） |
| 家长周报 | 不适用 | ✅ 默认开启，监护人可关 |

白名单必须在**服务端单点**强制（见 P1 架构），客户端隐藏入口只是体验优化、不是安全边界。

### 2.3 数据决策

- 14 岁以下：监护人**单独同意**（个保法儿童条款），无有效同意则账号只能停留在"未激活"态，不进入语音会话。14-18 岁：监护人知情同意 + 本人同意。
- 未成年人语音原始音频：默认**不留存**（转写后丢弃），转写与记忆按既有 PII 脱敏管线处理；监护人可整体导出/删除子女数据（复用既有导出/删除+墓碑传播，ADR-0007）。
- 未成年数据与成人数据同库不同 schema + 独立 RLS 角色（见 P1），物理分库列入 P4 后待办。
- 家长周报只含聚合结论（情绪趋势、学习时长、话题分布），**不含对话原文**——既保护孩子信任，也降低数据面风险。

### 2.4 合规清单（PM 跟踪，非代码但阻塞发布）

- [ ] 儿童个人信息保护影响评估（PIA）文档
- [ ] 算法备案（生成合成类）现状确认
- [ ] 隐私政策/儿童隐私政策/监护人同意书文本（法务）
- [ ] 危机转介话术库专业评审记录
- [x] `docs/media-runtime-acceptance-runbook.md:67` 的儿童语料计划已并入 P1 的 `corpus_recording` 分项同意、独立保留期、20 条/账号上限、撤销/到期/销户删除机制。
- [ ] 实际取得 200 条合规授权儿童语料及对应同意证据（代码能力不等于真实采集完成）。

---

## 3. 架构总体方案（架构师必读）

### 3.1 核心原则：复用四个既有单点，不开新范式

本计划**不新增微服务进程、不引入新中间件**（与 `docs/silicon-life-implementation-plan.md` 第 1 章边界一致）。学生域通过四个既有控制点接入：

1. **账号类别（新增）**：`subject_category: adult | minor`，落在账号档案上，是所有门禁的根输入。
2. **模式冻结（复用）**：`services/control_api/app/mode_policy.py` 已实现"服务端冻结 interaction_mode/版本/权限，浏览器不能自行升级"。新增 `tutor` 作为 companion 模式下的**会话子模式**（不新增顶层 interaction_mode，理由见 3.2）。
3. **能力门禁（复用）**：`services/control_api/app/account_gate.py` + 各路由。未成年白名单在此单点实施。
4. **话轮控制面（复用，禁止绕开）**：`services/agent/src/orchestration/utterance_router.py` 规则表。导师模式的话轮语义（如"我不会""再讲一遍"）只允许加进 Router 规则表，**禁止在 `duplex_runtime.py` 加平行 if**（`AGENTS.md` 既有铁律，本计划升级为硬门禁，见 P0）。

### 3.2 为什么 tutor 是会话子模式而不是新 interaction_mode

既有四模式（companion/self_preview/legacy/archive，`services/control_api/app/routes/session.py:231`）区分的是**证据与人格污染边界**（谁的输出能成为主人证据）。导师对话在证据语义上就是 companion：学生的话是学生的证据，进学生的记忆账本。因此：

- `interaction_mode` 保持 `companion`；
- 会话新增 `session_focus: chat | tutor_english | tutor_homework`，由 Control API 在建会话时冻结（与 mode_policy 同一冻结机制），Agent 端据此切换系统提示与 Router 规则集；
- 好处：mode_policy、history_eligible、fence、记忆写入策略全部零改动复用。

### 3.3 学生域新增模块总览

```
services/
  guardian/                # P1 新增：监护关系、同意、家长端读模型
    domain.py
    consent.py             # 同意版本、撤销、生命周期（对齐 Consent Grant 语义, CONTEXT.md）
    postgres_store.py
    postgres_schema.sql    # FORCE RLS，模式对齐 services/legacy/postgres_schema.sql
    weekly_report.py       # P3：聚合只读投影（可重建，遵循 ADR-0001 投影原则）
    tests/
  tutor/                   # P2 新增：导师域
    domain.py              # LessonTask / PracticeTurn / StudyProgress
    catalog.py             # 口语陪练任务目录（模式对齐 services/growth/tasks_catalog.py）
    prompts.py             # 导师系统提示（苏格拉底式、不给答案原则）
    progress.py            # 学习进度投影（写入走既有 EvidenceEvent → Claim 管线）
    tests/
services/control_api/app/routes/
  guardian.py              # P1 新增路由
  tutor.py                 # P2 新增路由
services/agent/src/
  tutor_session.py         # P2：session_focus 感知（提示组装、Router 规则集选择）
```

数据库：新增 `memoria_guardian` 角色 + guardian/tutor 两组表，全部 FORCE RLS + controller policy，模式照抄 evolution 的既有做法（8/8 FORCE RLS，见 `HANDOFF.md` 2026-08-08 条目）。

### 3.4 冻结与降级决策（释放产能）

- **Go StreamCore 权威迁移（A6B）冻结**：维持 `go_authoritative_allowed: false`（`architecture-status.yaml`），shadow 继续采数据但不再投入迁移工时。`multi_instance_ownership`、chaos、灰度等 A8/A9 门禁工作全部推迟到学生线 P3 验收之后。写一份 ADR 记录冻结决定与解冻条件。
- **数字分身/Legacy/进化**：功能保留（成人线资产），但停止新特性开发；只做安全修复。
- **端到端语音模型跟踪**：每季度评估一次 speech-to-speech 模型（豆包 Realtime / Qwen-Omni）对级联架构的替代可行性，作为 `docs/research/` 下的固定议题，防止级联层持续过度投资。

---

## 4. 工作包依赖与里程碑

```
P0 治理止血（1 周，立即）
  → P1 未成年人合规层（2-4 周）——学生功能的入场券，未验收不得开始 P2 对外
      → P2 学生导师域（4-6 周）
          → P3 情绪陪伴 + 家长周报 + 危机链（3-4 周）
              → 学生线首发（体验版）
P4 硬件最小闭环 + 容灾收尾（与 P2/P3 并行，独立节奏）
```

每个工作包的验收沿用既有质量门：`ruff` / `mypy services --strict` / 全量 `pytest`（coverage ≥ 87%基线不倒退）/ H5 与小程序测试 / 发布证据写入 `docs/releases/`。

---

## 5. P0：治理止血（1 周内完成，不依赖任何决策）

### 5.1 密钥与信息卫生

- [x] 将仓库根目录 `private.wx20a3a044b52fcbb7.key` 移出工作区，进入部署机 root-only 路径或 secret manager；`.gitignore` 增加 `*.key` 兜底；用 `git log --all --diff-filter=A -- '*.key'` 确认历史从未入库并记录结论。（已迁移到工作区外 0700/0600 私密目录；上传脚本改用外置默认路径/环境变量；全 Git 历史无 `*.key` 新增记录。）
- [x] `README.md` 移除生产 IP/端口/路径细节（第 9-13 行），改为指向 `docs/production-deployment.md`（该文档不随代码公开分发）。
- [x] 清理 `.coverage`、`.DS_Store` 等本地产物并入 `.gitignore`。

### 5.2 `duplex_runtime.py` 硬门禁（4748 行，只减不增）

`AGENTS.md` 已有"修打断/门禁类 bug 优先改 Router 规则表"的原则，本包把它变成机器强制：

- [x] 新增 CI 检查脚本 `scripts/check_module_budget.py`：对 `services/agent/src/duplex_runtime.py`（基线 4748）与 `services/agent/src/agent.py`（基线 3450）记录行数基线，**任何使行数超过基线的 PR 直接失败**；重构减行后自动收紧基线（写入 `architecture-status.yaml` 新增 `module_budgets` 段）。（`check` 只读拒绝增长/未登记减行，`update` 仅允许自动下调预算；CI、Makefile 与 6 条测试已接入。）
- [x] 后续所有学生域逻辑必须落在新模块（3.3 节），由该检查兜底。

### 5.3 架构冻结记录

- [x] 新增 ADR-0032《学生第一站与 Go 权威迁移冻结》：记录 3.4 节决策、解冻条件（学生线 P3 验收 + 团队产能允许）、以及"学生功能未过 P1 不得对外"的门禁。
- [x] `architecture-status.yaml` 增加并持续更新 `student_track`，当前明确为 P3 本地软件链路完成、外部发布门禁待完成。

---

## 6. P1：未成年人合规层（入场券，2-4 周）

### 6.1 账号类别与监护关系

**数据模型**（`services/guardian/domain.py` + `postgres_schema.sql`）：

```
GuardianLink:
  link_id, guardian_user_id, minor_user_id,
  relation: parent | legal_guardian,
  status: pending | active | revoked,
  verified_via: wechat_identity | manual_review,
  created_at, revoked_at
ConsentRecord:                       # 对齐 CONTEXT.md 的 Consent Grant 语义
  consent_id, link_id, consent_kind:
    minor_voice_session | memory_retention | weekly_report | corpus_recording,
  policy_version, granted_at, revoked_at, evidence_event_id
```

- [x] 实现 `GuardianLink` / `ConsentRecord`、绑定码、孩子端确认、分项授权、撤销与有效期模型；`corpus_recording` 强制 1～30 天有效期，其余同意不得伪造到期时间。
- [x] 账号档案（`services/control_api/app/database.py` 的 profile 存取处）新增 `subject_category: adult | minor` 与 `birth_year_band`（只存年龄段不存生日，最小化）。注册默认 `adult`；小程序侧提供"学生模式"开通流程写入 `minor`。
- [x] **单向棘轮**：`minor → adult` 只能在到龄 + 监护人确认后迁移；`adult → minor` 允许（家长代注册后修正），迁移同步撤销旧会话、Legacy、声音/声纹等既有能力资产与令牌。
- [x] 每条同意/撤销写入既有 Evidence Ledger（ADR-0001），保持可追溯；事件只引用 guardian 同意 ID，不误用 archive consent 外键。
- [x] 监护人身份第一期使用微信身份摘要（复用 ADR-0026 `wechat_auth.py`）+ 同意书签署记录；人脸/实名核验仍列为后续增强。

**路由**（`services/control_api/app/routes/guardian.py`）：

```
POST /v1/guardian/links                # 家长发起绑定（生成绑定码，孩子端确认）
POST /v1/guardian/links/{id}/consents  # 逐项授权（版本化）
DELETE .../consents/{id}               # 撤销（触发对应能力立即降级）
GET  /v1/guardian/minors/{id}/summary  # P3 家长周报入口
POST /v1/guardian/minors/{id}/export   # 复用既有导出管线
POST /v1/guardian/minors/{id}/delete   # 复用既有删除+墓碑传播（ADR-0007）
```

- [x] 上述绑定、确认、授权、撤销、周报、导出、删除路由均已接入；家长与孩子归属校验、幂等键、撤销后即时失效均有契约测试。

### 6.2 未成年白名单的服务端单点强制

- [x] **实施位置：`services/control_api/app/account_gate.py` 新增 `require_capability_for_subject(user, capability)`**，2.2 节表格编码为规则表（数据驱动，不写散落 if），未声明能力 fail-closed；所有私有读取或副作用均在动作前检查。接入点：

| 路由文件 | 动作 |
|---|---|
| `routes/voice.py` | minor → enrollment/激活/试听全部 403 `minor_forbidden` |
| `routes/digital_self.py`、`routes/self_preview.py` | minor → 403 |
| `routes/legacy.py` | minor 作为授予人或接收人 → 403 |
| `routes/evolution.py` | minor 账号 scope 学习信号 → 拒收（evolution 侧 `services/evolution/account_fence.py` 同步加账户类别检查，双保险） |
| `routes/speaker.py` | minor → 声纹登记 403（shadow 也不采集） |
| `routes/session.py` | minor 且无有效 `minor_voice_session` 同意 → 不签发 LiveKit token，会话创建失败并返回引导监护人授权的错误码 |

- [x] **测试要求已完成**：成人、未成年、类别缺失矩阵覆盖 Voice Profile、Digital Self、Self Preview、Legacy 双向、Evolution、Speaker、Session；另覆盖同意撤销后会话能力立即失效。代表性矩阵见 `services/control_api/tests/test_minor_capability_routes.py`。**白名单矩阵仍属发布 REJECT 级门禁。**

### 6.3 未成年数据边界

- [x] guardian/tutor 新表使用独立 `memoria_guardian` PG 角色 + FORCE RLS，并提供 forward-only 安装脚本 `scripts/upgrade_guardian_postgres.sh`；本地 schema/Compose/契约已验，生产安装另受 8.4 发布门禁约束。
- [x] 未成年账号语音原始音频默认在转写后丢弃，不进 MinIO 长存；只有匹配权威孩子话轮且存在有效 `corpus_recording` 同意时，才进入 `authorized-child-corpus` 加密对象路径。持久化事务会再次锁定并校验同意/监护关系，原子限制单账号最多 20 条有效样本；撤销、到期 worker、销户均先删对象再删元数据，已覆盖撤销/上传竞态。
- [x] 记忆提取对 minor 增加保守策略：只允许学习偏好、兴趣等低风险事实进入长期记忆；家庭矛盾、健康、身体特征等仅留当前会话，未授权时同时关闭历史、私人记忆、人格与 evolution 写入。

### 6.4 容灾前置（学生数据落库前必须完成）

- [x] 仓库已提供 PostgreSQL WAL 归档、每日 `pg_basebackup`、SHA-256 manifest、分钟级异地上传配置，以及在独立主机拉取远端 `LATEST`、`pg_verifybackup`、网络隔离恢复和业务表校验的演练脚本。
- [ ] 在真实生产远端对象存储持续运行 WAL/base backup 上传，并完成一次独立主机恢复报告；本地脚本与 Compose 校验不能替代该证据。
- [x] 仓库已提供 `memoria-archive`、`memoria-voice` 关键 bucket 的异地镜像能力，并强制拒绝本地 endpoint、要求远端 bucket 开启版本控制。
- [ ] 使用真实生产凭据完成关键 bucket 异地同步与恢复抽查。
- [x] `README.md`/宣传物料继续保留"不能宣传永不丢失"的诚实边界，并明确仓库存在脚本不代表生产异地容灾已经运行。

---

## 7. P2：学生导师域（4-6 周）

### 7.1 会话子模式 `session_focus`

- [x] `routes/session.py` 建会话请求增加 `session_focus: chat | tutor_english | tutor_homework`（默认 `chat`），与 interaction_mode 一起由服务端冻结（进 `mode_policy.py` 的冻结快照）；客户端不能在会话中途自行升级。
- [x] Agent 侧 `services/agent/src/tutor_session.py` 读取冻结的 focus：
  - 组装导师系统提示（见 7.2），**替换而非叠加** `VOICE_SYSTEM_PROMPT` 的人设段，安全段（危机响应、拒答规则）原样保留为公共段——为此先把 `prompts.py` 拆为 `SAFETY_CORE` + `COMPANION_STYLE` + `TUTOR_STYLE` 三段常量；
  - 选择 Router 规则集（见 7.3）。

### 7.2 导师人格与陪伴人格的关系

- [x] 新增导师向伙伴进入 `services/common/companions.py` 的既有 `CompanionDefinition` 结构，复用音色、欢迎语与表情链路，**零新增前端范式**。
- [x] 导师提示与轮次策略（`services/tutor/prompts.py`、`turn_policy.py`、`domain.py`）完成：
  - 作业督导：先问思路 → 引导下一步 → 只在学生两次卡住后给最小提示，**永不直接报答案**；
  - 口语陪练：跟读/情景对话/温和纠音（发音纠错以复述正确示范为主，不羞辱式指错）；
  - 番茄钟：借用 `services/growth/tasks.py` 的任务状态机模式（draft/active/paused/completed）建 `PracticeSession`，不重造轮子。

### 7.3 话轮语义扩展（只动 Router）

- [x] `services/agent/src/orchestration/utterance_router.py` 规则表新增 tutor 焦点下的 `request_hint`、`request_repeat`、`pace_control`、`give_up`；每条规则都有单测。`duplex_runtime.py` 没有新增 tutor 平行分支，P0 行数门禁继续兜底。

### 7.4 学习档案（"更懂你"在学生域的落点）

- [x] 复用 Evidence → Claim 管线：`DomainCategory` 增加 `study_progress`、`learning_preference`，规则提取与 Qwen 提示同步支持并保持 minor 保守写入策略。
- [x] `services/tutor/progress.py` 生成可重建的学习进度投影（薄弱点、练习时长、连续天数），由 tutor Evidence 事件重放恢复，供 P3 家长周报与导师开场使用。

### 7.5 内容边界

- [x] 第一批 50 个英语口语情景卡与作业督导卡已进入 `services/tutor/catalog.py`；不自建题库/课程体系，作业督导只做方法引导与学生口述题目。

### 7.6 验收

- [x] 离线 E2E（`python scripts/run_e2e.py --profile tutor`）覆盖两次卡住才给提示、"我不会"路由与 tutor 模式危机固定回复，已通过。
- [ ] 真机：小程序体验版在真实 iOS/Android 各覆盖一次完整口语陪练与一次作业督导会话。

---

## 8. P3：情绪陪伴、家长周报与危机链（3-4 周）

### 8.1 纵向情绪档案（内部，非"监测"宣称）

- [x] 数据源只消费权威 `emotion_observation` 且要求原话轮 `history_eligible=true`，归属按 generation fence 冻结，拒绝访客、歧义或迟到话轮污染。
- [x] `services/guardian/weekly_report.py` 完成可删除重建的周聚合投影：情绪分布、波动天、学习时长、话题域分布，不新增权威事实。

### 8.2 家长周报（小程序）

- [x] `GET /v1/guardian/minors/{id}/summary` 返回聚合 JSON；小程序已新增家长页，包含孩子绑定确认、分项同意、周报与危机提醒入口。
- [x] 展示只含趋势与建议，不返回或显示对话原文、原始危机文本、严重度评分。
- [ ] 家长页与危机通知的正式文案仍需 PM 按 2.1 节终审。
- [x] 孩子端已明示"每周会给爸爸妈妈一份成长小结"，不做暗中上报。

### 8.3 危机响应链（本计划最高优先级的单项）

现状：`prompts.py` 危机规则 + response planner 固定安全回复（`review.md` C7）。补齐为完整链：

1. [x] **识别**：新增 `services/agent/src/providers/crisis_semantic_classifier.py`，小模型只提供 `CRISIS / NON_CRISIS / UNSURE` 证据；有界第一人称、当前未解决话轮才进入分类，确定性规则拥有执行权，超时/非法/迟到结果 fail closed。
2. [x] **响应代码**：固定转介回复进入 response planner 安全路径，确定性危机规则优先级高于 tutor/companion；即使通知 outbox 暂时写失败也必须先返回固定安全回复，失败独立告警并保持发布 REJECT，显式与语义证据路径均有回归。
3. [ ] **响应内容外审**：当前 `crisis-transfer-draft-v1` 仍是草案，必须取得心理专业人士书面评审后才能上线。
4. [x] **通知本地链路**：危机事件写最小 Evidence、进入幂等 outbox，并在家长小程序页显示不含原文/严重度的提醒与求助渠道。
5. [ ] **真实微信送达**：取得审核通过的订阅消息模板，在生产保存可投递身份并完成真实家长账号送达演练；本地 outbox/页面提醒不等于微信订阅消息送达。
6. [x] **审计**：危机事件只记事件 ID、时间、通知状态和幂等键，不记正文、不做严重度分级。

- [x] **发布 REJECT 门禁**：危机固定回复被 tutor/companion 覆盖，或事件未进入通知 outbox，任一出现即 REJECT；规则、测试与 `README.md` 验收节已同步。真实微信送达仍是 8.4 的独立发布门禁。

### 8.4 学生线首发验收

- [x] 本地 P1 白名单矩阵、P2 tutor E2E、危机优先级/outbox 契约、小程序自动化与构建均通过。
- [ ] 完成 2.4 合规清单、危机话术专业评审和 PM 文案终审。
- [ ] 完成真实微信订阅消息送达演练、生产 PostgreSQL/对象异地备份与独立恢复报告。
- [ ] 完成真实 iOS/Android 小程序语音、口语陪练、作业督导与危机链回归。
- [ ] 取得以上证据后写入 `docs/releases/`，再创建学生线体验版；当前不得对外发布。

---

## 9. P4：硬件最小闭环与后续（并行推进，不阻塞学生线软件首发）

- [ ] **ESP32-S3 最小 demo**：配网 → 设备鉴权 → 双工语音 → 表情/LED 命令；尚无真实硬件与 AEC 测量证据，`hardware_aec` gate 保持 pending。
- [ ] 完成 200 条监护人授权儿童语料真实采集并关闭 `authorized_child_corpus_200` gate；同意、限额和删除代码已完成，但样本数仍为外部事实。
- [ ] 学生线 PMF 后再评估未成年数据物理分库、KMS、多实例；本阶段按明确不做项保持 pending。

## 10. 明确不做清单（本阶段）

1. 心理健康"监测/诊断"宣称及任何量表化评分输出；
2. 数学/物理讲题、拍题识别、自建题库；
3. 未成年人的声音复刻、声纹登记、数字分身、Legacy；
4. Go 权威迁移（A6B）及多实例/chaos/灰度投入；
5. 家长端查看对话原文；
6. 新微服务进程、新中间件、物理分库。

## 11. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 危机响应漏报/误覆盖 | 极高 | 8.3 REJECT 门禁 + 专业评审话术 + 演练记录 |
| 监护人同意流程流失率高 | 高 | 微信身份最短路径绑定；同意书分项、非一揽子 |
| 单人/小团队产能不足 | 高 | P0 冻结 Go 迁移与分身/Legacy 新特性；工作包串行不并行 |
| 级联架构被端到端模型替代 | 中 | 3.4 季度评估；学生域逻辑全部收在 provider 无关的 Router/tutor 模块 |
| 白名单绕过（新路由忘接门禁） | 中 | account_gate 单点 + 矩阵契约测试 + code review checklist 进 `AGENTS.md` |

---

## 12. 2026-08-09 本地验收记录

- [x] 后端 CI 等价 PostgreSQL 环境：`2108 passed, 2 skipped`；两项 skip 均为需显式本地 CAM++ 模型与官方 WAV 的真实 ONNX 垂直测试。总覆盖率 `87.0985%`，编排层 `90%`，provider 协议层 `97%`。
- [x] `ruff check services scripts tests`、`mypy services --strict`（281 个源文件）、`scripts/check_module_budget.py check`、`git diff --check` 均通过；`duplex_runtime.py` / `agent.py` 预算仍为 `4748 / 3450`。
- [x] tutor 离线 E2E 通过；H5 `304` 项测试和 production build 通过。
- [x] 小程序 `90` 项测试通过；Node 24 下 `upload:test --dry-run` 编译预检通过 `87` 个文件。该结果不代表真实上传、提审或发布。
- [x] guardian/tutor schema、production Compose、升级环境、备份/恢复脚本静态与合同测试通过；PostgreSQL 实库合同确认 7/7 FORCE RLS、7/7 controller policy、语料撤销后拒绝写入。
- [ ] 远端 CI、生产部署/数据库升级、真实异地恢复、微信订阅消息、登录后 H5、iOS/Android 声学与 P4 硬件均未在本轮执行。

---

*执行约定：每个工作包完成后更新 `HANDOFF.md` 与 `architecture-status.yaml` 的 `student_track` 状态；新增 ADR 从 0032 起编号；所有新表 forward-only + FORCE RLS；所有新门禁 fail-closed。*
