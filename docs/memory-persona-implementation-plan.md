# Memoria 终身记忆、人格复刻与声纹系统实施计划

> 更新时间：2026-07-21
> 当前阶段：唯一客户端为 `apps/h5`；P0.5、P1～P6 已部署；授权真人样本、真实手机矩阵和异地容灾仍待完成
> 原则：每阶段都是可运行的纵向切片；完成后更新本文件与 `HANDOFF.md`，运行该阶段门禁，再向产品负责人报告。

## 1. 最终完成定义

“全部完成”分为两层，避免把本地代码完成误报为生产能力完成：

### 1.1 工程完成

- [x] 级联主链持续可用，实时打断、实际已听文本和旧 generation 隔离无回归。
- [x] 注册/登录可恢复稳定 `user_id`，且账号身份与说话人身份、人物实体严格分离。
- [x] 证据账本、人物/关系/时间线/知识、人格、声纹和声音档案均有正式数据模型、API、权限和自动化测试；CAM++ 模型服务已纳入本地纵向合同。
- [x] 现有 SQLite 数据可幂等迁移到 PostgreSQL，失败可回滚；无法证明为 owner 的 legacy user 证据保持 `uncertain`，不污染主人上下文。
- [x] H5 能查看时间线、搜索并审核人生记忆候选/冲突、管理声纹、已生效人格、声音档案和原始语音授权；Persona 内部候选不要求客户逐条查看或确认。
- [x] 导出、纠错、撤销、删除、备份和恢复路径可执行。

### 1.2 生产验收

- [ ] 使用至少 200 条明确授权的真人录音覆盖主人、访客、未知人、噪声、重叠、远场和回放攻击。
- [ ] 完成 FAR、FRR、EER、unknown rejection、ASR/人物抽取和检索命中率报告。
- [ ] 完成 CosyVoice 3.5 复刻音色的授权登记与真人盲测。
- [ ] 在真实手机 H5 浏览器、耳机和扬声器完成全双工、权限与记忆污染验收；原生 iOS 客户端已从仓库移除。
- [ ] 完成独立环境数据库、对象、密钥和投影全量恢复演练。

已完成同机生产 PostgreSQL/pgvector、MinIO 部署、376 条事件迁移与联合恢复演练；由于数据库、对象、WAL archive 和备份仍在同一台服务器，不能替代上述独立环境/异地恢复门槛。

没有真实授权样本、生产 PostgreSQL/对象存储/KMS 或部署授权时，可以完成工程层，但不能宣称生产验收完成。

## 2. 已确认技术路线

- 实时主链：FunASR Realtime + Qwen LLM + 豆包 Seed-TTS 2.0 双向流式；历史 CosyVoice 3.5 只保留复刻资产治理，不进入当前播放主链。
- 证据真相：追加式 Evidence Event；聊天、人物、时间线、知识和人格是投影。
- 生产长期底座：PostgreSQL + pgvector；大对象：S3/OSS；Redis 仅短期状态。
- 身份控制：`owner / guest / uncertain`，普通对话与私人权限分开。
- 声纹、合成声音、敏感动作授权相互隔离。
- 不新增 Neo4j、Elasticsearch、独立向量库、Kafka。

## 3. 阶段总览

| 阶段 | 状态 | 主要可验收结果 |
|---|---|---|
| P0 领域与架构 | **COMPLETED（2026-07-18）** | 词汇、总体架构、3 份 ADR、公开 seam、阶段门禁 |
| P0.5 稳定账号身份 | **COMPLETED（2026-07-19，本地工程）** | 注册/登录、匿名身份原地升级、H5 账号门、跨账号本地隔离 |
| P1 证据账本与底座 | **COMPLETED（2026-07-19，本地工程）** | 每个可信话轮可幂等落账；SQLite 可迁移；可备份恢复 |
| P2 说话人权限 | **COMPLETED（2026-07-19，本地工程）** | owner/guest/uncertain 三态、可运行 CAM++ ONNX 服务、shadow 与 readiness 版本门禁 |
| P3 人生知识库 | **COMPLETED（2026-07-19，本地工程）** | 人物、关系、时间线、经验知识、审核、冲突、全文/结构检索 |
| P4 人格复刻 | **COMPLETED（2026-07-19，本地工程）** | 风格/价值观版本化；PersonaCapsule 接入实时 Qwen；豆包当前不发送会破坏字幕对齐的 `context_texts` |
| P5 声音复刻治理 | **COMPLETED（2026-07-19，本地工程）** | CosyVoice 3.5 登记、授权、版本、激活、撤销和 H5 管理 |
| P6 全链收口 | **COMPLETED（2026-07-19，本地工程）** | P6.1～P6.4 本地工程门禁完成；真实 Provider、真人样本、生产基础设施和设备验收待外部条件 |
| 生产平台发布 | **DEPLOYED（首次平台版 `20260719-215553`；当前版本见 `HANDOFF.md`）** | H5-only、PostgreSQL/pgvector、MinIO、CAM++、Provider/readiness、公网与回滚门禁通过；真人/异地门槛未完成 |

