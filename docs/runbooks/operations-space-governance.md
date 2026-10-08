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
- **禁止删除的引用**：运行中容器的 compose 文件在 `docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <容器>` 里。2026-10-01 的引用是：Postgres 用 `releases/20260827-architecture-split-v1`，Redis 用 `releases/20260823-210222-voice-fix`，MinIO 用 `current/infra`，media-edge 用 `releases/20260930-local-stop-v2` 加 `component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml`（2026-10-02 19:05 起；此前是 `20260930-late-receipt-v1-media-edge`，旧目录留作记录，新目录里的 `media-edge-rollback.override.yml` 回到它的镜像；组件发布的 media-edge 一直挂在它当时的整栈发布树上）；回滚 control-api 组件链还要 `component-releases/20260930-vector-keyword-v1` 的两个 override。
- **结果**：根分区已用 90 → 37 GB（80% → 33%），其中镜像约 29.5 GB（`/var/lib/containerd` 43 → 14 GB）、`incoming` 20.9 GB、旧发布树 1.2 GB、组件 build 0.3 GB、`/home/ubuntu` 构建残留 1.7 GB。清理后全部容器 healthy，`/health/ready` 内外均 200，回滚镜像与 compose 链文件逐项核对存在。未动：journald（约 1.1 GB）、WMS/saas 文件、Docker 卷、`/var/backups/memoria`。

### 2026-10-02 第二次手工清理（经用户授权「先清理磁盘」）

同一套规则，间隔 1.5 天、11 次整栈。执行方式比第一次更固化：①只读盘点（容器、镜像含完整 id、compose 引用、`incoming`、`du -x` 逐层核对磁盘去向；注意不带 `sudo` 的 `du` 会漏掉 root 目录，`incoming` 看起来 133 MB 实际 19 GB）；②保留集写成规则（当前 + 紧邻回滚 + 本次 freeze 的 `rollback-<当前>-pre` + runtime-base + media-edge 三个 tag + 非 memoria 镜像），候选由脚本按规则生成并与同 id 的保留 tag 比对（同 id 的旧 tag 只是去标签、不占空间）；③候选清单和目录清单各算 sha256，写进带门禁的脚本，脚本在服务器上先复核 sha256 再执行；④分阶段：先镜像、复核、再目录、复核。每阶段门禁：没有 `release-ops`/`docker load` 进程、七个运行容器 healthy、内部与外部 readiness、保留集镜像都在。
结果：根分区 80 → 41 GB（71% → 37%），containerd 38 → 15 GB，`incoming` 19 → 3.0 GB。增长速度实测：11 次整栈 +43 GB，约每次 4 GB（containerd 约 2.2 GB + `incoming` 约 1.5 GB），所以约每 6 次整栈清一次，或在根分区超过 70% 时清。这次没动 `releases/*` 旧源码树（1.2 GB）、`component-releases`（0.5 GB）、journald、数据卷、`/var/www/memoria-releases`。收据在服务器 `/root/memoria-release/cleanup-20261002-*.log` 与 `cleanup-20261002-pre/`。

### 2026-10-04 第三次手工清理（经用户授权「清理 worktree 和磁盘」）

