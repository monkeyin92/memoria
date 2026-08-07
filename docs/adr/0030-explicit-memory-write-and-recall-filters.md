---
status: accepted
date: 2026-08-07
---

# 显式记忆写入与确定性召回过滤

## 背景

用户希望在多轮、跨天对话中继续找回已确认事实。文章章节建议使用上下文检索、时间和实体索引；Memoria 还必须保持主人权限、不可变 evidence ledger、可撤销和可重建投影边界。

## 决策

1. Control API 只在正式 owner、companion、非模拟、完整 `session_id + turn_id + generation_id + tool_epoch` fence 下，识别句首严格命令“请/帮我记住……”，并写入 server-owned `explicit-memory-v1` intent。客户端提交的同名字段一律丢弃。
2. `MemoryWritePolicy` 只自动确认单一 `subject_key=self`、可回指原文、置信度足够且整份 extraction 为 `public/personal` 的事实。PII、健康、金融、法律、生物特征、关系或未知敏感度、缺 fence 和单值冲突都保持 `candidate`。
3. 自动确认复用人工 review projection，并追加稳定的 `memory.claim_reviewed` policy evidence（含 policy version、reason、source event 和 claim）。因此状态可审计、可幂等重放、可从 ledger 重建；策略不会静默覆盖既有单值事实。
4. `RecallPlanner` 只把无歧义的相对日期（昨天、前天、N 天前、最近 N 天、周/月范围）转换为本地日历窗口后以 UTC 查询，并只使用 confirmed `PersonItem` 的 confirmed 名称/别名生成 `entity_ids`。多义时间、多人共享别名和非 owner 召回不猜测、不放宽权限。
5. claim、episode、knowledge 的上下文前缀只用于 sparse/dense 检索；回答规划读取规范化 claim value、episode title 或 knowledge answer，不能把上下文前缀当作新事实。

## 重建与发布

`scripts/rebuild_memory_projections.py` 调用现有 `rebuild_postgres_memory_projections()`，清空派生 memory 表并按 archive evidence/outbox 重放。生产切换或上下文前缀升级前必须在维护窗口停写或切只读，执行：

```bash
uv run python scripts/rebuild_memory_projections.py --confirm-rebuild
```

命令从 `MEMORIA_MEMORY_REBUILD_DATABASE_URL` 读取具备 RLS bypass 的 maintenance DSN，不打印 DSN 或 payload；普通 app/compiler role 会在截断前被拒绝。输出只包含编译、忽略、失败和 skill projection 计数，结束时仍有未完成 outbox 也会失败。`failed_events` 非零时不得切流。重建后必须重跑固定中文记忆评测、candidate/跨账户/冲突泄漏门禁和 RLS 验收。

## 代价

首次显式记忆仍可能因模型抽取低置信或敏感而进入人工审核；这是宁可延迟确认也不把误识别写入主人长期档案的取舍。相对日期过滤使用有界 SQL 条件，暂不引入新向量库、reranker 或 GraphRAG。