## 4. P0：领域、架构与契约

**状态：COMPLETED（2026-07-18）**

### 交付物

- [x] 根目录 `CONTEXT.md`，统一说话人、人物、记忆、人格、声纹和声音术语。
- [x] `docs/memory-persona-architecture-v1.md`。
- [x] ADR-0001：证据账本与可重建投影。
- [x] ADR-0002：PostgreSQL + 对象存储的最小多底座。
- [x] ADR-0003：声纹、合成声音和动作授权隔离。
- [x] ADR-0010：固定 CAM++ 供应链、shadow-only 与 anti-spoof/readiness 边界。
- [x] 拟定 `LifeArchive`、`SpeakerAuthority`、`PersonaEngine` 与 Control API/H5 公开 seam。
- [x] 明确“长期可靠保存”而非绝对“永不丢失”的产品边界。

### 验收

- [x] 文档内本地链接均存在。
- [x] 领域词汇不包含数据库、队列或供应商实现细节。
- [x] 架构包含数据归属、权限矩阵、失败降级、迁移和恢复策略。
- [x] 产品负责人确认公开 seam 后进入 TDD 红绿循环。

## 4.5 P0.5：账号注册、登录与稳定身份

**状态：COMPLETED（2026-07-19，本地工程）**

### 交付物

- [x] `accounts` 表以稳定 `user_id` 绑定规范化唯一用户名和 scrypt 密码哈希。
- [x] `POST /v1/auth/register`、`POST /v1/auth/login`、`GET /v1/auth/me`。
- [x] 旧匿名 Bearer 注册时原地升级，原消息和 Profile 继续归属同一 `user_id`。
- [x] H5 账号门、注册/登录切换、密码显隐、表单错误和提交状态。
- [x] 身份、离线消息和 Profile 缓存按 `user_id` 分键，禁止跨账号串数据。
- [x] Nginx 为注册/登录设置独立 `4k` 请求体和 `memoria_session burst=3` 限流。
- [x] ADR-0004：稳定账号身份与匿名升级。

### 完成门禁

- [x] 注册、规范化用户名冲突、统一登录错误、匿名升级和跨账号隔离自动化测试通过。
- [x] 临时 SQLite 上注册、重复注册、Control API 重建、旧 token 恢复和再次登录保持同一 `user_id`；聊天、声纹档案和声音复刻授权跨重启仍归属该账号。
- [x] SQLite 只保存 `scrypt$` 哈希，明文密码命中数为 0。
- [x] H5 在 375px、横屏、密码切换、冲突、登录、刷新和服务重启恢复上通过真实浏览器验收，console 为 0 warning/error。
- [x] Python/H5 全量测试、Ruff、mypy 与 H5 production build 最终复跑通过。

### 明确不在本阶段

- 找回/重置密码、邮箱或手机号验证、MFA/Passkey、跨设备会话管理和生产部署；需要产品与安全策略后单独实施。

## 5. P1：不可变证据账本、迁移与恢复

**状态：COMPLETED（2026-07-19，本地工程；已随 `20260719-215553` 部署）**

### 纵向切片

