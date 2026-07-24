# 项目交接

## 当前开发目标：硅基生命路线

- 开发分支：`codex/silicon-life-roadmap`；S1–S10 的代码、生产部署与公开验收已完成。
  S1–S10 基线 release 为 `20260723-192611`
  (`0d13d2b22e1d0160529bfd61d2eb1d586ba18d29`)；当前生产 runtime hotfix 见下文。
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
  exact preview provenance、stale/version gate 与 H5 预览工作台均已打通。
- S8 已提交为 `2919516`：Doubao Voice Clone adapter、manifest v3 个人音色引用、
  session 七字段冻结、所选伙伴独立 fallback、seed-tts/seed-icl 独立资源池、
  generation-bound 实际音色 provenance、
  首音频前一次安全回退和 H5 启用/版本重建流程均已落地。真实样本、synth speaker 映射、
  本人盲测和真机听感仍是明确外部验收项。
- S9 已完成：新增 `LegacyAccessResolver` seam，明确区分 actor、Digital Self owner 与
  speaker subject；grant 固定 frozen version/manifest、approved relationship、exact item
  scope、声音权限和时效；grantee 关系外壳与 owner core 分离，不走 owner 学习管道。
  owner 可在世预演、激活和撤销；registered grantee 仅在 active、未过期、未撤销时进入；
  每 generation 重验 scope、response plan 与实际音色，失败回到冻结设计音色或安全拒答。
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
  - H5 定向 140 passed；H5 全量 215 passed；production build 通过；
  - Python 定向 `interaction / digital_self / self_preview / preview_registry / mode_policy / interaction_mode_agent`
    61 passed；
  - PostgreSQL 17 + pgvector 临时环境全量：1005 collected，1003 passed、2 skipped、0 failed；
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

## 硅基生命路线 S8 验证

- DigitalSelf manifest v3 可选绑定一个 exact VoiceProfile ref；Self Preview session 冻结
  profile/version/provider/model/resource/expiry/speaker digest 七字段，并独立冻结所选伙伴
  fallback。Companion 不绑定 personal voice；profile 撤销、过期、版本、expiry 或 digest
  变化均回退会话所选伙伴，不读取进程默认伙伴。
- Doubao runtime 将 `seed-tts-2.0` 与 `seed-icl-2.0` 分池；个人音色首音频前失败在同一
  generation 只回退一次，首音频后不重放，旧 generation callback 不会污染新回答。
- response provenance 只记录实际 profile/resource 和 speaker SHA-256；Control 再按冻结
  session 核验；personal 的 version/expiry 必须完整，设计音色 digest 由服务端批准目录重算。
  raw provider speaker ID、clone key 和样本正文均不进入 Archive/H5。
- active profile 后续复评失败、质量失败或 Doubao expiry 缺失/过期时立即停止解析；
  `pending/manual` 云端删除只能由独立 `voice_cleanup` 能力携带工单引用审计收敛，Agent/H5
  不能调用。
- H5 支持 v3 manifest、豆包个人音色启用、重建并批准新版本提示，以及过期、撤销和
  `pending/manual` 供应商清理状态；日常陪伴始终使用伙伴音色。
- 正式临时 PostgreSQL 17 + pgvector 全量：1079 passed、3 skipped、0 failed；总覆盖率
  87.76%，orchestration 92%，protocol 92%。H5 18 files / 219 tests 与 production build
  通过；Ruff、142 个 strict mypy source files、Shell/JSON、`git diff --check` 通过。
- In-app browser：390×844 与 667×375 均显示“陪伴者有稳定但克制的工作风格 / 数字分身
  独立成长”及“默认使用所选伙伴豆包设计音色”，无横向溢出；干净 reload 后 console
  error/warn 为空。approved v1 + active owner voiceprint 已创建 `self_preview / s8-v1`
  会话，并冻结玄墨 `low_magnetic / seed-tts-2.0` fallback；本机无 LiveKit，媒体连接未冒充
  真实音频验收。
- S8 阶段未进行真实 provider/sample/device 验收：synth-ready speaker 映射保持
  fail closed；未上传本人录音，未宣称个人声音已真实复刻或应用。该实现随后随
  `20260723-192611` 部署，但上述外部验收边界没有改变。
