# 项目交接

## 当前开发目标：硅基生命路线

- 开发分支：`codex/silicon-life-roadmap`；基线为已提交并推送的
  `98b04727b5521f9ffdd4061c3bc5a95b538d0bde`。生产仍保持下节所述
  `20260721-224804`，本分支尚未发布。
- 已完成当前 HEAD/线上/架构差距审计，并新增
  [`docs/silicon-life-implementation-plan.md`](docs/silicon-life-implementation-plan.md)
  与 ADR-0016。产品固定采用“轻人格陪伴者 + 空白成长数字分身”：伙伴只学习如何陪伴，
  Digital Self 只从主人证据、确认、纠错和负面证据成长。
- S1 内测可信底座已完成：Agent 新鲜 heartbeat、短 access/旋转 refresh、最长 24 小时且
  可跨重载恢复的旧匿名身份迁移、消息幂等独立密钥、对象 keyring、严格 generation fence、
  事件合同/浏览器日志/CSP/签名 URL 日志，以及 commit-bound source/image/H5 发布门禁。
- S2 已完成：建立 `companion / self_preview / legacy / archive` 的服务端 ModePolicy，
  冻结会话模式与轻量 Companion Style；Self Preview 与 Legacy 在依赖未满足时由服务端
  明确 blocked。实时语音统一走 session-bound archive contract；shadow owner candidate
  只允许独立的低敏 Persona style 候选，不获得主人历史、学习资格、private/tools 或
  owner projection。
- S3 已完成：不可变 `DigitalSelfVersion`、canonical manifest、digest CAS、完整状态机、
  rollback、同事务生命周期审计、SQLite/PostgreSQL 双实现、FORCE RLS、账户导出/删除和
  H5 管理面均已落地。
- S4 已完成：从现有 Evidence、Memory、Persona 与 manifest 派生七维成长地图、四类
  培育任务、来源权重、冲突、版本就绪度和高权重负面证据；不建立第二份人物事实或
  伪精确人格百分比。
- 当前开发阶段已完成 S6：建立统一的 `DigitalSelfResponsePlanner`，
  让回答显式区分 fact / inference / unknown，并把生成的 response plan、
  response provenance 以 exact fence 写回 archive evidence。旧 `decision_habit/value_priority`
  和原始关系事实只允许作为待审核候选，不能直接成为生效认知策略、关系画像或
  Digital Self manifest 条目。
- S7 已完成：owner-only Self Preview、来源展开、“不像我”/纠正负面证据、
  版本比较、holdout Fidelity Evaluation、owner step-up、active speaker gate、
  exact preview provenance、stale/version gate 与 H5 预览工作台均已打通；下一阶段进入
  S8 本人声音、S9 Legacy、S10 全量验收与部署。
- 本轮不建设分布式、多区域、KMS、异地副本或 PITR；保留为正式商用前待办。当前
  response-plan first-write-wins 快照依赖 Control API 单进程，扩展到多副本前必须迁移到
  共享一致性存储。遗嘱、死亡认证与法律执行也不在当前工程能力内。

## 硅基生命路线 S1 验证

- 后端正式临时 pgvector 环境：796 collected，793 passed、3 skipped、0 failed；总覆盖率
  88.41%，orchestration 92%，protocol 91%，全部正式门槛通过。
- H5 全量测试与 production build 通过；Ruff、strict mypy、Compose 解析、Shell/JSON、
  `git diff --check` 通过。
- Standards 与 Spec 最终复审均无 P0/P1。容灾、KMS、异地备份/PITR 与历史第三方凭据
  轮换证明仍按用户边界列为正式商用前待办，不能借本阶段验收宣称已完成。

## 硅基生命路线 S2 验证

- 正式临时 pgvector 环境：842 passed、2 skipped、0 failed；总覆盖率 89.40%。
- 父事件、中断 generation 与 425 spool 定向再验收：55 passed、1 skipped。
- H5 全量：158/158；production build、Ruff、strict mypy、Compose 合同、
  `git diff --check` 通过。
