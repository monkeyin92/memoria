# 项目交接

## 当前状态（2026-08-11）

### 多主体整改本地工作区（未提交、未推送、未部署）

- 《Memoria 多用户场景产品策略与架构开发调整方案》PR-01~PR-17 的主体、
  绑定、关系、Runtime Profile、Session epoch、Policy V2、Memory Scope、Tutor、
  Notification、Device Fleet 与 PostgreSQL RLS 软件主体已落入当前 dirty worktree。
- 本轮完成 Identity FORCE RLS、Guardian 核心表 actor/subject RLS、Policy
  nullable-subject receipt scope、Session action subject fence、Notification 写入 actor
  防伪与主体本人读取语义、Device Fleet actor fence，以及 Agent action-policy
  装配；Agent 策略装配已从 `agent.py` 抽离，模块预算保持 `3426`。
- PR-10/12/14 的仓库软件闭环已补齐：Agent 通过生产 HTTP
  `TransactionalToolEffectCommitPort` 提交/对账 Session Runtime 持久化 intent/outbox；
  Memory capture 由 Control 生产 authority 装配和 Archive canonical evidence projector
  驱动同事务写入；家庭共享由生产 executor 完成不同主体 propose/confirm/promotion、
  object/withdraw 与回滚。缺配置或 authority 时仍 fail-closed，不回退 legacy 写入。
- 本地证据：全部服务测试目录与所有真实 PostgreSQL 合约文件通过（pgvector 专用
  文件使用 pgvector 0.8.1/PostgreSQL 17，其余使用 PostgreSQL 16）；strict mypy
  `380` 个源码文件通过；H5 `372` 项测试和 production build、小程序 `180` 项测试和
  JS 语法检查通过；scripts 测试、canonical 合同、模块预算、Ruff、compileall 与
  `git diff --check` 通过。以上均不是生产迁移、外部副作用送达、真实通知或真机证据。
- 详细状态以 `docs/architecture/multi-subject-pr-plan.md` 的
  “2026-08-11 本地实现状态”为准；不再新建平行会话文档。

### 学生线本地工作区（未提交、未部署、未发布）

- `implementation-plan-20260808-student-first-remediation.md` 的 P0、P1 仓库能力、P2 导师域和 P3
  本地软件链路已实现并逐项回填；外部/生产门禁仍保持未完成，不能据此创建对外学生体验版。
- 未成年账号能力由 `account_gate.py` 单点 fail-closed；guardian 绑定/孩子确认/分项同意/撤销、
  tutor focus/Router/进度投影、周报、危机固定回复 + Evidence/outbox、授权儿童语料限额/到期删除均已接线。
- guardian/tutor PostgreSQL FORCE RLS schema 与 forward-only 升级脚本、WAL/base backup、异地对象镜像
  和独立恢复演练脚本已进入仓库；这些只证明可执行能力，不证明生产已安装、远端持续上传或完成恢复。
- CI 等价本地 PostgreSQL 环境为 `2108 passed, 2 skipped`（两项为需显式 CAM++ 模型/官方 WAV 的
  真实 ONNX 测试），总覆盖率 `87.0985%`，编排层 `90%`、
  provider 协议层 `97%`；ruff、strict mypy、module budget、tutor E2E、H5 304 项、小程序 90 项与
  Node 24 的 87 文件 upload dry-run 均通过。
- 仍阻塞发布：PIA/法务/算法备案确认、危机话术专业评审、真实微信订阅消息、生产 guardian 升级、
  真实异地备份与独立恢复报告、iOS/Android 完整语音链、ESP32/AEC、200 条真实授权儿童语料。

### 当前生产基线（未切换）

- 生产 runtime/H5 均为 `20260808-171749`，源码 commit
  `9812fac155ef4f46a74d0d8dbfaf197fe9c5fa5a`；直接回滚目标为 `20260807-163916`。完整证据见
  `docs/releases/20260808-171749.md`。
- 四个应用容器及 PostgreSQL/MinIO 均 healthy；真实 LiveKit、QwenRealtimeSearch、DeepSeek、
  Doubao、FunASR、InterruptSemantic smoke 通过，readiness 绑定新 tag，core 10/10 ready。
- PostgreSQL 已 forward-only 安装独立 `memoria_evolution` 角色、8 张表、8/8 FORCE RLS 与 8/8
  controller policy。不要为代码回滚删除这些对象；旧 runtime 可与 additive schema 共存。
