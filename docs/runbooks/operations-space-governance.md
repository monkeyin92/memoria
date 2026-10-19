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

Freeze 转储清理（只删 `/opt/memoria/releases/*/.cutover/` 下超过 3 天的 `memoria-pre-*.dump` 与 `memoria.pre-*.sqlite3`；执行仍需另行授权）：

~~~bash
sudo install -m 0755 scripts/cleanup_freeze_dumps.py /opt/memoria/ops-tools/cleanup_freeze_dumps.py
sudo install -m 0644 infra/memoria-freeze-dump-cleanup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-freeze-dump-cleanup.timer
~~~

容器日志（control-api 与 voice-core-media-bridge，`/var/log/memoria`）由 `memoria-log-rotate.timer` 每小时轮转：每个服务最坏约 160 MiB 未压缩，两个服务约 320 MiB；另有 Docker 本地缓存每个容器 30 MiB，在 `/var/lib/docker/containers/<id>/` 下，随容器删除。数字在 `infra/memoria-logs.logrotate` 与 `docker-compose.production.yml`。空间清理不动这个目录；安装、停写、清理与回滚只按 `docs/runbooks/release-rollback.md`「容器日志落盘」做，执行同样需另行授权。

儿童记忆两层的到期物理删除不装 systemd timer。control-api 启动时跑一次，之后每 24 小时跑一次（`ChildLayerRetentionWorker`，`services/memory_scope/child_layer_retention.py`）：先删已过 `retention_expires_at` 的 `kind=dialogue_summary`，再删连续 180 天无活跃的 `kind=child_archive`。读不到 `guardian_subject_activity` 时跳过档案、照删摘要。日志只有计数，不含使用人 id。control-api 停机的日子不会补跑，下一次启动会补上。清除前 30 天、7 天的家长提醒还没有发送，180 天清除照常执行。

确认它在跑：

~~~bash
docker compose -f docker-compose.production.yml logs control-api | grep 'child layer retention'
~~~

这条只读当前容器的 Docker 缓存。跨重建的历史在主机文件里：

~~~bash
sudo sh -c "zgrep -h 'child layer retention' /var/log/memoria/control-api.log* | sort"
~~~

每一轮两行，形如 `child layer retention kind=dialogue_summary subjects=0 failed=0 memory_records=0 memory_status_events=0`；`kind=child_archive ... skipped reason=...` 表示档案这一轮没有删。手工核对用一次性 Control API 容器，先不带 `--apply` 看候选，带 `--apply` 才删；重复 `--apply` 删除计数为零、退出码 0，`failed` 非零时退出码 1。生产 compose 需要 `MEMORIA_RELEASE_TAG` 和 `MEMORIA_RELEASE_COMMIT`，从运行中的 control-api 容器导出后再跑：

~~~bash
control=memoria-control-api-1
export MEMORIA_RELEASE_TAG="$(docker exec "$control" printenv MEMORIA_RELEASE_TAG)"
export MEMORIA_RELEASE_COMMIT="$(docker exec "$control" printenv MEMORIA_RELEASE_COMMIT)"
docker compose -f docker-compose.production.yml run --rm --no-deps control-api /app/.venv/bin/python -m scripts.expire_child_dialogue_summaries
docker compose -f docker-compose.production.yml run --rm --no-deps control-api /app/.venv/bin/python -m scripts.expire_idle_child_archive
~~~

## 记忆向量缺口巡检

家长纠正记忆或编译期嵌入失败时，best-effort 路径会提交审核、删掉旧向量，却可能没有写回当前模型的新向量；这类 claim 只缺向量通道，词法召回仍在。control-api 的 `MemoryVectorBackfillWorker` 默认每 3600 秒跑一轮（`MEMORIA_MEMORY_VECTOR_BACKFILL_INTERVAL_S`），每轮最多处理 `MEMORIA_MEMORY_VECTOR_BACKFILL_BATCH` 条（默认 200），只按 `memory_claims` 当前值重嵌，不读原话、历史 payload 或 search 行里的 `context:` 段。向量表或 embedder 未配置时不启动，日志里没有任何 ID。