同一套规则，间隔约 1 天（10-02 23:00 → 10-04 00:41，北京时间）、4 次整栈（`20261003-endpoint-latency-v1`、`20261003-child-memory-v1`、`20261003-echo-merge-v1`、`20261004-followup-warm-v1`）。清理前根分区 61 GB（54%）、containerd 27 GB、`incoming` 11 GB、memoria 镜像 49 个 tag。保留集：`memoria-{agent,control-api,speaker-model}` 的当前 `20261004-followup-warm-v1`、紧邻回滚 `20261003-echo-merge-v1` 与本次 freeze 的 `rollback-20261004-followup-warm-v1-pre`；media-edge 运行中的 `20261002-late-progress-v1` 与前两个 `20260930-late-receipt-v1`、`20260930-edge-reject-log-v1`；`memoria-agent-runtime-base` 与 `memoria-sensevoice-asr` 的全部 tag、所有非 memoria 镜像；`incoming/` 留当前与上一版整栈目录和三个 media-edge 目录。候选 31 个镜像 tag（sha256 `8700c6b1…`）与 6 个目录（sha256 `059888a0…`）由脚本按规则生成，并断言没有一个与保留 tag 或运行容器同镜像 id。脚本 `/root/memoria-release/cleanup-20261004.sh`（sha256 `0a5f15d2…`，root 0700）比 10-02 版多两道门禁：三个整栈容器必须跑在当前 tag、`media_active_sessions` 必须为 0（不在有会话时删大文件）；其余门禁不变（没有发布进程、七个运行容器 healthy、内外 readiness、保留集镜像都在），镜像与目录分两阶段，每阶段前后各过一遍。

结果：根分区 61 → 43 GB（54% → 38%），containerd 27 → 15 GB，`incoming` 11 → 3.0 GB，memoria 镜像 49 → 18 个 tag。清理后带健康检查的容器都是 healthy，内外 readiness 200；运行容器引用的 compose 文件（整栈三个容器 `releases/20261004-followup-warm-v1`，media-edge `releases/20260930-local-stop-v2` 加 `component-releases/20261002-late-progress-v1-media-edge` 的 override，Postgres `releases/20260827-architecture-split-v1`，Redis `releases/20260823-210222-voice-fix`，MinIO `current/infra`）逐项核对存在。增长速度：从上次清理后的 41 GB 到 61 GB，4 次整栈约 +20 GB（每次约 5 GB：containerd 约 3 GB、`incoming` 约 2 GB），所以约每 5–6 次整栈清一次，或在根分区超过 70% 时清。这次同样没动 `releases/*` 旧源码树、`component-releases`、journald、数据卷、`/var/www/memoria-releases`。收据在服务器 `/root/memoria-release/cleanup-20261004-*.log` 与 `cleanup-20261004-pre/`（`docker ps`、镜像清单含完整 id、卷、`incoming` 列表、`df`、两份候选清单）。

### 2026-10-04 本机（Mac 构建机）Docker 清理（经用户授权「清理docker」）

发现于补跑带数据库的本地测试：Docker Desktop 虚拟机的数据盘（`/dev/vda1`，79 GB）写满，`memoria-pgv` 报 `No space left on device`；发布构建在本机做（`docker compose build`、`docker save`），写满会让下一次发布失败。检查：`docker exec memoria-pgv df -h /var/lib/postgresql/data`，或 `docker run --rm --privileged --pid=host --entrypoint nsenter <任一本地镜像> -t 1 -m -u -n -i sh -c 'df -h /var/lib/docker; du -xsh /var/lib/docker/*'` 看虚拟机里 `overlay2`（镜像层与构建缓存）与 `volumes` 各占多少。

做法：①`memoria-{agent,control-api,speaker-model}` 只留最新两个 tag、`memoria-media-edge` 只留当前与上一个组件 tag，其余逐项 `docker rmi`（本次 80 个 tag）；②**只删镜像不会让虚拟机磁盘变小**，这些层还被构建缓存引用，要再 `docker builder prune -af --keep-storage 8GB`（保留最近用过的约 8 GB，本次回收 26.6 GB）。结果：虚拟机磁盘空出约 28 GB（可用约 24 GB），`memoria-pgv` 自己从崩溃恢复。不动其他项目的镜像、已停止的容器与数据卷（`docker volume prune` 会删别的项目的数据，须单独授权），不用 `docker system prune`；`delta_build_images.sh` 要的底座镜像（最新发布 tag）保留。之后第一次本机发布构建会因缓存变小而慢一些。

### 2026-10-08 第四次手工清理（经用户授权「先清理磁盘，删除没用的备份数据等等」）