- S8 最终复审无 P0/P1；此前发现的复评门禁、canonical 版本绑定、首 PCM 后重放、
  Archive 精确声音证明和人工删除收敛 5 项 P1 均已有回归测试。

## 硅基生命路线 S9 验证

- `LegacyGrant` 固定一个 registered grantee、一个 exact frozen
  `DigitalSelfVersion + manifest_sha256`、一个 approved `RelationshipProfile`、manifest
  allowlist、声音权限、激活/到期/撤销状态和一个独立 `LegacyRelationshipShell`。
- owner 在激活前只可预演且不写 shell；grantee 只有在授权 active、未过期、未撤销时可进入。
  shell 只接收 actual-heard 的 grantee / digital-self 对话与白名单互动偏好，不进入 owner
  Evidence、Memory、Persona、Growth 或普通消息，也不能修改 owner core。
- 每轮 response plan、来源读取、声音选择、拒绝与归档都重验 exact grant/version/manifest/
  relationship/scope/fence；personal voice 只在 exact 版本且授权有效时使用，失败回退到会话
  冻结的设计音色，音色与计划不一致时停止回答。
- 最小审计只保存 actor/owner/grantee/grant/shell/session/fence/target 等 ID、固定动作、
  决定、原因与时间；不保存 query、prompt、source excerpt、response text 或自由 payload。
  SQLite/PostgreSQL、FORCE RLS、跨账户 404、账户导出/删除与 refresh/revoke/expiry 均有回归。
- 当前 manifest v1/v2/v3 没有统一的可分享范围字段，因此首版严格 fail closed：
  Memory 仅允许明确 `family/public` 的 `sensitive_domain`，Persona 暂不授权，
  Cognitive/Decision/Relationship 仅允许明确 `family/public` 的 `sharing_scope`。
  后续可通过 manifest v4 或本人逐项分享审核扩展，但不能自动放宽既有授权。
- 正式临时 PostgreSQL 17 + pgvector 全量：1175 passed、3 skipped、0 failed；总覆盖率
  88.13%，orchestration 92%，protocol 92%。H5 19 files / 232 tests 与 production build
  通过；Ruff、148 个 strict mypy source files、Shell/JSON、Compose、
  `git diff --check` 和离线 ASR→LLM→TTS E2E 通过。
- In-app browser：390×844 与 667×375 均可从“数字心智与声音”进入“传承模式”，显示
  “基于冻结资料生成的数字分身，不是本人”，授权人/接收人视角可切换且无水平溢出；
  console error/warn 为空。验收仅使用隔离本地账号和空授权状态，未采集真实声音。
- 未进行真实 provider/sample/device 验收；未实现死亡认证、遗嘱执行、多执行人、争议冻结、
  异地容灾、PITR、KMS 或分布式一致性。上述均保留为正式商用前待办。

## 硅基生命路线 S10 验证

- `20260723-192611` 已按“runtime-first、H5-last”发布；前一失败候选
  `20260723-190001` 因 Agent 镜像缺少 `services.archive` 自动回滚，未切 H5，
  tag 保留且未复用。修复后全量/增量 Agent 镜像统一复制完整 `services/`，
  生产 Compose 合同 22 tests 和真实 amd64 import/heartbeat 均通过。
- 新候选在服务器完成固定哈希、release verifier、amd64/OCI label、隔离
  `smoke_server_deployment.sh`、最小权限 env、PostgreSQL/SQLite 迁移预演、
  隔离 LiveKit 注册与 Agent heartbeat 验收。
- runtime 于 2026-07-23 19:47:57 CST 切换；LiveKit、FunASR、Qwen、
  Doubao PCM/字时间戳/CancelSession、`verify_env` 和 readiness mark/check 均通过。
  H5 于 19:51:26 CST 最后切换，183 个新旧 immutable assets 逐项 HTTPS 200。
- 最终 readiness 为 `ready / 20260723-192611`，Agent `ready`，LLM `qwen`、
  TTS `doubao`、9/9 core ready；三容器 healthy、restart 0。