巡检脚本是只读的，只做 SELECT；输出一行 JSON 计数，`missing` 大于 0 时退出码 1，无缺口 0，脚本自身错误 3（同 `disk_patrol.sh` 的约定）。`unlinked_child_claims` 是观察项，不影响退出码：它统计 `subject_key` 以 `son:`/`daughter:` 开头（不区分大小写）、`about_subject_id` 为空、未撤回且源证据仍在的 claim。账户有多个绑定孩子时按「多孩不关联、宁缺不错」保持为空，这类记忆在删孩子时不会被连带删除，是已知且预期的状态，所以非 0 只用于观察，不报警、也不改退出码；只有向量缺口 `missing` 决定 1。

~~~bash
control=memoria-control-api-1
export MEMORIA_RELEASE_TAG="$(docker exec "$control" printenv MEMORIA_RELEASE_TAG)"
export MEMORIA_RELEASE_COMMIT="$(docker exec "$control" printenv MEMORIA_RELEASE_COMMIT)"
docker compose -f docker-compose.production.yml run --rm --no-deps control-api /app/.venv/bin/python -m scripts.count_missing_memory_vectors
# {"corrected": 0, "missing": 0, "unlinked_child_claims": 2}
~~~

直接在 postgres 容器里核对的等价只读查询如下；把 `<model>` 和 `<dimensions>` 换成控制面容器里的 `MEMORIA_MEMORY_EMBEDDING_MODEL` / `MEMORIA_MEMORY_EMBEDDING_DIMENSIONS`，不要靠猜：

~~~sql
WITH current AS (
    SELECT '<model>'::text AS model, <dimensions>::integer AS dimensions
)
SELECT count(*) AS missing,
       count(*) FILTER (
           WHERE EXISTS (
               SELECT 1 FROM archive_evidence_events review
               WHERE review.account_id = claim.account_id
                 AND review.event_type = 'memory.claim_reviewed'
                 AND review.source = 'user.memory_review'
                 AND review.payload->>'action' = 'correct'
                 AND review.payload->>'target_id' = claim.claim_id::text
           )
       ) AS corrected
FROM memory_search_documents document
JOIN memory_claims claim
  ON claim.claim_id = document.item_id
 AND claim.account_id = document.account_id
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
CROSS JOIN current
WHERE document.kind = 'claim'
  AND document.status <> 'retracted'
  AND claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_vector_documents stored
      WHERE stored.account_id = document.account_id
        AND stored.item_id = document.item_id
        AND stored.embedding_model = current.model
        AND stored.embedding_dimensions = current.dimensions
  );
~~~

`unlinked_child_claims` 的等价只读查询，同样只出计数；需要单账户时加上 `AND claim.account_id = '<account>'`：

~~~sql
SELECT count(*) AS unlinked_child_claims
FROM memory_claims claim
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE claim.about_subject_id IS NULL
  AND claim.status <> 'retracted'
  AND (lower(btrim(claim.subject_key)) LIKE 'son:%'
       OR lower(btrim(claim.subject_key)) LIKE 'daughter:%');
~~~

定时任务每轮一行日志，形如 `memory vector backfill attempted=0 embedded=0 failed=0 skipped=0 remaining=0`，`remaining` 就是上面查询的总数。确认它在跑：

~~~bash
docker compose -f docker-compose.production.yml logs control-api | grep 'memory vector backfill'
sudo sh -c "zgrep -h 'memory vector backfill' /var/log/memoria/control-api.log* | sort"
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


### 2026-10-09 第五次手工清理（经用户授权「先做2，3吧」）

同一套规则，间隔约 1 天，在整栈 `20261009-textless-spare-v1` 切流之后做。清理前根分区 25 G / 40 G（65 %），`incoming/` 三个包各 1.5 G。回滚深度是 1，所以 20261006 整栈已不是回滚目标。执行方式比 10-08 又固化了一步：脚本 `/root/memoria-release/cleanup-20261009.sh`（root 0700，sha256 `66b56d94…`）分 `pre`（只读盘点、写快照与逐文件 sha256 清单，不改任何东西）与 `apply` 两个模式，候选是写死的清单，不用通配符；`apply` 前后各过一遍门禁：没有发布或 docker 进程、七个带健康检查的容器 healthy 加 SenseVoice 在跑、三个整栈角色在当前 tag 上、`current` 指向当前发布树、本机 readiness 200、保留集镜像都在、近 5 分钟 bridge 日志没有 `media turn committed`、近 10 分钟边缘日志没有 `device=dev_`；候选镜像还要断言与任何保留 tag 或容器用的镜像 id 不同。

