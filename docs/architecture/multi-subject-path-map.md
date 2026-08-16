# 多主体整改：当前路径 → 目标模块/Seam 映射

> 依据 ADR-0033 冻结的 D-01~D-08 与 `multi-subject-pr-plan.md`。目标是先建立
> 统一决策内核，再拆大文件；路由/WebSocket/Agent
> entrypoint 只做协议适配，不直接实现领域规则。状态列：✅=已存在可复用；
> 🔧=需收敛/改造；🆕=需新建；⬜=非软件可完成（外部/真机门禁）。

## 统一决策内核（§11.1，优先建立）

| Seam | 当前仓库落点 | 状态 | 工作 |
| --- | --- | --- | --- |
| SubjectResolver（当前是谁） | `services/speaker/authority.py`、`services/control_api/app/routes/speaker.py`、`services/agent/src/speaker_authority_client.py` | 🔧 | 把声纹候选/App 选择/多说话人/低置信度统一为 PR-06 的解析器；输出 `SpeakerState` 与候选主体 |
| ServiceModeResolver（当前模式） | `services/control_api/app/mode_policy.py`、`services/agent/src/mode_policy_client.py` | 🔧 | 由 Binding 声明模式 + 主体解析结果解析 `ServiceMode`（未知→`unknown_safe`），值集收敛到 canonical 合同 |
| PolicyEngine（此刻是否允许） | `services/control_api/app/account_gate.py`（现有单点规则表）、`services/agent/src/mode_policy_client.py` | 🔧 | 演进为 PolicyContext + `PolicyEffect`/`PolicyObligation`/`PolicyReceipt`（PR-10）；规则表未声明能力一律 deny |
| PersonaRenderer（如何表达） | `services/persona/engine.py`、`services/agent/src/persona_client.py`、`services/agent/src/prompts.py` | 🔧 | Persona 只管表达；`AI_IDENTITY_CLARIFICATION` 等义务由策略产出、Renderer 分龄表达（PR-11） |
| MemoryScopeResolver（写入哪域） | `services/archive/memory_write_policy.py`、`services/archive/memory_catalog.py` | 🔧 | 五类记忆空间 + `unknown` 兜底；写入先经主体解析与策略回执（PR-12） |
| SessionEpochFence（旧结果失效） | `services/agent/src/duplex_runtime.py`、`services/agent/src/orchestration/utterance_router.py`、`services/agent/src/contracts/events.py` | 🔧 | epoch 提升/换人清除私有上下文/迟到事件拒绝（PR-08） |
| PolicyReceiptWriter（可追溯） | `services/control_api/app/routes/session.py`、`services/archive/` | 🔧 | 敏感副作用写回执；证据、播放文本、主体与回执同一事务（PR-10/§11.6） |

## Agent 侧（services/agent）

| 当前路径 | 目标模块 | 状态 | 工作 |
| --- | --- | --- | --- |
| `src/duplex_runtime.py`、`src/agent.py` | session / subject_resolution / turn | 🔧 | 引入 `active_subject`、`runtime_profile`、epoch fence；抽取 SubjectResolver/ModeResolver/PolicyClient/PersonaRenderer |
| `src/prompts.py` | persona_rendering / service_mode / policy_client | 🔧 | 去除“绝对隐藏 AI 身份”规则；按不可变安全底线→Persona→ServiceMode→Obligations→主体关系→筛选记忆→任务 组装（§11.3） |
| `src/orchestration/utterance_router.py` | turn / interruption 控制面 | ✅ | 已有统一话轮路由（AGENTS.md 约定），继续作为唯一入口，不另开平行 if |
| `src/contracts/*` | packages/contracts 生成物 | 🔧 | 事件/ID 合同保留；多主体枚举收敛到 `generated/`，禁止本地再定义 |
| `src/mode_policy_client.py`、`src/persona_client.py`、`src/speaker_authority_client.py` | policy / persona / subject 客户端 seam | 🔧 | 跟随服务端合同升级，fail-closed 处理未知值 |
| `src/tutor_session.py`、`src/archive_sink.py` | tutor / memory_capture | 🔧 | 学习进度绑定 active subject；证据与回执落库（PR-13） |

## Control API 侧（services/control_api）

