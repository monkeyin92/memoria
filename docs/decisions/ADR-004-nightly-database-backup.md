# ADR-004: 每日数据库备份方案

**状态:** 已批准（2026-10-19，用户）  
**决策日期:** 2026-10-19  
**实施日期:** 2026-10-19  
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

### 2. 备份时间点
- **3:15 AM CST**（Beijing UTC+8）
- 理由：避开发布窗口（通常白天）和用户活跃时段（晚间）
- 避开已有 systemd timer 任务（每 3/5/10 分钟的守护进程）

### 3. 保留策略
- **最近 7 天：** 每日备份全部保留
- **第 8-30 天：** 每周保留一份（周日备份）
- **超过 30 天：** 自动删除

**容量估算（实测 pg_dump ≈4.4 MB，SQLite ≈1-2 MB）：**
- 日备份 7 份：~42 MB PG + 14 MB SQLite = ~56 MB  
- 周备份 3 份（额外，仅周日）：~13 MB PG + 4 MB SQLite = ~17 MB  
- **总计约 73 MB**（占可用空间 9.3 GB 的 <1%）

### 4. 存储位置

`/var/lib/memoria/backups/nightly/` （新建目录，权限 `0700`）

**文件名规范：**
- PostgreSQL：`memoria-YYYYMMDD-HHMM.dump`
- SQLite：`control-YYYYMMDD-HHMM.sqlite3`

**不使用** `/root/memoria-release/`：
- release 目录为发布工件专用
- 避免与 `cleanup_freeze_dumps.py` 的 3 天清理逻辑冲突
- 独立路径便于未来迁移至对象存储或远程备份

### 5. 备份格式
PostgreSQL 自定义格式（`-Fc`），支持：
- 压缩（节省空间）
- 选择性恢复
- 并行恢复
- 与现有 freeze 备份一致

SQLite 备份使用官方 backup API（`sqlite3.backup()`)，确保一致性。

### 6. 通知机制
- 成功：无通知（静默）
- 失败：日志记录到 journald（systemd 默认）
- 用户按需通过 `systemctl status memoria-nightly-backup` 或 `journalctl -u memoria-nightly-backup` 查看

## 实施细节

### 脚本与配置
- **脚本：** `/opt/memoria/ops-tools/nightly_backup.sh`（root 0700）
- **保留策略：** `/opt/memoria/ops-tools/nightly_backup_retention.py`（root 0700）

### Systemd 配置
```ini
# /etc/systemd/system/memoria-nightly-backup.service
[Unit]
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/opt/memoria/ops-tools/nightly_backup.sh

# /etc/systemd/system/memoria-nightly-backup.timer
[Unit]
Description=Run Memoria nightly database backup every day at 03:15

[Timer]
OnCalendar=*-*-* 03:15:00
RandomizedDelaySec=5min
Persistent=true
```

### 关键逻辑
1. 检查磁盘可用空间（≥1 GB 才执行）
2. 执行 PostgreSQL 备份：`docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc`
3. 执行 SQLite 备份：`docker exec memoria-control-api-1` 通过 Python backup API
4. 输出到临时文件，验证非空，原子重命名
5. 执行保留策略清理：7天内全保，8-30天仅周日，30天+ 删除

### 故障安全
- 磁盘不足时（<1 GB）跳过备份，清理仍然执行，systemd 显示 OK（非失败）
- 备份或验证失败时退出码 1，systemd 显示 failed status
- 未找到备份目录、docker 容器不可达等条件下均退出 1

## 恢复演练

本小节提供一条可复制的恢复命令，用于从备份恢复到临时数据库验证备份可用性。

### PostgreSQL 恢复（到临时库 `test_restore`）

```bash
sudo bash <<'RESTORE'
set -u
# 选择一份备份（备份目录是 root 0700，重定向必须在 root shell 里做，不能写在 sudo 外面）
BACKUP=/var/lib/memoria/backups/nightly/memoria-20261019-0315.dump
q() { docker exec memoria-data-postgres-1 psql -U memoria_admin -d "$1" -Atc "$2"; }

# 恢复到一个临时库，不碰生产库 memoria（库里没有 postgres 角色，用 memoria_admin）
q postgres "create database test_restore owner memoria_admin"
docker exec -i memoria-data-postgres-1 pg_restore -U memoria_admin -d test_restore --no-owner < "$BACKUP"
echo "pg_restore exit=$?"

# 对照表数（2026-10-19 实测：恢复 168 张，线上 168 张）
q test_restore "select count(*) from information_schema.tables where table_schema = 'public'"
q memoria      "select count(*) from information_schema.tables where table_schema = 'public'"

# 清理临时库
q postgres "drop database test_restore"
RESTORE
```

### SQLite 恢复（到临时文件）

```bash
# 在生产主机上
# 1. 选择一份备份文件
BACKUP=/var/lib/memoria/backups/nightly/control-20261019-0315.sqlite3

# 2. 只读打开，做完整性检查并列出各表行数（主机没有 sqlite3 命令行，用 python3）
sudo python3 - "$BACKUP" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for (name,) in c.execute("select name from sqlite_master where type = 'table' order by 1"):
    print(name, c.execute(f'select count(*) from "{name}"').fetchone()[0])
PYEOF
```

## 未解决问题

1. **SQLite 控制库：仅备份 `memoria.sqlite3`**
   - `/data` 目录另有 `memoria-evolution.sqlite3` 与 `memoria-identity.sqlite3` 两个文件
   - 当前与 `freeze` 步骤保持一致，仅备份 `memoria.sqlite3`（Control API DSN 指向该文件）
   - 其他两个文件属于已退役的微服务（evolution、identity），不在生产编排内
   - 未来如需纳入，应另行评估其内容与恢复必要性

2. **远程异地备份:** 当前方案仅本地单点存储，主机级故障无法防护
   - 可选方向：定期同步至对象存储（OSS/COS）、另一台主机或开发机
   
3. **监控告警:** 依赖用户手动查看 systemd 日志，无主动推送
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
- **磁盘占用:** 约 50 至 75 MB。依据是实测 dump 约 4.4 MB（freeze 的 `memoria-pre-*.dump` 为 4,422,836 B），最多同时保留 7 份日备份加 3 份周备份。数据库本体 62 MB，压缩后远小于它；占用随 dump 体积线性增长。
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