1. **P1.1 账本最小闭环**
   - 新增领域类型 `EvidenceEvent`、`RecordResult`、`ContextQuery`、`ContextBundle`。
   - 新增 `LifeArchive.record()`，先提供 SQLite 测试适配器与 PostgreSQL 生产适配器。
   - `event_id` 唯一约束；重复事件返回已有结果，不重复产生 outbox。

2. **P1.2 可信话轮接线**
   - 级联用户 final 门禁通过后写 `speech.utterance_finalized`。
   - `HeardTextTracker` 只把实际听到文本写 `assistant.playout_*`。
   - 保留 `session_id / turn_id / generation_id / speaker_class`。
   - 记录失败不得阻塞实时回答；使用有界、加密、可重放 spool。

3. **P1.3 PostgreSQL 与对象存储**
   - schema：`evidence_events`、`evidence_blobs`、`transcript_versions`、`processing_outbox`、`consent_grants`。
   - 大对象通过对象适配器保存，数据库只保存引用、哈希和密钥版本。
   - 开发环境允许本地对象目录；生产要求 S3/OSS 配置。

4. **P1.4 SQLite 迁移**
   - 现有 `messages` 转为确定性事件 ID；`daily_summaries` 作为 legacy projection，不冒充证据。
   - dry-run、数量/哈希对账、重复运行、失败回滚。

5. **P1.5 备份与恢复**
   - PostgreSQL PITR、逻辑备份、对象清单与 SHA-256 校验 runbook。
   - 在空环境执行一次自动化恢复与投影重建。

### 完成门禁

- [x] `LifeArchive.record/context/review` seam 测试通过。
- [x] 同一事件提交 100 次只产生一条账本记录和一条 outbox。
- [x] 用户/助手可信话轮均可从 API 读回，未听助手后缀为 0。
- [x] 迁移重复执行结果一致；旧库不被改写。
- [x] 数据库不可用时实时对话降级，spool 有容量和告警上限。
- [x] SQLite 空环境恢复、对象明文 SHA 校验以及 PostgreSQL 17 逻辑备份空库恢复一致。

### 本地工程验收证据

- 全量 Python 测试通过；PostgreSQL 合同与 SQLite → PostgreSQL 原子迁移在临时 PostgreSQL 17 上 `2 passed`。
- 临时 PostgreSQL 执行 `pg_dump --format=custom` 后，在全新数据库使用 `pg_restore --single-transaction` 恢复；源库与恢复库均为 1 条事件、1 条 outbox，按 `event_id:content_sha256` 排序的清单完全一致。
- `EncryptedLocalObjectStore` 与 S3-compatible 适配器覆盖认证加密、明文 SHA-256、字节数、删除和篡改拒绝；SQLite 备份恢复覆盖 manifest、integrity check 与计数核对。
- 生产 PITR、异地对象副本和 KMS 联合演练按 [`archive-backup-restore-runbook.md`](./archive-backup-restore-runbook.md) 执行，属于获得生产基础设施与部署授权后的生产验收项。

## 6. P2：SpeakerAuthority 三态与正式声纹

**状态：COMPLETED（2026-07-19，本地工程；已随 `20260719-215553` 部署；shadow-only）**

### 纵向切片

1. **P2.1 三态领域控制面**
   - 新增 `SpeakerDecision(owner/guest/uncertain)`，替换二元 accepted/mismatch 业务语义。
   - 将 `UtteranceRouter` 的 enroll/interrupt/chat 规则与 speaker 权限合并到单一决策点。
   - Profile 的 `reject_non_owner_voice` 默认开启：formal guest/owner mismatch 与明确的 shadow guest 默认拒绝；shadow/formal ambiguous 为避免误静音主人可普通对话，但私人记忆、主人历史和敏感动作权限不升级。用户关闭后可放开访客交互，权限边界不变。

2. **P2.2 正式 embedding 适配器**
   - 评估并固定 CAM++、ERes2NetV2 或等价 ONNX 模型及许可证。
   - `SpeakerEmbeddingAdapter` 只做模型边界；生产和测试各一个适配器。
   - 首先 shadow 运行，不直接改变话轮行为。

3. **P2.3 登记、模板与撤销**
   - 多样本质检、授权、独立加密域、模板版本和模型版本。
   - 新模板 shadow → 评估 → 激活；撤销立即停止使用并进入删除流程。

