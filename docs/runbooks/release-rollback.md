# 发布、恢复与回滚运维手册

本文是从 `HANDOFF.md` 迁出的永久运维参考，保留原有发布、数据恢复和回滚底线。命令只表示操作基线，不表示本轮已经执行；生产执行、切流、恢复和固件操作仍需单独授权。

## 生产拓扑与安全边界

- `/opt/memoria/current` 指向当前整栈发布树（2026-10-05 16:31 起：`releases/20261005-subject-candidates-v1`，有效栈 `MEMORIA_RELEASE_TAG=20261005-subject-candidates-v1`；media-edge 是组件发布 `20261002-late-progress-v1`，它的编排还是上一个发布树 `releases/20261004-first-warm-v1` 的 compose 加 `component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml`，所以那个发布树不能删）。目录名、栈 tag、组件 tag 是三个概念；readiness 刷新必须取有效栈配置。
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
- **Control API 端口是 `127.0.0.1:18791`，由 `MEMORIA_CONTROL_API_PORT` 带着走**（2026-10-05 的端口参数化改动）：compose 写 `127.0.0.1:${MEMORIA_CONTROL_API_PORT:-8791}:8000`；`scripts/release_ops.sh` 的 `control_api_port()` 依次取该环境变量、在跑的 `memoria-control-api-1` 的 `docker port … 8000/tcp`（只认 `127.0.0.1` 的绑定）、默认 8791，`verify-load` 把它写进新发布树的 `.env`（Compose 从项目目录读 `.env`），`cutover` 先断言候选 compose 发布的正是线上这个端口，不一致就拒绝、线上不动（新发布树若落回 8791 会与 hr-tracker 冲突，control-api 起不来、切流中途中断），`finish` 与 `scripts/refresh_readiness.sh` 也探这个端口。`verify-load` 的空闲端口预检默认 `28791` / `28891`（`MEMORIA_PREFLIGHT_API_PORT` / `MEMORIA_PREFLIGHT_NGINX_PORT` 可改），避开线上的 18791（旧的固定值 18791 / 18891 会让预检在新机上直接拒绝）。新机上 2026-10-05 16:31 起的当前发布树 `20261005-subject-candidates-v1` 就是仓库原样加一个 `.env`（`MEMORIA_RELEASE_COMMIT`、`MEMORIA_RELEASE_TAG`、`MEMORIA_CONTROL_API_PORT=18791`）；上一个发布树 `20261004-first-warm-v1`（现在是回滚目标）里 compose、`refresh_readiness.sh`、`release_ops.sh` 三处仍是手工改成 18791 的（原件留作 `.orig`），是端口参数化之前的做法。
- **回滚深度是 1（2026-10-05 16:31 起；此前是 0）**：新机上 `/root/memoria-release/release-ops.sh`（仓库版，root 0700）随 `20261005-subject-candidates-v1` 装上，回滚目标是 `20261004-first-warm-v1`（发布树 `releases/20261004-first-warm-v1` 与镜像 `*:rollback-20261005-subject-candidates-v1-pre` 都在新机上）；再往前没有。`rollback` 这一步在新机上没演练过（`verify-load`、`freeze`、`cutover`、`finish` 实跑过）。每次整栈发布要把仓库里 PREV 已前移的新版 `release_ops.sh` 装上去（装前备份为 `.pre-<tag>`、比 sha256、权限 0700），`freeze` 才认当前这一栈。迁移用的 `bringup.sh`（按服务名逐个起；入库为 `scripts/bringup_shared_host.sh`，与新机上那份逐字相同，sha256 `398bd005…`）的 `TAG` / `COMMIT` 默认值仍是迁移时那版栈 `20261004-first-warm-v1`，冷启必须显式传当前的 `TAG=20261005-subject-candidates-v1 COMMIT=faf3efe3d02c23f519ad139b5860200a0609b800`，否则指向已被换掉的发布树。
- **在新机上发整栈（2026-10-05 `20261005-subject-candidates-v1` 实跑的做法）**：登录用户是 `ubuntu`（免密 sudo），`/root/memoria-release` 在 root 下，所以每一步都包一层 `ssh memoria-prod "sudo sh -c 'env TAG=… COMMIT=… [VERIFIER_SHA=… MANIFEST_SHA=…] /root/memoria-release/release-ops.sh <step> > /root/memoria-release/<step>-<tag>.log 2>&1; echo exit=$? >> /root/memoria-release/<step>-<tag>.log'"`，再读日志里的 PASS；上传用 `scripts/upload_release_artifacts.sh`（它用 `sudo -n` 与 `rsync --rsync-path='sudo -n rsync'`，所以 `ubuntu` 能用；本机 `PATH=/opt/homebrew/bin:$PATH` 以用到新版 rsync），先 dry-run 再真传；增量（seeded）模式要求新机上有上一个 tag 的 `incoming/<tag>/images.tar`，没有就是整包模式（第一次是整包，线上发送 502 MB / 总 1.53 GB）；本机构建用 `DOCKER_DEFAULT_PLATFORM=linux/amd64 COMPOSE_PARALLEL_LIMIT=1`，这次还设了 `UV_DEFAULT_INDEX=https://mirrors.cloud.tencent.com/pypi/simple`；`verify-load`、`freeze` 不动线上，可以在合并 PR 之前跑（它们会把 `MEMORIA_CONTROL_API_PORT` 写进新发布树的 `.env`、在 28791 / 28891 起预检栈、做发布前 DB 备份），任何需要的修正还能进同一个 PR；`cutover` 前先确认没有设备会话。
- **数据层**：两台机上 `memoria-data` 项目都是从 `releases/20260827-architecture-split-v1/infra` 起的。`current/infra` 里的同名 compose 多一个 `011-control-schema.sql` 的 initdb 挂载（只在空库首次初始化时用）；在已有数据的库上用 `current/infra` 去 `up -d` 会让 compose 判定配置变了而重建 postgres 容器（推断，没试），不要顺手做。
- **WAL 归档已关（P1-08，2026-10-06）**：`infra/memoria-data.production.yml` 的 postgres 参数是 `archive_mode=off`，不再有 `archive_timeout` / `archive_command`。原先每 5 分钟强制切一个 16 MB 段并拷进同盘的 `memoria-data_postgres_wal_archive` 卷：没有 base backup 可接，没有恢复价值，旧机实测每天涨约 0.6–1.1 GB。恢复靠每次整栈发布前 `freeze` 的 `memoria-pre-<tag>.dump`（见「数据层、备份与恢复」）。改这个参数必须重建 postgres 容器（`archive_mode` 要重启，`ALTER SYSTEM` 改不了：compose 的 `-c` 参数优先）；线上 compose 与仓库的同步和卷的清理是维护窗口里的一步，结果记在 HANDOFF。要重新启用（例如接异地备份 profile）：在 compose 里把 `archive_mode=on`、`archive_timeout=300s`、`archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f` 一并恢复，并保持该卷 `999:999 0700`，否则 `archive_command` 一直失败；同时要有 base backup 和按它裁剪的策略。
- **磁盘**：40 GB 盘，迁移后已用约 17 GB，2026-10-05 16:37 第一次整栈发布后已用 21 GB（剩 17 GB；`incoming/20261005-subject-candidates-v1` 1.5 GB 留着作下一次增量上传的种子，要腾盘就删它，代价是下一次改回整包上传）。WAL 归档关掉之后，增长主要是每次整栈发布的约 4–5 GB（镜像 + `incoming` 包）和库本身；`pg_wal` 里只剩 PostgreSQL 自己循环使用的几段，不要手动删。`memoria-disk-patrol.timer` 的告警线是 75% / 85%（百分比，对 40 GB 盘同样适用，只告警不清理）。
- **systemd**：`memoria-readiness-refresh.timer`（12 小时一次提供方冒烟 + readiness 标记；迁移后 01:36:57 跑过一次，从新机出口 PASS）与 `memoria-disk-patrol.timer` 已在新机启用。

