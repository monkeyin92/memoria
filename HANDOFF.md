# 项目交接

## 当前状态（2026-08-07，已发布）

- 生产 runtime/H5 均为 `20260807-144153`，四个应用容器 healthy，`/health/ready` 对该 tag 为 ready；直接 runtime/H5 回滚点为 `20260807-124256`。完整发布证据见 `docs/releases/20260807-144153.md`。
- 本次发布修复实时检索回复的简洁整理、播放期“好了/知道了”打断恢复，以及 H5 回顾的今天日期持久显示和消息保存 fence。公网 Chrome 登录态验收确认回顾接口无失败，切换旧日期不会移除 8 月 7 日入口。
- 生产账号当前语音话轮仍以 uncertain/shadow 说话人状态为主，故当天可能显示 `0 段`；只有 Agent 权威终稿的 `history_eligible=true` 主人话轮才会进入主人回顾，不能通过客户端绕过该隐私边界。
- 发布工件已在服务器完成验签、导入和隔离 smoke；runtime/H5 软链、4 个容器镜像标签与 readiness 均精确绑定 `20260807-144153`。数据库回滚备份为 `/var/backups/memoria/memoria-pre-20260807-144153.sqlite3`，SHA-256 `d419c8034e9d91bd796ebde195327589ad2ea4fde95fe7fc87bf64116f6708e1`。
- 候选 `20260807-113413` 在上传前因独立审阅发现 `explicit-memory-v1` 会把出生日期/婚姻等敏感事实自动确认而被拒绝，未部署。后续 `explicit-memory-v2` 使用完整 token grammar，安全偏好后拼接的敏感尾缀也会 fail closed，重新走门禁和工件发布。
- `20260807-115142` 的验签、镜像导入与隔离 smoke 都通过，但其 Control API 镜像遗漏 projection rebuild 脚本；维护命令在 truncate 前退出，writer 自动恢复，runtime/H5 已回滚到 `20260806-213522`，Archive evidence manifest 保持 `608562844dd3b188a7209dd9562629c0b565bdfe4d8c79debb24b02cadf73639`。不得复用该 tag。
- `20260807-124256` 修复 full/delta Dockerfile copy、模块入口和 runbook，并恢复 H5 QA Docker context guard；完整 amd64 工件、远端 verifier、隔离 smoke、projection rebuild、RLS/orphan、中文评测、provider readiness、immutable H5 union 和公网路由验收均已通过。
- 本轮范围：同会话滚动上下文、跨天 owner 记忆召回、显式低风险记忆写入、时间/人物过滤、中文检索上下文前缀；同时修复公共南京天气查询与 H5 回顾日期。
- 跨天记忆只使用 confirmed owner material；“请/帮我记住……”的低风险自我事实可自动确认，敏感、关系、冲突、缺 fence 或不确定内容保持 candidate。
- 新增 `scripts/rebuild_memory_projections.py`。生产 PostgreSQL 现有 evidence 需要在维护窗口以 `memoria_admin` maintenance DSN 执行 projection rebuild；应用 DSN 没有 BYPASSRLS，不能替代。重建复用 Control API 的 extractor/embedder，且必须先停止 Control API compiler、Agent、Gateway 和任何启用的 media-runtime writer，不能只切客户端只读；完成后重跑中文记忆评测、权限泄漏、orphan 与 RLS 门禁。
- 本次重放为 `compiled=4 / ignored=1126 / failed=0`；historical owner 内容没有可确认的记忆，因此当前 claim/search/vector projection 仍为 0。未来完整 owner fence 的对话会按新策略写入并作为跨会话上下文召回，不能把 guest/uncertain 历史升级为主人记忆。

## 已验证

- Python 全量 `uv run pytest -q`、Ruff、strict mypy；H5 `301 passed` 与 production build。
- 固定中文记忆评测 13/13 通过：`Recall@5/10=0.7692`、`nDCG@10=0.6727`、时间正确率 `1.0`，candidate/冲突/跨账户泄漏均为 `0`。
- 本地 PostgreSQL 17/pgvector projection rebuild、联合恢复和 self-model 外键保护合同通过。
- Open-Meteo 实际南京天气查询成功；H5 回顾页的今天占位和历史日期文案由组件回归覆盖。

## 发布注意

- release 工件必须绑定干净 commit 与新 annotated tag，完整步骤见 `docs/production-deployment.md`。
- 不删除 `docs/releases/`、ADR、`docs/restore-drills/`、`services/legacy/` 或仍受产品契约覆盖的 CosyVoice/Qwen Omni 兼容路径。
- 已清理无引用的 H5 视觉 QA 产物、早期首页原型和被替代的旧实施计划；README 改为指向当前运行状态与 release 记录，而不再硬编码过期版本号。