4. **P2.4 反重放与 step-up**
   - 增加音频质量、回放/合成风险和设备上下文。
   - 高风险动作使用设备解锁、Passkey 或主人确认，声纹只作信号。

### 完成门禁

- [x] `SpeakerAuthority.enroll/classify/revoke` seam 测试通过。
- [x] owner、guest、uncertain、短话、低 SNR、维度错误、重放风险和模型超时均有确定性结果。
- [x] 默认“过滤明显旁人（实验）”时，formal guest/owner mismatch 与明确的 shadow guest 不能提交普通话轮或控制播放；ambiguous 可对话但无主人历史/私人权限，关闭后访客可对话且权限仍不升级。
- [x] `history_eligible` 按 `(turn_id, generation_id)` 冻结；只有 formal owner / shadow owner 的双方终稿进入主人历史，缺失字段 fail-closed。
- [x] fail-open owner 路径为 0；低质量、无模型、无模板和超时统一为 uncertain。
- [x] 激活接口强制不少于 200 条样本且要求 FAR/FRR/EER/unknown rejection 报告明确 `passed`；真实授权样本报告仍属于生产验收，未凭空生成。

### 本地工程验收证据

- CAM++/3D-Speaker 使用独立 `speaker-model` HTTP embedding 契约；固定 ONNX digest、模型版本、维度、质量、SNR、有效语音和重放风险全部 fail-closed。
- Control API `/health/ready` 实时探测 `speaker-model/health/ready`，HTTP 非 200、`status` 非 `ready` 或 `model_version` 漂移均返回 503；模型未配置的开发/离线环境明确显示 `skipped`。
- CAM++ 不带 anti-spoof head，`risk_assessment=unavailable`、replay/synthetic sentinel 为 `1.0`；所有 shadow 结论保持 `uncertain`，不开放生产 owner 权限。
- SQLite 与 PostgreSQL 适配器均加密模板；原始登记 PCM 不入库，只保存样本哈希与质量元数据；撤销后模板密文为 `NULL`。
- PostgreSQL 17 上声纹合同与 P1 账本合同合计 `3 passed`；声纹表启用并强制 RLS，模板版本和 active 唯一约束由数据库保证。
- Agent 在 ASR final 前并行发起 session-scoped 分类，Control API 服务端解析账户；默认策略下明确 guest 被 Router 拒绝，ambiguous/无档案或用户关闭过滤时可聊天，但均关闭私人检索、长期学习和敏感动作。
- 旧 log-mel `OPEN` 已移除，只保留远场媒体/微噪声守卫，不能授予 owner 权限或因声音不同静音真实访客。

## 7. P3：人物、关系、时间线与经验知识

**状态：COMPLETED（2026-07-19，本地工程；人工标注集待外部验收）**

### 纵向切片

1. **P3.1 记忆声明**
   - `memory_claims` 支持 `candidate / confirmed / disputed / retracted`。
   - 每条声明有来源、有效时间、置信度、抽取器版本和敏感域。

2. **P3.2 人物与关系**
   - 人物别名、关系方向、属性声明和重复人物合并。
   - 被谈及人物与实际说话人使用不同 ID 空间。

3. **P3.3 时间线与人生情节**
   - 按事件时间而非写入时间组织；支持时间范围不确定。
   - 将多轮对话合并为人生情节，但保留每条来源。

4. **P3.4 经验知识**
   - 形成故事、经验问答、工作方法、家风家训、育儿理念和处世原则。
   - 记录适用条件、反例和来源，不生成脱离经历的通用鸡汤。

5. **P3.5 审核与混合检索**
   - 待确认、冲突、纠错、撤销流程。
   - PostgreSQL 全文 + pgvector + 结构/时间过滤；向量可完全重建。

### 完成门禁

- [x] 同名不同人、同人多别名、年龄变化、关系变化、互相冲突均有回归。
- [x] 每条展示结果保留 `source_event_id`，孤立无来源结论为 0。
- [x] guest/uncertain/assistant 数据默认不进入主人投影或搜索。
- [x] 用户撤销后当前全文检索和实时上下文立即不再返回；向量投影为可重建派生表，启用时沿用同一状态过滤。
- [ ] 人工标注集报告人物链接、关系和知识抽取 precision/recall。

