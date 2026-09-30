# 空间治理运维基线

本文是从 `HANDOFF.md` 迁出的永久运维参考，保留空间巡检、镜像保留和 systemd 安装基线。以下内容不表示本轮已在生产执行、安装或部署；生产 `--apply`、systemd 安装/启用和紧急删除均需另行授权。

以下是当前操作基线与命令模板。清理一律先 dry-run；数据卷、非 `memoria-*` 镜像、运行容器引用的镜像和 `memoria-agent-runtime-base` 的所有 tag 不得清理。镜像 apply 前先保存 `docker ps` 与 `docker volume ls` 快照：

~~~bash
sudo /opt/memoria/current/scripts/docker_image_retention.sh --keep 2 --min-age-days 14
sudo /opt/memoria/current/scripts/docker_image_retention.sh --keep 2 --min-age-days 14 --apply
~~~

该工具只逐个处理符合保留期的 `memoria-*` tagged images。即使磁盘告急，也须获授权后逐项 `docker inspect` 候选、逐项删除明确镜像；禁止 `docker system prune`，禁止删除数据卷或绕过上述保护项。

生产目录只做只读审计，不自动搬迁 `releases/`、`component-releases/` 或 `incoming/`，以免破坏 Compose 引用链。验收归档只覆盖 `outputs/acceptance/run-*`；apply 会生成归档及 SHA-256、解包比对原目录文件哈希，验证相同后才删除原目录：

~~~bash
sudo /opt/memoria/current/scripts/production_layout_audit.sh /opt/memoria
python scripts/archive_acceptance_outputs.py --older-than-days 30 --keep 5
python scripts/archive_acceptance_outputs.py --older-than-days 30 --keep 5 --apply
~~~

Docker 构建统一走 wrapper：常规本地构建使用 BuildKit；legacy 通过 `MEMORIA_DOCKER_BUILDKIT=0` 显式选择。生产机按尚无 buildx 插件处理，远端 Agent 快速发布继续保持 `DOCKER_BUILDKIT=0`；安装并灰度验证前不得切换生产构建器。需要且已验证 buildx 时才设置 `MEMORIA_DOCKER_BUILDER=buildx`（wrapper 自动 `--load`）：

~~~bash
scripts/docker_build.sh -f infra/Dockerfile.media-edge -t memoria-media-edge:local .
MEMORIA_DOCKER_BUILDKIT=0 scripts/docker_build.sh -f infra/Dockerfile.media-edge -t memoria-media-edge:legacy .
MEMORIA_DOCKER_BUILDER=buildx scripts/docker_build.sh -f infra/Dockerfile.media-edge -t memoria-media-edge:buildx .
~~~

磁盘巡检只读取 `df`/Docker 空间，`--apply` 也只写状态快照而不清理；退出码为 0 正常、1 warning、2 critical、3 自身错误。以下 systemd 安装/启用命令保留为基线，**执行仍需另行授权**：

~~~bash
scripts/disk_patrol.sh --warn-pct 75 --crit-pct 85 --json
sudo install -d -m 0755 /opt/memoria/ops-tools
sudo install -m 0755 scripts/disk_patrol.sh /opt/memoria/ops-tools/disk_patrol.sh
sudo install -m 0644 infra/memoria-disk-patrol.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-disk-patrol.timer
~~~

## 发布制品的手工清理（2026-10-01 首次执行，经用户授权）

规则（与 `release-rollback.md` 一致）：只保留当前发布和一个可运行的紧邻回滚，更早的普通制品核验后按授权清理。`docker_image_retention.sh` 因保护全部 `rollback-*` 与 runtime-base 而给不出候选（TODOLIST「定期运维」跟踪），所以本次是手工清单：先只读盘点并生成候选清单与它的 sha256，复核后带着这个 sha256 才能执行，执行时逐项 `docker rmi <repo:tag>`，不用 `docker system prune`，不动数据卷。

- **镜像保留集**（`memoria-{agent,control-api,speaker-model,media-edge}`）：运行中容器的 tag；紧邻回滚 = 上一版整栈（三个角色）加 control-api 组件版，以及本次发布冻结时生成的全部 `rollback-<当前 tag>-pre`；media-edge 保留运行中的和前两个组件 tag；`memoria-agent-runtime-base` 的全部 tag（其底层镜像因此也留着）；所有非 `memoria-*` 镜像。
- **目录**：`incoming/` 只留当前与上一版整栈、最近三个 media-edge；`releases/<tag>` 旧版删源码树、保留 `.cutover`（发布前 DB 备份、pre/post 状态、env 快照）；`component-releases/*/build` 可删、同目录下的 rollback/component override 与 `CUTOVER_RESULT.txt` 保留。
- **禁止删除的引用**：运行中容器的 compose 文件在 `docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <容器>` 里。2026-10-01 的引用是：Postgres 用 `releases/20260827-architecture-split-v1`，Redis 用 `releases/20260823-210222-voice-fix`，MinIO 用 `current/infra`，media-edge 用 `releases/20260930-local-stop-v2` 加 `component-releases/20260930-late-receipt-v1-media-edge/media-edge-component.override.yml`（组件发布的 media-edge 一直挂在它当时的整栈发布树上）；回滚 control-api 组件链还要 `component-releases/20260930-vector-keyword-v1` 的两个 override。
- **结果**：根分区已用 90 → 37 GB（80% → 33%），其中镜像约 29.5 GB（`/var/lib/containerd` 43 → 14 GB）、`incoming` 20.9 GB、旧发布树 1.2 GB、组件 build 0.3 GB、`/home/ubuntu` 构建残留 1.7 GB。清理后全部容器 healthy，`/health/ready` 内外均 200，回滚镜像与 compose 链文件逐项核对存在。未动：journald（约 1.1 GB）、WMS/saas 文件、Docker 卷、`/var/backups/memoria`。