## 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. 在候选镜像跑 Agent 发布门（`scripts.verify_agent_release_artifact`：bridge 与提供方适配器不导入 livekit、自有媒体遥测隐私 canary、DTLN 去噪器可加载）；镜像解析显式给 expected candidate 并对齐有效 overrides。已有自动门禁接线，当前候选/生产复验要求见 P1-01。
3. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
4. dry-run→上传校验→授权切流→候选 provider smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片，只切 `voice-core-media-bridge`；线上 bridge 可以是整栈镜像（不带 `com.memoria.release.kind`，且 version 必须等于该容器的 `MEMORIA_RELEASE_TAG`），也可以是组件或回滚恢复镜像；已存在的同名回滚 tag（`release_ops.sh` 也用 `rollback-<tag>-pre` 命名上一栈）只有与当前 bridge 镜像 image id 相同时才复用。`--dry-run` 不检查线上 bridge，这一步的拒绝只在 `--cutover` 出现，且发生在任何切换之前。`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

`scripts/deploy_control_component.sh`（control-api 单组件发布）会让 control-api 落在组件链上，而 `release_ops.sh` 的 `freeze` 只认三个角色同在 PREV 整栈 compose 上；认组件链的支持（`LIVE_CONTROL_RELEASE`）自 2026-10-01 的整栈发布（`20261001-audience-recap-v1`）起已不在脚本里。此后若再做 control-api 单组件发布，须先把链支持补回、并核对 `freeze` 认得它，否则下一次整栈发布会在 `freeze` 被拒；整栈发布之后 control-api 回到纯整栈链。

运行门禁需剥离本地 `OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