### 本地工程验收证据

- SQLite `MemoryCatalog` 已实现 outbox 幂等编译、失败重试、人物/别名/关系、人生情节、时间线、知识、冲突队列、审核和撤销传播。
- Qwen 严格 JSON 抽取为主，模型响应通过结构与实体引用校验；超时或非法结果才回退保守中文规则，所有模型结论默认 `candidate`。
- Control API 已提供 `/v1/archive/search`、`/review-queue`、`/memories/{id}/review`、`/people` 和 `/life-timeline`；旧 `/timeline` 继续承载原始证据，避免破坏既有对话记录契约。
- PostgreSQL 17 合同验证中，P1/P2/P3 合计 `4 passed`；P3 投影表全部启用并强制 RLS，全文 GIN 索引和无 pgvector 开发环境降级通过。
- 全量 Python 测试通过（PostgreSQL 条件合同在普通本地运行中跳过）；Ruff、mypy 和 `git diff --check` 通过。
- 未完成项仅是需要授权数据或生产基础设施的验收：人工标注 precision/recall、生产 pgvector/embedding 命中率、真实负载和部署验证。

## 8. P4：PersonaEngine 与实时人格胶囊

**状态：COMPLETED（2026-07-19，本地工程；真人 A/B 待授权样本）**

### 纵向切片

1. **P4.1 表达统计**
   - 口头禅、句长、语速、停顿、措辞和叙事结构按场景聚合。
   - 排除助手、合成音频、访客、回声、低质量和未授权样本。

2. **P4.2 特征抽取与稳定门槛**
   - 每条特征保存情境、证据、反例、置信度和有效期。
   - 单轮观察只进入 candidate；owner 低敏风格至少 3 次可信观察自动发布。
   - shadow-only uncertain 只接受同一 `shadow_owner_candidate` profile 的合格文本，至少 6 次、跨 3 个非空 session 后自动发布；不采纳声学语速/停顿指标。
   - 互斥句长/语速/停顿 bucket 采用 2:1 主导门槛且同类别最多一个 confirmed；不同 shadow profile 不合并，声纹轮换后新 profile 独立累计；价值与决策特征不自动发布。

3. **P4.3 自动版本与用户控制**
   - 稳定低敏特征自动生成可解释 Persona 版本；candidate 不进入客户默认界面。
   - 支持纠正、sticky 禁用和回滚；review API 保留为兼容 seam，不是日常确认步骤。

4. **P4.4 PersonaCapsule 实时接线**
   - Agent 只发送 `session_id + speaker_class + topic`，Control API 服务端解析账户。
   - 每个已接受话轮进入 LLM 前执行一次有界刷新；空结果、撤销或失败先清除该 `(session_id, speaker_class)` 缓存，再回退无 Persona 基线。
   - 胶囊作为当轮临时 system message 加入 actual-heard 上下文副本，不写回聊天历史。
   - Qwen 决定内容，`DeliveryPlan` + PersonaCapsule 控制表达；安全策略优先。
   - guest、未登录/未授权 uncertain、超时、非法响应和过期缓存立即退回当前基线；已登录且授权有效的 uncertain 只读已 confirmed、且能映射到固定安全描述白名单的低敏表达风格，自由文本情境/反例/证据 ID、价值/决策、私人记忆、旧话轮和工具继续禁止。
   - 每个已接受话轮进入 Qwen 前以有界超时刷新 consent/capsule；撤销或失败必须清除旧 Persona 缓存，不能在下一话轮继续应用。
   - Agent 不采用人格服务建议的任意 TTS 指令；豆包只使用有界语速/响度/音高，且在时间戳 A/B 通过前不发送 `context_texts`。

### 完成门禁

