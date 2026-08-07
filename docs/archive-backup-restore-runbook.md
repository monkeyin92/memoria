# 终身档案备份、时间点恢复与恢复演练

> 状态：P6 本地 PostgreSQL 17 联合恢复已验证；生产 PITR、S3/OSS 与 KMS 演练待基础设施  
> 适用：PostgreSQL 17、S3/OSS 兼容对象存储、独立密钥管理  
> 原则：数据库、对象和密钥必须作为同一个恢复点验收；只有“备份成功”日志不算可恢复。

## 1. 恢复目标

- 账本目标 RPO：不高于 PostgreSQL WAL 归档间隔；建议生产持续归档，告警阈值 5 分钟。
- 账本目标 RTO：先恢复只读时间线，再重建人物、知识、向量和人格投影；目标值由上线压测确定。
- 对象目标：开启版本控制和跨可用区冗余；每个对象以数据库中的 `byte_count`、`content_sha256` 和 `encryption_key_version` 校验。
- 密钥目标：Fernet/KMS 数据密钥与数据库、对象备份分开保存；恢复环境只授予演练期最小权限。

任何一次恢复必须同时证明：全部权威表数量一致、事件内容哈希一致、对象明文哈希一致、抽样投影可重建、孤儿引用为 0、账户隔离仍生效。声音 enrollment operation、盲测、主观评价和客观质量探针与证据账本同属恢复合同，不能只恢复最终 active profile。

## 2. 生产备份

### 2.1 PostgreSQL

托管 PostgreSQL 优先启用服务商 PITR。自管 PostgreSQL 17 至少配置：

```conf
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /archive/%f && cp %p /archive/%f'
```

`archive_command` 仅为本地示例；生产应写入具备对象锁、版本控制和独立凭据的异地仓库。每天另做一次逻辑备份：

```bash
pg_dump --format=custom --no-owner --no-acl \
  --file=memoria-archive-YYYYMMDDTHHMMSSZ.dump "$MEMORIA_ARCHIVE_DATABASE_URL"
sha256sum memoria-archive-YYYYMMDDTHHMMSSZ.dump \
  > memoria-archive-YYYYMMDDTHHMMSSZ.dump.sha256
```

备份任务不得把 DSN、密码、访问令牌或 SQL payload 打入日志。备份文件与校验文件上传后，从另一身份执行一次只读下载校验。

### 2.2 对象存储

对象桶开启版本控制、服务端加密、生命周期和异地复制。每次逻辑备份同时导出对象清单：

```sql
COPY (
  SELECT object_key, byte_count, content_sha256, encryption_key_version,
         retention_policy, created_at
  FROM archive_evidence_blobs
  ORDER BY object_key
) TO STDOUT WITH CSV HEADER;
```

清单本身计算 SHA-256，并与数据库 dump 使用同一个恢复点标签。对象删除必须经过生命周期/删除工作流，不能用“数据库已删”替代供应商对象删除。

### 2.3 密钥与配置

- `MEMORIA_ARCHIVE_OBJECT_KEY`、声纹模板密钥和声音样本密钥必须属于不同密钥域。
- 只备份密钥引用、版本和恢复说明；长期文档、dump manifest 与日志中不写明文密钥。
- 每次轮换保留仍有存量密文依赖的旧版本；只有重加密完成并核对清单后才销毁。
- 每季度由未参与日常备份的人使用恢复凭据完成一次演练。

## 3. 空环境恢复

1. 创建全新的 PostgreSQL 实例、空对象桶/前缀和最小权限恢复身份。
2. 恢复密钥版本，但暂不开放应用写流量。
3. 对目标时间点执行 PITR；逻辑备份演练则执行：

   ```bash
   createdb memoria_restore
   pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
     --dbname=memoria_restore memoria-archive-YYYYMMDDTHHMMSSZ.dump
   ```

4. 恢复对象版本，并按对象清单逐个解密、核对字节数与明文 SHA-256。
5. 使用与生产相同的 schema/编译器版本，从 `archive_processing_outbox` 和 `archive_evidence_events` 重建人物、时间线、知识、向量和人格投影。
6. 执行第 4 节验收；通过前不切换生产读写。

### 3.1 仅重建 memory projection

当检索上下文格式、显式记忆策略或编译器版本变化，但不需要完整数据库恢复时，在维护窗口将应用切为只读，然后运行：

```bash
MEMORIA_MEMORY_REBUILD_DATABASE_URL='<maintenance postgres dsn>' \
   uv run python scripts/rebuild_memory_projections.py --confirm-rebuild
```