删除的：6 个镜像 tag，对应 3 个镜像（`memoria-{agent,control-api,speaker-model}` 的 `20261006-speaking-flush-v1` 与同 id 的过期 `rollback-20261007-n8-diag-v1-pre`；逐个 `docker rmi <repo:tag>`，不是 prune）；`incoming/20261006-speaking-flush-v1`（1.5 G，6 个文件的 sha256 清单）；`releases/20261006-speaking-flush-v1` 的源码树（1,685 个文件的 sha256 清单，保留 `.cutover`，71 M → 4.3 M）。

有意保留：`incoming/20261007-n8-diag-v1`（下一次增量上传的种子）与 `incoming/20261009-textless-spare-v1`（当前）；`releases/20261007-n8-diag-v1`（回滚目标的发布树）与当前发布树；镜像 `20261009`、`20261007` 与 `rollback-20261009-textless-spare-v1-pre`、media-edge、SenseVoice；`releases/20261004-first-warm-v1` 与 `releases/20260827-architecture-split-v1`（compose 链）；数据卷、journald、`/root/old-host-20261005/` 都没动。

结果：根分区 25 G → 21 G（65 % → 55 %，剩 18 G），`docker system df` 的镜像数 19 → 16。清理前后门禁全部通过，四个关键容器 healthy，本机与外部 readiness 200，`current` 不变。收据在服务器 `/root/memoria-release/cleanup-20261009.log`、`cleanup-20261009-precheck.log`、`cleanup-20261009-apply.out` 与 `cleanup-20261009-pre/`（前后快照、两份 sha256 清单、候选镜像与 id）。

### 2026-10-10 第六次手工清理（经用户授权「清理20261007 的 incoming 包、镜像和发布树」）

同一套规则，间隔约 1 天，在整栈 `20261010-stale-interim-v1` 切流之后做。清理前根分区 25 G / 40 G（65 %），`incoming/` 三个包各 1.5 G。回滚深度是 1：当前栈 20261010，回滚目标 20261009，所以 20261007 既不在线、也不是回滚目标。

只读盘点先行：所有容器（含已停止的）的 compose 标签都不引用 `releases/20261007-n8-diag-v1`；`incoming/20261007-n8-diag-v1` 的六个文件硬链接数都是 1（没有和别的目录共享 inode）；六个候选镜像标签（`memoria-{agent,control-api,speaker-model}` 的 `20261007-n8-diag-v1`，以及同 id 的 `rollback-20261009-textless-spare-v1-pre`，那是 20261009 冻结时从 20261007 打的标签）没有任何容器在用；保留集的镜像都在。脚本 `/root/memoria-release/cleanup-20261010.sh`（root 0700）分 `pre`（只读：快照、两份 sha256 清单、候选镜像与 id）与 `apply`。`apply` 前后各过一遍门禁：没有发布或 docker 进程；七个带健康检查的容器与 SenseVoice 在跑；三个整栈角色都在当前 tag 上；`current` 指向当前发布树；本机 readiness 200；保留集镜像都在；近 5 分钟没有 `media turn committed`，近 10 分钟边缘没有 `device=dev_`。删 incoming 之前重新校验它的 sha256。

删除的：6 个镜像标签（逐个 `docker rmi <repo:tag>`，不是 prune；同 id 的两个标签都删掉后镜像层才释放）；`incoming/20261007-n8-diag-v1`（1.5 G）；`releases/20261007-n8-diag-v1` 的源码树（保留 `.cutover`，4.4 M：pre-cutover 数据库转储、pre/post 状态、env 快照）。

有意保留：`releases/20261007-n8-diag-v1/.cutover`（收据）；`incoming/20261009-textless-spare-v1`（回滚目标的增量种子）与 `incoming/20261010-stale-interim-v1`（当前）；镜像 `20261010`、`20261009` 与 `rollback-20261010-stale-interim-v1-pre`（= 20261009 镜像）、media-edge、SenseVoice；`releases/20261009-textless-spare-v1`（回滚目标的 compose 链）、`releases/20261004-first-warm-v1`、`releases/20260827-architecture-split-v1`；数据卷、journald、`/root/old-host-20261005/`。本机（Mac）上 `~/Library/Caches/memoria-release-build/20261007-n8-diag-v1/`（64 M）与本机 Docker 里的 20261007 镜像没有动。