| 当前路径 | 目标模块 | 状态 | 工作 |
| --- | --- | --- | --- |
| `app/routes/session.py` | binding / session / runtime profile | 🔧 | 路由变薄；创建会话时解析 Binding、签发 RuntimeProfile（PR-03/PR-07） |
| `app/database.py` | identity / party 权威数据 | 🔧 | `subject_category` 默认 `unknown`、新增 `age_evidence_status`（PR-02） |
| `app/account_gate.py` | consent_policy（单一决策点） | 🔧 | 与 PolicyEngine V2 对齐；保持规则表单点、矩阵测试（PR-10） |
| `app/routes/guardian.py`、`services/guardian/` | guardian（监护/周报/危机通知） | ✅🔧 | 监护关系、同意、摘要与一般账号权限分离（PR-05/PR-15） |
| `app/routes/tutor.py`、`services/growth/` | tutor | 🔧 | 服务端评分、主体化进度（PR-13） |
| `app/routes/archive.py`、`app/routes/memory.py`、`services/archive/` | memory | 🔧 | subject/scope/co-subject/policy receipt/consent snapshot（PR-12） |
| `app/routes/persona.py`、`services/persona/` | persona | ✅🔧 | Robot Persona 与 User Digital Self 分离；Persona 不承载权限（PR-09） |
| `app/routes/digital_self.py`、`services/digital_self/` | digital self | 🔧 | 版本化、授权、来源等级（PR-09） |
| `app/routes/legacy.py`、`services/legacy/` | legacy | 🔧 | beneficiary/传承授权与主体解耦（PR-09） |
| `app/routes/self_preview.py`、`app/routes/self_model.py`、`services/self_model/` | digital self 预览/模型 | 🔧 | 敏感能力依据当前主体再决策（D-07） |
| `app/routes/evolution.py`、`services/evolution/`、`services/governance/` | governance / self-evolution 控制面 | 🔧 | 进化/数据治理继续受现有 fence 与账号门禁约束 |
| `app/routes/speaker.py`、`services/speaker/` | voice profile（声纹） | 🔧 | `voice_profile_create` 敏感能力门禁（PR-02/PR-06） |
| `app/routes/interaction.py`、`app/routes/media.py`、`services/media_edge/`、`services/miniprogram_gateway/` | 实时接入/媒体网关 | ✅🔧 | 既有 generation/tool fence 与证据链保留，接 epoch（PR-08） |

## 客户端与固件

| 当前路径 | 目标模块 | 状态 | 工作 |
| --- | --- | --- | --- |
| `apps/h5/src`、`apps/miniprogram/pages` | 首次绑定四分流/切换使用者/RuntimeProfile 驱动 UI | 🔧 | 四类绑定流程、当前使用者切换、不在客户端推断成人/儿童权限（PR-04/PR-07） |
| `infra/device/`、机器人固件 | device fleet | 🔧⬜ | 设备证书、binding version、短期 Profile 缓存与离线降级、物理隐私灯、OTA/反回滚（PR-16；隐私灯/麦克风切断需硬件链路验收） |

## 合同与文档（本次工作包已落地）

| 路径 | 内容 | 状态 |
| --- | --- | --- |
| `packages/contracts/schemas/multi-subject/multi-subject.schema.json` | 12 枚举单一 canonical JSON Schema | ✅ 本次 |
| `packages/contracts/generated/{python,typescript,go,firmware}/` | codegen 产物（`--check` 无 diff） | ✅ 本次 |
| `scripts/generate_multi_subject_contracts.py` + `scripts/tests/` | 确定性生成器与无漂移测试 | ✅ 本次 |
| `docs/adr/0033-multi-subject-contract-and-decision-freeze.md` | 冻结 D-01~D-08、解耦、unknown_safe、单体与迁移顺序 | ✅ 本次 |
| `docs/architecture/multi-subject-pr-plan.md` | PR-01~PR-18 依赖/风险表与门禁标注 | ✅ 本次 |

## 使用约定

1. 新功能先落统一内核的 seam，再动路由/客户端；不要在 `duplex_runtime.py`
   或其他大文件里开平行控制路径（AGENTS.md「举一反三」）。
2. 枚举值一律引用 `packages/contracts/generated/`，改动先改 schema + 测试再
   重新生成；任何 `subject_category`/`service_mode`/`speaker_state` 字面量
   在对应 PR 中收敛。
3. 标 ⬜ 的门禁（真机、语料、AEC、法务/PIA、真实通知、生产恢复）必须由对应
   发布分级（R0~R5）独立验收，软件侧不得宣称已完成。
