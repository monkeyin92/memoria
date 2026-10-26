# 发布、恢复与回滚运维手册

本文是从 `HANDOFF.md` 迁出的永久运维参考，保留原有发布、数据恢复和回滚底线。命令只表示操作基线，不表示本轮已经执行；生产执行、切流、恢复和固件操作仍需单独授权。

## 百炼实时检索与 Omni 开关

`LIVE_SEARCH_QWEN_ENABLED` 与 `QWEN_OMNI_SESSION_ENABLED` 默认都是 `false`。部署之后，除非环境里把它们写成 `true`，通义实时检索不再安装、不再出站，Qwen Omni 会话创建和 SDP 交换直接拒绝。`DASHSCOPE_API_KEY` 必须留下，agent 的 FunASR 仍读它。control-api 的 `ASR_PROVIDER` 默认 `none`，这时 readiness 不因为缺少这把密钥而 503。只有 `ASR_PROVIDER=funasr` 时 control-api 才把它算进缺失项。

天气在实时检索关闭且未配和风天气数据源时，第一轮和孩子接着报城市的第二轮都回固定句 weather-fallback-v1，不调用模型，也不为这句合成音频。

回滚这两条路径：把 `LIVE_SEARCH_QWEN_ENABLED=true` 和 `QWEN_OMNI_SESSION_ENABLED=true` 写回环境后重建对应进程。不要删 `DASHSCOPE_API_KEY`。只有在希望 control-api readiness 继续要求这把密钥时才设 `ASR_PROVIDER=funasr`。

## 生产拓扑与安全边界

- `/opt/memoria/current` 指向当前整栈发布树（2026-10-21 只读核对：`releases/20261020-minor-bypass-a94c438`，有效栈 `MEMORIA_RELEASE_TAG=20261020-minor-bypass-a94c438`；control-api 与 bridge 通过组件覆盖链运行 `20261021-a94c438-…` 镜像，见下文「release-ops.sh 与 PREV」；media-edge 仍是组件发布 `20261002-late-progress-v1`，它的编排是 `releases/20261004-first-warm-v1` 的 compose 加 `component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml`，所以那个发布树不能删）。目录名、栈 tag、组件 tag 是三个概念；readiness 刷新必须取有效栈配置。
- Control/Direct Edge 仅回环端口 `8791/8794`（新机上 Control 是 `18791`，见下文「生产主机」）；Bridge 容器 `memoria-voice-core-media-bridge-1` 是 `memoria-agent` 镜像唯一的运行者，并发送 Agent heartbeat。PostgreSQL 17 + pgvector、MinIO、独立 mTLS Redis。LiveKit server、LiveKit Agent worker（`memoria-agent-1`）与 Python 小程序/设备媒体网关（`8792/8793`）自 LiveKit 退役版本起不在仓库栈内，见下文「LiveKit 媒体链退役」。SQLite 兼容库 `/data/memoria.sqlite3` 挂载自 `/var/lib/memoria`。
- readiness 入口 `https://aginice.cn:8443/memoria-api/health/ready`；443 根站不代表 Memoria（旧机是 WMS，新机只放绑定页、根路径返回 404），不用它的状态判断 Memoria 健康。
- ESP32 Direct：`wss://aginice.cn:8443/memoria-device-edge/v1/device/media`。公共 8080 不承载设备 WSS。H5 `/memoria-h5` 固定返回 `410 Gone`，不再发布静态前端。
- 旧机（2026-10-06 到期前）：443 Nginx 只保留以下 Memoria include（原 `memoria-miniprogram-media.conf` include 随小程序网关退役删除），不改同机 WMS 路由/数据；8443 经 `memoria-stream.conf` 纯 TCP 透传到 `127.0.0.1:9443` 的 Memoria TLS server（不再做 LiveKit 的 `ssl_preread` 分流）。

~~~nginx
include /etc/nginx/snippets/memoria-bind.conf;
~~~

`memoria-bind.conf`（仓库 `infra/nginx-memoria-bind.conf`，2026-09-27 起）只服务 `/memoria-bind/`：`/var/www/memoria-bind/` 下的说明页 `index.html`（仓库 `infra/memoria-bind/index.html`）与微信「扫普通链接二维码打开小程序」的校验文件，关闭访问日志。该前缀必须在 443（微信规则不支持非标准端口）。新机上对应 `snippets/memoria-prod-bind.conf` 与 `/var/www/memoria-prod/memoria-bind/`；2026-10-05 起规则前缀是 `https://aginice.cn/memoria-bind/`（旧规则 `https://aigcnice.com/memoria-bind/` 随旧域名弃用），校验文件由公众平台在添加规则时生成，不入仓库。

Secret 仅在 root-only `/etc/memoria-*.env`（root:root 0600）；候选从真实源复制并按 `scripts/split_production_env.py` 分流，禁止在输出/日志/manifest 留值。内部 token 不等于账号身份；设备/媒体 token 必须短期且绑定 audience/subject/fence。Direct 缺少 mTLS Device State Redis 时 fail closed，不回退本地权威。

## 生产主机（2026-10-05 起：110.42.235.198）

整套服务 2026-10-05 01:33–01:40（CST）从旧机 122.51.108.140 冷拷贝迁到 110.42.235.198：与 pocketSparks 的 MySQL、node、hr-tracker 共用的主机（Ubuntu 24.04，4 vCPU，3.7 GiB 内存 + 2 GiB swap，40 GB 盘）。本机 ssh 别名 `memoria-prod` 指新机，`memoria-prod-old` 指旧机，本文命令里的 `memoria-prod` 都是新机。迁移回执与回到旧机的路径见 `HANDOFF.md`「2026-10-05 服务迁移到 110.42.235.198」；对外域名从 `aigcnice.com` 换成 `aginice.cn`（2026-10-05）见同文件「2026-10-05 域名切换到 aginice.cn」。