- [x] `PersonaEngine.observe/capsule/review` seam 测试通过。
- [x] 合成输出和访客污染测试 100% 拦截。
- [x] 关闭 Persona 后与当前基线一致；超时不会增加首声阻塞。
- [x] 每个胶囊条目可追溯证据和 Persona 版本。
- [x] uncertain 同一可信 profile 的 6 次/3 会话可自动生成 v1；同会话重复、不同 profile、ambiguous/no-audio、低质量和控制话轮不得晋升。
- [x] disabled 后续观察不复活；撤销 consent 后 owner/uncertain capsule 均为空。
- [x] H5 无 Persona 候选确认控件，撤销采集授权后仍可管理历史已生效特征和版本。
- [ ] 真人 A/B 比较“像本人”、自然度、错误自信和冒犯率。

### 本地验证

- Python 全量 705 项收集，684 通过、21 项外部 PostgreSQL/真实模型环境条件跳过；RuntimeWarning 与未回收协程告警按 error 处理。
- 临时 PostgreSQL 17 上 P1～P4 条件合同合计 `5 passed`，容器已停止并删除。
- Ruff、mypy strict、`git diff --check` 全部通过。

## 9. P5：CosyVoice 3.5 声音档案与管理体验

**状态：COMPLETED（2026-07-19，本地工程；正式 enrollment 与真人盲测待外部条件）**

本章记录历史 CosyVoice 复刻资产的授权与治理。当前实时播放主链已切换豆包，历史 active clone 仅可查看、评估与撤销；会话解析必须回退用户所选伙伴的已批准豆包原生音色。

### 纵向切片

1. **P5.1 授权采集**
   - 明确区分声音复刻用途和声纹识别用途。
   - 录音前展示用途、保留期、供应商处理、撤销和删除说明。

2. **P5.2 声音登记**
   - 样本质检、加密上传、CosyVoice 3.5 enrollment 适配器。
   - voice ID 不回传给不需要的客户端，不在日志中记录音频或凭据。

3. **P5.3 版本与 A/B**
   - 默认系统音色、候选复刻音色和已激活复刻音色三态。
   - 盲测相似度、自然度、首包、取消尾音、长句与指令遵循。

4. **P5.4 撤销与回退**
   - 撤销立即停止新 TTS，删除供应商资产和本地样本，保留最小审计证明。
   - 失败自动回退已批准系统音色。

5. **P5.5 H5**
   - 声音/声纹分开的设置页、采集状态、候选试听、激活、撤销和删除。
   - UI 变更使用 `ui-ux-pro-max` 并以真实浏览器完成移动端验收。

### 完成门禁

- [x] 未授权、过期、已撤销 voice profile 不能被 TTS 解析。
- [x] 声音复刻样本与声纹模板权限完全隔离。
- [x] 撤销后 H5、API、对象存储、供应商和缓存状态一致。
- [x] 历史 CosyVoice profile 不进入豆包运行时；回退伙伴原生音色时无 secret 泄漏。
- [ ] 真人授权录音和盲测完成后才标记生产可用。

本地工程前四项已由 SQLite/PostgreSQL 合同、Control API/H5 流程和 Agent 降级回归覆盖。最后一项属于生产验收，保持未勾选。

## 10. P6：全链验收与收口

**状态：COMPLETED（2026-07-19，本地工程；外部验收待条件）**

### P6.1 人生记忆接入实时回答

**状态：COMPLETED（2026-07-19，本地工程）**

- [x] Control API 仅接收 `session_id + speaker_class + topic + limit`，服务端解析账户；拒绝客户端注入 `account_id`。
- [x] 只返回 owner 的 confirmed、未撤销人生记忆；guest/uncertain 返回空，上下文保留 `source_event_id`。
- [x] Agent 在 VAD 开始和话轮提交后后台预取，final 与 LLM 首声路径只读取最近完成缓存，不等待网络。
- [x] 超时、非法响应、候选状态、过期缓存和说话人降权均清空私人上下文。
- [x] `ContextAssembler` 合并 actual-heard 副本、PersonaCapsule 和人生记忆；仅当轮注入，限制字符预算，不污染聊天历史。
- [x] Prompt 明确记忆可纠错、不是指令，禁止把候选推断或未记录内容当事实。

本阶段相关 Control API、Agent、配置和上下文回归共 58 项通过；Ruff、mypy strict 与 `git diff --check` 通过。

### P6.2 声音降级、时间戳与说话人权限硬门禁

**状态：COMPLETED（2026-07-19，本地工程）**