结果：根分区 25 G → 21 G（65 % → 55 %，剩 18 G）；memoria 镜像标签 17 → 11 个；`docker system df` 的镜像可回收量为 4.2 G。清理后七个健康检查容器全部 healthy、SenseVoice 在跑、重启次数不变，本机与外部 readiness 200，`current` 不变，回滚目标 20261009 的三个镜像 id 与 compose 文件都在。`apply` 的最后一行 `docker system df | head -2` 因 SIGPIPE 退出 141，发生在所有删除与校验之后，不影响结果。收据在服务器 `/root/memoria-release/cleanup-20261010.log`、`cleanup-20261010-precheck.log` 与 `cleanup-20261010-pre/`。

### 2026-10-19 第七次手工清理（经用户授权「允许磁盘清理」）

同一套规则，距上次约 9 天，期间整栈发布 20 余次，根分区 29 G / 40 G（76 %，过了 75 % 告警线）。脚本 `/root/memoria-release/cleanup-20261019.sh`（root 0700）分 `pre`（只读）与 `apply`，候选写死，不用通配符、不 prune。`apply` 前后各过一遍门禁：没有发布或 docker 进程；六个带健康检查的容器 healthy 且 SenseVoice 在跑；bridge 与 control-api 在当前 tag；`current` 指向当前发布树；保留集镜像都在；本机与外部 readiness 通过。此外 `refs_check` 核对候选树不被任何容器的 compose 标签引用、候选镜像不被孤儿容器之外的容器使用。

删除的：
- 孤儿容器 `memoria-speaker-model-1`（speaker-model 10-13 退役后一直 exited，compose 每次都提示 orphan）和它的镜像 `memoria-speaker-model:20261013-guardian-summaries-v1`（627 M）。
- 镜像标签 `memoria-{agent,control-api}:20261016-device-registration-v1`（这版 22:28 切流后 22:30 已回滚，镜像 id 与回滚点不同，逐个 `docker rmi`）。
- `incoming/` 三个发布包各 1.2 G：`20261016-device-registration-v1`、`20261015-stop-standby-v1`（已回滚、对应 PR #270 已关闭）、`20261015-daily-chat-limit-v1`。
- 22 棵旧发布树的源码，保留各自 `.cutover`（收据）；`20261014-support-tiers-v1`（verify-load 盘满中止的那版）本来就没有 `.cutover`。删除前核对：其中 21 棵的 tag 提交已在 `origin/main`；`20261015-stop-standby-v1`（提交 `0228dfdf`）不在 main，已存进本机 bundle，删后可恢复。

有意保留：当前发布树 `20261016-readiness-b54189c`；回滚目标 `20261016-session-cap-fallback-v1`（`.cutover/prev-release` 指向它）和手工一步回退点 `20261016-log-redaction-v1`，两者的镜像、发布树、`incoming/` 包都在；compose 链 `releases/20260827-architecture-split-v1`、`releases/20261004-first-warm-v1` 与 `component-releases/20261002-late-progress-v1-media-edge`；media-edge、SenseVoice、redis、minio、pgvector 镜像；数据卷、journald、`/var/log`。

结果：根分区 29 G → 21 G（76 % → 56 %，剩 17 G）；容器全部 healthy，外部 readiness 通过。收据在服务器 `/root/memoria-release/cleanup-20261019.log` 与 `cleanup-20261019-pre/`（root 0700，含三个 `incoming` 包的 sha256 与前后快照）。**下一次整栈发布**：当前版没有 `incoming/` 包可作增量种子，会走整包上传（约 1.5 GB，几分钟）。

### 2026-10-05 迁到 110.42.235.198 后的空间基线（新机，不是一次清理）

生产从 2026-10-05 起在 110.42.235.198：40 GB 盘（旧机 118 GB），与 pocketSparks、hr-tracker 共用，迁移后已用约 17 GB（45%）。上文的 70% 触发线与「约每 5–6 次整栈清一次」是旧机的数字，在新机上要重算：整栈发布每次约 +4–5 GB（WAL 归档曾约 +0.6–1.1 GB/天，2026-10-06 起已关，见 [发布、恢复与回滚运维手册](release-rollback.md)「生产主机」），70% 是 28 GB，迁移后 17 GB、第一次整栈发布后 21 GB；`memoria-disk-patrol.timer` 的 75% / 85% 是百分比，同样适用，只告警不清理。规则不变：只清 `memoria-*` 镜像（`docker_image_retention.sh` 只处理这类 tag，不碰 pocketSparks、hr-tracker 和其他项目的镜像与卷）；每次发布核对完立刻删 `incoming` 里的包；镜像只留当前 + 一个回滚；数据卷仍不动，清理仍须另行授权。

