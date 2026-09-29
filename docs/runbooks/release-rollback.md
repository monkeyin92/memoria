# 发布、恢复与回滚运维手册

本文是从 `HANDOFF.md` 迁出的永久运维参考，保留原有发布、数据恢复和回滚底线。命令只表示操作基线，不表示本轮已经执行；生产执行、切流、恢复和固件操作仍需单独授权。

## 生产拓扑与安全边界

- `/opt/memoria/current` 最后指向 `/opt/memoria/releases/20260827-architecture-split-v1`；有效栈 `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。目录名、栈 tag、组件 tag 是三个概念；readiness 刷新必须取有效栈配置。
- Control/Direct Edge 仅回环端口 `8791/8794`；Bridge 容器 `memoria-voice-core-media-bridge-1` 是 `memoria-agent` 镜像唯一的运行者，并发送 Agent heartbeat。PostgreSQL 17 + pgvector、MinIO、独立 mTLS Redis。LiveKit server、LiveKit Agent worker（`memoria-agent-1`）与 Python 小程序/设备媒体网关（`8792/8793`）自 LiveKit 退役版本起不在仓库栈内，见下文「LiveKit 媒体链退役」。SQLite 兼容库 `/data/memoria.sqlite3` 挂载自 `/var/lib/memoria`。
- readiness 入口 `https://aigcnice.com:8443/memoria-api/health/ready`；443 根站是 WMS，不用该端口的 404 判断 Memoria 健康。
- ESP32 Direct：`wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`。公共 8080 不承载设备 WSS。H5 `/memoria-h5` 固定返回 `410 Gone`，不再发布静态前端。
- 443 Nginx 只保留以下 Memoria include（原 `memoria-miniprogram-media.conf` include 随小程序网关退役删除），不改同机 WMS 路由/数据；8443 经 `memoria-stream.conf` 纯 TCP 透传到 `127.0.0.1:9443` 的 Memoria TLS server（不再做 LiveKit 的 `ssl_preread` 分流）。

~~~nginx
include /etc/nginx/snippets/memoria-bind.conf;
~~~

`memoria-bind.conf`（仓库 `infra/nginx-memoria-bind.conf`，2026-09-27 起）只服务 `/memoria-bind/`：`/var/www/memoria-bind/` 下的说明页 `index.html`（仓库 `infra/memoria-bind/index.html`）与微信「扫普通链接二维码打开小程序」的校验文件，关闭访问日志。该前缀必须在 443（微信规则不支持非标准端口）。

Secret 仅在 root-only `/etc/memoria-*.env`（root:root 0600）；候选从真实源复制并按 `scripts/split_production_env.py` 分流，禁止在输出/日志/manifest 留值。内部 token 不等于账号身份；设备/媒体 token 必须短期且绑定 audience/subject/fence。Direct 缺少 mTLS Device State Redis 时 fail closed，不回退本地权威。

## 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. 在候选镜像核依赖版本、双进程隐私默认值与真实 exporter；镜像解析显式给 expected candidate 并对齐有效 overrides。已有自动门禁接线，当前候选/生产复验要求见 P1-01。
3. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
4. dry-run→上传校验→授权切流→候选 provider smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片，只切 `voice-core-media-bridge`；`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

运行门禁需剥离本地 `LISTENER_CUES_ENABLED/OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

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
- 当前保留的本地还原点：`/var/backups/memoria/drill-20260914-p0-02/base`；2026-09-14 通过 `pg_verifybackup`、隔离启动、应用表/对象核对。报告 `outputs/acceptance/run-20260914-p0-02-restore-drill/report.json`。另保留 `/var/backups/memoria/20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump`。它们不会自动更新。
- WAL 最后观察仍归档且无自动裁剪；旧日增长估计/磁盘余量不是当前值。P1-08 单独处理保留策略，不因备份暂缓而遗漏。禁止只按文件年龄删 WAL，必须保护仍保留 base backup 所需连续链；本轮未删任何数据。
- 重新启用备份时，已修的 pg_basebackup CLI 仍需真实部署验证；网桥复制受现有 pg_hba 限制，优先评估 `network_mode: service:postgres` 走 loopback。真实异地 endpoint/凭据与恢复演练须另行补齐。

恢复集合须含 PostgreSQL base/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest/回执。用 `scripts/run_offsite_restore_drill.sh` 在隔离环境校验备份、对象清单/哈希、外键和应用读取；不能拿缓存当权威。

恢复后、恢复流量和重建投影之前，必须先重放已完成的按使用人删除：删除台账在控制库（切换前是 SQLite 文件，切换后在 PostgreSQL），使用人数据在 PostgreSQL/MinIO，恢复数据层会让已删除的孩子/老人数据复活而台账仍显示 completed。用一次性 Control API 容器执行（只输出计数，`incomplete` 非零则退出码 1，未完成项由删除 worker 续跑）：

~~~bash
python -m scripts.replay_subject_deletions --confirm-replay
~~~

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

该版本的 `images.tar` 只含 `memoria-agent`（即 Voice Core 媒体桥镜像）、`memoria-control-api`、`memoria-speaker-model`；compose 不再定义 `agent`、`miniprogram-gateway`、`device-media-gateway`，env 分流只写 Control/Agent/Speaker Model/Media Edge 四份文件，旧 env 文件里的 LiveKit/网关键会被接受但不再分发。`release-ops.sh` 的 `PREV_STACK_SERVICES`/`TARGETS` 仍描述含这三项的 PREV：`freeze` 照旧核验并打 `rollback-$TAG-pre` 标签；`cutover` 在 bridge 健康后只 `docker stop`（不删除）`memoria-agent-1`、`memoria-miniprogram-gateway-1`、`memoria-device-media-gateway-1`；`rollback` 用 PREV 的 compose/镜像/env 整体 force-recreate 六个服务，所以停掉的容器、`/etc/memoria-miniprogram-gateway.env`、`/etc/memoria-device-media-gateway.env` 和 PREV 的 Nginx 片段必须保留到 PREV 不再是回滚目标为止。

仓库外的主机清理不随发布自动进行，需单独授权并在回滚窗口关闭后执行（`20260929-livekit-retire-v1` 已于 2026-09-29 按下述步骤完成，收据见 HANDOFF；此后整栈 `rollback` 只能回到同样不含旧媒体链的 PREV，否则按组件回滚）：停止并移除 LiveKit server compose 项目与其 sysctl 配置；安装新的 `memoria-stream.conf`、`memoria-https.conf`、IP server 片段后删除 `/etc/nginx/snippets/` 下的 `memoria-livekit.conf`、`memoria-miniprogram-media.conf`、`memoria-device-media.conf`（同时删掉 443 server 里对 `memoria-miniprogram-media.conf` 的 include），`nginx -t` 通过后 reload（若先删片段再回滚，PREV 网关的公网路由会缺失）；移除三个已停止容器、两份网关 env 文件和不再被引用的网关镜像。

## 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-all` 或误用旧 run 回滚件。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。
