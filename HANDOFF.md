# 项目交接

## 当前状态（2026-08-07，待发布）

- 生产基线：runtime/H5 均为 `20260806-213522`，`memoria-prod` 四个应用容器 healthy，`/health/ready` 为 9/9 ready；直接回滚点也是该 tag。
- 本轮范围：同会话滚动上下文、跨天 owner 记忆召回、显式低风险记忆写入、时间/人物过滤、中文检索上下文前缀；同时修复公共南京天气查询与 H5 回顾日期。
- 跨天记忆只使用 confirmed owner material；“请/帮我记住……”的低风险自我事实可自动确认，敏感、关系、冲突、缺 fence 或不确定内容保持 candidate。
- 新增 `scripts/rebuild_memory_projections.py`。生产 PostgreSQL 现有 evidence 需要在维护窗口以 `memoria_admin` maintenance DSN 执行 projection rebuild；应用 DSN 没有 BYPASSRLS，不能替代。重建期间必须停写/只读，完成后重跑中文记忆评测、权限泄漏、orphan 与 RLS 门禁。

## 已验证

- Python 全量 `uv run pytest -q`、Ruff、strict mypy；H5 `301 passed` 与 production build。
- 固定中文记忆评测 13/13 通过：`Recall@5/10=0.7692`、`nDCG@10=0.6727`、时间正确率 `1.0`，candidate/冲突/跨账户泄漏均为 `0`。
- 本地 PostgreSQL 17/pgvector projection rebuild、联合恢复和 self-model 外键保护合同通过。
- Open-Meteo 实际南京天气查询成功；H5 回顾页的今天占位和历史日期文案由组件回归覆盖。

## 发布注意

- release 工件必须绑定干净 commit 与新 annotated tag，完整步骤见 `docs/production-deployment.md`。
- 不删除 `docs/releases/`、ADR、`docs/restore-drills/`、`services/legacy/` 或仍受产品契约覆盖的 CosyVoice/Qwen Omni 兼容路径。
- 已清理无引用的 H5 视觉 QA 产物、早期首页原型和被替代的旧实施计划；README 改为指向当前运行状态与 release 记录，而不再硬编码过期版本号。