该命令只清空并重建可派生的 memory projection，immutable `archive_evidence_events` 不会被修改；policy confirmation evidence 也会按原顺序重放。它会在截断前验证当前 maintenance role 具备 RLS bypass，在结束时验证没有未完成的 compile outbox；普通 app/compiler DSN 会在修改前被拒绝。若存在 `self_model_relationship_profiles`，其外键引用的 `person_entities`/`relationships` 稳定行会被保留，未被权威 profile 引用的其余行才会删除，随后由账本重放补齐派生数据。输出中的 `failed_events` 必须为 0，随后重新执行固定中文记忆评测、权限/冲突泄漏门禁、`postgres_orphan_counts` 和 RLS 检查。不要在有并发写入的生产库上执行，也不要把 DSN 或 payload 写入日志。

## 4. 必过验收

数据库一致性：

```sql
SELECT count(*) AS evidence_events FROM archive_evidence_events;
SELECT count(*) AS processing_outbox FROM archive_processing_outbox;
SELECT count(*) AS evidence_blobs FROM archive_evidence_blobs;
SELECT count(*) AS transcript_versions FROM archive_transcript_versions;
SELECT count(*) AS voice_enrollment_operations FROM voice_enrollment_operations;
SELECT count(*) AS voice_blind_trials FROM voice_blind_trials;
SELECT count(*) AS voice_quality_measurements FROM voice_quality_measurements;
```

客户端还应按 `event_id` 排序，将 `event_id:content_sha256` 连接后计算 SHA-256 清单值；源库和恢复库必须相同。这样无需为了验收临时向生产安装扩展。

还必须完成：

- 随机抽取不少于 100 条事件，比对事件 ID、内容哈希、时间和账户归属；数据不足时全量比对。
- 随机抽取不少于 100 个对象，解密后比对 `byte_count/content_sha256`；数据不足时全量比对。
- 对 owner、guest、uncertain 各执行一次权限查询，guest/uncertain 不得读到主人私人档案。
- 重建投影后抽样打开来源话轮；孤立无来源结论必须为 0。
- 对账户生命周期清单中的全部权威表和投影表执行孤儿/未验证外键检查；声音登记 operation、盲测和质量探针不得悬空。
- 对全部启用 RLS 的表检查 `relrowsecurity/relforcerowsecurity`，再以非 owner 应用角色逐项验证账户 A 不能读取账户 B。
- 在恢复实例执行 `ANALYZE` 后再测检索与时间线，不以冷缓存首轮延迟作容量结论。

### 4.1 P6 本地联合恢复证据

最终本地报告：[20260719-p6-final.json](./restore-drills/20260719-p6-final.json)。

- PostgreSQL 17 custom dump 在全新数据库使用单事务恢复，21 张权威表的源/目标计数一致。
- 档案与声音对象各 1 个，独立密钥版本解密后字节数与明文 SHA-256 一致。
- `voice_enrollment_operations / voice_blind_trials / voice_quality_measurements` 各有 1 条实际数据随库恢复。
- 投影重建 `compiled=2 / ignored=1 / failed=0`；14 项孤儿/外键检查全部为 0。
- 31 张 RLS 表通过，25 个账户作用域检查无越权；实测 RTO 3.76 秒。

该报告使用本地容器、Fernet 测试密钥和本地加密对象目录，只证明工具、schema 和验收合同可执行。它不证明生产 WAL/PITR、异地对象版本、真实 KMS 权限或生产数据量下的 RPO/RTO。

## 5. 失败与回退

- 任一数据库计数、清单哈希、对象哈希或密钥版本不一致，演练失败，不切流。
- `pg_restore --single-transaction` 失败时丢弃本次新建的恢复数据库，修复原因后从空环境重来；禁止在半恢复库上人工补表后宣称成功。
- 对象缺失时保留账本和对象引用，标记恢复缺口并从版本历史/异地副本补齐；不得用空文件替代。
- 投影重建失败不修改证据账本；修复编译器后从账本重新生成投影。

## 6. 演练记录模板

- 恢复点与备份标签：
- PostgreSQL 版本、对象区域、密钥版本：
- 开始/结束时间与实测 RTO：
- 事件、outbox、对象、转写、投影数量：
- 数据库清单 SHA-256、对象清单 SHA-256：
- 抽样数量与失败数量：
- 账户隔离、撤销传播、检索抽样结果：
- enrollment saga、声音盲测、质量探针和删除 tombstone 结果：
- 未通过项、负责人和修复期限：

本地 SQLite 的快速回归继续使用 `services.archive.backup`；它只能证明开发库备份/校验路径，不能替代 PostgreSQL PITR、对象和密钥的联合恢复演练。
