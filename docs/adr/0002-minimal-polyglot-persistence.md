---
status: accepted
date: 2026-07-18
---

# 逻辑上分层，物理上先使用 PostgreSQL 加对象存储

长期档案使用 PostgreSQL 统一承载结构化关系、JSON 元数据、全文检索和 pgvector 语义索引；授权原始音频、图片和文档进入支持版本与校验的 S3/OSS 对象存储；Redis 仅保存短期会话和任务状态。暂不引入 Neo4j、Elasticsearch、独立向量库或 Kafka，因为当前规模下 PostgreSQL 的事务、递归查询、全文检索和向量扩展已能覆盖需求，减少运维面和数据一致性成本。

## Considered Options

- 所有数据继续放 SQLite：适合单机原型，但并发写、在线迁移、行级权限、时间点恢复和多 worker 能力不足。
- PostgreSQL + Neo4j + 向量库 + Elasticsearch：每类查询都有专用底座，但数据复制、权限、备份和故障恢复复杂度过早放大。
- 文档数据库：写入灵活，但人物关系、版本、授权和证据链本质上需要强关系与事务。

## Consequences

- SQLite 保留为开发和迁移输入，不再作为终身档案生产目标底座。
- 语义向量是可重建投影，不是权威记忆；模型升级时可重算。
- 对象存储只保存大对象，PostgreSQL 保存对象键、加密信息、校验值、授权和生命周期状态。
- 当单一 PostgreSQL 的容量或隔离指标被真实压测突破后，再按声纹、搜索或事件流逐项拆分。