- H5 已最后切流；240 个 immutable URL 全部 HTTPS 200，历史 4776 条资源引用均可用。公网正向路由
  为 200，internal/PocketSparks/Goods Invoice 为 404；390x844 与 667x375 浏览器无横向溢出、
  console warning/error 为 0。
- 小程序 `0.8.66` 已于 `2026-08-08 19:21 CST` 经产品负责人授权使用微信开发者工具官方 CLI
  上传成功；AppID `wx20a3a044b52fcbb7`，包体 `656904 bytes（641.5 KB）`，说明为
  “绑定助手表情完整话轮 fence”。只创建开发/体验版，未提审、未正式发布。
- 项目内 `upload:test` 仍是长期可复现链路：Node 25/Web Storage 问题已 fail-fast，Node 24.16
  compile-only 通过；真实 CI 上传仍需把 `112.20.18.77` 加入微信代码上传 IP 白名单。本次 DevTools
  上传来自干净远端一致 HEAD `05a933f`，小程序业务源码相对 runtime tag `9812fac` 无变化。

## 回滚与备份

- 可执行回滚 receipt：`/var/backups/memoria/rollback-20260808-171749.env`，`root:root 0600`；它由
  上线前冻结的 legacy receipt 安全转换，未从切流后的 `current` 反推旧版本。原
  `release-state-pre-20260808-171749` 继续保留。
- H5 回滚 snapshot：`/var/www/memoria-releases/rollback-20260808-171749`，使用旧入口/provenance
  与当前 240 个 immutable assets；manifest SHA-256：
  `4f37ea9952463ef556aaf28e241c9d5244acf6a1236a46f49a5b968702890169`，逐文件验签与
  `www-data` 可读门禁通过。
- SQLite 双副本 SHA-256：`6443bde9695697296413ed9d7486b744bc52bc52162670d158529bbf4d1577af`。
- PostgreSQL dump SHA-256：`209b09028fdc31be2f2d5d8763b3fe456d34a625c8c43777827c815875cb3fdf`；
  MinIO 对象清单 SHA-256：`c7e273671460428a5fa9183841134a2eae6ded9814c0aeffab3fb6d5ee6155ff`。
- 旧 PostgreSQL/Control/Agent/Speaker/Gateway env 均已 root-only 备份。Runtime 故障先恢复旧 data
  Compose 与全部 env，再切旧软链和四应用；H5-only 故障只切 H5。

## 仍需完成

- 多主体能力上线前必须先做生产备份和 forward-only schema dry-run，配置并验证
  Policy/Session/Memory 内部 token、Redis/outbox、角色权限与 readiness，再执行回滚演练；
  当前 dirty worktree 未提交、未推送、未部署，线上仍是旧基线。
- `TransactionalToolEffectCommitPort` 已保证本地持久化、幂等和 worker 领取合同，但仓库
  当前没有具体第三方业务工具/投递 worker；只有在明确业务动作和供应商后才能实现并验收
  外部 delivery，不能把 outbox completed 或单测通过表述为第三方副作用已发生。
- 先完成学生线全部外部门禁并把证据写入 `docs/acceptance/` / `docs/releases/`；当前危机通知只有本地
  outbox 与家长页提醒，不能宣称微信订阅消息已送达，也不能用自动化代替真实 iOS/Android 声学验收。
- 在任何学生数据进入生产前，先执行 guardian forward-only 升级，配置真实远端备份 endpoint，验证
  WAL/base backup 与关键 bucket 持续同步，并在独立主机完成恢复演练；仓库脚本通过不是生产证据。
- 后续为恢复项目脚本真实上传，可把 `112.20.18.77` 加入微信代码上传 IP 白名单；这不再阻塞
  `0.8.66` 体验版交付。
- `0.8.66` 仍需在真实 iOS/Android 上覆盖麦克风、扬声器/AEC、弱网、前后台与蓝牙；H5 仍需真实
  登录后语音验收。现有单测、浏览器、WSS、Provider smoke 或上传成功都不能替代。
- 成功上传并确认回滚工件后，精确清理旧 incoming 大归档；保留当前 `20260808-171749` basis、
  `20260807-163916` runtime/H5/镜像以及全部备份。