- In-app browser：390×844 和 667×375 均无水平溢出，console error/warn 为空。
- S3 开始前的硬边界：没有 approved DigitalSelfVersion 时 Self Preview 不可用，
  没有 frozen version/relationship/grant 时 Legacy 不可用；伙伴的说话内容不进入主人
  Evidence、Persona 或 Digital Self manifest。
- canonical 用户话轮是后续 assistant 已听证据和原始音频的父事件；派生证据只继承
  同 session/turn/generation 的服务端资格。Persona 仅消费显式
  `persona_eligible=true` 的 owner 或可信 shadow-owner 话轮。

## 硅基生命路线 S3 验证

- 正式临时 PostgreSQL 17 + pgvector 环境：861 collected，859 passed、2 skipped、
  0 failed；总覆盖率 89.35%，85% 正式门槛通过。
- PostgreSQL 集成以 NOBYPASSRLS 角色验证跨账户隔离、digest CAS、不可变版本、
  rollback、生命周期审计、账户导出与删除；SQLite 与 PostgreSQL 共享同一领域合同。
- H5 全量 165/165，production build、Ruff、strict mypy、Compose 合同和
  `git diff --check` 通过。
- In-app browser：390×844 和 667×375 均无水平溢出，console error/warn 为空；
  实测空来源拒绝、草稿 → 测试 → 批准、密码 modal 初始聚焦、Tab/Shift+Tab 圈定和
  操作后焦点恢复。
- manifest 只采用主人 canonical user speech 支撑的 confirmed Memory 与当前
  PersonaVersion confirmed trait；guest、uncertain、assistant、companion 输出和候选
  都不能进入。
- Self Preview 即使已有 approved version 仍由 `self_preview_runtime` 缺项阻断；
  Legacy 继续要求 frozen version、approved relationship profile 和 grant。

## 硅基生命路线 S4 验证

- 正式临时 PostgreSQL 17 + pgvector 环境全量通过，仅 2 个既有真实模型用例跳过；
  总覆盖率 89.09%，85% 正式门槛通过。
- H5 全量 14 files / 180 tests，production build、Ruff、strict mypy 和
  `git diff --check` 通过。
- 成长地图只从现有权威证据派生七个定性维度，展示采用来源、拒绝原因、冲突、最近变化
  和 Digital Self 版本就绪度；没有新建第二份人物事实或人格完成百分比。
- 自然聊天、人生访谈、情境选择和决策复盘使用事件溯源任务；自然聊天任务与语音会话
  冻结绑定，结束响应丢失时可通过任务状态幂等收敛。
- 新证据显式 fail closed；已确认且早于新资格字段落地的历史主人语音，只在已有确认投影
  时按普通权重兼容读取。显式 false、访客、不确定、伙伴输出和模拟输出仍不能进入。
- in-app browser 实测 approved 版本下来源显示“版本已就绪”；提交“不像我”后同步变为
  “存在冲突 / 版本待更新”，下一次构建明确拒绝空来源，没有生成空 manifest。
- 667×375 横屏无水平溢出；全新 reload 后 console error/warn 为空。
- S5 的首个硬门禁：在新领域模型生效前，旧 Persona
  `decision_habit/value_priority` 与原始 relationship 只能显示为 legacy candidate，
  不得进入 adopted/effective 状态或 manifest。

## 硅基生命路线 S5 验证

- 已完成 `CognitiveClaim / DecisionCase / RelationshipProfile`：候选、审核、生效、反例、
  负面证据、版本和来源均可追溯；高敏主张与关系画像要求 step-up。
- SQLite/PostgreSQL 三类创建接口将主记录、sources、receipt/audit 放在同一事务；来源缺失、
  越权、重复或写入失败都会整体回滚；PostgreSQL 旧关系版本唯一约束可幂等迁移。