- **共用主机的边界**：hr-tracker 占着 `127.0.0.1:8791`，共用 nginx 拥有 80/443/8443，pocketSparks 的站点、卷不动；`sites-enabled/aginice`（aginice.cn / www，Memoria 现在的对外域名，与 pocketSparks 共用）只加了两行 include（`snippets/memoria-prod-https.conf`、`snippets/memoria-prod-bind.conf`，并把 `/` 的静态根指到 `/var/www/memoria-prod`），原件在 `/etc/nginx/backup-20261005-domain-switch/aginice.orig`。Memoria 的 nginx 文件一律用新名字：`sites-enabled/memoria-prod`（旧域名 aigcnice.com，过渡期保留）、`snippets/memoria-prod-{device-edge,https,site-common,bind}.conf`、`conf.d/memoria-prod-limits.conf`，静态页 `/var/www/memoria-prod`（只有绑定页，没有 WMS）；证书：aginice.cn 用 `/etc/nginx/ssl/aginice.cn/aginice.cn_bundle.crt` + `aginice.cn.key`（腾讯云免费 DV，约 90 天一期、不自动到服务器；该站点的 `ssl_ciphers` 只有 RSA 套件，新证书必须是 RSA、不能是 ECDSA）；aigcnice.com 的旧证书 `/etc/nginx/ssl/aigcnice.com/…`（到 2026-12-17）只服务过渡期。Let's Encrypt 的 HTTP-01 对 aginice.cn 走不通（境外验证节点访问 80 端口被腾讯云的备案提示页拦截，同机房境内访问正常），证书只能从腾讯云控制台下载，或用 DNSPod API 做 DNS-01。8443 的 stream 仍是 7 月留下的 `stream-conf.d/memoria-rtc.conf`（`ssl_preread`：TLS 到 `127.0.0.1:9443`，非 TLS 的一支指向已退役的 LiveKit 8444，没有监听者），没改。
- **换 aginice.cn 证书**（现用的一份 2026-10-05 09:08 装入，2027-01-02 到期）：证书只能由用户在腾讯云「SSL 证书」控制台下载（Nginx 格式、RSA，含 `aginice.cn` 与 `www.aginice.cn`）。步骤：本机只读预检（SAN、未过期且 ≥ 30 天、RSA 密钥与证书匹配、链在系统信任库里验证通过，不打印私钥）→ scp 到服务器 root 0700 的临时目录 → 以**文件**运行安装脚本（先备份旧对到 `/root/domain-switch-20261005/`，同样的校验，沿用原属主与权限，`nginx -t` 失败则还原并不 reload，reload 后用 SNI `aginice.cn` 核对 443 与 8443 的序列号与 `Verify return code: 0 (ok)`，再核对 `aigcnice.com` 站点与 hr-tracker 不受影响）→ `shred -u` 暂存的私钥。脚本在服务器 `/root/domain-switch-20261005/scripts/`（`cert_install.sh`、`verify_domain.sh`；本机副本 `outputs/ops-domain-switch-20261005/`，被 git 忽略，没有入仓库）；一次实跑的收据见 `HANDOFF.md`「2026-10-05 域名切换到 aginice.cn」。
- **Control API 端口是 `127.0.0.1:18791`，由 `MEMORIA_CONTROL_API_PORT` 带着走**（2026-10-05 的端口参数化改动）：compose 写 `127.0.0.1:${MEMORIA_CONTROL_API_PORT:-8791}:8000`；`scripts/release_ops.sh` 的 `control_api_port()` 依次取该环境变量、在跑的 `memoria-control-api-1` 的 `docker port … 8000/tcp`（只认 `127.0.0.1` 的绑定）、默认 8791，`verify-load` 把它写进新发布树的 `.env`（Compose 从项目目录读 `.env`），`cutover` 先断言候选 compose 发布的正是线上这个端口，不一致就拒绝、线上不动（新发布树若落回 8791 会与 hr-tracker 冲突，control-api 起不来、切流中途中断），`finish` 与 `scripts/refresh_readiness.sh` 也探这个端口。`verify-load` 的空闲端口预检默认 `28791` / `28891`（`MEMORIA_PREFLIGHT_API_PORT` / `MEMORIA_PREFLIGHT_NGINX_PORT` 可改），避开线上的 18791（旧的固定值 18791 / 18891 会让预检在新机上直接拒绝）。2026-10-05 16:31 部署的 `20261005-subject-candidates-v1` 树采用仓库原样加 `.env`（`MEMORIA_RELEASE_COMMIT`、`MEMORIA_RELEASE_TAG`、`MEMORIA_CONTROL_API_PORT=18791`）；当时的回滚树 `20261004-first-warm-v1` 里 compose、`refresh_readiness.sh`、`release_ops.sh` 三处仍手工改成 18791（原件留作 `.orig`），属于参数化前的历史做法。
- **release-ops.sh 与 PREV（脚本不再写 PREV，发布之间不用改它）**：`freeze` 从线上取证推出上一栈——`current` 软链指向的发布树、各容器的 compose 链标签、镜像的 OCI version / revision——写入 `$S/prev-release`（`tag=`、`commit=`、`services=` 三行，有覆盖时再加第 4 行 `overrides=`）。链只接受两种形态：基础 compose 本身，或基础 compose 加恰好一份白名单内的只换镜像覆盖。2026-10-21 起线上就是后者：control-api 是 `releases/20261020-minor-bypass-a94c438/docker-compose.production.yml` + `component-releases/20261021-a94c438-pr345-pr346-pr348-pr352/override-control-pr352.yml`（sha256 `95a5e033…`，`services.control-api.image`），bridge 是同一基础 compose + `component-releases/20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355/override-pr355.yml`（sha256 `895b17fe…`，`services.voice-core-media-bridge.image`），两份的 `/tmp` 原件分别是 `/tmp/override-control-pr352.yml`、`/tmp/override-pr355.yml`。覆盖必须只有 `services.<该服务>.image` 一个键、不能带 environment/volumes/ports 等任何其他键或别的服务，image 必须等于该容器实际运行的镜像；`/tmp` 路径只有在持久拷贝存在且两者 sha256 都等于白名单登记值时才接受，记录一律写持久路径。白名单不再放在脚本里，而是主机状态文件 `/opt/memoria/state/component-overrides.tsv`（每行 4 个空格分隔的列：服务、持久路径、sha256、`/tmp` 原件；0600、root 属主，权限或属主不对即整体拒绝读）。切换 release-ops.sh 版本后跑一次 `release-ops.sh migrate-component-overrides` 把历史行播种进状态文件（状态文件已存在且内容不同时拒绝执行，防的是顺手覆盖手工登记）；新的组件切换后用 `release-ops.sh record-override <服务> /opt/memoria/component-releases/<tag>/override-…yml /tmp/override-…yml` 登记：它要求持久文件与 `/tmp` 原件逐字节一致、覆盖只含 `services.<该服务>.image` 且 image 等于该容器实际运行的镜像，校验通过才以 flock 加临时文件原子追加（固定写 0600）；同一行 sha 不变的重复执行无事，sha 变了直接拒绝。状态文件缺行或有坏行时 `freeze` 以 `unexpected live chain`、`not whitelisted` 或 `malformed state file line` 拒绝，线上不动。有覆盖时 `prev-release` 追加第 4 行 `overrides=`，每个服务一组四个以空格分隔的字段 `服务名 持久覆盖路径 sha256 镜像ID`，不同服务的组依次相接；没有覆盖时仍只有三行，旧记录（无 `overrides=`）照常可用。覆盖服务的 OCI version/revision 是组件自己的标签，不参与 `tag=` 比对；当两个 ship 服务都被覆盖时，`commit=` 取 `current` 发布树 `.env` 的 `MEMORIA_RELEASE_COMMIT`，没有该文件时要求所有覆盖镜像的 revision 标签一致，否则拒绝。重跑 `freeze` 必须推出同一份记录，否则拒绝改写（切流之后再跑会因 control-api 已在新链上而被拒）；`cutover`、`finish`、`rollback` 只读这份记录，没有它就拒绝执行。唯一的旧例外是给绕过发布脚本上线的 `20261016-readiness-b54189c` 留的窄口子：镜像 version 为空、revision 是 40 位十六进制、tag 以 `-<revision 前 7 位>` 结尾且容器环境 `MEMORIA_RELEASE_TAG` 等于 tag 时，`freeze` 向 stderr 记一行 `accepting empty version label…` 后按通过处理，其余 version 不一致仍拒绝。**运维在 `env` 之前把这一行与 HANDOFF 记录的现网栈对一遍，不一致就停。** `rollback` 在动 env、`current` 或容器之前重算每个持久覆盖的 sha256，文件缺失或不符即拒绝，线上不动；随后为每个有覆盖的服务单独执行一条 compose（`-f 基础compose -f 该服务的持久覆盖`），避免把两份 `-f` 一起盖到每个容器上。重建并健康检查后，逐服务核对容器镜像 ID 等于记录值，不符即失败。主机 `/root/memoria-release/release-ops.sh` 目前是 2026-10-20 10:23 安装的版本（SHA-256 `3d39394d15069a30c868397bc0abbb63c9ecc8a93ac324cd28ed5de9919ade25`，与本次改动前的仓库版一致），它不认识 2026-10-21 的覆盖链。**下一次整栈发布前安装合并后的仓库版并记下它的 SHA-256**；那次 `freeze` 的 PREV 是 `20261020-minor-bypass-a94c438@a94c43813a8893a07bbe3bf4815cae52745cd88a`，`services=control-api voice-core-media-bridge`，并带 `overrides=` 行。
- **在新机上发整栈（2026-10-05 `20261005-subject-candidates-v1` 实跑的做法）**：登录用户是 `ubuntu`（免密 sudo），`/root/memoria-release` 在 root 下，所以每一步都包一层 `ssh memoria-prod "sudo sh -c 'env TAG=… COMMIT=… [VERIFIER_SHA=… MANIFEST_SHA=…] /root/memoria-release/release-ops.sh <step> > /root/memoria-release/<step>-<tag>.log 2>&1; echo exit=$? >> /root/memoria-release/<step>-<tag>.log'"`，再读日志里的 PASS；上传用 `scripts/upload_release_artifacts.sh`（它用 `sudo -n` 与 `rsync --rsync-path='sudo -n rsync'`，所以 `ubuntu` 能用；本机 `PATH=/opt/homebrew/bin:$PATH` 以用到新版 rsync），先 dry-run 再真传；增量（seeded）模式要求新机上有上一个 tag 的 `incoming/<tag>/images.tar`，没有就是整包模式（第一次是整包，线上发送 502 MB / 总 1.53 GB）；本机构建用 `DOCKER_DEFAULT_PLATFORM=linux/amd64 COMPOSE_PARALLEL_LIMIT=1`，这次还设了 `UV_DEFAULT_INDEX=https://mirrors.cloud.tencent.com/pypi/simple`；`verify-load`、`freeze` 不动线上，可以在合并 PR 之前跑（它们会把 `MEMORIA_CONTROL_API_PORT` 写进新发布树的 `.env`、在 28791 / 28891 起预检栈、做发布前 DB 备份），任何需要的修正还能进同一个 PR；`cutover` 前先确认没有设备会话；边缘日志里近期没有 `device=dev_` 不能证明没有空闲的设备 WSS（稳态连接不打日志），2026-10-09 的切流就断开了一条（边缘记 `device WSS socket error … use of closed network connection`），机器人下一次唤醒会重连。**恢复一次半截的发布时**，先只读核对主机：`docker images | grep <tag>`（`verify-load` 遇到已存在的同名 tag 镜像会拒绝）、`incoming/<tag>/` 里是否只有六个制品、`current` 与容器没动；残留的无标签镜像按 tag 删掉；镜像一律用显式 `docker build --platform linux/amd64` 重建并核对 revision / version / role 标签（见 `HANDOFF.md`「2026-10-09 整栈发布」）；`upload_release_artifacts.sh` 遇到已有的 `images.tar` 会走 `resume` 模式，结束时仍会逐个制品核对 sha256。
- **数据层**：两台机上 `memoria-data` 项目都是从 `releases/20260827-architecture-split-v1/infra` 起的。`current/infra` 里的同名 compose 多一个 `011-control-schema.sql` 的 initdb 挂载（只在空库首次初始化时用）；在已有数据的库上用 `current/infra` 去 `up -d` 会让 compose 判定配置变了而重建 postgres 容器（推断，没试），不要顺手做。
- **WAL 归档已关（P1-08，2026-10-06）**：`infra/memoria-data.production.yml` 的 postgres 参数是 `archive_mode=off`，不再有 `archive_timeout` / `archive_command`。原先每 5 分钟强制切一个 16 MB 段并拷进同盘的 `memoria-data_postgres_wal_archive` 卷：没有 base backup 可接，没有恢复价值，旧机实测每天涨约 0.6–1.1 GB。恢复靠每次整栈发布前 `freeze` 的 `memoria-pre-<tag>.dump`（见「数据层、备份与恢复」）。改这个参数必须重建 postgres 容器（`archive_mode` 要重启，`ALTER SYSTEM` 改不了：compose 的 `-c` 参数优先）；线上 compose 与仓库的同步和卷的清理是维护窗口里的一步，结果记在 HANDOFF。要重新启用（例如接异地备份 profile）：在 compose 里把 `archive_mode=on`、`archive_timeout=300s`、`archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f` 一并恢复，并保持该卷 `999:999 0700`，否则 `archive_command` 一直失败；同时要有 base backup 和按它裁剪的策略。
- **磁盘（最近一次只读核验：2026-10-10 23:31）**：40GB盘、已用25GB（65%）、剩14GB；09:40既有清理删掉20261007旧镜像tag/incoming/源码树、保留`.cutover`。`incoming/`保留`20261009-textless-spare-v1`、`20261010-stale-interim-v1`和`20261010-n8-endpoint-diag-v1`三个目录，镜像保留当前与一个可运行回滚；详细收据见`docs/acceptance/run-20261010-verify/findings.md`。WAL归档关掉后主要增长是每次发布约4–5GB（镜像+包）与库本身；不要手删`pg_wal`。`memoria-disk-patrol.timer`只在75%/85%告警、不自动清理，生产清理另授权。
- **systemd**：`memoria-readiness-refresh.timer`（12 小时一次提供方冒烟 + readiness 标记；迁移后 01:36:57 跑过一次，从新机出口 PASS）与 `memoria-disk-patrol.timer` 已在新机启用。`memoria-freeze-dump-cleanup.timer`（每日）应在下一次整栈发布时一并安装：只删 `/opt/memoria/releases/*/.cutover/` 下超过 3 天的 `memoria-pre-*.dump` 与 `memoria.pre-*.sqlite3`。`memoria-log-rotate.timer`（每小时轮转 `/var/log/memoria`）与 rsyslog drop-in `/etc/rsyslog.d/30-memoria.conf` 在第一次带 `logging:` 块的整栈发布里、`env` 之前安装，见「容器日志落盘」。`memoria-nightly-backup.timer`（每日 03:15）备份 PostgreSQL 与 SQLite 控制库到 `/var/lib/memoria/backups/nightly/`，只保留最近 7 天（详见下方「夜间数据库备份」）。

## 夜间数据库备份

### 夜间备份决策（2026-10-19 用户批准）

- **为什么做**：WAL 归档已于 2026-10-06 关闭，此前只有发布 `freeze` 步骤的 `pg_dump`（保留 3 天），发布间隔内没有备份；主机 40 GB 盘与 pocketSparks 共享。
- **关键决定**：
  - 每日 03:15 CST 由 systemd timer 触发（`RandomizedDelaySec=5min`、`Persistent=true`）。
  - 备份目录 `/var/lib/memoria/backups/nightly/`（`0700`），不放 release 目录，避免与 `cleanup_freeze_dumps.py` 的 3 天清理冲突。
  - PostgreSQL 用 `pg_dump -Fc`，SQLite 用 Python `sqlite3` backup API；只备份 `memoria.sqlite3`（与 freeze 一致；`evolution`、`identity` 两个 SQLite 属已退役服务）。
  - 文件名 `memoria-YYYYMMDD-HHMM.dump` 与 `control-YYYYMMDD-HHMM.sqlite3`。
  - 只保留最近 7 天：`age_days <= 6` 保留，满 7 天（含周日）全删，不再有周日至 30 天的第二档。唯一例外是最新一份 dump 与最新一份 control 永远保留，即使已超过 7 天；这样备份连续失败一周以上也不会把最后一份删光。
  - 最新一份 dump 或 control 超过 48 小时没更新时，`memoria-backup-freshness.timer`（每 6 小时）让 `memoria-backup-freshness.service` 进入 failed 并在 journal 打 `ALERT backup freshness:`。报警后必须在 7 天内修好备份并跑出一份新备份，否则「最多再留 7 天」的承诺不成立；修好后手动跑一次 `nightly_backup.sh`，再跑一次 `--check-freshness` 确认退出 0，并 `systemctl reset-failed memoria-backup-freshness.service`。
  - 容量约 38-45 MB，按 7 份重算：单份 dump 约 4.4 MB（freeze 的 `memoria-pre-*.dump` 实测 4,422,836 B）×7 ≈ 31 MB，control 单份约 1-2 MB ×7 ≈ 7-14 MB。
  - 可用空间 <1 GB 时跳过备份但仍做清理、退出 1；备份或校验失败退出 1。成功静默，失败看 `journalctl`。
  - **`daily_summaries` 的删除约束**：这张表可能含孩子原话（模型失败时 fallback 会把最多 3 句原话写进去），生产读写目前已全部关闭。若以后重新开写，按主体删除（`services/governance/subject_deletion.py` / `subject_archive.py`）、解绑删除（`services/control_api/app/routes/identity_lifecycle.py`）、恢复后重放删除（`scripts/replay_subject_deletions.py`）三条路径都必须覆盖这张表，并有测试。
