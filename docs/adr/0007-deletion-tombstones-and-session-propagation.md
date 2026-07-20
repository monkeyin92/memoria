---
status: accepted
date: 2026-07-19
---

# 账户删除使用持久 tombstone 并同步终止实时会话

账户删除横跨浏览器令牌、在途写入、LiveKit 房间、档案对象、供应商声音、PostgreSQL/SQLite 行和可重建投影。若直接级联删除数据库行，迟到的 Agent 事件或仍在线的会话可能重新创建数据；若供应商或对象删除失败，也会留下无法追踪的生物特征资产。Memoria 因此先设置账户写入 fence，排空在途写，持久化删除请求与 checkpoint，关闭实时连接和房间并写会话 tombstone，再按“供应商/对象先于权威行”的顺序幂等删除。完成后保留哈希化账户 tombstone 与最小审计计数。

## Considered Options

- 单事务级联删除所有数据库行：数据库内原子，但无法覆盖 LiveKit、对象存储和供应商资产。
- 先删账户凭据再异步清理：用户立即无法登录，但外部清理失败后失去稳定重试关联。
- 仅短期缓存删除状态：进程重启后会丢失 fence，迟到事件可能复活数据。

## Consequences

- `deleting` 与 `completed` 都使旧账户令牌、登录、会话读取和内部 session 写入 fail-closed。
- 删除 worker 从最后 checkpoint 重试；外部资产未确认删除前，不伪报完成，也不删除重试所需的最小关联。
- 会话 ID tombstone 阻止旧 session 在数据库行删除后被迟到请求重新使用。
- 删除敏感动作要求账户密码复核和精确确认短语；声纹不能作为唯一授权。
- 当前单 worker 使用一个进程内删除锁与账户写门；扩展多 worker 前必须将写门和租约迁移到共享协调层。