- Growth Map 的 `decision_review` 必须提供至少两个方案、选项、约束、结果、反思和当前认同；
  缺约束或纯文本只能形成 unresolved hypothetical；`scenario_choice` 始终 hypothetical。
- H5 新增认知/决策/关系审核面板、可展开 Evidence 原话/反例摘要、密码错误可重试、
  “不授予访问权”边界文案和结构化成长任务表单。
- DigitalSelf manifest v2 增加认知主张、真实决策和关系画像计数/typed entries，同时保留
  v1 digest/rollback 兼容；账户导出、删除、RLS 和生命周期表已覆盖新领域。
- 正式临时 PostgreSQL 17 + pgvector 环境 920 passed、2 skipped、0 failed，总覆盖率
  89.32%；H5 全量 195 passed，production build、Ruff、strict mypy、
  `git diff --check` 通过。
- S6 已完成的核心改动：
  - `services/digital_self/response_planner.py` 提供纯确定性的响应规划器；
  - `/v1/interaction/response-plan` 只接受 session/fence/speaker_decision 的最小契约，
    返回 bounded instructions / grounded_items / voice_target / provenance；完整 fence
    采用 first-write-wins 快照，同 fence 改 query 或 speaker snapshot 返回 409；
  - `services/agent/src/agent.py` 改为在 committed fence 后获取并缓存 response plan，
    严格比对 speaker、ModePolicy、Digital Self/relationship 引用和 voice target；
    LLM 节点只消费这份控制端计划，不再拼 persona/memory prompt；
  - `ContextAssembler` 收窄到 heard/current/resume + response-plan 指令；
  - false-interrupt recovery 仅发固定控制 ack，不再重新拼私有 prompt；

## 硅基生命路线 S7 验证

- H5：新增 Self Preview 工作台，明确区分 preview 版本与 fidelity 评测版本；支持
  owner step-up、孩子/朋友视角预演、来源展开、“不像我”/纠正负面证据、版本比较、
  忠实度盲选与 verdict。
- Control API / Agent / Registry：新增 `/v1/digital-self/preview-capability`、
  preview grants、sources、feedback、`fidelity-evaluations` 全链路；Self Preview
  会话固定 `version_id + manifest_sha256 + perspective + preview_grant_id`，显式禁止
  companion style、history/private/tools/learning/voice_profile 写入；反馈会将版本标记为 stale，
  并阻断后续 preview / fidelity 通过。
- 精确 provenance：assistant 最终已听回答携带 bounded `preview_provenance`
  （`version_id / manifest_sha256 / turn / generation / tool_epoch / epistemic_status / source_refs`），
  H5 仅在 Self Preview 中渲染，普通 companion 不暴露。
- 忠实度门槛：7 类 holdout（fact / decision / relationship / humor / emotion / unknown / privacy）
  均要求 hidden A/B mapping；approve verdict 必须满足 coverage 完整、identity disclosure、
  decision inference disclosure、privacy refusal 和 blind preference safety gate。
- 本地质量门：
  - H5 定向 138 passed；H5 全量 213 passed；production build 通过；
  - Python 定向 `interaction / digital_self / self_preview / preview_registry / mode_policy / interaction_mode_agent`
    61 passed；
  - Ruff、strict mypy、`git diff --check` 通过。
- 本地浏览器验收（2026-07-23）：
  - 390×844：真实登录后进入“数字心智与声音”→“数字分身预览”，显示
    “数字分身预览，不代表本人”、忠实度评测和 1 个 approved 版本；console error/warn 为空。
  - 667×375：横屏面板无水平溢出；step-up 密码框高度 44px，可用按钮已启用；
    console error/warn 为空。