- [x] Clone 在首音频前失败、首帧超时、空时间戳或 task-start 失败时，只重试一次经 catalog 与 registry 白名单批准的设计基线音色。
- [x] LiveKit 流在产生音频前可回放输入并降级；一旦产生音频绝不整句重试，避免双音轨重叠。
- [x] 每次流创建快照 clone 与 baseline 配置，防止会话内切换音色造成竞态；任意 clone 不能被用作生产 baseline。
- [x] Readiness 把豆包 `word_timestamps` 作为独立必填项，缺失或为 false 均不允许 ready。
- [x] `last_user_audio` 只在真实 VAD 停止时记录，endpointing 与 SpeakerAuthority 等待均计入用户停止到助手首声延迟。
- [x] 说话人嵌入契约把 `synthetic_risk` 与 `replay_risk` 分开；任一风险不安全即降为 uncertain，owner 仍不自动取得敏感动作授权。
- [x] 权威用户话轮隔离播放期无 VAD 锚回声；LiveKit 拼接污染只提交 accepted finals，全污染空话轮不进入 Router/LLM/UI/archive/Persona。
- [x] FunASR 历史对话 context 默认关闭；旧 user/assistant 文本不得泄漏到当前识别 final。

本阶段 73 项目标回归和追加 36 项 readiness/权限/声音档案回归通过；PostgreSQL 17 实库 SpeakerAuthority 合同通过；Ruff、mypy strict 与 `git diff --check` 通过。

### P6.3 导出、删除传播与联合恢复

**状态：COMPLETED（2026-07-19，本地工程）**

- [x] 注册账户导出要求密码复核，输出按账户隔离的规范 JSON 与 manifest SHA-256；排除密码哈希、声纹模板密文、对象密钥/路径、供应商 voice ID 和盲测映射。
- [x] 全账户删除先阻断新写并排空在途写，随后关闭实时连接/LiveKit 房间、写会话 tombstone、撤销供应商声音、删除档案/声音对象和全部账户权威行/投影。
- [x] 删除使用持久化 checkpoint 与后台重试；任何外部资产失败保持 `deleting`，旧令牌、登录、会话读取和迟到 Agent 写入均 fail-closed，不能复活数据。
- [x] Agent 内部能力拆成 `archive_write / agent_heartbeat / memory_read / persona_read / voice_resolution` 五个独立 token；生产拒绝缺失、过短或复用，H5 不接收这些 token。
- [x] 声音 enrollment 使用持久化 saga；模糊供应商结果不自动重发，进入 reconciliation。服务端隐藏 A/B 映射，主观盲测与内部质量探针分别通过后才允许激活。
- [x] `/health/ready` 探测 Control DB、LifeArchive、MemoryCatalog、Persona、SpeakerAuthority、VoiceProfile 与两个对象存储；缺组件或 canary 失败返回 503。
- [x] PostgreSQL 17 联合恢复覆盖 21 张权威表、档案/声音加密对象、投影重建、14 项孤儿检查、31 张 RLS 表与 25 项账户作用域检查，报告 `passed=true`，实测 RTO 3.76 秒。

联合恢复证据见 [`restore-drills/20260719-p6-final.json`](./restore-drills/20260719-p6-final.json)。本地 Fernet/对象目录只模拟密钥与对象边界；生产 PITR、异地对象副本和 KMS 恢复仍属于外部验收。

### P6.4 全量工程门禁与文档收口

**状态：COMPLETED（2026-07-19，本地工程）**