## 完整制品上传与校验

构建机先从干净 tag 生成 source/images，再生成 portable verifier 和绑定 source/images 的 manifest；上传脚本要求 `source.tar.sha256` 与 `images.tar.sha256` 成对存在：

~~~bash
docker save -o "$ARTIFACT_DIR/images.tar" \
  memoria-agent:$RELEASE_TAG memoria-control-api:$RELEASE_TAG \
  memoria-speaker-model:$RELEASE_TAG
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

## media-edge 组件发布（手工，只换 media-edge 一个容器）

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
- 当前保留的本地还原点只在新机的旧机归档包 `/root/old-host-20261005/var-backups-memoria.tar.gz`（旧机 `/var/backups/memoria` 整体打包，root 0700，2026-10-05 迁移时取）里：`drill-20260914-p0-02/base`（2026-09-14 通过 `pg_verifybackup`、隔离启动、应用表/对象核对，报告 `outputs/acceptance/run-20260914-p0-02-restore-drill/report.json`）和 `20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump`；它们不会自动更新。09-14 base 的 WAL 链只在旧机上，旧机到期后消失，所以这个 base 只能还原到演练时刻，没有时间点恢复能力。最近一份逻辑转储是每次整栈发布前 freeze 的 `memoria-pre-<tag>.dump`，最新一份 2026-10-05 16:29（`20261005-subject-candidates-v1`，4,402,127 B），在新机 `/opt/memoria/releases/20261005-subject-candidates-v1/.cutover/`；再早一份 2026-10-04 18:15（`20261004-first-warm-v1`）在上述归档目录的 `release-receipts.tar.gz` 里。新机上没有持续的数据库备份。
- WAL 归档已关（P1-08，2026-10-06，见上文「生产主机」）：没有 base backup 的归档接不上任何还原点，所以不再产生。没有时间点恢复能力是已知现状：恢复点只有上述每次发布前的 `freeze` 转储和归档包里的 09-14 base。重新启用必须同时有 base backup 与按它裁剪的策略；禁止只按文件年龄删 WAL，`pg_wal` 里的段由 PostgreSQL 自己回收。
- 重新启用备份时，已修的 pg_basebackup CLI 仍需真实部署验证；网桥复制受现有 pg_hba 限制，优先评估 `network_mode: service:postgres` 走 loopback。真实异地 endpoint/凭据与恢复演练须另行补齐。

恢复集合须含 PostgreSQL base/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest/回执。用 `scripts/run_offsite_restore_drill.sh` 在隔离环境校验备份、对象清单/哈希、外键和应用读取；不能拿缓存当权威。

恢复后、恢复流量和重建投影之前，必须先重放已完成的按使用人删除：删除台账在控制库（切换前是 SQLite 文件，切换后在 PostgreSQL），使用人数据在 PostgreSQL/MinIO，恢复数据层会让已删除的孩子/老人数据复活而台账仍显示 completed。用一次性 Control API 容器执行（只输出计数，`incomplete` 非零则退出码 1，未完成项由删除 worker 续跑）：

~~~bash
python -m scripts.replay_subject_deletions --confirm-replay
~~~