- **不选的方案**：重开 WAL 归档（磁盘 0.6-1.1 GB/天扛不住）；云托管数据库（MVP 阶段成本）；只靠发布 freeze（RPO 1-3 天太长）。本方案 RPO 最多 24 小时。
- **未解决**：无异地备份；只有 systemd 单元 failed + journal 的新鲜度检查（`memoria-backup-freshness.timer` 每 6 小时跑一次，最新 dump 或 control 超过 48 小时未更新就让服务 failed），没有推送到手机或群，需要有人看 `systemctl --failed`；DB 到单次备份 >500 MB 或日增 >50 MB 时重评 WAL + base backup。
- **验收**：手动跑一次成功；`pg_restore --list` 能列对象；timer 连续 3 天无失败；首日备份满 7 天后由第 8 天凌晨的清理删除；模拟磁盘不足时跳过并记日志。

### 夜间备份安装与运维

每日 03:15 CST 自动备份两个库到 `/var/lib/memoria/backups/nightly/`：
- PostgreSQL：`memoria-<YYYYMMDD>-<HHMM>.dump`（pg_dump 自定义格式，约 4.4 MB）
- SQLite 控制库：`control-<YYYYMMDD>-<HHMM>.sqlite3`（约 1-2 MB）

保留策略：最近 7 天全保，满 7 天（含周日）全删。唯一例外是最新一份 dump 与最新一份 control 永远保留，即使已超过 7 天。总容量约 38-45 MB，按 7 份 dump（约 4.4 MB/份）+ 7 份 control（约 1-2 MB/份）估算。

**安装（一次性，需授权）：**

```bash
# 1. 在生产主机上创建备份目录
sudo install -d -m 0700 /var/lib/memoria/backups/nightly

# 2. 从仓库复制脚本
sudo install -d -m 0755 /opt/memoria/ops-tools
sudo install -m 0700 scripts/nightly_backup.sh scripts/nightly_backup_retention.py /opt/memoria/ops-tools/

# 3. 安装 systemd 单元
sudo cp infra/memoria-nightly-backup.service /etc/systemd/system/
sudo cp infra/memoria-nightly-backup.timer /etc/systemd/system/
sudo cp infra/memoria-backup-freshness.service /etc/systemd/system/
sudo cp infra/memoria-backup-freshness.timer /etc/systemd/system/

# 4. 启用与启动定时任务
sudo systemctl daemon-reload
sudo systemctl enable memoria-nightly-backup.timer
sudo systemctl start memoria-nightly-backup.timer
sudo systemctl enable --now memoria-backup-freshness.timer

# 5. 验证
sudo systemctl status memoria-nightly-backup.timer
sudo systemctl list-timers memoria-nightly-backup.timer
```

**监控与故障排查：**

```bash
# 查看最近备份运行状态
sudo systemctl status memoria-nightly-backup.service

# 查看完整日志
sudo journalctl -u memoria-nightly-backup.service -n 100

# 手动触发一次备份（测试）
sudo /opt/memoria/ops-tools/nightly_backup.sh

# 查看备份文件
ls -lh /var/lib/memoria/backups/nightly/

# 列出将要被删除的备份（dry-run）
python3 /opt/memoria/ops-tools/nightly_backup_retention.py --backup-dir /var/lib/memoria/backups/nightly/

# 手动检查备份新鲜度（全部新鲜退出 0，过期或缺失退出 2）
sudo python3 /opt/memoria/ops-tools/nightly_backup_retention.py --backup-dir /var/lib/memoria/backups/nightly --check-freshness

# 查看新鲜度检查状态与日志
sudo systemctl status memoria-backup-freshness.service
sudo journalctl -u memoria-backup-freshness.service
```

**内测阶段每日人工检查（招 30 户前必须换成会有人收到的提醒）：**

每天查一次新鲜度定时器和服务，发现 `failed`、`ALERT backup freshness:` 或超 48 小时没有新备份，把输出贴到 Memoria总部群：

```bash
sudo systemctl status memoria-backup-freshness.timer
sudo systemctl status memoria-backup-freshness.service
sudo systemctl list-timers memoria-backup-freshness.timer
sudo journalctl -u memoria-backup-freshness.service -n 100
```

现在 failed 只在主机上可见，不推手机也不进群；招 30 户之前必须接一个有人会收到的提醒渠道，否则夜间的备份失败可能一周都没人发现。

### 从备份恢复

恢复顺序固定，不能跳步：

1. **停流量。** 停 control-api 与设备/小程序写入，确认没有新的归档、记忆或删除请求；恢复和 replay 期间的新写入会让账本与数据再次分叉。
2. **导出线上账本快照。** 在仍可读的线上控制库上导出 `status=completed` 的删除记录。快照含 `subject_id`/`account_id`，放 root 0700 目录、文件 0600，用完即删。stdout 只有计数。
3. **恢复。** PostgreSQL 用夜间 `memoria-*.dump`；控制库是 SQLite 时再用 `control-*.sqlite3`。先按下面「恢复 PostgreSQL 到临时库验证」核对，再按授权恢复到生产库/文件。
4. **合并快照并 replay。** `--ledger-snapshot` 先把快照里恢复后账本缺失的 completed 行插入（已存在的不重复插入，幂等），再跑完整删除 saga。stdout 只有 `merged`/`existing`/`replayed`/`incomplete` 计数，不打印 `subject_id`/`account_id`。
5. **检查 `incomplete=0`。** 非零时脚本退出 1；不要恢复流量，先让删除 worker 续跑并重跑这一步。
6. **删快照。** 用 `shred -u` 删文件并删临时目录。
7. **恢复流量和投影重建。** 先 `python -m scripts.rebuild_memory_projections --confirm-rebuild`，再恢复流量。

命令在 Control API 一次性容器里执行；镜像需含本分支的 `scripts/replay_subject_deletions.py`，旧镜像按本节末尾的挂载说明处理：

~~~bash
SNAPSHOT_DIR=/root/memoria-restore
sudo install -d -m 0700 "$SNAPSHOT_DIR"

# 2. 导出线上账本（只读；stdout 只有 {"exported": N}）
docker compose -f docker-compose.production.yml --profile media-runtime run --rm --no-deps \
  --user 0:0 -v "$SNAPSHOT_DIR:/snapshot" \
  control-api /app/.venv/bin/python -m scripts.replay_subject_deletions \
  --export-ledger /snapshot/subject-deletions.json

# 3. 恢复 PostgreSQL 与（如使用）SQLite 控制库（按授权执行；恢复前停 control-api）

# 4. 合并快照并 replay（stdout 只有计数；incomplete 非零时退出码 1）
docker compose -f docker-compose.production.yml --profile media-runtime run --rm --no-deps \
  -v "$SNAPSHOT_DIR:/snapshot:ro" \
  control-api /app/.venv/bin/python -m scripts.replay_subject_deletions \
  --ledger-snapshot /snapshot/subject-deletions.json --confirm-replay

# 6. 删快照
sudo shred -u "$SNAPSHOT_DIR/subject-deletions.json"
sudo rmdir "$SNAPSHOT_DIR"
~~~

**线上账本不可用时（例如整库损坏才恢复）：** 导出这一步做不了，只能使用备份时刻的账本。备份之后完成、只存在于线上账本里的删除不会出现在恢复后的账本中，replay 覆盖不到，这些数据会复活。这是已知缺口，不要声称已经覆盖；在恢复记录里写明备份时间、无法导出的原因和这段窗口，并按合规流程人工核查/补删。只要线上控制库还能读，就不要走这条路径。

**daily_summaries。** 该表在控制库按 `user_id`（账户）存，没有 `subject_id`。subject 删除 saga 不删它。孩子没有独立账户时，摘要挂在家长账户下，subject 删除不涉及该表；孩子有独立账户时，`guardian.py` 的删除路径走 `delete_account(minor_user_id)`，由 `profiles` 的 `ON DELETE CASCADE` 删除该账户的摘要。replay 同样不操作该表。

**向量。** replay 的 saga 按捕获的 lineage 删除 `memory_vector_documents`（`source_event_id IN` 或合并条目的 `item_id IN`）；replay 结束后还会按 `subject` 与 `archive_evidence_events.subject_id` 的 join 再删一次，作为未合向量删除 PR 的兜底。证据行已经不在的孤儿向量无法按 subject 归属，留给下一次投影重建。

**恢复 PostgreSQL 到临时库验证：**

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

**恢复 SQLite 控制库到临时文件：**

```bash
# 选择一份备份
BACKUP=/var/lib/memoria/backups/nightly/control-20261019-0315.sqlite3

# 只读打开，做完整性检查并列出各表行数（主机没有 sqlite3 命令行，用 python3）
sudo python3 - "$BACKUP" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for (name,) in c.execute("select name from sqlite_master where type = 'table' order by 1"):
    print(name, c.execute(f'select count(*) from "{name}"').fetchone()[0])
PYEOF
```