同一套规则，间隔约 4 天，在整栈 `20261007-n8-diag-v1` 切流之后做。清理前根分区 28 G / 40 G（75 %），containerd 镜像层约 12 G，`incoming/` 4.3 G，`/var/backups/memoria-july-archive-20261005` 1.6 G。步骤：①只读盘点（`du -x`；root 目录的通配符要在 root 下展开，`sudo sh -c 'du … /dir/*'`，否则非 root 的 shell 展不开，输出是空的）；②保留集与运行中容器的 compose 引用核对（`com.docker.compose.project.config_files` 标签：Postgres 与 MinIO 用 `releases/20260827-architecture-split-v1`，Redis 与 media-edge 用 `releases/20261004-first-warm-v1`，media-edge 另加 `component-releases/20261002-late-progress-v1-media-edge` 的 override）；③候选的逐文件 sha256 清单写进 `/root/memoria-release/cleanup-20261008-pre/`；④逐项执行，过程写进 `/root/memoria-release/cleanup-20261008.log`。

删除的：镜像 tag 12 个（`memoria-{agent,control-api,speaker-model}` 的 `20261004-first-warm-v1`、`20261005-subject-candidates-v1`，以及两个过期的 `rollback-20261005-…-pre`、`rollback-20261006-…-pre`；都是 `docker rmi <repo:tag>`，不是 prune）；`incoming/20261005-subject-candidates-v1`（1.5 G）；`releases/20261005-subject-candidates-v1` 的源码树（保留 `.cutover`，4.3 M）；`/var/backups/memoria-july-archive-20261005`（1.6 G：7 月候选部署的 volumes 归档、旧 env 文件（含密钥）与旧发布树；它自己的 README 写着 nothing in use，逐文件 sha256 清单 14,559 条）。

有意保留：`/root/old-host-20261005/`（99 M，其中 `var-backups-memoria.tar.gz` 是旧机 `/var/backups/memoria` 的唯一本地还原点，见上文「2026-10-05」一节）；`/var/backups/memoria`（11 M，未动）；`releases/20261004-first-warm-v1` 与 `releases/20260827-architecture-split-v1`（compose 链）；`incoming/20261006-speaking-flush-v1` 与 `incoming/20261007-n8-diag-v1`（上一版整栈与当前）；`/home/ubuntu` 下不属于 Memoria 的 family-growth-h5、prepulse（归属未核实，未动）；数据卷（未 prune）；journald（约 1 G，未动）。

结果：根分区 28 G → 21 G（75 % → 55 %，剩 18 G）。容器全部 healthy，本机与外部 readiness 200，`current` 不变，20261006 回滚镜像的 image id 与之前一致。`docker system df` 仍显示约 4.2 G 可回收，那是 20261006 整栈与 `rollback-20261007-*-pre`（同 id），按规则不删。


### 2026-10-05 迁到 110.42.235.198 后的空间基线（新机，不是一次清理）

生产从 2026-10-05 起在 110.42.235.198：40 GB 盘（旧机 118 GB），与 pocketSparks、hr-tracker 共用，迁移后已用约 17 GB（45%）。上文的 70% 触发线与「约每 5–6 次整栈清一次」是旧机的数字，在新机上要重算：整栈发布每次约 +4–5 GB（WAL 归档曾约 +0.6–1.1 GB/天，2026-10-06 起已关，见 [发布、恢复与回滚运维手册](release-rollback.md)「生产主机」），70% 是 28 GB，迁移后 17 GB、第一次整栈发布后 21 GB；`memoria-disk-patrol.timer` 的 75% / 85% 是百分比，同样适用，只告警不清理。规则不变：只清 `memoria-*` 镜像（`docker_image_retention.sh` 只处理这类 tag，不碰 pocketSparks、hr-tracker 和其他项目的镜像与卷）；每次发布核对完立刻删 `incoming` 里的包；镜像只留当前 + 一个回滚；数据卷仍不动，清理仍须另行授权。