**镜像里有没有这个脚本**：`infra/Dockerfile.control-api` 与 `scripts/delta_build_images.sh` 从 2026-10-05 的端口参数化改动起会复制 `scripts/replay_subject_deletions.py`，之后构建的 Control API 镜像可直接运行上面的命令（`20261005-subject-candidates-v1` 的线上容器里已确认有 `/app/scripts/replay_subject_deletions.py`，`python -m scripts.replay_subject_deletions --help` 能解析参数）；更早的镜像（含回滚目标 `20261004-first-warm-v1`）没有它，命令报 `No module named scripts.replay_subject_deletions`，可以从发布树把脚本只读挂进一次性容器：在 `docker compose run`（或 `docker run`）上加 `-v <发布树>/scripts/replay_subject_deletions.py:/app/scripts/replay_subject_deletions.py:ro`。2026-10-05 只在新机上用 `docker run` 验证过带挂载的 `--help` 能解析参数；带 `--confirm-replay` 的真实重放（会导入 `services.control_api.app.main.create_app` 并改库）没有跑过，首次使用前先在隔离的恢复库上试。

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
4. 设备装上并重启进 N+1：串口出现 `OTA rollback drill: build N+1 stays PENDING_VERIFY`，**没有** `confirmed`。设备只在开机空闲约 30 s 后检查一次，之后每 6 小时一次（传输失败后 15 min），所以 `upload` 之后要**复位板子**（打开常驻串口记录进程就会复位）才会马上发现；下载约 70 s，其间任何唤醒都会暂停它，别碰机器人。
5. 板子仍停在 N+1 的 `PENDING_VERIFY` 时，**先**把 `current.json` 指回真实 build N（再次 `upload --build N`，或 `withdraw`）：否则回滚回来的 N 会发现 N+1 比自己新，再下载、再重启、再回滚，反复循环。N+1 这个号永久作废，下一个真实 build 用 N+2。
6. 复位板子（拔插 USB、再打开串口记录进程，或 `esptool … --after hard-reset`）：预期引导程序放弃 N+1，回到旧槽，串口 `MEMORIA_FIRMWARE_BUILD=N`，能唤醒、能对话；之后固件检查返回 200 且没有再下载镜像。
7. 通过判据：第 6 步回到 N 且对话正常，之后不再出现下载。失败时按下一节的固件回写恢复（只写唯一紧邻 app 与空 otadata，不动保护区）。取证（可选）：第 5 步之后、第 6 步复位之前，用 `firmware/esp32/scripts/flash_backup.py` 的 `read_region` 只读 `0xd000` 起 8 KiB（芯片停在 ROM 下载模式，不消耗 `PENDING_VERIFY`）：应见槽 0 seq 1 `VALID`（ota_0）、槽 1 seq 2 `PENDING_VERIFY`（ota_1），boot、分区表、phy、identity、ota_0、assets 的 MD5 与演练前相同（nvs 与 otadata 会变）。回滚复位之后同样转储一次（先停串口记录进程；转储后用 `esptool --chip esp32s3 -p <端口> --after hard-reset read-mac` 复位）：应见槽 1 `ABORTED`，其余区 MD5 与演练前相同（nvs 只在 OTA 下载那一步变过）。2026-10-04 那次实测（23:23）：槽 0 seq 1 `ota_0` `VALID`、槽 1 seq 2 `ota_1` `ABORTED`；没有演练「复位时指针仍在 N+1」的循环情形。

## 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-all` 或误用旧 run 回滚件。

这块板的 USB-Serial-JTAG 上 `esptool read-flash` 会在个别 4 KB 块处确定性失败（2026-10-02：`0x106000`，4 MB 槽里另有约五处，报「No more data to read from the serial port」，同样的字节分成 2 KB 读就没事），所以整槽备份和保护区比较用 `firmware/esp32/scripts/flash_backup.py`（用 IDF 的 python；先停串口记录进程）：`backup` 分 64 KB 读，失败的片重连后改读 2 KB 帧，每个备份文件都与芯片自己算的 MD5 核对；`md5` 给出各区的设备端 MD5，写前写后各跑一次，比较引导/分区表、nvs、phy_init、身份区 `0x10000` 与 assets 不变（otadata 在新 app 首次启动后会被引导程序重写成与写前相同的记录，不要求保持空白）。写入只用 `esptool write-flash 0x20000 <app> 0xd000 <8 KiB 的 0xff>`，以其 `Hash of data verified.` 为写入校验；刷完用 `esptool … --after hard-reset read-mac` 或重开串口记录进程让板子回到 app。