## 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. **手动补一条 tag 名下的 CI 收据**。ci.yml 对 main 不再跑 push 触发，PR run 都挂在分支头，main 收下合并没有自己的 run（2026-10-23 发现 99b8d32a 名下无收据）。发布的目标提交若是 merge commit（不在任何分支），先 `git push origin <sha>:refs/heads/deploy/<tag>-ci-receipt` 再 `gh workflow run ci --ref <该分支>`（workflow_dispatch 的 `--ref` 不收裸 sha，HTTP 422）。收据是 run URL；分支留着（deploy/* 前缀约定）。
3. 在候选镜像跑 Agent 发布门（`scripts.verify_agent_release_artifact`：bridge 与提供方适配器不导入 livekit、自有媒体遥测隐私 canary、DTLN 去噪器可加载）；镜像解析显式给 expected candidate 并对齐有效 overrides。已有自动门禁接线，当前候选/生产复验要求见 P1-01。
4. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
5. dry-run→上传校验→授权切流→候选 provider smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。
5. 对外声称「我们自己的服务器不保存录音和原话」之前：每次更换服务器或改动与存储相关的代码后，都要在目标服务器上重新核查存储。最近一次核查：aginice.cn 测试服务器，2026-10-14（见 docs/compliance/deletion-record-20261014.md）。另外，「机器人本身不存录音」这句话依赖当前固件：不挂载 SD 卡，也不把音频写入 flash。任何固件存储改动（例如启用 SD 卡、加入离线缓存、修改分区表）都要像服务器规则一样重新核查这句话。试用机的 SD 卡槽要封住。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片，只切 `voice-core-media-bridge`；线上 bridge 可以是整栈镜像（不带 `com.memoria.release.kind`，且 version 必须等于该容器的 `MEMORIA_RELEASE_TAG`），也可以是组件或回滚恢复镜像；已存在的同名回滚 tag（`release_ops.sh` 也用 `rollback-<tag>-pre` 命名上一栈）只有与当前 bridge 镜像 image id 相同时才复用。`--dry-run` 不检查线上 bridge，这一步的拒绝只在 `--cutover` 出现，且发生在任何切换之前。默认要求运行时底座与线上 bridge 是不同镜像，否则拒绝 `runtime base image is missing, has invalid provenance, or is not independent`；手工在发布机上构建的候选（如 `memoria-agent:20261021-a94c438-pr345`）底座就是线上 bridge 本身，需显式加 `--allow-manual-base-image --reason TEXT`（必须同时给 `--cutover` 和非空理由），此时回滚到上一个镜像等于连底座一起回滚。该决定会写进本地预检日志、远端 `ROLLBACK_POINT.txt` 与 `CUTOVER_RESULT.txt`（`manual_base_image_accepted` / `manual_base_image_reason`），其余检查不变；不带该选项的行为不变。`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

`scripts/deploy_control_component.sh`（control-api 单组件发布）会让 control-api 落在组件链上。`release_ops.sh` 的 `freeze` 只认白名单（主机状态文件，见「release-ops.sh 与 PREV」）里已登记的覆盖链，并不通用地接受任意组件链；新的 control-api 单组件发布会落在基础 compose + 未登记覆盖上，下一次整栈发布会在 `freeze` 被拒。做新的 control-api（或 bridge）组件发布并停留在组件链时，切换后先 `record-override <服务> <持久路径> </tmp/原件>` 把该覆盖的持久路径与 sha256 登记进状态文件，并核对 `freeze` 认得它；整栈发布之后该服务回到纯整栈链，登记过的行留在文件里不影响整栈（只换镜像的旧覆盖仍指向旧镜像，不再出现在新 PREV 的链上）。

运行门禁需剥离本地 `OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。门禁里的 pytest 在 macOS 上同样可跑：进程采样经 `scripts/local_process_probe.py`，Linux 读 `/proc`，macOS 读 libproc 的 RSS 与存活并用一次连接探测证明监听端口已关闭，ESTABLISHED 连接数记为未测量（`null`，不是 0），不因平台被跳过。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

## 完整制品上传与校验

构建机先从干净 tag 生成 source/images，再生成 portable verifier 和绑定 source/images 的 manifest；上传脚本要求 `source.tar.sha256` 与 `images.tar.sha256` 成对存在：

~~~bash
docker save -o "$ARTIFACT_DIR/images.tar" \
  memoria-agent:$RELEASE_TAG memoria-control-api:$RELEASE_TAG
python3 scripts/verify_release_source.py \
  --expected-commit "$SOURCE_COMMIT" --release-tag "$RELEASE_TAG" \
  --create-archive "$ARTIFACT_DIR/source.tar"
uv run python scripts/package_release_verifier.py \
  --expected-commit "$SOURCE_COMMIT" --release-tag "$RELEASE_TAG" \
  --output "$ARTIFACT_DIR/release-verifier.pyz"
uv run python scripts/create_release_manifest.py \
  --release-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --output "$ARTIFACT_DIR/release-manifest.json"
for artifact in source.tar images.tar; do
  (cd "$ARTIFACT_DIR" && sha256sum "$artifact" > "$artifact.sha256")
done
for artifact in source.tar images.tar release-manifest.json release-verifier.pyz; do
  (cd "$ARTIFACT_DIR" && sha256sum "$artifact")
done
~~~

macOS 构建机没有 `sha256sum` 时用 `shasum -a 256`（输出格式相同）。

`images.tar + images.tar.sha256` 必须成对上传。先 dry-run，再 seeded upload；不要对 basis 使用 `rsync --inplace`，失败不得污染不可变基座。

~~~bash
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG" --dry-run
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG"
~~~

构建机可信摘要须经已认证运维通道提供；先验 verifier/manifest，再运行 verifier（导入前），`docker load` 后再带 `--verify-imported-images` 复验，成功后才解包。`source.tar` 带 `memoria/` 前缀，解包到发布目录时去掉一层：

~~~bash
: "${MEMORIA_RELEASE_VERIFIER_SHA256:?required}"
: "${MEMORIA_RELEASE_MANIFEST_SHA256:?required}"
printf '%s  %s\n' "$MEMORIA_RELEASE_VERIFIER_SHA256" "$UPLOAD_DIR/release-verifier.pyz" | sha256sum -c -
printf '%s  %s\n' "$MEMORIA_RELEASE_MANIFEST_SHA256" "$UPLOAD_DIR/release-manifest.json" | sha256sum -c -
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT"
docker load -i "$UPLOAD_DIR/images.tar"
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --verify-imported-images
install -d -o root -g root -m 0755 "/opt/memoria/releases/$RELEASE_TAG"
tar --extract --file "$UPLOAD_DIR/source.tar" \
  --directory "/opt/memoria/releases/$RELEASE_TAG" --strip-components=1 --no-same-owner
~~~

不允许手工 retag 未绑定 manifest 的模型镜像。切流后运行 `scripts/smoke_server_deployment.sh`、真实 provider smoke、健康/私有 readiness/外部路由和延迟复核。

## 容器日志落盘（control-api 与 voice-core-media-bridge）

control-api 与 voice-core-media-bridge 的 stdout/stderr 由 Docker 的 syslog 驱动经 UDP 发到本机 rsyslog 的专用输入 `127.0.0.1:5514`，按服务写进两个文件。容器重建不影响这两个文件：

- `/var/log/memoria/control-api.log`
- `/var/log/memoria/voice-core-media-bridge.log`

一行形如 `2026-10-17T10:02:03.123456+08:00 voice-core-media-bridge/3f2a1b9c8d7e INFO:...`。时间是 Docker 收到这一行的本机时间，斜杠后是写这一行的容器 id 前 12 位。重建后 id 变，文件接着写，同一文件里 id 换掉的地方就是重建点。`release-ops.sh env` 的一次性容器（Control 配置校验、`verify_env`、声纹开关检查、提供方冒烟）用候选 compose 的 `logging:` 块，也写进这两个文件。media-edge、device-state-redis、media-slo-reporter 不变，仍用 daemon 默认驱动。

| 项目 | 数字 | 所在文件 |
|---|---|---|
| 当前文件轮转 | 超过 20 MiB（每小时查一次）或每天一次 | `infra/memoria-logs.logrotate`（`maxsize 20M`、`daily`） |
| 保留的轮转份数 | 7 份，`.2` 起 gzip | `infra/memoria-logs.logrotate`（`rotate 7`、`compress`、`delaycompress`） |
| 轮转文件的年龄上限 | 7 天 | `infra/memoria-logs.logrotate`（`maxage 7`） |
| 一行在主机盘上最长留存 | 约 8 天（当前文件最多 1 天，加 7 次每日轮转）；timer 停跑的日子另计 | 由上三行推出 |
| 每个服务的主机上限 | 最坏约 160 MiB 未压缩，另加一小时的写入 | 由上推出 |
| Docker 本地缓存（`docker logs` 读它） | 每个容器 30 MiB，随容器删除 | `docker-compose.production.yml`（`cache-max-size: "10m"`、`cache-max-file: "3"`） |
| 主机文件里的单行长度 | 超过 8 KiB 截断（rsyslog 默认）；Docker 缓存里是整行 | 未设置 |

目录 `syslog:syslog 0700`，文件 `0600`。属主是 syslog，root 也能读，登录用户读不了。`ubuntu` 登录后一律 `sudo`；目录里的通配符要放进 `sudo sh -c '…'` 才能展开。目录放在 `/var/log` 下，是为了落在 rsyslogd 已有的写入范围里（Ubuntu 的 AppArmor 配置按 `/var/log` 放行，本机未核对，安装核对会证明）。轮转由 `memoria-log-rotate.timer` 每小时执行，用独立状态文件 `/var/lib/logrotate/memoria-logs.status`。配置不在 `/etc/logrotate.d`，系统每天的 logrotate 不会再轮转一次。配置不写 `notifempty`，并总是 `create` 当前文件，所以停写之后每天照样轮转，旧文件照样按份数和年龄删掉。

**`docker logs` 照常可用。** Docker 20.10 起，非本地驱动也写一份本地缓存（dual logging），`docker logs -t`、`--since`、`--tail` 读它。浸泡与时延脚本（`scripts/voice_soak.py`、`scripts/voice_commit_timing.py`、`scripts/voice_soak_pause_split.py`、`scripts/voice_session_capture.py`）和 `wait_healthy` 的诊断不用改。这份缓存随容器删除，跨重建的历史只在 `/var/log/memoria`。若 `daemon.json` 设了 `cache-disabled`，`docker logs` 会报 `configured logging driver does not support reading`，上面这些脚本都会失败，所以预检先看它。

**什么时候会丢行。** UDP 不会阻塞写日志，也不会让容器起不来。代价是 rsyslog 不在的时候（开机顺序、重启 rsyslog、drop-in 被换掉），这段时间的行不进主机文件，只留在 Docker 缓存里。没有常驻告警，下一次整栈发布的 `env` 会发现。每次重启 rsyslog 之后跑一次：

~~~bash
sudo journalctl -u docker --since -10min | grep -c 'Error writing log message'   # 0
~~~

不为零时，dockerd 可能把发送失败的那几行连同原文记进了共用的 journald。按下文「事故处理」算进事故范围；journald 的清理另行授权，因为主机共用。

**只随整栈发布生效。** `logging:` 块在 `docker-compose.production.yml` 里。组件发布只接受只换镜像的 override，并按哈希固定基座 compose，带不上这个块。第一次带这个块的 `release_ops.sh` 整栈发布之后，基座提交含此块的组件发布沿用它。主机 `/root/memoria-release/release-ops.sh` 必须先换成合并后的版本（见「生产主机」一节），否则 `env` 里没有下面的门。新主机用 `scripts/bringup_shared_host.sh` 起栈、且发布树含此块时，先装主机部分再跑 `apps`；脚本本身不检查。

### 只读预检

~~~bash
systemctl is-active rsyslog                                  # active
ps -o user= -C rsyslogd                                      # syslog
sudo grep -rn 'imudp' /etc/rsyslog.conf /etc/rsyslog.d/      # 无输出；已有时去掉 drop-in 里 module(load="imudp") 一行
sudo ss -Hlun 'sport = :5514'                                # 无输出
docker version --format '{{.Server.Version}}'                # 20.10 或更高
sudo cat /etc/docker/daemon.json                             # 不得含 cache-disabled；只读，不改
docker info --format '{{.LoggingDriver}}'                    # 记下；其他容器继续用它
test -x /usr/lib/rsyslog/rsyslog-rotate && logrotate --version
grep -n RepeatedMsgReduction /etc/rsyslog.conf               # 记下；安装核对会测它是否吞掉重复行
~~~

任何一项不符就停，不安装。

### 安装（另行授权）

在本次发布的 `verify-load`、`freeze` 之后、`env` 之前，从本次发布树运行：

~~~bash
cd "/opt/memoria/releases/$TAG"
sudo install -d -o syslog -g syslog -m 0700 /var/log/memoria
sudo install -m 0644 infra/rsyslog-memoria.conf /etc/rsyslog.d/30-memoria.conf
sudo rsyslogd -N1 && sudo systemctl restart rsyslog
sudo journalctl -u docker --since -10min | grep -c 'Error writing log message'   # 0
sudo install -d -m 0755 /opt/memoria/ops-tools
sudo install -m 0644 infra/memoria-logs.logrotate /opt/memoria/ops-tools/memoria-logs.logrotate
sudo install -m 0644 infra/memoria-log-rotate.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-log-rotate.timer
sudo systemctl start memoria-log-rotate.service && echo rotate=PASS
for _ in 1 2; do logger -d -n 127.0.0.1 -P 5514 --rfc5424 -t control-api/install-check 'memoria log sink install check'; done
sudo tail -n 2 /var/log/memoria/control-api.log              # 两行，都含 control-api/install-check
~~~