- 本地验收使用临时数据库 `/tmp/memoria-s7-browser.sqlite3` 与临时 speaker 库
  `/tmp/memoria-s7-speakers.sqlite3`；浏览器夹具账号 `s7-browser-owner` 仅用于本地 UI 验证，
  未写入生产。
  - `DuplexRuntime` 已删除旧 memory refresher 与 DeepSeek background generation seam；
  - archive evidence 绑定 bounded response provenance，并由服务端重算
    fact/inference/unknown/disclosure、核验 owner source 与 projection eligibility；
  - shadow owner candidate 只允许低敏 persona style 来源。
- S6 正式门禁：PostgreSQL 17 + pgvector 全量 998 passed、2 skipped、0 failed，
  总覆盖率 89.20%；H5 15 files / 195 tests 与 production build 通过；Ruff、139 个
  strict mypy source files、`git diff --check` 通过。S6 不新增 H5 可见入口，因此没有
  将静态 build 冒充浏览器 acceptance。
- Self Preview 已在本地完成 owner-only 预览与 Fidelity 闭环，Legacy 仍未开放；
  进入 S8/S9 前不能宣称本人声音或传承模式已可用。

## 当前生产

- 唯一交付客户端为 H5：
  - <https://122.51.108.140:8443/>
  - <https://aigcnice.com:8443/>
- runtime/H5 当前 release 均为 `20260721-224804`；固定主链为
  FunASR Realtime → 百炼 Qwen → 豆包 Seed-TTS 2.0 双向流式 → LiveKit/H5。
- `agent / control-api / speaker-model` 均 healthy、restart 0；发布后 15 分钟日志
  error marker 为 0。readiness 绑定 `20260721-224804`，LLM `qwen`、TTS `doubao`，
  9/9 core checks ready。
- PostgreSQL 17 + pgvector、MinIO 与 LiveKit 继续独立同机运行；WMS 的 443、
  `/wms/` 和 `/wms/api/` 未改动。

## 本次热修：避免主人被误静音

- `20260721-212040` 把 shadow ambiguous 也当作非主人拒绝。真实问题会话有 4 个
  非空 ASR final，owner score 为 `0.420678 / 0.473613 / 0.437784 / 0.469538`，
  但 4/4 全被门禁拦截，导致 0 个提交话轮、LLM 或 TTS。线上先紧急回滚到
  runtime `20260721-181810`，再发布本修正版。
- Profile 字段 `reject_non_owner_voice` 仍默认 `true`；H5“我的 → 陪伴偏好”改为
  “过滤明显旁人（实验）”，不再把概率型声纹包装成“仅听主人”。
- 开启时只拒绝 formal `guest/owner_mismatch` 与明确的
  `shadow_guest_candidate`。shadow `shadow_ambiguous_candidate` 和 formal
  `ambiguous_score` 可普通聊天以避免误静音主人，但保持 non-owner/uncertain，
  `history_eligible=false`，不能获得私人记忆、工具、敏感权限或主人历史资格。
- Shadow guest cutoff 默认 `0.40`，分类取 Profile 已存阈值与运行时阈值的较小值。
  本次真实样本回放为主人 4/4 放行、孩子 7 段中 6 段拒绝；这只是止损矩阵，不是
  FAR/FRR/EER 或强身份认证结果。
- 关闭开关后访客可以聊天和打断，但仍不能升级 `owner` 或写入主人回顾。服务端明确
  拒绝时，H5 会给出可操作的内联提示，不再表现为无反馈。

## 滋滋声取证与保护

- 问题会话只有欢迎语进入播放；订阅、track 附着、播放和首包都成功，没有持续增长的
  packet loss/concealment。`packetsDiscarded=164` 在首个有效采样即存在且后续不增长，
  不能把累计值直接归因于当次播放丢包。
- H5 WebRTC 遥测已改为上报 received/lost/discarded packet、concealed sample 和
  total sample 的相邻采样增量。
- 豆包 PCM 流新增连续性守卫：跨 chunk 缓冲奇数字节边界，只输出完整 PCM16 sample；
  总长度为奇数时明确失败，并记录不含音频正文的 `doubao_pcm_summary`。