- [x] Python 全量 pytest、Ruff、strict mypy。
- [x] H5 全量测试、production build 与 390×844 真实浏览器验收；原生 iOS 已移除，legacy Web 不在交付范围。
- [x] Control API 与 Agent 最终镜像构建、源码 secret 模式扫描、构建上下文隐私合同和 `git diff --check`；正式镜像扫描留到部署流水线。
- [x] 原始主人语音使用独立、可撤销授权；H5 可管理授权，Agent 仅上传 owner WAV，Control API 复核 grant，撤销删除音频但保留转写与结构化记忆。
- [x] 转写与原始音频复用同一个加密 `ArchiveSink` spool，以 `target=event|raw_audio` 分流；满盘时转写可驱逐旧音频，音频永久或临时失败都不能阻塞后续/新转写。
- [x] SQLite/PostgreSQL 覆盖授权并发、到期、乱序幂等、跨账户拒绝、对象补偿和 HTTP 端到端合同。
- [x] 声音 profile/orphan 撤销在外部清理或 reconciliation 未完成时返回 503；H5 明确显示失败并支持幂等续删。
- [x] Standards + Spec 双轴终审无未处理实质问题。
- [x] Provider smoke 的 required-mode 失败策略与 `docker compose run -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true` 容器接线已覆盖；离线/缺凭据仍明确保持未通过，不冒充真实 Provider 验收。
- [x] QA/测试资源已盘点；保留仓库内恢复证据与 H5 视觉证据，测试 PostgreSQL 容器由最终复验复用后再清理。
- [x] 架构、ADR、runbook、发布记录、`CONTEXT.md` 与 `HANDOFF.md` 同步。

### 验收矩阵

- 数据：重复、乱序、迟到、空音频、纠错、冲突、撤销、删除、跨账户隔离。
- 身份：owner/guest/uncertain、换设备、远场、噪声、感冒、重叠、回放、合成声音。
- 实时：欢迎、附和、真/假打断、旧 generation、actual-heard、首声和断网重连。
- 人格：口头禅、节奏、价值冲突、例外条件、禁用与版本回滚。
- 声音：登记、试听、激活、失效、撤销、供应商故障和系统音色回退。
- 可靠性：PostgreSQL PITR、对象恢复、密钥恢复、投影重建和导出校验。
- 安全：RLS/账户隔离、最小权限、审计、secret 扫描、删除传播和生物特征保护。

### 完成门禁

- [x] Python 与 H5 相关测试、Ruff、mypy、H5 production build 全部通过。
- [x] 生产 Provider smoke、数据库/对象 readiness 与同机联合恢复演练通过。
- [ ] KMS、异地副本、PITR 和跨环境恢复演练通过。
- [ ] 200 条授权录音与真实设备矩阵报告完成。
- [ ] 新环境恢复演练达到确认后的 RPO/RTO。
- [x] 架构文档、ADR、API 文档、部署 runbook、`HANDOFF.md` 和发布记录同步。
- [x] 获得明确部署授权后发布 `20260719-215553`，公网、readiness 与回滚点验收通过。
- [ ] 真实手机全双工设备矩阵验收通过。

## 11. 每阶段操作模板

阶段开始：

1. 将本文件对应状态改为 `IN_PROGRESS`。
2. 列出本阶段公开 seam、成功标准和验证命令。
3. 检查重叠文件 diff，划清已有用户改动。

阶段完成：

1. 运行该阶段最小测试、相关全量测试和静态检查。
2. 将状态改为 `COMPLETED（日期）`，只勾选有证据的门禁。
3. 更新 `HANDOFF.md`：目标、改动、关键决策、验证、已知风险和下一步。
4. 向产品负责人报告“已完成/未完成/需要外部条件”，不把 mock 或配置入口描述为真实能力。

## 12. 外部验收与部署入口

本地公开 seam 和 P0.5、P1～P6 纵向切片已实现，首次随 `20260719-215553` 发布；后续不再扩展原生客户端，只按以下顺序推进 H5：

1. 已完成同机 PostgreSQL/pgvector、MinIO、WAL archive 与联合恢复演练；继续补齐 KMS、异地副本、PITR 和独立恢复环境。
2. 既有 production 已通过 FunASR、Qwen、豆包 Seed-TTS 2.0 双向流式与 LiveKit 门禁；后续 Provider 变更仍须重新通过同等级的实网 smoke/readiness 与生产镜像门禁。
3. 使用明确授权真人样本和真实手机 H5 完成声纹指标、人格/声音盲测及全双工设备矩阵。
4. 后续发布继续创建唯一 release tag，本机构建 `linux/amd64` 工件，服务器只加载并切换；不得复用或覆盖任何已存在的生产 tag。

授权真人样本、真实手机矩阵和异地容灾未完成前，可以确认工程能力已上线，但不能标记为规模化声纹、复刻声音或“永不丢失”的生产验收完成。