只有一行、或出现 `message repeated`，说明 `$RepeatedMsgReduction` 作用到了这两个文件。这时在 drop-in 第一行加 `$RepeatedMsgReduction off`、最后一行加 `$RepeatedMsgReduction on`，重装并重做核对（本机未试过）。核对写入的两行留在文件里，随轮转删除。

### 发布时的门

`release-ops.sh env` 在第一个一次性容器之前记下两个文件的大小。声纹开关检查之后、提供方冒烟之前，它要求两个文件里新出现本次的标记行：`control-api/<12 位 id> … control_env_valid=PASS` 与 `voice-core-media-bridge/<12 位 id> … speaker_authority_disabled=PASS`。缺一个就失败，线上不动：

~~~text
no control-api line with control_env_valid=PASS reached /var/log/memoria/control-api.log; install the host log sink (docs/runbooks/release-rollback.md, 容器日志落盘) and rerun env
~~~

轮转把当前文件改名为 `.1` 时，门从快照偏移接着读 `.1`。再轮转一次进 `.2` 才会漏掉标记，重跑 `env` 即可。

提供方冒烟（`env`、`finish` 的 readiness 刷新、`memoria-readiness-refresh.timer`）测的是这一个部署实际会跑的引擎。`TTS_PROVIDER` 只有一个合法值，冒烟永远说豆包；ASR 由 `ASR_PROVIDER` 决定，`funasr` 走 FunASR 冒烟，`volc` 走 Volc 冒烟。

冒烟在调用任何供应商之前先打一行 `voice_engines asr=… tts=…`。发布门用它和仓里脚本按同一份 env 推出来的 PASS 行比对，Control API 拒收引擎与自身配置不符的标记（409），bridge 心跳里带的引擎与 Control 配置不一致时 `/health/ready` 报 `engine_mismatch`。所以「冒烟测了一个引擎、线上跑的是另一个」这一类错配会在发布时或第一次心跳里暴露，不会等设备说话。

2026-10-21 21:49 火山把这个 app 的 seedtts 授权收回后，冒烟每 12 小时失败一次，readiness 从 10-22 起一直是 `not_ready` / `smokes=expired`，`env` 也被挡住；授权恢复后自动回到 PASS。当时冒烟硬编码测豆包，而 StepFun 是主用，那是这段历史的起因。现在只有豆包一条路，同样的授权收回会直接让冒烟失败，这是对的。

### 读日志

~~~bash
ssh memoria-prod 'sudo tail -F /var/log/memoria/voice-core-media-bridge.log'
ssh memoria-prod "sudo sh -c \"zgrep -h 'media turn committed' /var/log/memoria/voice-core-media-bridge.log* | sort\""
ssh memoria-prod "docker inspect -f '{{.Id}}' memoria-voice-core-media-bridge-1 | cut -c1-12"   # 当前容器写在哪个 id 下
~~~

行首是时间，同一时区偏移下 `sort` 之后就是时间顺序。

### 事故处理：日志里出现孩子原话

1. 停写：按「回滚二」第 ① 步换上关闭版 drop-in。
2. 删除并核对：

~~~bash
sudo find /var/log/memoria -type f -delete
sudo find /var/log/memoria -type f | wc -l       # 0
~~~

3. 清 Docker 缓存。缓存随容器删除：经授权，用线上同一棵发布树重建这两个容器，`sudo env MEMORIA_RELEASE_TAG=… MEMORIA_RELEASE_COMMIT=… docker compose -f docker-compose.production.yml --profile media-runtime up -d --no-deps --no-build --force-recreate control-api voice-core-media-bridge`（与「media-edge 组件发布」第 3 步一样必须 `sudo env`）。重建后 `docker logs` 里不应再有那段原文。
4. journald：跑上面的 `journalctl -u docker … | grep -c 'Error writing log message'`，不为零另行授权处理。
5. 恢复写入：重装开启版 drop-in（「安装」一节第 3、4 行），重做安装核对。时间、范围和核对结果记进 `HANDOFF.md`。

### 回滚

**回滚一：发布回滚。** `release-ops.sh rollback` 的流程不变；PREV 带 `overrides=` 时按记录逐服务追加该服务的持久 `-f`，执行前校验 sha256，回滚后核对镜像 ID（见「release-ops.sh 与 PREV」）。注意它回到的是**整栈** PREV：下一次整栈发布的 PREV 是 `20261020-minor-bypass-a94c438`，记录自带上面两条覆盖，两个服务都会回到 `20261021-a94c438-…` 组件镜像；只想退单个组件时用对应组件脚本的 rollback。回滚到的 PREV 若没有 `logging:` 块（第一次带此块的发布就是这样），两个容器回到 daemon 默认驱动，`docker logs` 照常。`/var/log/memoria` 里已有的文件不再增长，timer 照常每天轮转，约 8 天内按份数删完，不需要手动操作。要立刻清掉，做「回滚二」第 ② 步。主机部分留着不碍事，下一次带此块的发布直接用它。

**回滚二：拿掉这项功能。** 按顺序做，不能换。

① 立刻停写，端口仍在听：

~~~bash
cd "/opt/memoria/releases/$TAG"                  # 任一含 infra/rsyslog-memoria-off.conf 的发布树
sudo install -m 0644 infra/rsyslog-memoria-off.conf /etc/rsyslog.d/30-memoria.conf
sudo rsyslogd -N1 && sudo systemctl restart rsyslog
sudo journalctl -u docker --since -10min | grep -c 'Error writing log message'   # 0
sudo sh -c 'stat -c "%s %n" /var/log/memoria/*.log; sleep 60; stat -c "%s %n" /var/log/memoria/*.log'   # 两次相同
~~~

关闭版只丢弃，不关端口。容器若往关闭的端口发，dockerd 可能把发送失败的行连同原文记进共用的 journald。

② 处理已有文件。默认删除：

~~~bash
sudo find /var/log/memoria -type f -delete
sudo find /var/log/memoria -type f | wc -l       # 0
~~~

要留作排障就不删，但 timer 必须一直开着，直到 `sudo sh -c 'zcat -f /var/log/memoria/* 2>/dev/null' | wc -c` 为 0（停写后最多约 8 天）。不能先停 timer：停了就没有东西再删这些文件。

③ 从仓库拿掉：一个 PR 同时删去两个 `logging:` 块；`release_ops.sh` 里的 `HOST_LOG_DIR`、`host_log_size`、`assert_host_log_has`，以及 `step_env` 里记大小和两次断言的几行；两处测试；`infra/` 下的五个文件；本节，以及 `docs/compliance/child-audio-transcript-ephemeral.md` 里对应的句子。然后走一次整栈发布。在这之前，`env` 会拒绝任何仍带 `logging:` 块的候选，因为关闭版让标记行进不了文件。这是有意的。

④ 那次发布 `finish` 之后，已经没有容器往 5514 发，再拆掉主机部分：

~~~bash
sudo sh -c 'zcat -f /var/log/memoria/* 2>/dev/null' | wc -c   # 0；不为 0 就回到第 ② 步，或等 timer 删完
sudo rm /etc/rsyslog.d/30-memoria.conf
sudo rsyslogd -N1 && sudo systemctl restart rsyslog
sudo ss -Hlun 'sport = :5514'                    # 无输出
sudo systemctl disable --now memoria-log-rotate.timer
sudo rm /etc/systemd/system/memoria-log-rotate.service /etc/systemd/system/memoria-log-rotate.timer \
  /opt/memoria/ops-tools/memoria-logs.logrotate /var/lib/logrotate/memoria-logs.status
sudo systemctl daemon-reload
sudo find /var/log/memoria -type f -delete && sudo rmdir /var/log/memoria
~~~

第 ④ 步之后若要回滚那次发布：PREV 带着 `logging:` 块，容器又会往 5514 发。先装回关闭版 drop-in（第 ① 步的前三行），再回滚。

## media-edge 组件发布（手工，只换 media-edge 一个容器）

**删镜像前的核对规则（2026-10-23）**：删除任何 `memoria-*` 镜像标签之前，先核对最新 `.cutover/prev-release` 的 `overrides=` 记录里每个 image id 仍能 `docker image inspect` 成功。回滚只 recreate `services=` 名单里的服务、逐服务按记录追加覆盖 `--no-deps --no-build`，所以只要覆盖镜像还在，基础标签（整栈同名 tag）被删不影响回滚起得来；但覆盖镜像一旦丢失，`verify_prev_image_ids` 会直接拒。composite 合并配置里出现的其他标签（如 media-edge / media-slo-reporter 解析出的整栈 tag）不参与 recreate，允许被清。10-22 的清理就删掉了 `memoria-{control-api,agent,media-edge}:20261020-minor-bypass-a94c438`，14:1x 复核确认回滚仍完整可跑。

media-edge 不在 `release_ops.sh` 的整栈切流里，镜像按组件 tag 单独换，后续整栈发布没有换过它。编排链以线上容器为准：`docker inspect memoria-media-edge-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'`（2026-10-02 起是 `releases/20260930-local-stop-v2/docker-compose.production.yml` + `component-releases/<tag>-media-edge/media-edge-component.override.yml`）。收据样例见 `docs/HANDOFF-archive-0924-1003.md`「2026-10-02 media-edge 组件发布 20261002-late-progress-v1」。

1. 干净 detached worktree 在要发布的提交上构建（`DOCKER_DEFAULT_PLATFORM=linux/amd64`，导出 `MEMORIA_RELEASE_TAG/COMMIT`，`docker compose -f docker-compose.production.yml --profile media-runtime build media-edge`）；核对镜像的 revision/version/role 标签，用 `docker cp` 取出 `/usr/local/bin/memoria-media-edge` 确认含新代码并记下 sha256；`docker save` 后 scp 到 `/opt/memoria/incoming/<tag>-media-edge/`，服务器上 `sha256sum -c` 通过再 `docker load`（服务器 image id 与本机 containerd 存储里的不同，以标签和二进制 sha256 为准）。
2. 在 `/opt/memoria/component-releases/<tag>-media-edge/` 写 `media-edge-component.override.yml`（`services.media-edge.image` 指向新 tag）与 `media-edge-rollback.override.yml`（当前线上的 tag），并把切前全部容器的「名字 镜像 启动时间 重启次数」快照存为 `pre-all.txt`。
3. 确认设备没在用（edge 近 10 分钟没有 `device=dev_…` 日志），再在上述 compose 树里切换：`sudo env MEMORIA_RELEASE_TAG=<该树的栈 tag> MEMORIA_RELEASE_COMMIT=<提交> docker compose -f docker-compose.production.yml -f <组件 override> --profile media-runtime up -d --no-deps --no-build media-edge`。必须 `sudo env`：`ubuntu` 读不了 `/etc/memoria-agent.env`；`docker compose config` 的渲染含 env 展开值，只取需要的行并立即删除渲染文件。
4. 验收：存 `post-all.txt` 并与 `pre-all.txt` 对比，只有 media-edge 变；healthy、restarts 0；运行中二进制的 sha256 等于构建出的（容器没有 shell，用 `docker cp` 取出）；启动日志无 WARN/ERROR；外部 readiness 200、未带凭证的设备入口 401；设备重连与真机对话等下次唤醒。
5. 回滚：同一条切换命令，把组件 override 换成 `media-edge-rollback.override.yml`。