- 这些改动消除一个可验证的分片风险并补齐取证，但没有同 generation 的供应商原始
  PCM、浏览器接收流和设备外录前，不能宣称滋滋声已经闭环或归责豆包/WebRTC。

## 清理结果

- 已删除零引用 `QWEN_OMNI_MODEL` alias、H5 deprecated Omni helper、当前 Doubao
  runtime 不再消费的 CosyVoice settings/split-env 旧键，以及 Hydra `outputs/`。
- 新 source artifact 使用 Git clean 文件清单，排除 `__pycache__`、`.pyc`、本地 env、
  Node cache、数据和构建输出；H5 bundle secret-like 扫描通过。
- 新 H5 union 没有继承 164 个 macOS `._*` AppleDouble 元数据垃圾；保留 178 个真实
  新旧 immutable assets。CosyVoice 治理、Omni 隔离 A/B、迁移、ADR 与历史 release
  仍有回滚或取证价值，不误删。

## 验证与发布证据

- Python：全量 `uv run pytest -q` 退出码 0，`730 tests collected`；Ruff、strict
  mypy、`git diff --check` 通过。最近一次覆盖率证据为 `82.05%`，仍低于仓库 85%
  门槛，不能写成 CI 全绿。
- H5：`138/138`，production build 通过。
- 服务器候选 smoke：candidate H5、SPA、API、默认/关闭偏好与 SQLite 重启持久化通过。
- Provider：LiveKit、FunASR、Qwen、Doubao PCM/字时间戳/CancelSession 全通过；
  readiness 为 `20260721-224804`、9/9 core ready。
- 公网：IP/域名 root、H5、SPA、live/ready、新 JS/CSS、负向 internal 路由、Nginx 与
  WMS 共存通过；178 个真实 union assets 经 SNI loopback 逐项返回成功。
- 内置浏览器：390×844 与 667×375 无横向溢出，console 0 warning/error；未创建
  生产测试账号。当前 bundle 为 `index-B2cPI3-1.js` / `index-C6TCaVi0.css`。
- 发布记录：[`docs/releases/20260721-224804.md`](docs/releases/20260721-224804.md)。

## 回滚

- runtime：`20260721-181810`；回滚前恢复：
  - `/var/backups/memoria/memoria-control-api.env-pre-20260721-224804`
  - `/var/backups/memoria/memoria-agent.env-pre-20260721-224804`
- H5：`20260721-181810-rollback-union-20260721-212040`。
- SQLite 双快照：
  - `/var/lib/memoria/memoria-pre-20260721-224804.sqlite3`
  - `/var/backups/memoria/memoria-pre-20260721-224804.sqlite3`
- 旧 runtime 可忽略新增 SQLite 列；常规代码回滚不恢复数据库。恢复双 env 后必须以旧
  tag 显式重启 Compose 并刷新 readiness。

## 已知边界与下一步

1. 用真实手机复测主人、孩子、ambiguous、明确旁人、短句打断，以及开关开启/关闭两种
   状态；自动化不能替代这一步。
2. 同一 generation 同步采集豆包原始 PCM、浏览器远端 MediaStream 与设备外录，闭环
   滋滋声。
3. “（开心地笑着）”被朗读是 LLM 舞台指令原样进入 TTS；本 release 未把普通括号文本
   假装成笑声控制。需单独设计过滤与可验证的笑声降级策略。
4. 尚未完成 200 条授权录音 FAR/FRR/EER、回放攻击、耳机/扬声器、噪声和弱网矩阵；
   生产 PostgreSQL/MinIO 仍同机，无异地副本/KMS/PITR。
5. 当前发布来自本地 dirty worktree，Git `HEAD=411b2ea`，尚未提交或推送；生产工件以
   release artifact SHA-256 为复现依据。