- Nginx 已启用 CSP，signed provider sample 路由关闭 access log；IP/域名证书、
  certbot timer、WMS 共存和公网正/负向门禁均通过。
- 生产 in-app browser 使用一次性账号验证注册、混合陪伴方案和声纹授权边界：
  390×844 / 667×375 无水平溢出，console error/warn 为 0；未申请麦克风，
  账号已永久删除且重新登录为 401。
- Agent 启动后观察 15 分 33 秒，7 次采样均为 restart 0/healthy、
  readiness/Agent ready、H5 200、WMS active/enabled、错误标记 0。
- 详细工件、备份、证书和回滚证据见
  [`docs/releases/20260723-192611.md`](docs/releases/20260723-192611.md)。

## 当前生产

- 唯一交付客户端为 H5：
  - <https://122.51.108.140:8443/>
  - <https://aigcnice.com:8443/>
- runtime 当前为 `20260724-121900`，H5 有意保持 `20260723-192611`；固定主链为
  FunASR Realtime → 百炼 Qwen → 豆包 Seed-TTS 2.0 双向流式 → LiveKit/H5。
- `agent / control-api / speaker-model` 均 healthy、restart 0；readiness 绑定
  `20260724-121900`，Agent `ready`，
  LLM `qwen`、TTS `doubao`，
  9/9 core checks ready。
- PostgreSQL 17 + pgvector、MinIO 与 LiveKit 继续独立同机运行；WMS 的 443、
  `/wms/` 和 `/wms/api/` 未改动。

## 当前 runtime 热修：漏识别与双音播放

- source/tag：`8412e74c7c3899cba122ad3ff715212d26d11b5b / 20260724-121900`；
  runtime 于 2026-07-24 12:36:00 CST 原子切换，H5 未切换。
- 真实会话的 `missing_speech_epoch` 不是声纹开关拒绝：LiveKit 有时会给出无
  started/stopped 时间戳的 endpoint FINAL。现在只有同一 current VAD epoch、fresh
  speech 和该 epoch PCM 同时存在才把它视作真实语音；无该组合的孤立 FINAL 继续拒绝，
  不放宽回声保护。
- “再见”属普通 `CHAT`，此前在播放期会同时得到 control ACK 和正式回复，形成两条
  LiveKit 音轨。现在仅 `INTERRUPT_COMMAND` 才发送短 ACK；CHAT、带内容打断和空候选
  都不再发送。恢复监听也不再丢弃原播放 fence，保证迟到的
  `playback_finished` 可完成 heard/history/archive。
- 三件套、隔离 smoke、候选 Agent 唯一 LiveKit 注册/heartbeat、SQLite 双备份、
  PostgreSQL dump、env/Nginx 备份、runtime 容器、真实 Provider/readiness 和公网
  API/WMS 均通过。启动后错误标记为 0；H5 仍为 `20260723-192611`。
- 已在登录态 Chrome 打开实时陪伴页；真实麦克风验收待用户完成，重点复测普通句子、
  “再见”和“停一下”。详情见
  [`docs/releases/20260724-121900.md`](docs/releases/20260724-121900.md)。

## 当前 runtime 热修：陪伴语音静默与分类事件冲突

- source/tag：`b58d6a2e71a9f1a8bdffb26f9638c91c9048f839 / 20260723-223448`；
  runtime 于 2026-07-23 23:07:37 CST 原子切换，H5 未切换。
- 根因不是“过滤明显旁人（实验）”开关：普通 `companion` 话轮没有 generation TTS
  voice snapshot，response provenance 在 LLM 前 fail closed；同一 speaker epoch 的迟到
  classification 还会以同 event ID、不同 fence 重发并触发 Archive 409。
- 所有 TTS-backed mode 现在统一绑定当前 generation voice；speaker classification
  task 每个 epoch 只启动一次，waiter 取消经 `asyncio.shield` 隔离。
- 生产真实浏览器测试（2026-07-24 11:56:34 CST）为 `companion`、`reject_non_owner_voice=0`：
  speaker gate 未拒绝，`turn → response plan → LLM → TTS → first_playback` 完整出现；
  provenance failure、409、spool/durable failure 均为 0。