## 数据层、备份与恢复

仅在需要启动/恢复且获授权时，先创建 runtime 共享网络，再从真实目录启动数据层，避免异地工作目录建错卷：

~~~bash
DATA_COMPOSE_DIR=/opt/memoria/current/infra
cd /opt/memoria/current
docker compose -f docker-compose.production.yml create --no-build
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" up -d
~~~

- 用户 2026-09-14 决定验证阶段暂缓自动备份/异地副本；最后核查 offsite profile 未启用、无真实 endpoint/告警。真实家庭数据、正式发布或价值/量级增长前必须重评，不把同机副本称为异地灾备或已验证 PITR。
- 当前保留的本地还原点只在新机的旧机归档包 `/root/old-host-20261005/var-backups-memoria.tar.gz`（旧机 `/var/backups/memoria` 整体打包，root 0700，2026-10-05 迁移时取）里：`drill-20260914-p0-02/base`（2026-09-14 通过 `pg_verifybackup`、隔离启动、应用表/对象核对，报告 `outputs/acceptance/run-20260914-p0-02-restore-drill/report.json`）和 `20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump`；它们不会自动更新。09-14 base 的 WAL 链只在旧机上，旧机到期后消失，所以这个 base 只能还原到演练时刻，没有时间点恢复能力。最近一份发布前 freeze 逻辑转储为 2026-10-10 22:18 的 `memoria-pre-20261010-n8-endpoint-diag-v1.dump`（4,445,037 B，发布收据 SHA-256 `36344e4d…`），在 `/opt/memoria/releases/20261010-n8-endpoint-diag-v1/.cutover/`；10-11 09:22只读复核文件时间/大小，完整摘要见10-10 findings。此前07:57旧栈转储为4,427,691 B / `cc7217ae…`；10-04 18:15转储在旧机归档目录的 `release-receipts.tar.gz` 里。新机上没有持续的数据库备份。
- **Freeze 转储与迁移备份保留 3 天**：`freeze` 把 `memoria-pre-<tag>.dump` 与 `memoria.pre-<tag>.sqlite3` 都写进该发布树的 `.cutover/`（不再留在 `/var/lib/memoria`），迁移备份落在 `/var/backups/memoria/migrations/` 或发布树 `.control-store-*`。`scripts/cleanup_freeze_dumps.py` 扫描 `/opt/memoria/releases/*/{.cutover,.control-store-*}/` 以及 `/var/backups/memoria/migrations/`，自动清理 mtime 超过 3 天的转储与迁移备份；`freeze` 结束时会跑一次，`release_ops.sh cleanup-freeze-dumps` 与 `memoria-freeze-dump-cleanup.timer`（每日）也会跑。因此**用 freeze 转储或迁移备份做库级回滚/恢复只在转储未满 3 天时可行**；更早的转储与临时迁移备份会被定时任务自动清除。安装基线（执行仍需授权）：

~~~bash
sudo install -d -m 0755 /opt/memoria/ops-tools
sudo install -m 0755 scripts/cleanup_freeze_dumps.py /opt/memoria/ops-tools/cleanup_freeze_dumps.py
sudo install -m 0644 infra/memoria-freeze-dump-cleanup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-freeze-dump-cleanup.timer
~~~

- WAL 归档已关（P1-08，2026-10-06，见上文「生产主机」）：没有 base backup 的归档接不上任何还原点，所以不再产生。没有时间点恢复能力是已知现状：恢复点只有上述每次发布前的 `freeze` 转储（且仅 3 天内）和归档包里的 09-14 base。重新启用必须同时有 base backup 与按它裁剪的策略；禁止只按文件年龄删 WAL，`pg_wal` 里的段由 PostgreSQL 自己回收。
- 重新启用备份时，已修的 pg_basebackup CLI 仍需真实部署验证；网桥复制受现有 pg_hba 限制，优先评估 `network_mode: service:postgres` 走 loopback。真实异地 endpoint/凭据与恢复演练须另行补齐。

恢复集合须含 PostgreSQL base/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest/回执。用 `scripts/run_offsite_restore_drill.sh` 在隔离环境校验备份、对象清单/哈希、外键和应用读取；不能拿缓存当权威。

恢复后、恢复流量和重建投影之前，必须先按上文「从备份恢复」的完整顺序做：停流量 → 从线上控制库导出 completed 删除与注销快照（含 `subject_deletions` 与 `account_deletions`） → 恢复数据层 → `--ledger-snapshot` 合并快照并 replay → 确认 `incomplete=0`（且 `account_incomplete=0`） → 删快照 → 重建投影 → 恢复流量。只跑 `python -m scripts.replay_subject_deletions --confirm-replay` 只覆盖恢复后账本里仍然存在的 completed 行；备份之后才完成的主体删除与账号注销，其账本行也随备份回滚了，必须靠恢复前导出的快照补回来。

**镜像里有没有这个脚本**：`infra/Dockerfile.control-api` 与 `scripts/delta_build_images.sh` 从 2026-10-05 的端口参数化改动起会复制 `scripts/replay_subject_deletions.py`；本分支新增的 `--export-ledger` 与 `--ledger-snapshot` 只存在于包含本分支代码的镜像。更早的镜像（含回滚目标 `20261004-first-warm-v1`）没有这个模块，命令报 `No module named scripts.replay_subject_deletions`，可以从发布树把脚本只读挂进一次性容器：在 `docker compose run`（或 `docker run`）上加 `-v <发布树>/scripts/replay_subject_deletions.py:/app/scripts/replay_subject_deletions.py:ro`。2026-10-05 只在新机上用 `docker run` 验证过带挂载的 `--help` 能解析参数；带 `--confirm-replay` 的真实重放（会导入 `services.control_api.app.main.create_app` 并改库）没有跑过，首次使用前先在隔离的恢复库上试。

然后再恢复不可变 evidence/claims 的投影，在 Control API 镜像中重建：

~~~bash
python -m scripts.rebuild_memory_projections --confirm-rebuild
~~~

## 控制库从 SQLite 迁到 PostgreSQL（一次性，需授权）

控制库（账号、登录会话、设备、语音会话、消息、删除台账、数字分身预览）原先是 `/data/memoria.sqlite3`。代码两种后端都支持：`MEMORIA_CONTROL_DATABASE_URL` 为空时用 SQLite 文件，设为 `memoria_control` 角色的 PostgreSQL DSN 后用 PostgreSQL。

前置条件：已上线一个包含 `services/control_api/app/database/postgres_schema.sql` 的整栈版本。该版本的 `env` 步骤生成 `MEMORIA_DB_CONTROL_PASSWORD`，`schema` 步骤建立 `memoria_control` 角色和 28 张空表，`verify_authoritative_postgres.sh` 检查它们都开启了 FORCE RLS。发布本身不会写入 DSN，所以上线后控制库仍在 SQLite 上。

1. 先干跑：把线上文件的快照复制进 PostgreSQL，逐表核对行数和校验和，然后回滚，不停服务。

   ~~~bash
   CONTROL_STORE_MODE=dry-run TAG=<当前线上 tag> COMMIT=<commit> /root/memoria-release/release-ops.sh control-store
   ~~~

2. 确认干跑收据中每张表的行数与预期一致后，再正式执行。这一步会停止 control-api（设备和小程序约有一分钟不可用），备份 SQLite 三件套和 `pg_dump`，迁移并核对，然后写入 DSN 并重建容器。之后任何一步失败，都会恢复 env 并以 SQLite 重新启动。

   ~~~bash
   CONTROL_STORE_MODE=apply TAG=<当前线上 tag> COMMIT=<commit> /root/memoria-release/release-ops.sh control-store
   ~~~

3. 验收：readiness 200；小程序登录和 `runtime-profile` 正常；设备 `display-profile` 轮询 200；新写入一条消息后能读回。收据和备份位于 `$R/.control-store-<时间>/`，只含行数、校验和与哈希，不含行内容。

回滚：从 `/etc/memoria-control-api.env` 删除 `MEMORIA_CONTROL_DATABASE_URL` 行，再重建 control-api，即回到原 SQLite 文件。该文件在切换时保持不动，但切换之后写入 PostgreSQL 的数据不会回到 SQLite，所以回滚只适用于切换后尽快发现问题的情况。PostgreSQL 中已迁移的行保留；如需再次迁移，先清空这 28 张表，脚本会拒绝向非空表写入。

## 控制库 schema 版本

`schema` 步骤先重放幂等基线 `postgres_schema.sql`，再按编号执行 `services/control_api/app/database/migrations/` 中尚未记入 `control_schema_migrations` 的文件，每个文件连同台账行在一个事务里完成，失败则整体回退、发布中止。control-api 启动时若库版本低于代码所需版本会拒绝启动，报错会提示先跑 `schema` 步骤。回滚代码不需要回退 schema：库版本高于代码时照常启动，所以新迁移必须对上一版代码保持兼容（先加列、后删列，分两次发布）。

## LiveKit 媒体链退役（首个不含 LiveKit 的整栈版本）

该版本的 `images.tar` 只含 `memoria-agent`（即 Voice Core 媒体桥镜像）、`memoria-control-api`、`memoria-speaker-model`；compose 不再定义 `agent`、`miniprogram-gateway`、`device-media-gateway`，env 分流只写 Control/Agent/Speaker Model/Media Edge 四份文件，旧 env 文件里的 LiveKit/网关键会被接受但不再分发。`20260929-livekit-retire-v1` 当时的 `release-ops.sh` 让 PREV 保留六个服务：cutover 在 bridge 健康后只停止（不删除）`memoria-agent-1`、`memoria-miniprogram-gateway-1`、`memoria-device-media-gateway-1`，rollback 可按 PREV 整体重建。主机清理完成后，其后的版本 PREV 与本版本同为 speaker-model、control-api、voice-core-media-bridge 三个角色，不再有 retire 步骤。

仓库外的主机清理不随发布自动进行，需单独授权并在回滚窗口关闭后执行（`20260929-livekit-retire-v1` 已于 2026-09-29 按下述步骤完成，收据见 `docs/HANDOFF-archive-0924-1003.md`「2026-09-29 整栈发布 20260929-livekit-retire-v1」；此后整栈 `rollback` 只能回到同样不含旧媒体链的 PREV，否则按组件回滚）：停止并移除 LiveKit server compose 项目与其 sysctl 配置；安装新的 `memoria-stream.conf`、`memoria-https.conf`、IP server 片段后删除 `/etc/nginx/snippets/` 下的 `memoria-livekit.conf`、`memoria-miniprogram-media.conf`、`memoria-device-media.conf`（同时删掉 443 server 里对 `memoria-miniprogram-media.conf` 的 include），`nginx -t` 通过后 reload（若先删片段再回滚，PREV 网关的公网路由会缺失）；移除三个已停止容器、两份网关 env 文件和不再被引用的网关镜像。

## speaker-model 退役（首个不含声纹嵌入服务的整栈版本）

声纹注册与声音克隆已删（#221），说话人权威保持关闭，生产不再调用 CAM++ 嵌入服务。该版本起 `images.tar` 只含 `memoria-agent`、`memoria-control-api`；compose 不再定义 `speaker-model`，control-api 不再 `depends_on` 它，并把 `MEMORIA_SPEAKER_EMBEDDING_URL` 固定为空串：主机 `/etc/memoria-control-api.env` 里可能还留着旧的 `http://speaker-model:8001/…`，compose 的值覆盖它；空串时 control-api 不建 HTTP 嵌入客户端，`validate_production` 接受空串。readiness 去掉 `speaker_model` 核心检查；`speaker_authority` 检查与声纹相关的表保留。