服务器域名变了要重写身份区（NVS，`0x10000`，64 KiB）里的 `control_api_url`，否则机器人启动后还去旧域名取激活清单（2026-10-05 `aigcnice.com` → `aginice.cn` 做过一次，收据见 `HANDOFF.md`「2026-10-05 域名切换到 aginice.cn」收据 ⑥）。做法：用 `firmware/esp32/scripts/provision_identity.py` **离线**生成身份镜像（不带 `--port`；输入是原来的 device-id / certificate-id / client-id、种子文件和激活公钥，只改 `--control-api-url`），先再用**旧** URL 生成一份，与芯片里读出的身份区逐字节比较，相同才说明新镜像只改了 URL；然后与新 app、空白 otadata 一起一次 `esptool write-flash 0x10000 <身份> 0x20000 <app> 0xd000 <8 KiB 的 0xff>`（写前按上一段备份各区，写后比较 md5，身份区应等于新镜像、其余保护区不变）。身份镜像含种子，权限 0600，放在被 git 忽略的 `firmware/esp32/artifacts/dev-path2/`，不进输出和日志。这只改机器人取清单的入口；已绑定机器人的签名清单里的端点（`control_api`、`device_media`）要靠解绑再重绑才会换成新域名。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。

## 固件 bench 构建（调试镜像，不发布）

`firmware/esp32/scripts/build.sh --bench` 构建调试镜像：`config.bench.json` 是产品 `config.json` 加且仅加 `CONFIG_MEMORIA_BENCH_SERIAL=y`（patch `0035`，默认关）与 `CONFIG_LV_USE_SNAPSHOT=y`，`check-overlay.sh` 逐项核对这一点，产品清单里不得出现 bench 开关。镜像文件名带 `-bench`，内嵌标记 `MEMORIA_BENCH_BUILD=1;`：`publish_firmware_release.py` 见到它拒绝签名与上传；`flash.sh` 要求「所求类型」与树里的构建一致（不带 `--bench` 拒绝刷 bench 构建，带 `--bench` 拒绝刷产品构建）；`build.sh` 在生成的 sdkconfig 与所求类型不符时直接停下。bench 镜像不占 OTA 指针，也不产生新的 `MEMORIA_FIRMWARE_BUILD`。

bench 镜像在 USB 串口多认两条命令；产品镜像不含这两个动词，收到只记 `usb command ignored (not a command)`：

- `snap`：把屏幕当前内容（文字层在内）按 `SNAP <id8hex> <w>x<h> <seq>/<total> <crc8hex> <base64>` 行分块吐出（每块 384 B RGB565 小端），以 `SNAP <id> end …` 或 `SNAP <id> failed reason=…` 行收尾，约 6–8 s；`scripts/snap_to_png.py` 从串口记录还原 PNG，逐块验 base64、长度与 CRC-32，再对 end 行验整幅。
- `status`：吐一行 `MemoriaBench: status up_ms=… phase= mood= frame= screen_off= sleeping= captioned= frames= drawn= render_us= render_max_us= busy_us= px= composed_px= extra_ms= heap_free= psram_free= anim_stack_free=`，给出画了多少帧、每帧渲染耗时、帧调速器拉长了多少毫秒，以及堆与动画任务栈的余量；`scripts/bench_status.py`（只用标准库）解析它，字段表与固件格式串由测试逐项比对。

用法：先起常驻的 `scripts/voice_soak_serial_logger.py` 持有串口，再 `scripts/voice_soak_serial_command.py snap --log <串口记录> [--out shot.png]` 或 `… status --log <串口记录> [--after 60]`（`--after` 隔这么多秒再问一次，并算出这段时间里帧的开销）。刷入 bench 镜像和每次发命令都是对设备的一次操作，须用户当场授权；刷入前按上一节的方法备份保护区，量完**必须刷回产品镜像**。上游 `self.screen.snapshot` MCP 工具虽然链接在产品镜像里（`LV_USE_SNAPSHOT` 上游默认开），但 Memoria 的设备协议不转发 MCP（读源码得出，没在真机上试过），所以截屏只走 bench 构建。
