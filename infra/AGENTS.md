# AGENTS.md — infra

## OVERVIEW
生产部署面：Docker 镜像、compose 数据栈、nginx、systemd 定时任务、Postgres 引导、备份、语音资源与 ASR sidecar。得分 9，独立知识域（本 writer 子树顶层，repo root 只留指针）。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 服务镜像（agent/control-api/media-edge/speaker-model） | `Dockerfile.*` + `*-source-overlay` |
| 数据栈（postgres/minio/offsite-backup profile） | `memoria-data.production.yml` |
| Postgres 角色/库/迁移引导 | `postgres/init-memoria.sh` |
| 全量环境变量（约 430 key，drift 有测试） | `memoria.env.production.example` |
| 备份（base backup + 异地镜像） | `backup/postgres-base-backup.sh` / `backup/offsite-mirror.sh` |
| 语音注册表 / 安全音频 | `voices/{designed,doubao}_voice_ids.json` / `voices/fixed_safety/` |
| ASR 救援 sidecar | `sensevoice-asr/` |
| nginx 拆分 / 限流 | `nginx-memoria-{https,http,limits,stream,bind,device-edge,ip-server,loopback-smoke}.conf` |

## CONVENTIONS
- 镜像模式：全量构建（`python:3.12-slim`）+ 可选 `*-source-overlay`，后者 `rm -rf /app/services /app/packages` 后整树复制 `memoria/services`+`packages`（防陈旧兄弟 import）；依赖全部 `uv export --frozen --no-dev --require-hashes`；base 尽量 `@sha256` 钉死；镜像带 opencontainers 与 `com.memoria.release.*` 标签。
- 构建期跑发布门：`verify_agent_release_artifact`（离线）/ `verify_control_release_artifact --expected-commit/tag --require-metadata`；所有 shell 是 POSIX `sh` + `set -eu` + `: "${VAR:?...}"` 守卫 + `umask 077`。
- Postgres：一个 wrapper 引导 19 个登录角色 + 编号迁移 `NNN-*-schema.sql` 依序执行；DDL 属主是 NOLOGIN schema-owner，运行时角色 `NOBYPASSRLS`、每个服务一个独立登录角色，public 撤销 CREATE；幂等靠 `WHERE NOT EXISTS` + 无条件 `ALTER ROLE`（密码漂移）。
- systemd 单元从 `/opt/memoria` 运行，`RandomizedDelaySec` + `Persistent=true`；备份用 UTC `%Y%m%dT%H%M%SZ` 标签、`.partial` + `mv` 原子发布。
- sidecar（sensevoice）硬化：`read_only` + tmpfs、`mem_limit == memswap_limit`、模型 `:ro` 挂载绝不 bake 进镜像、pip 全部 `--hash` 钉死。
- `*.example` 模板绝不原地填真实值；`config.toml.example` 只列 env 变量名不列值（`media-edge` 的 `[rollout] default_runtime` 是死配置，env 才是真相）。

## ANTI-PATTERNS
- 填好的 env 文件永不提交回仓库；`STREAMCORE_TOKEN_SECRET` 必须等于 `MEDIA_EDGE_JWT_SECRET`；生产 redis URL 必须 `rediss://`。
- `offsite-mirror.sh`：异地端点解析到 `localhost|127.0.0.1|memoria-minio|host.docker.internal` 即拒绝（exit 2）；lifecycle 策略文本必须精确等于 pinned 形式，否则"policy drift"停止同步；保留期只能 1-7 天。
- `pg_basebackup` 不得用 `--dbname`（PG17 拒绝裸库名）、不得 `--format=tar`（plain + `--manifest-checksums=SHA256`）；`memoria_memory_maintenance` 只做 subject erasure，永不作为 API/worker DSN。
- nginx：`/memoria-api/internal/` 系列公网 server block 永不代理；`access_log off` 的内部路径不得进日志；限流 zone 只用 `$binary_remote_addr`，绝不按客户端声称的 Device-Id 或前向 header 键控；只有 loopback TLS 监听器接受 PROXY protocol。
- nginx device-edge 是唯一设备媒体入口；已退役的 LiveKit worker / WHIP terminator / `/memoria-device-media/v1/device/media` 不得复活。
- 安全音频模板禁止从草稿重合成，改文字必须同时重做 `fixed_safety/victim-support-v1` 并重听（TODOLIST 强制）；`DEEPSEEK_BASE_URL` 已弃用，只读 `DEEPSEEK_API_URL`。

## NOTES
- 评分 9（file 3 + subdir 2 + config 1 + export 2 + ref 1；LSP 不适用）。
- drift 测试守护本目录：`test_env_template_drift.py` / `test_production_compose.py` / `test_offsite_mirror.py` / `test_control_release_contract.py`。
- 改动 `offsite-mirror.sh` 会破坏 sha256 pin 的测试，属预期，需同步更新测试。