`release_ops.sh` 的 `STACK` 表把 speaker-model 标为 `retired`。`freeze` 记下的 PREV 仍含它（它在上一栈的 compose 链上）；`cutover` 在 ship 角色全部健康后只停止（不删除）`memoria-speaker-model-1`；`rollback` 按 PREV 的 compose 重建 control-api、voice-core-media-bridge、speaker-model 三个服务。再下一次整栈发布时，停着的容器仍挂在旧链上，`freeze` 推出的 PREV 不再含它，`cutover` 没有可停的，不需要改脚本。

主机清理（停着的容器、`/etc/memoria-speaker-model.env`、包括 `rollback-<tag>-pre` 在内的 `memoria-speaker-model:*` 镜像）不随发布进行：须等再下一次整栈发布之后、没有回滚目标还运行 speaker-model 时单独授权。提前清理会让 `rollback` 失败，因为 PREV 的 compose 引用那份 env 文件和镜像；env 分流工具仍会写这份文件，也是为了回滚。`STACK` 里的 `retired` 行同样要等没有回滚目标运行该服务后才能删。

## 设备信任开关（仅绑定链路，无硬件 attestation）

`MEMORIA_BOUND_DEVICE_TRUST_ENABLED`（control-api env，默认 `false`）。生产设备没有 attestation，`action_device_lock_trust` 恒返回 `untrusted`，Policy 因此不给任何设备 `memory_recall_private`、`guardian_summary_view` 与 `memory_capture`。打开后，满足下列全部条件的设备按 `trusted` 对待：onboarding 记录为 `bound` 且绑定到当前 binding 与版本、fleet 未封禁、其激活证书在 fleet 里没有被吊销或过期。`trusted` 只放行这三项记忆类能力，其余敏感能力（声纹、声音克隆、数字自我、原始录音、训练、支付、转让、`memory_promotion`、家庭共享）仍要求硬件 attestation 的 `verified`。

SQL 侧只多返回事实 `device_bound`，与开关无关，所以 `schema` 步骤可以先于 cutover；旧代码忽略该字段、永远看不到 `trusted`，回滚代码不需要回退 schema。

1. 打开：整栈发布后确认设备会话正常，在 `/etc/memoria-control-api.env` 加 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED=true`，重建 control-api 并等 healthy。此后新的设备会话取新 profile；已在进行的会话保留旧 profile（设备会话最长 `DEVICE_RUNTIME_PROFILE_TTL_S`，默认 3600 s；小程序控制会话 5 分钟）。
2. 验收（只读查库）：`session_runtime_profiles` 里最新设备会话的 `payload_json->'capabilities'` 含 `memory_recall_private`；`policy_receipts_v2` 里该能力不再有 `device_untrusted` 拒绝，新 receipt 的 `device_trust` 为 `trusted`（`trusted` 只会来自这个开关，`verified` 才是硬件证明）；说一句带专名的话后 `archive_evidence_events` 出现该使用人的 `speech.utterance_finalized`，`archive_processing_outbox` 中该事件为 completed 而不是 dead。
3. 关闭：删掉该行或改回 `false`，重建 control-api。已签发的 profile 到期前仍带 `memory_recall_private`，要立刻停止归档就让机器人重新唤醒（新会话取新 profile）。设备被解绑或吊销时，无论开关如何都立即变为 `revoked`。
4. 排障：打开后 profile 仍没有 `memory_recall_private` 时，看 `policy_receipts_v2` 里该次决策的 `reason_code`；`device_untrusted` 说明 `device_bound` 为假（onboarding 记录不是 bound、绑定版本不匹配，或激活证书被吊销或过期）。

## 固件 OTA 发布与回滚演练（设备在线升级）

机制（`memoria_firmware_update.*`、`services/control_api/app/device_firmware.py`）：服务器 `current.json` 是唯一的发布指针，设备只装 **build 严格大于自己** 且签名通过的镜像；空闲时检查，下载进另一个 OTA 槽并核长度与 SHA-256，空闲时重启；新镜像以 `PENDING_VERIFY` 启动，Control API 答复激活后才 `mark_app_valid`，在此之前复位会被引导程序回滚到旧槽（`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`）。没有按设备定向：服务器上的 `current.json` 对同一块板的所有设备生效。

发布一个 build（需要用户当场授权，是对线上设备的变更）：改 `memoria_firmware_release.h` 的 `MEMORIA_FIRMWARE_BUILD` → `firmware/esp32/scripts/build.sh --no-idf-install` → `publish_firmware_release.py sign`（写 `outputs/firmware-releases/<board>/<build>/`，用本机私钥签并按设备内置的公钥验）→ `publish_firmware_release.py upload --remote memoria-prod --build N`（暂存在 `ubuntu` 家目录，`sudo` 装入 `/var/lib/memoria/firmware-releases/<board>/`，核哈希后才原子切 `current.json`）。验收看串口：约 30 s 内 `Firmware build N … available`，约 80 s 下载，`staged`，空闲重启，之后 `MEMORIA_FIRMWARE_BUILD=N; slot=ota_x` 与 `Firmware build N confirmed`。`withdraw` 只是不再提供，已装的设备保持。

**回滚演练**（2026-10-04 20:26–20:37 在真机做过一次，通过：N=21、演练号 22，收据见 HANDOFF「2026-10-04 夜 OTA 回滚演练…」一节；只在有人在场、USB 连着、串口记录进程开着时做；做之前先备份，见下一节）：
1. 先让板子跑着真实 build N 并确认（OTA 装上或 USB 刷入都行；2026-10-04 那次是 USB 刷的 21，OTA 正常路径 2026-09-27 已验过）。
2. `firmware/esp32/scripts/build_ota_rollback_drill.sh N+1`：同一份源码、更大的 build 号、**不确认自己**的演练镜像，落在 `artifacts/ota-rollback-drill/app-<N+1>.bin`（缓存里的两处源码构建后自动还原，演练代码不进仓库）。**先构建并签好真实 build，再构建演练**：它会覆盖 `artifacts/memoria-esp-vocat-app.bin`。
3. `publish_firmware_release.py sign --image artifacts/ota-rollback-drill/app-<N+1>.bin --build <N+1>` 与 `upload --build <N+1>`（`sign` 默认只认头文件里的 build，演练镜像必须显式给号，默认路径永远不会误签它）。
4. 设备装上并重启进 N+1：串口出现 `OTA rollback drill: build N+1 stays PENDING_VERIFY`，**没有** `confirmed`。设备只在开机空闲约 30 s 后检查一次，之后每 6 小时一次（传输失败后 15 min），所以 `upload` 之后要**复位板子**（拔插 USB，或停掉常驻串口记录进程后 `esptool … --after hard-reset`、再重开记录进程；记录进程自 2026-10-21 起开口不再复位板子）才会马上发现；下载约 70 s，其间任何唤醒都会暂停它，别碰机器人。
5. 板子仍停在 N+1 的 `PENDING_VERIFY` 时，**先**把 `current.json` 指回真实 build N（再次 `upload --build N`，或 `withdraw`）：否则回滚回来的 N 会发现 N+1 比自己新，再下载、再重启、再回滚，反复循环。N+1 这个号永久作废，下一个真实 build 用 N+2。
6. 复位板子（拔插 USB，或停掉记录进程后 `esptool … --after hard-reset`，之后再开记录进程；只重开记录进程不会复位）：预期引导程序放弃 N+1，回到旧槽，串口 `MEMORIA_FIRMWARE_BUILD=N`，能唤醒、能对话；之后固件检查返回 200 且没有再下载镜像。
7. 通过判据：第 6 步回到 N 且对话正常，之后不再出现下载。失败时按下一节的固件回写恢复（只写唯一紧邻 app 与空 otadata，不动保护区）。取证（可选）：第 5 步之后、第 6 步复位之前，用 `firmware/esp32/scripts/flash_backup.py` 的 `read_region` 只读 `0xd000` 起 8 KiB（芯片停在 ROM 下载模式，不消耗 `PENDING_VERIFY`）：应见槽 0 seq 1 `VALID`（ota_0）、槽 1 seq 2 `PENDING_VERIFY`（ota_1），boot、分区表、phy、identity、ota_0、assets 的 MD5 与演练前相同（nvs 与 otadata 会变）。回滚复位之后同样转储一次（先停串口记录进程；转储后用 `esptool --chip esp32s3 -p <端口> --after hard-reset read-mac` 复位）：应见槽 1 `ABORTED`，其余区 MD5 与演练前相同（nvs 只在 OTA 下载那一步变过）。2026-10-04 那次实测（23:23）：槽 0 seq 1 `ota_0` `VALID`、槽 1 seq 2 `ota_1` `ABORTED`；没有演练「复位时指针仍在 N+1」的循环情形。

## 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。若回滚还需要恢复 `freeze` 写入的 PostgreSQL / 控制库快照，那些文件只保留 3 天（见上文「Freeze 转储保留 3 天」）；超过窗口后只能回滚镜像与配置，不能再从 `.cutover` 转储还原库。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-flash` 或误用旧 run 回滚件。

`build.sh` 不再把整片镜像复制成 `merged.bin`。需要整片包时用 `--danger-full-erase`，产物文件名带 `DANGER_FULL_ERASE`。`flash.sh` 默认拒绝烧这类文件，也拒绝任何盖住 `memoria_identity` 的写入。该分区在 `0x10000`，长 64 KiB。只有 `--allow-danger-full-erase` 才允许这一次写入。烧录前和烧录后各只读一次该分区，只判断 NVS namespace `device` 是否存在，不读取、不打印键值。烧前已有、烧后没有，脚本失败退出。烧前就没有身份时打印警告并退出 0，提示运行 `provision_identity.py`。回滚时恢复 `build.sh`、`flash.sh` 和 `check-overlay.sh` 的上一版，并删除 `identity_guard.py`。

保留身份分区。解绑、恢复出厂，以及将来还没定 GPIO 的恢复出厂按键，都不得擦除它。擦除只能按名字擦默认 `nvs` 分区，或擦具体 namespace。禁止全片擦除，也禁止不带分区名的 `nvs_flash_erase()`。`esp_partition_erase_range` 和 `esp_flash_erase_region` 默认禁止，只有 `identity_guard.py` 里按文件和函数列出的调用可以保留。`esp_ota_*` 不受这条规则限制。仓库里还没有恢复出厂擦除函数。`check-overlay.sh` 已经按这套规则扫描叠加层和脚本，以后的按键代码走同一条检查。句柄跨函数再 `nvs_erase_all` 时，这次扫描看不到，那种写法仍然不能擦身份分区。

这块板的 USB-Serial-JTAG 上 `esptool read-flash` 会在个别 4 KB 块处确定性失败（2026-10-02：`0x106000`，4 MB 槽里另有约五处，报「No more data to read from the serial port」，同样的字节分成 2 KB 读就没事），所以整槽备份和保护区比较用 `firmware/esp32/scripts/flash_backup.py`（用 IDF 的 python；先停串口记录进程）：`backup` 分 64 KB 读，失败的片重连后改读 2 KB 帧，每个备份文件都与芯片自己算的 MD5 核对；`md5` 给出各区的设备端 MD5，写前写后各跑一次，比较引导/分区表、nvs、phy_init、身份区 `0x10000` 与 assets 不变（otadata 在新 app 首次启动后会被引导程序重写成与写前相同的记录，不要求保持空白）。写入只用 `esptool write-flash 0x20000 <app> 0xd000 <8 KiB 的 0xff>`，以其 `Hash of data verified.` 为写入校验；刷完用 `esptool … --after hard-reset read-mac` 或重开串口记录进程让板子回到 app。

