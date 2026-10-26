# ADR-004: 每日数据库备份方案

**状态:** 已批准并装机（2026-10-23 用户批准，同日安装到生产）  
**决策日期:** 2026-10-19（批准 2026-10-23）  
**决策人:** Monkey（2026-10-23 「这5个按你建议推进」中含本项）  
**作者:** Keystone

## 背景

生产环境（110.42.235.198）当前无持续数据库备份机制：

- WAL 归档已于 2026-10-06 关闭（节省磁盘增长 0.6-1.1 GB/天）
- 唯一备份为发布流程中 `freeze` 步骤产生的 pg_dump（保留 3 天）
- 数据库当前规模：62 MB
- 主机资源受限：40 GB 总空间，当前 29 GB 已用 / 9.3 GB 可用（76%）
- 与 pocketSparks 共享主机（MySQL + Node.js）

**风险:** 非发布时段的数据变更（用户绑定、儿童档案、会话记录）在发布间隔内无备份覆盖，主机故障或人为误操作可能导致数据永久丢失。

## 决策

实施每日凌晨自动备份机制，采用保守的磁盘占用策略：

### 1. 备份时间点
- **3:15 AM CST**（Beijing UTC+8）
- 理由：避开发布窗口（通常白天）和用户活跃时段（晚间）
- 避开已有 cron 任务（每 3/5/10 分钟的守护进程）

### 2. 保留策略
- **最近 7 天：** 每日备份全部保留
- **第 8-30 天：** 每周保留一份（周日备份）
- **超过 30 天：** 自动删除

**容量估算（当前 DB 规模 62 MB）：**
- 日备份 7 份：~434 MB
- 周备份 3 份（额外）：~186 MB  
- **总计约 620 MB**（当前规模下，占可用空间 9.3 GB 的 6.7%）

### 3. 存储位置
`/var/lib/memoria/backups/nightly/` （新建目录，权限 `700`）

**不使用** `/root/memoria-release/`：
- release 目录为发布工件专用
- 避免与 `cleanup_freeze_dumps.py` 的 3 天清理逻辑冲突
- 独立路径便于未来迁移至对象存储或远程备份

### 4. 备份格式
PostgreSQL 自定义格式（`-Fc`），支持：
- 压缩（节省空间）
- 选择性恢复
- 并行恢复
- 与现有 freeze 备份一致

### 5. 通知机制
- 成功：无通知（静默）
- 失败：日志记录到 `/var/lib/memoria/backups/nightly.log`（rotate 保留最近 100 KB）
- 用户按需通过 `tail -f` 或 cron `MAILTO` 监控

## 实施细节

### 2026-10-23 装机记录（生产 110.42.235.198）

实现与本文原稿有几处演进，以装到生产的实现为准：

- 调度不用 cron，用 systemd：`memoria-nightly-backup.timer`（03:15 + RandomizedDelaySec 5min + Persistent，`ExecStart=/opt/memoria/ops-tools/nightly_backup.sh`），另有 `memoria-backup-freshness.timer` 每 6 小时跑 `nightly_backup_retention.py --check-freshness --max-age-hours 48`（dump 或 control 副本缺、超 48 小时即非零退出）。
- 保留策略按 `scripts/nightly_backup_retention.py` 实现：**保留 7 个日份 + 总是留最新一份**（本文原写的第 8-30 天每周留一份没有实装；容量更省，当前 dump 约 4.9 MB/天）。
- 脚本会同时备份控制库（容器内 `s.backup()` → `docker cp` → integrity_check）。控制库 2026-10-16 起已在 PostgreSQL（`MEMORIA_CONTROL_DATABASE_URL`），容器里的 `/data/memoria.sqlite3` 是 16 KB 迁移前遗留文件，SQLite 这条腿当前只产出一份陈旧副本，待后续发布把这段从脚本裁掉（裁前先确认没有代码路径还要读这个文件）。
- 落盘与校验：备份目录 `/var/lib/memoria/backups/nightly/`（700）；首份 dump `memoria-20261023-1416.dump` 4,956,707 B，`pg_restore --list` 吐 TOC 1820 项通过；`--check-freshness` 通过；retention dry-run 无待删。
- 目录里 2026-10-19 的两份旧 dump/control 是当天试跑残留，retention 会在到期时收敛。

### 脚本路径
`/usr/local/bin/memoria-nightly-backup.sh`（root 0700）

### Cron 配置
```cron
15 3 * * * /usr/local/bin/memoria-nightly-backup.sh >> /var/lib/memoria/backups/nightly.log 2>&1
```

### 关键逻辑
1. 检查磁盘可用空间（≥1 GB 才执行）
2. 执行 `docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc`
3. 输出到临时文件（原子性）
4. 验证备份文件非空（>100 字节）
5. 重命名为最终文件名（`memoria-YYYYMMDD-HHMM.dump`）
6. 执行保留策略清理

### 故障安全
- 磁盘不足时跳过备份并记录警告
- 备份失败保留临时文件供排查
- 清理逻辑独立执行（即使备份失败也会清理旧备份）

## 未解决问题

1. **远程异地备份（2026-10-26 确立保留策略）:**
   - 镜像服务：`infra/backup/offsite-mirror.sh`（profile: `offsite-backup`）
   - 保留期：书面明确配置为 7 天（合规门槛 ≤7 天，`MEMORIA_OFFSITE_RETENTION_DAYS=7`）
   - 自动过期机制：镜像脚本启动时通过 `mc ilm rule add --expire-days 7 --noncurrent-expire-days 7` 在异地 S3 桶上配置对象生命周期规则，超时自动到期清理
   
2. **恢复演练:** 需定期验证备份可用性（每月一次 pg_restore 试验）

3. **监控告警:** 依赖用户手动查看日志，无主动推送
   - 可选方向：失败时发送钉钉/飞书通知，或集成监控系统

4. **增量备份:** 当前全量备份，DB 增长至 GB 级别后考虑 WAL 归档 + 基础备份
   - 临界点：单次备份 >500 MB 或日增长 >50 MB 时重新评估

## 替代方案对比

### 方案 A：重新启用 WAL 归档
- **优点:** 连续备份，RPO 接近 0，支持 PITR
- **缺点:** 磁盘增长恢复 0.6-1.1 GB/天，3-4 天耗尽剩余空间
- **结论:** 当前主机资源不支持

### 方案 B：云数据库托管
- **优点:** 自动备份、高可用、无运维负担
- **缺点:** 月费用 ¥200-500，需迁移并改造连接配置
- **结论:** MVP 阶段成本优先，待融资后评估

### 方案 C：发布频率优化
- **优点:** 无额外机制，依赖现有 freeze 备份
- **缺点:** 发布间隔通常 1-3 天，RPO 过长
- **结论:** 不满足数据保护需求

## 决策影响

- **开发工作量:** 约 2 小时（脚本编写 + 测试 + 文档）
- **磁盘占用:** 初期 ~620 MB，DB 每增长 10 MB 日均增加 ~100 MB
- **运维负担:** 首月需每周检查日志，稳定后按月
- **恢复能力:** RPO 提升至最多 24 小时（vs 当前 72 小时）

## 验收标准

1. 脚本执行一次手动备份成功
2. 备份文件可用 `pg_restore --list` 列出对象
3. Cron 任务运行 3 天无失败日志
4. 保留策略在第 8 天正确清理首日备份
5. 磁盘不足场景（模拟）跳过备份并记录警告

## 参考

- `scripts/release_ops.sh` freeze 函数（L260-285）
- `scripts/cleanup_freeze_dumps.py`（保留策略参考）
- Memory: `backend-migration-to-pocketsparks-host.md`
