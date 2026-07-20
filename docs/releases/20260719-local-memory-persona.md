# P0.5～P6 本地工程候选记录（2026-07-19）

> 复审更正：正式 CAM++ ONNX 模型服务、Control API readiness 版本探针和 P6 本地工程门禁已完成；真实 Provider、真人样本、生产基础设施与真实设备验收仍待外部条件。本记录不是生产上线证明。

> 快照状态：LOCAL-ENGINEERING-COMPLETE（生成本记录时尚未生产发布）
>
> 唯一客户端：`apps/h5`；原生 iOS 与 legacy `apps/web` 不进入实现、CI、Compose、发布或验收。

> 后续状态：该候选已由 `20260719-215553` 发布；生产证据与剩余边界见 [`20260719-215553.md`](./20260719-215553.md)。本文件继续保留为发布前本地工程快照。

## 生产边界

- 本快照生成时线上 runtime/H5 为 `20260719-000731`；随后已由正式 release `20260719-215553` 替换。
- 本轮固定级联主链为 FunASR Realtime + Qwen LLM + CosyVoice 3.5。
- 本记录中的“完成”只表示本地代码、合同、构建和文档门禁完成，不表示真人样本、真实手机听感或生产基础设施已经验收。

## 本地交付

- P0.5：用户名/密码注册登录、重复用户名提示、稳定 `user_id`、匿名身份原地升级与跨账户隔离。
- P1：追加式证据账本、加密 spool、PostgreSQL/对象存储适配、SQLite 迁移、备份和恢复。
- P2：`owner / guest / uncertain` 说话人权限、正式 embedding 契约、声纹登记、撤销和 fail-closed 门禁。
- P3：人物、关系、时间线、人生故事、经验知识、冲突审核与全文/pgvector 混合检索。
- P4：口头禅、表达节奏、价值观和决策习惯的证据化 Persona 版本，以及实时 Qwen `PersonaCapsule`。
- P5：CosyVoice 3.5 声音登记 saga、候选盲测、质量门禁、激活、撤销、供应商补偿与 H5 管理。
- P6：私人记忆实时注入、声音降级与时间戳硬门禁、导出/删除 fence、tombstone、联合恢复和原始语音独立授权。
- 原始语音与普通转写复用同一个加密 `ArchiveSink` spool，通过 `target=event|raw_audio` envelope 分流；满盘时转写可驱逐旧音频，临时失败音频保留重试但不阻塞后续或新转写。
- 声音撤销覆盖正式 profile 与 orphan enrollment operation；供应商/对象清理或 reconciliation 未完成时返回 503，H5 显示失败并提供对应的幂等重试入口。

## 最新本地门禁

- Python：`506 passed, 19 skipped`；跳过项均依赖未注入的外部 Provider 或独立生产条件。
- Ruff：通过。
- mypy strict：104 个 source files 通过。
- H5：11 个 test files、98 项测试通过；production build 通过。
- 离线 E2E：通过，旧 generation 音频与旧 tool epoch 输出均为 0。
- `verify_env`：`OFFLINE_MOCK=true` 模式通过。
- PostgreSQL 17 + pgvector：档案与 Control API 原始语音最终合同 5 项通过；声音 Provider 删除失败重试与 orphan sample reconciliation 合同通过。
- H5 390×844 浏览器回归：授权查询失败 fail-closed、重试、撤销确认焦点、无横向溢出、console 0 warning/error。
- Control API 与 Agent 本地镜像：`memoria-control-api:local-final`、`memoria-agent:local-final` 构建成功。
- Provider smoke required-mode 与 Compose `run -e` 接线测试通过；离线 mock/缺凭据不会再以退出码 0 冒充发布门禁通过。

## 未完成的外部验收

- 至少 200 条明确授权真人录音及 FAR、FRR、EER、unknown rejection、抽取和检索指标。
- CosyVoice 3.5 正式 enrollment、人格 A/B 与声音真人盲测。
- 真实手机 H5 在耳机/扬声器、噪声、重叠、回放和弱网条件下的全双工体验验收。
- 生产 PostgreSQL、pgvector、S3/OSS、KMS、PITR 与独立环境联合恢复。
- 真实 Provider smoke/readiness、生产镜像门禁、部署、公网与回滚点验收已在正式 release `20260719-215553` 完成。

授权真人样本、声音盲测、真实手机矩阵和异地容灾完成前，不能宣传为规模化声纹、复刻声音或“永不丢失”已经生产验收；正式工程能力的上线证据以 `20260719-215553.md` 为准。