**空白 otadata 与任何填充文件必须先在本地验字节再上机（2026-10-06 事故）**：那次用 `head -c 8192 /dev/zero | tr '\0' '\377' > blank.bin` 生成「8 KiB 的 0xff」，那条管道把每个 0xFF 按 Latin‑1 折成 U+00FF 再按 UTF‑8 写出，文件实为 **16384 字节、每两字节 `c3 bf`**；`esptool write-flash … 0xd000 blank.bin` 于是写了 16 KiB，越过 otadata 边界，**擦掉整个 phy_init（`0xf000`–`0xffff`）与 identity 分区的前 4096 字节**（`0x10000`–`0x10fff`）。现场特征：identity 读回前 4096 字节是 `c3bf` 交替、之后与备份逐字节相同——这个精确边界就是越界写入的指纹。所以：填充文件用 Python 之类的二进制方式生成（`python3 -c "import sys; sys.stdout.buffer.write(b'\xff'*8192)"`），**写前用 `xxd` 与 `stat -f%z` 验字节与长度**，并核 `esptool` 报出的 `Wrote N bytes` 是否等于期望值（正常应为 8192；写 16384 就是这类越界）。损坏可逆的前提是先备份：`ota0-before.bin` 还了 app，`nvs-otadata-phy-before.bin` 里的 phy 段与 `identity-before.bin` 的前 4096 字节逐区写回即可复原，写后用 `flash_backup.py md5` 逐项核回刷前值（boot/分区表、phy_init、identity、assets、otadata 都应与刷前相同）。

服务器域名变了要重写身份区（NVS，`0x10000`，64 KiB）里的 `control_api_url`，否则机器人启动后还去旧域名取激活清单（2026-10-05 `aigcnice.com` → `aginice.cn` 做过一次，收据见 `HANDOFF.md`「2026-10-05 域名切换到 aginice.cn」收据 ⑥）。做法：用 `firmware/esp32/scripts/provision_identity.py` **离线**生成身份镜像（不带 `--port`；输入是原来的 device-id / certificate-id / client-id、种子文件和激活公钥，只改 `--control-api-url`），先再用**旧** URL 生成一份，与芯片里读出的身份区逐字节比较，相同才说明新镜像只改了 URL；然后与新 app、空白 otadata 一起一次 `esptool write-flash 0x10000 <身份> 0x20000 <app> 0xd000 <8 KiB 的 0xff>`（写前按上一段备份各区，写后比较 md5，身份区应等于新镜像、其余保护区不变）。身份镜像含种子，权限 0600，放在被 git 忽略的 `firmware/esp32/artifacts/dev-path2/`，不进输出和日志。这只改机器人取清单的入口；已绑定机器人的签名清单里的端点（`control_api`、`device_media`）要靠解绑再重绑才会换成新域名。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。

## 固件 bench 构建（调试镜像，不发布）

`firmware/esp32/scripts/build.sh --bench` 构建调试镜像：`config.bench.json` 是产品 `config.json` 加且仅加 `CONFIG_MEMORIA_BENCH_SERIAL=y`（patch `0035`，默认关）与 `CONFIG_LV_USE_SNAPSHOT=y`，`check-overlay.sh` 逐项核对这一点，产品清单里不得出现 bench 开关。镜像文件名带 `-bench`，内嵌标记 `MEMORIA_BENCH_BUILD=1;`：`publish_firmware_release.py` 见到它拒绝签名与上传；`flash.sh` 要求「所求类型」与树里的构建一致（不带 `--bench` 拒绝刷 bench 构建，带 `--bench` 拒绝刷产品构建）；`build.sh` 在生成的 sdkconfig 与所求类型不符时直接停下。bench 镜像不占 OTA 指针，也不产生新的 `MEMORIA_FIRMWARE_BUILD`。

bench 镜像在 USB 串口多认两条命令；产品镜像不含这两个动词，收到只记 `usb command ignored (not a command)`：

- `snap`：把屏幕当前内容（文字层在内）按 `SNAP <id8hex> <w>x<h> <seq>/<total> <crc8hex> <base64>` 行分块吐出（每块 384 B RGB565 小端），以 `SNAP <id> end …` 或 `SNAP <id> failed reason=…` 行收尾，约 6–8 s；`scripts/snap_to_png.py` 从串口记录还原 PNG，逐块验 base64、长度与 CRC-32，再对 end 行验整幅。
- `status`：吐一行 `MemoriaBench: status up_ms=… phase= mood= frame= screen_off= sleeping= captioned= frames= drawn= render_us= render_max_us= busy_us= px= composed_px= extra_ms= heap_free= psram_free= anim_stack_free=`，给出画了多少帧、每帧渲染耗时、帧调速器拉长了多少毫秒，以及堆与动画任务栈的余量；`scripts/bench_status.py`（只用标准库）解析它，字段表与固件格式串由测试逐项比对。

用法：先起常驻的 `scripts/voice_soak_serial_logger.py` 持有串口，再 `scripts/voice_soak_serial_command.py snap --log <串口记录> [--out shot.png]` 或 `… status --log <串口记录> [--after 60]`（`--after` 隔这么多秒再问一次，并算出这段时间里帧的开销）。刷入 bench 镜像和每次发命令都是对设备的一次操作，须用户当场授权；刷入前按上一节的方法备份保护区，量完**必须刷回产品镜像**。上游 `self.screen.snapshot` MCP 工具虽然链接在产品镜像里（`LV_USE_SNAPSHOT` 上游默认开），但 Memoria 的设备协议不转发 MCP（读源码得出，没在真机上试过），所以截屏只走 bench 构建。

## 固件 keep-awake 构建（临时远程测试，不发布）

产品镜像和 bench 镜像仍会深睡。`CONFIG_MEMORIA_KEEP_AWAKE` 默认关。需要 USB 串口一直在的测试机，在 `firmware/esp32` 下执行：

```bash
./scripts/build.sh --keep-awake
./scripts/flash.sh --keep-awake --port /dev/cu.usbmodem101
```

`config.keep-awake.json` 只比产品清单多 `CONFIG_MEMORIA_KEEP_AWAKE=y`。固件停在 wifi hold，不调用 `esp_deep_sleep_start()`。空闲时麦克风关闭，不上传音频。屏幕轻点或 USB `wake` 才开始会话。文件名带 `-keep-awake`，`publish_firmware_release.py` 拒绝发布。测完刷回产品镜像。`--bench` 与 `--keep-awake` 不能同时使用。

## 电脑语音测试的停止证据（2026-10-10 修正）

每轮真机、串口命令、刷机与调整音量仍须当场授权；本节只规定判据，不授权执行。运行脚本时显式给用户确认的 `--volume`，不靠脚本默认值；缺声学测量不报端到端声学停止延迟。

`voice_soak.py` 记录 `reply_started_ts`、`interrupt_start_ts` / `interrupt_end_ts` / `interrupt_deadline_ts`、`device_returned_ts` 与串口时区偏移。共用 `voice_soak_evidence.py` 按设备开口 generation 找唯一完整 delivery fence，要求同回复 `preempted` + 实际存在的 `media spoken stop interrupted reply`（flush=True）+ 同代播放收据和窗口内退出说话。自然播完、任务异常、断线、泛化superseded、错代/跨会话、缺日志都不算成功；证据不足为 `stopped=null`、`stop_verdict=unverified`。`voice_soak_analyze.py` 重新核对而不是信任历史布尔值；日志稍后补到可离线再算，原时间线不改。

```bash
# 只读本机既有文件：不接串口、不播放声音、不访问生产
uv run python scripts/voice_soak_evidence.py outputs/acceptance/run-20261010-verify-standard
```

停止延迟从打断素材起点到设备退出说话，`stop_delay_origin=interrupt_start`；不是DAC/麦克风测得的停止时刻。旧记录未记刺激绝对时间，只能用完整回复终态排除自然完成/异常，不可补成成功；10-09/10-10的纠正与目标路径边界见`docs/acceptance/run-20261010-verify/findings.md`。`--duration-min`只是上限，N-5长稳按时间线实际跨度计。

10-11 09:16设备窗口取得一条正向停止样本和一条自然完成反例（[专项收据](../acceptance/run-20261011-stop-target/findings.md)）；两个停止素材音色不同，尚不构成稳定性或音量对照。开测前先保存完整音量/静音设置，退出后核对恢复；现有驱动只保存/恢复音量数值，静音需在外层恢复。选择语音库时逐一核对提问及停止素材，避免缺文件时的Tingting回退改变实验条件。

## ASR / endpoint / 关闭诊断（2026-10-10 已发布）

诊断随 PR #199 / `20261010-n8-endpoint-diag-v1` 于 2026-10-10 23:12 CST 部署。每次读新收据仍先核对发布身份；旧版日志缺字段不代表没有阻挡区间或只走词法规则。只有拿到已授权设备窗口的新收据后才能用下面的字段作设备结论，本节不授权发布或测试。23:17 中性问答轮已捕获 4 条 overlap 快照与 1 条 reopen-window `no_text` 原因；其余出口未触发，#197/#198 目标路径仍未复现，见 `docs/acceptance/run-20261010-verify/findings.md`。

- `media ASR result rejected`：`rescue_synthesized` 标记被拒稿来源；`overlap` 为 supervisor 决策当时的规范化区间和阻挡快照（最多4条，另有 `conflict_count`），含 task epoch、sample range、文字长度、rescue 标志，不含文字和 sentence id。按同 session / stream epoch 解释，不能用未知区间猜着放宽保护。
- `media turn has no text`：`text_after_endpoint` 是文字起点在提交区间之后的数量，结合 `pending_end`、`active_vad_start`、`last_playback_end`、`committed_sample` 区分“没有文字”与“文字在旧 endpoint 外”。它不证明麦克风输入质量，也不授权强制把晚到文字拼进旧回合。
- `media early conversation-close endpoint`：`rule_match` 只表示当前文本是否符合词法关闭规则；`source=final` 且规则不命中时同步判断来自已有缓存，规则命中时仍可能同时有缓存。ASR 范围 / task epoch / rescue 标志用来关联输入，原文仍不进日志；没有原文和声学采集不能断言误听了哪句话。
- `media reopen evidence window`：`armed` 记录覆盖旧 endpoint 的已知文字和时间窗；`skipped` 的 `reason` 区分 `no_text`、`endpoint_uncovered` 与 `reply_in_flight`；`held` 区分 `turn_end_advanced`、`partial_after_endpoint`、`turn_changed`、session/stream 变化与回复仍在途。`media reopened turn committing without new text` 表示到期后提交；这些日志只带 ASR 样本区间、文字长度和已提交水位，不带终稿或 token 文本。
- `services/agent/tests/unit/test_media_reopen_replay.py` 用于离线差分 t038 已记录事件与一个明确标注的合成 interim 对照。两条分支不复原历史识别内容；只有新的已授权设备收据才能判定当时实际走了哪条路径。

本地素材的 WAV RMS / peak 是数字电平；噪声文件名不等于实测电平。`afplay` 起止是进程时刻，串口和桥时间不是校准后的 ADC/DAC 时钟；要判断输入与噪声因果须取得同一窗口的实际输入证据。10-10 的留存文件读数和同期 VAD 观察见 `docs/acceptance/run-20261010-verify/input-audit.json`，不是新声学验收。