- 12 小时以上观察中三容器 healthy、restart 0、readiness 及公网 API/WMS 正常，H5 仍为
  `20260723-192611`。详情见
  [`docs/releases/20260723-223448.md`](docs/releases/20260723-223448.md)。

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
- 当前 H5 union 没有继承 macOS `._*` AppleDouble 元数据垃圾；保留 183 个真实
  新旧 immutable assets。CosyVoice 治理、Omni 隔离 A/B、迁移、ADR 与历史 release
  仍有回滚或取证价值，不误删。

## 验证与发布证据

- Python：正式临时 PostgreSQL 17 + pgvector 全量 1175 passed、3 skipped、0 failed；
  总覆盖率 88.13%，orchestration/protocol 均为 92%；Ruff、148 个 strict mypy
  source files、Shell/JSON、Compose、`git diff --check` 通过。
- H5：19 files / 232 tests，production build 通过。
- 服务器候选 smoke：candidate H5、SPA、API、默认/关闭偏好、SQLite 重启持久化、
  真实 Agent LiveKit 注册和 accepted heartbeat 通过。
- Provider：LiveKit、FunASR、Qwen、Doubao PCM/字时间戳/CancelSession 全通过；
  readiness 为 `20260723-192611`、Agent ready、9/9 core ready。
- 公网：IP/域名 root、H5、SPA、live/ready、新 JS/CSS、能力令牌负向门禁、
  CSP、Nginx、证书续期 timer 与 WMS 共存通过；183 个 union assets 经 HTTPS
  逐项返回成功。
- 内置浏览器：生产一次性账号验证注册、混合陪伴方案与声纹授权说明；
  390×844 与 667×375 无横向溢出，console 0 warning/error，未请求麦克风；
  账号已删除。
- 发布记录：
  [`docs/releases/20260723-192611.md`](docs/releases/20260723-192611.md)。

## 回滚

- 当前 hotfix 的普通回滚点为 runtime `20260723-192611`；H5 本来就是该版本。切回 runtime
  后以旧 tag 重启三项 runtime 容器并刷新 readiness，不自动恢复数据库。
- 本 hotfix 的 root-only 回滚资产：
  - `/var/lib/memoria/memoria-pre-20260723-223448.sqlite3`
  - `/var/backups/memoria/memoria-pre-20260723-223448.sqlite3`
  - `/var/backups/memoria/memoria-pre-20260723-223448.dump`
  - `/var/backups/memoria/memoria-<service>.env-pre-20260723-223448`
  - `/var/backups/memoria/nginx-20260723-223448`
- runtime/H5 回滚点：`20260721-224804`；runtime 回滚前恢复：
  - `/var/backups/memoria/memoria-control-api.env-pre-20260723-192611`
  - `/var/backups/memoria/memoria-agent.env-pre-20260723-192611`
  - `/var/backups/memoria/memoria-speaker-model.env-pre-20260723-192611`
- SQLite 双快照：
  - `/var/lib/memoria/memoria-pre-20260723-192611.sqlite3`
  - `/var/backups/memoria/memoria-pre-20260723-192611.sqlite3`
- PostgreSQL custom dump：
  `/var/backups/memoria/memoria-pre-20260723-192611.dump`。
- Nginx：
  `/var/backups/memoria/nginx-20260723-192611`。
- 旧 runtime 可忽略新增表/列；常规代码回滚不恢复数据库。恢复三份 env 后必须以旧
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
5. 个人声音代码已部署但继续 fail closed：未完成真实样本、synth-ready provider 映射、
   本人盲测与真机听感，不能宣称已经复刻或应用本人声音。
6. 传承运行时已部署，但尚未用真实家庭关系、冻结版本和接收人做生产正向会话；死亡认证、
   遗嘱执行、多执行人和争议冻结仍不在当前能力内。
7. IP 证书有效至 2026-07-26 21:15:49 UTC，certbot timer 当前 active/enabled；
   到期前继续监控自动续期与 Nginx reload。
