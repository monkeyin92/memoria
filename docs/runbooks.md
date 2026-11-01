# Memoria Runbooks

本文件由 `scripts/consolidate_docs.py` 按类型归并生成。每个来源章节保留原文和来源路径。

## 来源文件

- `docs/runbooks/followup-grace-experiment.md`
- `docs/runbooks/operations-space-governance.md`
- `docs/runbooks/release-rollback.md`
- `docs/runbooks/sensevoice-asr.md`
- `docs/runbooks/tts-circuit-breakers.md`
- `docs/runbooks/volc-asr-protocol-check.md`

---

## 来源：`docs/runbooks/followup-grace-experiment.md`

## 播放后续问宽限实验（TODOLIST N-14 第 3 档）

### 要回答的问题

机器人说完一段话之后，孩子的下一句没有设备 VAD 边沿，服务端只能靠 ASR 终稿定端点：终稿到达后固定再等 **1.2 s**（`media_session_playback_stop.py` 的 `_PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S`），其间没有新的终稿才提交。把它改短能让每一次这样的后续问早开口 0.1–0.9 s；代价是孩子句子中间的停顿可能把一句话切成两次提交。**判据**：0.6 s 及更短的停顿上，切分率不比 1.2 s 基线高，才可以把默认值改短。

读代码得到的预期（实验要验证的假设）：这条路径只在「同一句话产生了两条终稿」时才有差别——停顿短于 FunASR 自己的断句阈值时只有一条终稿，宽限无关；停顿更长时第一条终稿到达后要等第二条**终稿**（说完才到，不是部分结果）在宽限内到达，第二半句稍长就赶不上 1.2 s，所以真正受宽限影响的是**第二半句很短**的句子（「……吗」「……呀」）。因此句子里要有短后半句，也要有长后半句。

### 旋钮

`MEDIA_PLAYBACK_FOLLOWUP_GRACE_S`（bridge 的环境变量，0.3–1.2 s，只能比默认短；不设、写错、超出范围都是 1.2 s，并记一条警告日志）。默认行为不变；旋钮随下一次整栈发布上线。

### 步骤

1. 准备句子：`sentences.json`，每条 `{"tag","a","b"}`，建议 10 句（半数后半句 ≤ 4 个字）。
2. 渲染两半：`voice_soak_pause_split.py items` → `voice_soak_bank.py render`（生产豆包音色）。
3. 拼接静音与场景：`voice_soak_pause_split.py clips --pauses 0,0.3,0.6,0.9,1.2 --repeat 10`（0 秒是不切的对照；每轮每个停顿长度各一次，句子逐轮错位，不会有某句总配同一个停顿）。
4. 真机一轮（**需要用户点头**；Mac 音量 30 即可——2026-10-04 基线和候选都在 30 下跑，各 50 步里 49 步有回答，不必放开到 50；USB 唤醒、先起常驻串口记录进程，见 `docs/acceptance.md` 的方法一节）：先在 1.2 s（不设旋钮）上跑基线，再把旋钮设成候选值重跑同一份场景（2026-10-04 用 0.6；要试中间值就用 0.8–0.9）。改旋钮是生产环境变更：写 `/etc/memoria-agent.env`（先备份）并**重建** bridge 容器（`env_file` 只在创建时读，普通 restart 不会生效），须用户当场授权；做完把变量去掉再重建即回到默认，并核对 env 文件哈希与原备份一致。2026-10-04 用本机套件的 `drill/grace_set.sh <值|default>` 做（门禁、备份、一处编辑、重建、失败自动还原；要作为文件在服务器上跑，不要走 `bash -s`）。
5. 看数：`voice_soak_pause_split.py report` 按「片段结束」定窗口，长后半句的前半句在片段还在播时就提交了，会被算到上一步（2026-10-04 基线算出假的「停顿 0 切 3/10」），不能直接用；要按「片段开始」定窗口，再统计每个停顿长度的「一次提交 / 两次及以上」与说完到开口的中位数（本机套件 `grace/grace_report.py` 的 `report` / `compare` 就这样做），并加指标「前半句单答」（回复开口距片段结束 < 1.3 s 且不是切成两次提交：前半句被当成整句回答，后半句的终稿在回复中到达被丢掉；要用设备串口的 `listening -> speaking` 算（本机套件 `grace/early_reply_recount.py`），驱动的 `latency_s` 窗口从片段结束前 0.5 s 起算，回复更早开口会被记成「无回复」）；另看 `media playback-followup endpoint`、`media pending turn split` 的日志。
6. 看宽限到底省了多少：每次提交有一行 `media turn commit timing`，`final_to_start_ms` 是「最后一个终稿 → 提交开始」，改旋钮后它应当约少 1.2 s 与候选值之差；`python scripts/voice_commit_timing.py <bridge.log>` 汇总成 p50 / p90（含两种云端分类器调用的耗时；提交有没有等结束对话判定看 `close_ms`，`classifier verdict wait` 行也含终稿时的背景调用，不能用来数）。缩短宽限要在 TODOLIST N-14 ⑦（结束对话判定在终稿时起跑）上线之后做，否则宽限短于分类器的 p90 0.82 s 时，提交又要等分类器，会低估宽限的收益。这几行日志不含原文。

### 结果（2026-10-04 夜，每种宽限一遍，50 步）

基线 1.2 s 对候选 0.6 s，同一份场景，全部数字见 `docs/acceptance.md` 第十七轮：终稿 → 提交开始 p50 1201 → 602 ms；上面的判据满足（停顿 0.3 + 0.6 s 切成两次提交 0/20 对 0/20），但它漏了两类：①停顿 ≥ 0.9 s 的句子切成两次提交 1/19 → 8/20，长、短后半句都受影响（与前面「只有短后半句受影响」的预期不符）；②前半句单答，基线 1.2 s 下就有 7/50，候选 14/50（单看不显著）。结论：现在不把默认值改成 0.6 s（你已采纳，默认值维持 1.2 s）；中间值（0.8–0.9 s）没测。下次判据改成「整句没被一次答对」（切成两次提交 + 前半句单答）按停顿分组比较，并先量真实孩子的停顿分布（本实验答不了）。

### 边界

单设备、Mac 合成音、安静房间；停顿是拼接的数字静音，真实孩子的停顿会带呼吸和语气词。数字只用来决定「改短会不会明显多切」，不是对孩子真实说话习惯的统计。

---

## 预发回复请求（speculative reply）

ASR 终稿到达、提交开始时，bridge 就按预测把这一轮要发的 DeepSeek 回复请求发出去，回来的字先存着。提交做完后，只有这一轮真正要发的请求与预发的逐字相同，才播放存着的字；句子、说话人、fence、模式策略、上下文、记忆、语气计划有任何一处不同，就取消预发请求，按原路径重发。危机、结束对话、实时查询、本地直答（如报时）这几条路不问回复模型，从不播放预发的字。预发请求只调用 DeepSeek，不写上下文、转写或记忆。

每个预发请求以一行日志结束，不含原文：`speculative reply session_id=… turn_id=… outcome=… lead_ms=…`。`outcome` 是 `used`（被采用）、`other_text` / `other_speaker` / `other_fence` / `other_policy`（提交的不是预测的那一轮）、`other_request`（同一轮，但请求内容变了）、`late`（提交时请求还没发出，通常是记忆还没取回）、`close` / `crisis` / `lookup` / `plan`（这一轮不问回复模型）、`memory`（记忆在范围内但这句话没有预取）、`context`（快照预测不了）、`unused`（匹配了但这一轮最后没问回复模型）、`not_committed`（提交拒绝了这句话）、`restarted`（同一句话换了一次预测）。`lead_ms` 是被采用时请求已经发出多久，即这一轮首字等待最多省下的时间。

看数：`python scripts/voice_commit_timing.py bridge.log` 的最后一段 `== speculative replies` 给出各 outcome 的次数和 `used` 的 `lead_ms` p50 / p90；采用率按 `used` 除以 `result=committed` 的提交数算。

开关：bridge 环境变量 `MEMORIA_SPECULATIVE_REPLY_ENABLED=false` 关掉，行为回到旧路径，不发预发请求，也不打这行日志。改 env 是生产变更，要用户授权，并且要重建 bridge 容器才生效。代价：没被采用的预发请求是一次中途取消的 DeepSeek 调用。

---

## 来源：`docs/runbooks/operations-space-governance.md`

## 空间治理运维基线

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

容器日志（control-api 与 voice-core-media-bridge，`/var/log/memoria`）由 `memoria-log-rotate.timer` 每小时轮转：每个服务最坏约 160 MiB 未压缩，两个服务约 320 MiB；另有 Docker 本地缓存每个容器 30 MiB，在 `/var/lib/docker/containers/<id>/` 下，随容器删除。数字在 `infra/memoria-logs.logrotate` 与 `docker-compose.production.yml`。空间清理不动这个目录；安装、停写、清理与回滚只按 `docs/runbooks.md`「容器日志落盘」做，执行同样需另行授权。

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

### 记忆向量缺口巡检

家长纠正记忆或编译期嵌入失败时，best-effort 路径会提交审核、删掉旧向量，却可能没有写回当前模型的新向量；这类 claim 只缺向量通道，词法召回仍在。control-api 的 `MemoryVectorBackfillWorker` 默认每 3600 秒跑一轮（`MEMORIA_MEMORY_VECTOR_BACKFILL_INTERVAL_S`），每轮最多处理 `MEMORIA_MEMORY_VECTOR_BACKFILL_BATCH` 条（默认 200），只按 `memory_claims` 当前值重嵌，不读原话、历史 payload 或 search 行里的 `context:` 段。向量表或 embedder 未配置时不启动，日志里没有任何 ID。

巡检脚本是只读的，只做 SELECT；输出一行 JSON 计数，`missing` 大于 0 时退出码 1，无缺口 0，脚本自身错误 3（同 `disk_patrol.sh` 的约定）。`unlinked_child_claims` 是观察项，不影响退出码：它统计未撤回、源证据仍在、`memory_claim_subjects` 里没有任何关联行，且 `subject_key` 指向孩子的 claim。指向孩子的判据与编译期关联一致：`subject_key` 以 `son:`/`daughter:` 开头（不区分大小写、忽略首尾空白），或同账户 `person_entities` 里 `canonical_key = subject_key` 且 `relationship_to_owner` 为 `son`/`daughter`。现在编译时多孩全关联：一条家长说的「儿子/女儿」记忆会关联账户下所有绑定孩子，新编译的 claim 不会因此留下未关联行；升级前已经留下的未关联行不会自动回填。剩下的未关联只表示当时身份服务不可用、编译时没有绑定孩子等没关联上的情况，这类记忆在删孩子时不会被连带删除。非 0 仍只用于观察，不报警、也不改退出码；只有向量缺口 `missing` 决定 1。

~~~bash
control=memoria-control-api-1
export MEMORIA_RELEASE_TAG="$(docker exec "$control" printenv MEMORIA_RELEASE_TAG)"
export MEMORIA_RELEASE_COMMIT="$(docker exec "$control" printenv MEMORIA_RELEASE_COMMIT)"
docker compose -f docker-compose.production.yml run --rm --no-deps control-api /app/.venv/bin/python -m scripts.count_missing_memory_vectors
## {"corrected": 0, "missing": 0, "unlinked_child_claims": 2}
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
WHERE claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_claim_subjects linked
      WHERE linked.account_id = claim.account_id
        AND linked.claim_id = claim.claim_id
  )
  AND (lower(btrim(claim.subject_key)) LIKE 'son:%'
       OR lower(btrim(claim.subject_key)) LIKE 'daughter:%'
       OR EXISTS (
           SELECT 1 FROM person_entities person
           WHERE person.account_id = claim.account_id
             AND person.canonical_key = claim.subject_key
             AND lower(person.relationship_to_owner) IN ('son', 'daughter')
       ));
~~~

定时任务每轮一行日志，形如 `memory vector backfill attempted=0 embedded=0 failed=0 skipped=0 remaining=0`，`remaining` 就是上面查询的总数。确认它在跑：

~~~bash
docker compose -f docker-compose.production.yml logs control-api | grep 'memory vector backfill'
sudo sh -c "zgrep -h 'memory vector backfill' /var/log/memoria/control-api.log* | sort"
~~~

### 发布制品的手工清理（2026-10-01 首次执行，经用户授权）

规则（与 `release-rollback.md` 一致）：只保留当前发布和一个可运行的紧邻回滚，更早的普通制品核验后按授权清理。`docker_image_retention.sh` 因保护全部 `rollback-*` 与 runtime-base 而给不出候选（TODOLIST「定期运维」跟踪），所以本次是手工清单：先只读盘点并生成候选清单与它的 sha256，复核后带着这个 sha256 才能执行，执行时逐项 `docker rmi <repo:tag>`，不用 `docker system prune`，不动数据卷。

- **镜像保留集**（`memoria-{agent,control-api,speaker-model,media-edge}`）：运行中容器的 tag；紧邻回滚 = 上一版整栈（三个角色）加 control-api 组件版，以及本次发布冻结时生成的全部 `rollback-<当前 tag>-pre`；media-edge 保留运行中的和前两个组件 tag；`memoria-agent-runtime-base` 的全部 tag（其底层镜像因此也留着）；所有非 `memoria-*` 镜像。
- **目录**：`incoming/` 只留当前与上一版整栈、最近三个 media-edge；`releases/<tag>` 旧版删源码树、保留 `.cutover`（发布前 DB 备份、pre/post 状态、env 快照）；`component-releases/*/build` 可删、同目录下的 rollback/component override 与 `CUTOVER_RESULT.txt` 保留。
- **禁止删除的引用**：运行中容器的 compose 文件在 `docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <容器>` 里。2026-10-01 的引用是：Postgres 用 `releases/20260827-architecture-split-v1`，Redis 用 `releases/20260823-210222-voice-fix`，MinIO 用 `current/infra`，media-edge 用 `releases/20260930-local-stop-v2` 加 `component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml`（2026-10-02 19:05 起；此前是 `20260930-late-receipt-v1-media-edge`，旧目录留作记录，新目录里的 `media-edge-rollback.override.yml` 回到它的镜像；组件发布的 media-edge 一直挂在它当时的整栈发布树上）；回滚 control-api 组件链还要 `component-releases/20260930-vector-keyword-v1` 的两个 override。
- **结果**：根分区已用 90 → 37 GB（80% → 33%），其中镜像约 29.5 GB（`/var/lib/containerd` 43 → 14 GB）、`incoming` 20.9 GB、旧发布树 1.2 GB、组件 build 0.3 GB、`/home/ubuntu` 构建残留 1.7 GB。清理后全部容器 healthy，`/health/ready` 内外均 200，回滚镜像与 compose 链文件逐项核对存在。未动：journald（约 1.1 GB）、WMS/saas 文件、Docker 卷、`/var/backups/memoria`。

#### 2026-10-02 第二次手工清理（经用户授权「先清理磁盘」）

同一套规则，间隔 1.5 天、11 次整栈。执行方式比第一次更固化：①只读盘点（容器、镜像含完整 id、compose 引用、`incoming`、`du -x` 逐层核对磁盘去向；注意不带 `sudo` 的 `du` 会漏掉 root 目录，`incoming` 看起来 133 MB 实际 19 GB）；②保留集写成规则（当前 + 紧邻回滚 + 本次 freeze 的 `rollback-<当前>-pre` + runtime-base + media-edge 三个 tag + 非 memoria 镜像），候选由脚本按规则生成并与同 id 的保留 tag 比对（同 id 的旧 tag 只是去标签、不占空间）；③候选清单和目录清单各算 sha256，写进带门禁的脚本，脚本在服务器上先复核 sha256 再执行；④分阶段：先镜像、复核、再目录、复核。每阶段门禁：没有 `release-ops`/`docker load` 进程、七个运行容器 healthy、内部与外部 readiness、保留集镜像都在。
结果：根分区 80 → 41 GB（71% → 37%），containerd 38 → 15 GB，`incoming` 19 → 3.0 GB。增长速度实测：11 次整栈 +43 GB，约每次 4 GB（containerd 约 2.2 GB + `incoming` 约 1.5 GB），所以约每 6 次整栈清一次，或在根分区超过 70% 时清。这次没动 `releases/*` 旧源码树（1.2 GB）、`component-releases`（0.5 GB）、journald、数据卷、`/var/www/memoria-releases`。收据在服务器 `/root/memoria-release/cleanup-20261002-*.log` 与 `cleanup-20261002-pre/`。

#### 2026-10-04 第三次手工清理（经用户授权「清理 worktree 和磁盘」）

同一套规则，间隔约 1 天（10-02 23:00 → 10-04 00:41，北京时间）、4 次整栈（`20261003-endpoint-latency-v1`、`20261003-child-memory-v1`、`20261003-echo-merge-v1`、`20261004-followup-warm-v1`）。清理前根分区 61 GB（54%）、containerd 27 GB、`incoming` 11 GB、memoria 镜像 49 个 tag。保留集：`memoria-{agent,control-api,speaker-model}` 的当前 `20261004-followup-warm-v1`、紧邻回滚 `20261003-echo-merge-v1` 与本次 freeze 的 `rollback-20261004-followup-warm-v1-pre`；media-edge 运行中的 `20261002-late-progress-v1` 与前两个 `20260930-late-receipt-v1`、`20260930-edge-reject-log-v1`；`memoria-agent-runtime-base` 与 `memoria-sensevoice-asr` 的全部 tag、所有非 memoria 镜像；`incoming/` 留当前与上一版整栈目录和三个 media-edge 目录。候选 31 个镜像 tag（sha256 `8700c6b1…`）与 6 个目录（sha256 `059888a0…`）由脚本按规则生成，并断言没有一个与保留 tag 或运行容器同镜像 id。脚本 `/root/memoria-release/cleanup-20261004.sh`（sha256 `0a5f15d2…`，root 0700）比 10-02 版多两道门禁：三个整栈容器必须跑在当前 tag、`media_active_sessions` 必须为 0（不在有会话时删大文件）；其余门禁不变（没有发布进程、七个运行容器 healthy、内外 readiness、保留集镜像都在），镜像与目录分两阶段，每阶段前后各过一遍。

结果：根分区 61 → 43 GB（54% → 38%），containerd 27 → 15 GB，`incoming` 11 → 3.0 GB，memoria 镜像 49 → 18 个 tag。清理后带健康检查的容器都是 healthy，内外 readiness 200；运行容器引用的 compose 文件（整栈三个容器 `releases/20261004-followup-warm-v1`，media-edge `releases/20260930-local-stop-v2` 加 `component-releases/20261002-late-progress-v1-media-edge` 的 override，Postgres `releases/20260827-architecture-split-v1`，Redis `releases/20260823-210222-voice-fix`，MinIO `current/infra`）逐项核对存在。增长速度：从上次清理后的 41 GB 到 61 GB，4 次整栈约 +20 GB（每次约 5 GB：containerd 约 3 GB、`incoming` 约 2 GB），所以约每 5–6 次整栈清一次，或在根分区超过 70% 时清。这次同样没动 `releases/*` 旧源码树、`component-releases`、journald、数据卷、`/var/www/memoria-releases`。收据在服务器 `/root/memoria-release/cleanup-20261004-*.log` 与 `cleanup-20261004-pre/`（`docker ps`、镜像清单含完整 id、卷、`incoming` 列表、`df`、两份候选清单）。

#### 2026-10-04 本机（Mac 构建机）Docker 清理（经用户授权「清理docker」）

发现于补跑带数据库的本地测试：Docker Desktop 虚拟机的数据盘（`/dev/vda1`，79 GB）写满，`memoria-pgv` 报 `No space left on device`；发布构建在本机做（`docker compose build`、`docker save`），写满会让下一次发布失败。检查：`docker exec memoria-pgv df -h /var/lib/postgresql/data`，或 `docker run --rm --privileged --pid=host --entrypoint nsenter <任一本地镜像> -t 1 -m -u -n -i sh -c 'df -h /var/lib/docker; du -xsh /var/lib/docker/*'` 看虚拟机里 `overlay2`（镜像层与构建缓存）与 `volumes` 各占多少。

做法：①`memoria-{agent,control-api,speaker-model}` 只留最新两个 tag、`memoria-media-edge` 只留当前与上一个组件 tag，其余逐项 `docker rmi`（本次 80 个 tag）；②**只删镜像不会让虚拟机磁盘变小**，这些层还被构建缓存引用，要再 `docker builder prune -af --keep-storage 8GB`（保留最近用过的约 8 GB，本次回收 26.6 GB）。结果：虚拟机磁盘空出约 28 GB（可用约 24 GB），`memoria-pgv` 自己从崩溃恢复。不动其他项目的镜像、已停止的容器与数据卷（`docker volume prune` 会删别的项目的数据，须单独授权），不用 `docker system prune`；`delta_build_images.sh` 要的底座镜像（最新发布 tag）保留。之后第一次本机发布构建会因缓存变小而慢一些。

#### 2026-10-08 第四次手工清理（经用户授权「先清理磁盘，删除没用的备份数据等等」）

同一套规则，间隔约 4 天，在整栈 `20261007-n8-diag-v1` 切流之后做。清理前根分区 28 G / 40 G（75 %），containerd 镜像层约 12 G，`incoming/` 4.3 G，`/var/backups/memoria-july-archive-20261005` 1.6 G。步骤：①只读盘点（`du -x`；root 目录的通配符要在 root 下展开，`sudo sh -c 'du … /dir/*'`，否则非 root 的 shell 展不开，输出是空的）；②保留集与运行中容器的 compose 引用核对（`com.docker.compose.project.config_files` 标签：Postgres 与 MinIO 用 `releases/20260827-architecture-split-v1`，Redis 与 media-edge 用 `releases/20261004-first-warm-v1`，media-edge 另加 `component-releases/20261002-late-progress-v1-media-edge` 的 override）；③候选的逐文件 sha256 清单写进 `/root/memoria-release/cleanup-20261008-pre/`；④逐项执行，过程写进 `/root/memoria-release/cleanup-20261008.log`。

删除的：镜像 tag 12 个（`memoria-{agent,control-api,speaker-model}` 的 `20261004-first-warm-v1`、`20261005-subject-candidates-v1`，以及两个过期的 `rollback-20261005-…-pre`、`rollback-20261006-…-pre`；都是 `docker rmi <repo:tag>`，不是 prune）；`incoming/20261005-subject-candidates-v1`（1.5 G）；`releases/20261005-subject-candidates-v1` 的源码树（保留 `.cutover`，4.3 M）；`/var/backups/memoria-july-archive-20261005`（1.6 G：7 月候选部署的 volumes 归档、旧 env 文件（含密钥）与旧发布树；它自己的 README 写着 nothing in use，逐文件 sha256 清单 14,559 条）。

有意保留：`/root/old-host-20261005/`（99 M，其中 `var-backups-memoria.tar.gz` 是旧机 `/var/backups/memoria` 的唯一本地还原点，见上文「2026-10-05」一节）；`/var/backups/memoria`（11 M，未动）；`releases/20261004-first-warm-v1` 与 `releases/20260827-architecture-split-v1`（compose 链）；`incoming/20261006-speaking-flush-v1` 与 `incoming/20261007-n8-diag-v1`（上一版整栈与当前）；`/home/ubuntu` 下不属于 Memoria 的 family-growth-h5、prepulse（归属未核实，未动）；数据卷（未 prune）；journald（约 1 G，未动）。

结果：根分区 28 G → 21 G（75 % → 55 %，剩 18 G）。容器全部 healthy，本机与外部 readiness 200，`current` 不变，20261006 回滚镜像的 image id 与之前一致。`docker system df` 仍显示约 4.2 G 可回收，那是 20261006 整栈与 `rollback-20261007-*-pre`（同 id），按规则不删。


#### 2026-10-09 第五次手工清理（经用户授权「先做2，3吧」）

同一套规则，间隔约 1 天，在整栈 `20261009-textless-spare-v1` 切流之后做。清理前根分区 25 G / 40 G（65 %），`incoming/` 三个包各 1.5 G。回滚深度是 1，所以 20261006 整栈已不是回滚目标。执行方式比 10-08 又固化了一步：脚本 `/root/memoria-release/cleanup-20261009.sh`（root 0700，sha256 `66b56d94…`）分 `pre`（只读盘点、写快照与逐文件 sha256 清单，不改任何东西）与 `apply` 两个模式，候选是写死的清单，不用通配符；`apply` 前后各过一遍门禁：没有发布或 docker 进程、七个带健康检查的容器 healthy 加 SenseVoice 在跑、三个整栈角色在当前 tag 上、`current` 指向当前发布树、本机 readiness 200、保留集镜像都在、近 5 分钟 bridge 日志没有 `media turn committed`、近 10 分钟边缘日志没有 `device=dev_`；候选镜像还要断言与任何保留 tag 或容器用的镜像 id 不同。

删除的：6 个镜像 tag，对应 3 个镜像（`memoria-{agent,control-api,speaker-model}` 的 `20261006-speaking-flush-v1` 与同 id 的过期 `rollback-20261007-n8-diag-v1-pre`；逐个 `docker rmi <repo:tag>`，不是 prune）；`incoming/20261006-speaking-flush-v1`（1.5 G，6 个文件的 sha256 清单）；`releases/20261006-speaking-flush-v1` 的源码树（1,685 个文件的 sha256 清单，保留 `.cutover`，71 M → 4.3 M）。

有意保留：`incoming/20261007-n8-diag-v1`（下一次增量上传的种子）与 `incoming/20261009-textless-spare-v1`（当前）；`releases/20261007-n8-diag-v1`（回滚目标的发布树）与当前发布树；镜像 `20261009`、`20261007` 与 `rollback-20261009-textless-spare-v1-pre`、media-edge、SenseVoice；`releases/20261004-first-warm-v1` 与 `releases/20260827-architecture-split-v1`（compose 链）；数据卷、journald、`/root/old-host-20261005/` 都没动。

结果：根分区 25 G → 21 G（65 % → 55 %，剩 18 G），`docker system df` 的镜像数 19 → 16。清理前后门禁全部通过，四个关键容器 healthy，本机与外部 readiness 200，`current` 不变。收据在服务器 `/root/memoria-release/cleanup-20261009.log`、`cleanup-20261009-precheck.log`、`cleanup-20261009-apply.out` 与 `cleanup-20261009-pre/`（前后快照、两份 sha256 清单、候选镜像与 id）。

#### 2026-10-10 第六次手工清理（经用户授权「清理20261007 的 incoming 包、镜像和发布树」）

同一套规则，间隔约 1 天，在整栈 `20261010-stale-interim-v1` 切流之后做。清理前根分区 25 G / 40 G（65 %），`incoming/` 三个包各 1.5 G。回滚深度是 1：当前栈 20261010，回滚目标 20261009，所以 20261007 既不在线、也不是回滚目标。

只读盘点先行：所有容器（含已停止的）的 compose 标签都不引用 `releases/20261007-n8-diag-v1`；`incoming/20261007-n8-diag-v1` 的六个文件硬链接数都是 1（没有和别的目录共享 inode）；六个候选镜像标签（`memoria-{agent,control-api,speaker-model}` 的 `20261007-n8-diag-v1`，以及同 id 的 `rollback-20261009-textless-spare-v1-pre`，那是 20261009 冻结时从 20261007 打的标签）没有任何容器在用；保留集的镜像都在。脚本 `/root/memoria-release/cleanup-20261010.sh`（root 0700）分 `pre`（只读：快照、两份 sha256 清单、候选镜像与 id）与 `apply`。`apply` 前后各过一遍门禁：没有发布或 docker 进程；七个带健康检查的容器与 SenseVoice 在跑；三个整栈角色都在当前 tag 上；`current` 指向当前发布树；本机 readiness 200；保留集镜像都在；近 5 分钟没有 `media turn committed`，近 10 分钟边缘没有 `device=dev_`。删 incoming 之前重新校验它的 sha256。

删除的：6 个镜像标签（逐个 `docker rmi <repo:tag>`，不是 prune；同 id 的两个标签都删掉后镜像层才释放）；`incoming/20261007-n8-diag-v1`（1.5 G）；`releases/20261007-n8-diag-v1` 的源码树（保留 `.cutover`，4.4 M：pre-cutover 数据库转储、pre/post 状态、env 快照）。

有意保留：`releases/20261007-n8-diag-v1/.cutover`（收据）；`incoming/20261009-textless-spare-v1`（回滚目标的增量种子）与 `incoming/20261010-stale-interim-v1`（当前）；镜像 `20261010`、`20261009` 与 `rollback-20261010-stale-interim-v1-pre`（= 20261009 镜像）、media-edge、SenseVoice；`releases/20261009-textless-spare-v1`（回滚目标的 compose 链）、`releases/20261004-first-warm-v1`、`releases/20260827-architecture-split-v1`；数据卷、journald、`/root/old-host-20261005/`。本机（Mac）上 `~/Library/Caches/memoria-release-build/20261007-n8-diag-v1/`（64 M）与本机 Docker 里的 20261007 镜像没有动。

结果：根分区 25 G → 21 G（65 % → 55 %，剩 18 G）；memoria 镜像标签 17 → 11 个；`docker system df` 的镜像可回收量为 4.2 G。清理后七个健康检查容器全部 healthy、SenseVoice 在跑、重启次数不变，本机与外部 readiness 200，`current` 不变，回滚目标 20261009 的三个镜像 id 与 compose 文件都在。`apply` 的最后一行 `docker system df | head -2` 因 SIGPIPE 退出 141，发生在所有删除与校验之后，不影响结果。收据在服务器 `/root/memoria-release/cleanup-20261010.log`、`cleanup-20261010-precheck.log` 与 `cleanup-20261010-pre/`。

#### 2026-10-19 第七次手工清理（经用户授权「允许磁盘清理」）

同一套规则，距上次约 9 天，期间整栈发布 20 余次，根分区 29 G / 40 G（76 %，过了 75 % 告警线）。脚本 `/root/memoria-release/cleanup-20261019.sh`（root 0700）分 `pre`（只读）与 `apply`，候选写死，不用通配符、不 prune。`apply` 前后各过一遍门禁：没有发布或 docker 进程；六个带健康检查的容器 healthy 且 SenseVoice 在跑；bridge 与 control-api 在当前 tag；`current` 指向当前发布树；保留集镜像都在；本机与外部 readiness 通过。此外 `refs_check` 核对候选树不被任何容器的 compose 标签引用、候选镜像不被孤儿容器之外的容器使用。

删除的：
- 孤儿容器 `memoria-speaker-model-1`（speaker-model 10-13 退役后一直 exited，compose 每次都提示 orphan）和它的镜像 `memoria-speaker-model:20261013-guardian-summaries-v1`（627 M）。
- 镜像标签 `memoria-{agent,control-api}:20261016-device-registration-v1`（这版 22:28 切流后 22:30 已回滚，镜像 id 与回滚点不同，逐个 `docker rmi`）。
- `incoming/` 三个发布包各 1.2 G：`20261016-device-registration-v1`、`20261015-stop-standby-v1`（已回滚、对应 PR #270 已关闭）、`20261015-daily-chat-limit-v1`。
- 22 棵旧发布树的源码，保留各自 `.cutover`（收据）；`20261014-support-tiers-v1`（verify-load 盘满中止的那版）本来就没有 `.cutover`。删除前核对：其中 21 棵的 tag 提交已在 `origin/main`；`20261015-stop-standby-v1`（提交 `0228dfdf`）不在 main，已存进本机 bundle，删后可恢复。

有意保留：当前发布树 `20261016-readiness-b54189c`；回滚目标 `20261016-session-cap-fallback-v1`（`.cutover/prev-release` 指向它）和手工一步回退点 `20261016-log-redaction-v1`，两者的镜像、发布树、`incoming/` 包都在；compose 链 `releases/20260827-architecture-split-v1`、`releases/20261004-first-warm-v1` 与 `component-releases/20261002-late-progress-v1-media-edge`；media-edge、SenseVoice、redis、minio、pgvector 镜像；数据卷、journald、`/var/log`。

结果：根分区 29 G → 21 G（76 % → 56 %，剩 17 G）；容器全部 healthy，外部 readiness 通过。收据在服务器 `/root/memoria-release/cleanup-20261019.log` 与 `cleanup-20261019-pre/`（root 0700，含三个 `incoming` 包的 sha256 与前后快照）。**下一次整栈发布**：当前版没有 `incoming/` 包可作增量种子，会走整包上传（约 1.5 GB，几分钟）。

#### 2026-10-05 迁到 110.42.235.198 后的空间基线（新机，不是一次清理）

生产从 2026-10-05 起在 110.42.235.198：40 GB 盘（旧机 118 GB），与 pocketSparks、hr-tracker 共用，迁移后已用约 17 GB（45%）。上文的 70% 触发线与「约每 5–6 次整栈清一次」是旧机的数字，在新机上要重算：整栈发布每次约 +4–5 GB（WAL 归档曾约 +0.6–1.1 GB/天，2026-10-06 起已关，见 [发布、恢复与回滚运维手册](runbooks.md)「生产主机」），70% 是 28 GB，迁移后 17 GB、第一次整栈发布后 21 GB；`memoria-disk-patrol.timer` 的 75% / 85% 是百分比，同样适用，只告警不清理。规则不变：只清 `memoria-*` 镜像（`docker_image_retention.sh` 只处理这类 tag，不碰 pocketSparks、hr-tracker 和其他项目的镜像与卷）；每次发布核对完立刻删 `incoming` 里的包；镜像只留当前 + 一个回滚；数据卷仍不动，清理仍须另行授权。

---

## 来源：`docs/runbooks/release-rollback.md`

## 发布、恢复与回滚运维手册

本文是从 `HANDOFF.md` 迁出的永久运维参考，保留原有发布、数据恢复和回滚底线。命令只表示操作基线，不表示本轮已经执行；生产执行、切流、恢复和固件操作仍需单独授权。

### 百炼实时检索与 Omni 开关

`LIVE_SEARCH_QWEN_ENABLED` 与 `QWEN_OMNI_SESSION_ENABLED` 默认都是 `false`。部署之后，除非环境里把它们写成 `true`，通义实时检索不再安装、不再出站，Qwen Omni 会话创建和 SDP 交换直接拒绝。`DASHSCOPE_API_KEY` 必须留下，agent 的 FunASR 仍读它。control-api 的 `ASR_PROVIDER` 默认 `none`，这时 readiness 不因为缺少这把密钥而 503。只有 `ASR_PROVIDER=funasr` 时 control-api 才把它算进缺失项。

天气在实时检索关闭且未配和风天气数据源时，第一轮和孩子接着报城市的第二轮都回固定句 weather-fallback-v1，不调用模型，也不为这句合成音频。

回滚这两条路径：把 `LIVE_SEARCH_QWEN_ENABLED=true` 和 `QWEN_OMNI_SESSION_ENABLED=true` 写回环境后重建对应进程。不要删 `DASHSCOPE_API_KEY`。只有在希望 control-api readiness 继续要求这把密钥时才设 `ASR_PROVIDER=funasr`。

### 生产拓扑与安全边界

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

### 生产主机（2026-10-05 起：110.42.235.198）

整套服务 2026-10-05 01:33–01:40（CST）从旧机 122.51.108.140 冷拷贝迁到 110.42.235.198：与 pocketSparks 的 MySQL、node、hr-tracker 共用的主机（Ubuntu 24.04，4 vCPU，3.7 GiB 内存 + 2 GiB swap，40 GB 盘）。本机 ssh 别名 `memoria-prod` 指新机，`memoria-prod-old` 指旧机，本文命令里的 `memoria-prod` 都是新机。迁移回执与回到旧机的路径见 `HANDOFF.md`「2026-10-05 服务迁移到 110.42.235.198」；对外域名从 `aigcnice.com` 换成 `aginice.cn`（2026-10-05）见同文件「2026-10-05 域名切换到 aginice.cn」。

- **共用主机的边界**：hr-tracker 占着 `127.0.0.1:8791`，共用 nginx 拥有 80/443/8443，pocketSparks 的站点、卷不动；`sites-enabled/aginice`（aginice.cn / www，Memoria 现在的对外域名，与 pocketSparks 共用）只加了两行 include（`snippets/memoria-prod-https.conf`、`snippets/memoria-prod-bind.conf`，并把 `/` 的静态根指到 `/var/www/memoria-prod`），原件在 `/etc/nginx/backup-20261005-domain-switch/aginice.orig`。Memoria 的 nginx 文件一律用新名字：`sites-enabled/memoria-prod`（旧域名 aigcnice.com，过渡期保留）、`snippets/memoria-prod-{device-edge,https,site-common,bind}.conf`、`conf.d/memoria-prod-limits.conf`，静态页 `/var/www/memoria-prod`（只有绑定页，没有 WMS）；证书：aginice.cn 用 `/etc/nginx/ssl/aginice.cn/aginice.cn_bundle.crt` + `aginice.cn.key`（腾讯云免费 DV，约 90 天一期、不自动到服务器；该站点的 `ssl_ciphers` 只有 RSA 套件，新证书必须是 RSA、不能是 ECDSA）；aigcnice.com 的旧证书 `/etc/nginx/ssl/aigcnice.com/…`（到 2026-12-17）只服务过渡期。Let's Encrypt 的 HTTP-01 对 aginice.cn 走不通（境外验证节点访问 80 端口被腾讯云的备案提示页拦截，同机房境内访问正常），证书只能从腾讯云控制台下载，或用 DNSPod API 做 DNS-01。8443 的 stream 仍是 7 月留下的 `stream-conf.d/memoria-rtc.conf`（`ssl_preread`：TLS 到 `127.0.0.1:9443`，非 TLS 的一支指向已退役的 LiveKit 8444，没有监听者），没改。
- **换 aginice.cn 证书**（现用的一份 2026-10-05 09:08 装入，2027-01-02 到期）：证书只能由用户在腾讯云「SSL 证书」控制台下载（Nginx 格式、RSA，含 `aginice.cn` 与 `www.aginice.cn`）。步骤：本机只读预检（SAN、未过期且 ≥ 30 天、RSA 密钥与证书匹配、链在系统信任库里验证通过，不打印私钥）→ scp 到服务器 root 0700 的临时目录 → 以**文件**运行安装脚本（先备份旧对到 `/root/domain-switch-20261005/`，同样的校验，沿用原属主与权限，`nginx -t` 失败则还原并不 reload，reload 后用 SNI `aginice.cn` 核对 443 与 8443 的序列号与 `Verify return code: 0 (ok)`，再核对 `aigcnice.com` 站点与 hr-tracker 不受影响）→ `shred -u` 暂存的私钥。脚本在服务器 `/root/domain-switch-20261005/scripts/`（`cert_install.sh`、`verify_domain.sh`；本机副本 `outputs/ops-domain-switch-20261005/`，被 git 忽略，没有入仓库）；一次实跑的收据见 `HANDOFF.md`「2026-10-05 域名切换到 aginice.cn」。
- **Control API 端口是 `127.0.0.1:18791`，由 `MEMORIA_CONTROL_API_PORT` 带着走**（2026-10-05 的端口参数化改动）：compose 写 `127.0.0.1:${MEMORIA_CONTROL_API_PORT:-8791}:8000`；`scripts/release_ops.sh` 的 `control_api_port()` 依次取该环境变量、在跑的 `memoria-control-api-1` 的 `docker port … 8000/tcp`（只认 `127.0.0.1` 的绑定）、默认 8791，`verify-load` 把它写进新发布树的 `.env`（Compose 从项目目录读 `.env`），`cutover` 先断言候选 compose 发布的正是线上这个端口，不一致就拒绝、线上不动（新发布树若落回 8791 会与 hr-tracker 冲突，control-api 起不来、切流中途中断），`finish` 与 `scripts/refresh_readiness.sh` 也探这个端口。`verify-load` 的空闲端口预检默认 `28791` / `28891`（`MEMORIA_PREFLIGHT_API_PORT` / `MEMORIA_PREFLIGHT_NGINX_PORT` 可改），避开线上的 18791（旧的固定值 18791 / 18891 会让预检在新机上直接拒绝）。2026-10-05 16:31 部署的 `20261005-subject-candidates-v1` 树采用仓库原样加 `.env`（`MEMORIA_RELEASE_COMMIT`、`MEMORIA_RELEASE_TAG`、`MEMORIA_CONTROL_API_PORT=18791`）；当时的回滚树 `20261004-first-warm-v1` 里 compose、`refresh_readiness.sh`、`release_ops.sh` 三处仍手工改成 18791（原件留作 `.orig`），属于参数化前的历史做法。
- **release-ops.sh 与 PREV（脚本不再写 PREV，发布之间不用改它）**：`freeze` 从线上取证推出上一栈——`current` 软链指向的发布树、各容器的 compose 链标签、镜像的 OCI version / revision——写入 `$S/prev-release`（`tag=`、`commit=`、`services=` 三行，有覆盖时再加第 4 行 `overrides=`）。链只接受两种形态：基础 compose 本身，或基础 compose 加恰好一份白名单内的只换镜像覆盖。2026-10-21 起线上就是后者：control-api 是 `releases/20261020-minor-bypass-a94c438/docker-compose.production.yml` + `component-releases/20261021-a94c438-pr345-pr346-pr348-pr352/override-control-pr352.yml`（sha256 `95a5e033…`，`services.control-api.image`），bridge 是同一基础 compose + `component-releases/20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355/override-pr355.yml`（sha256 `895b17fe…`，`services.voice-core-media-bridge.image`），两份的 `/tmp` 原件分别是 `/tmp/override-control-pr352.yml`、`/tmp/override-pr355.yml`。覆盖必须只有 `services.<该服务>.image` 一个键、不能带 environment/volumes/ports 等任何其他键或别的服务，image 必须等于该容器实际运行的镜像；`/tmp` 路径只有在持久拷贝存在且两者 sha256 都等于白名单登记值时才接受，记录一律写持久路径。白名单不再放在脚本里，而是主机状态文件 `/opt/memoria/state/component-overrides.tsv`（每行 4 个空格分隔的列：服务、持久路径、sha256、`/tmp` 原件；0600、root 属主，权限或属主不对即整体拒绝读）。切换 release-ops.sh 版本后跑一次 `release-ops.sh migrate-component-overrides` 把历史行播种进状态文件（状态文件已存在且内容不同时拒绝执行，防的是顺手覆盖手工登记）；新的组件切换后用 `release-ops.sh record-override <服务> /opt/memoria/component-releases/<tag>/override-…yml /tmp/override-…yml` 登记：它要求持久文件与 `/tmp` 原件逐字节一致、覆盖只含 `services.<该服务>.image` 且 image 等于该容器实际运行的镜像，校验通过才以 flock 加临时文件原子追加（固定写 0600）；同一行 sha 不变的重复执行无事，sha 变了直接拒绝。状态文件缺行或有坏行时 `freeze` 以 `unexpected live chain`、`not whitelisted` 或 `malformed state file line` 拒绝，线上不动。有覆盖时 `prev-release` 追加第 4 行 `overrides=`，每个服务一组四个以空格分隔的字段 `服务名 持久覆盖路径 sha256 镜像ID`，不同服务的组依次相接；没有覆盖时仍只有三行，旧记录（无 `overrides=`）照常可用。覆盖服务的 OCI version/revision 是组件自己的标签，不参与 `tag=` 比对；当两个 ship 服务都被覆盖时，`commit=` 取 `current` 发布树 `.env` 的 `MEMORIA_RELEASE_COMMIT`，没有该文件时要求所有覆盖镜像的 revision 标签一致，否则拒绝。重跑 `freeze` 必须推出同一份记录，否则拒绝改写（切流之后再跑会因 control-api 已在新链上而被拒）；`cutover`、`finish`、`rollback` 只读这份记录，没有它就拒绝执行。唯一的旧例外是给绕过发布脚本上线的 `20261016-readiness-b54189c` 留的窄口子：镜像 version 为空、revision 是 40 位十六进制、tag 以 `-<revision 前 7 位>` 结尾且容器环境 `MEMORIA_RELEASE_TAG` 等于 tag 时，`freeze` 向 stderr 记一行 `accepting empty version label…` 后按通过处理，其余 version 不一致仍拒绝。**运维在 `env` 之前把这一行与 HANDOFF 记录的现网栈对一遍，不一致就停。** `rollback` 在动 env、`current` 或容器之前重算每个持久覆盖的 sha256，文件缺失或不符即拒绝，线上不动；随后为每个有覆盖的服务单独执行一条 compose（`-f 基础compose -f 该服务的持久覆盖`），避免把两份 `-f` 一起盖到每个容器上。重建并健康检查后，逐服务核对容器镜像 ID 等于记录值，不符即失败。主机 `/root/memoria-release/release-ops.sh` 目前是 2026-10-20 10:23 安装的版本（SHA-256 `3d39394d15069a30c868397bc0abbb63c9ecc8a93ac324cd28ed5de9919ade25`，与本次改动前的仓库版一致），它不认识 2026-10-21 的覆盖链。**下一次整栈发布前安装合并后的仓库版并记下它的 SHA-256**；那次 `freeze` 的 PREV 是 `20261020-minor-bypass-a94c438@a94c43813a8893a07bbe3bf4815cae52745cd88a`，`services=control-api voice-core-media-bridge`，并带 `overrides=` 行。
- **在新机上发整栈（2026-10-05 `20261005-subject-candidates-v1` 实跑的做法）**：登录用户是 `ubuntu`（免密 sudo），`/root/memoria-release` 在 root 下，所以每一步都包一层 `ssh memoria-prod "sudo sh -c 'env TAG=… COMMIT=… [VERIFIER_SHA=… MANIFEST_SHA=…] /root/memoria-release/release-ops.sh <step> > /root/memoria-release/<step>-<tag>.log 2>&1; echo exit=$? >> /root/memoria-release/<step>-<tag>.log'"`，再读日志里的 PASS；上传用 `scripts/upload_release_artifacts.sh`（它用 `sudo -n` 与 `rsync --rsync-path='sudo -n rsync'`，所以 `ubuntu` 能用；本机 `PATH=/opt/homebrew/bin:$PATH` 以用到新版 rsync），先 dry-run 再真传；增量（seeded）模式要求新机上有上一个 tag 的 `incoming/<tag>/images.tar`，没有就是整包模式（第一次是整包，线上发送 502 MB / 总 1.53 GB）；本机构建用 `DOCKER_DEFAULT_PLATFORM=linux/amd64 COMPOSE_PARALLEL_LIMIT=1`，这次还设了 `UV_DEFAULT_INDEX=https://mirrors.cloud.tencent.com/pypi/simple`；`verify-load`、`freeze` 不动线上，可以在合并 PR 之前跑（它们会把 `MEMORIA_CONTROL_API_PORT` 写进新发布树的 `.env`、在 28791 / 28891 起预检栈、做发布前 DB 备份），任何需要的修正还能进同一个 PR；`cutover` 前先确认没有设备会话；边缘日志里近期没有 `device=dev_` 不能证明没有空闲的设备 WSS（稳态连接不打日志），2026-10-09 的切流就断开了一条（边缘记 `device WSS socket error … use of closed network connection`），机器人下一次唤醒会重连。**恢复一次半截的发布时**，先只读核对主机：`docker images | grep <tag>`（`verify-load` 遇到已存在的同名 tag 镜像会拒绝）、`incoming/<tag>/` 里是否只有六个制品、`current` 与容器没动；残留的无标签镜像按 tag 删掉；镜像一律用显式 `docker build --platform linux/amd64` 重建并核对 revision / version / role 标签（见 `HANDOFF.md`「2026-10-09 整栈发布」）；`upload_release_artifacts.sh` 遇到已有的 `images.tar` 会走 `resume` 模式，结束时仍会逐个制品核对 sha256。
- **数据层**：两台机上 `memoria-data` 项目都是从 `releases/20260827-architecture-split-v1/infra` 起的。`current/infra` 里的同名 compose 多一个 `011-control-schema.sql` 的 initdb 挂载（只在空库首次初始化时用）；在已有数据的库上用 `current/infra` 去 `up -d` 会让 compose 判定配置变了而重建 postgres 容器（推断，没试），不要顺手做。
- **WAL 归档已关（P1-08，2026-10-06）**：`infra/memoria-data.production.yml` 的 postgres 参数是 `archive_mode=off`，不再有 `archive_timeout` / `archive_command`。原先每 5 分钟强制切一个 16 MB 段并拷进同盘的 `memoria-data_postgres_wal_archive` 卷：没有 base backup 可接，没有恢复价值，旧机实测每天涨约 0.6–1.1 GB。恢复靠每次整栈发布前 `freeze` 的 `memoria-pre-<tag>.dump`（见「数据层、备份与恢复」）。改这个参数必须重建 postgres 容器（`archive_mode` 要重启，`ALTER SYSTEM` 改不了：compose 的 `-c` 参数优先）；线上 compose 与仓库的同步和卷的清理是维护窗口里的一步，结果记在 HANDOFF。要重新启用（例如接异地备份 profile）：在 compose 里把 `archive_mode=on`、`archive_timeout=300s`、`archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f` 一并恢复，并保持该卷 `999:999 0700`，否则 `archive_command` 一直失败；同时要有 base backup 和按它裁剪的策略。
- **磁盘（最近一次只读核验：2026-10-10 23:31）**：40GB盘、已用25GB（65%）、剩14GB；09:40既有清理删掉20261007旧镜像tag/incoming/源码树、保留`.cutover`。`incoming/`保留`20261009-textless-spare-v1`、`20261010-stale-interim-v1`和`20261010-n8-endpoint-diag-v1`三个目录，镜像保留当前与一个可运行回滚；详细收据见`docs/acceptance.md`。WAL归档关掉后主要增长是每次发布约4–5GB（镜像+包）与库本身；不要手删`pg_wal`。`memoria-disk-patrol.timer`只在75%/85%告警、不自动清理，生产清理另授权。
- **systemd**：`memoria-readiness-refresh.timer`（12 小时一次提供方冒烟 + readiness 标记；迁移后 01:36:57 跑过一次，从新机出口 PASS）与 `memoria-disk-patrol.timer` 已在新机启用。`memoria-freeze-dump-cleanup.timer`（每日）应在下一次整栈发布时一并安装：只删 `/opt/memoria/releases/*/.cutover/` 下超过 3 天的 `memoria-pre-*.dump` 与 `memoria.pre-*.sqlite3`。`memoria-log-rotate.timer`（每小时轮转 `/var/log/memoria`）与 rsyslog drop-in `/etc/rsyslog.d/30-memoria.conf` 在第一次带 `logging:` 块的整栈发布里、`env` 之前安装，见「容器日志落盘」。`memoria-nightly-backup.timer`（每日 03:15）备份 PostgreSQL 与 SQLite 控制库到 `/var/lib/memoria/backups/nightly/`，只保留最近 7 天（详见下方「夜间数据库备份」）。

### 夜间数据库备份

#### 夜间备份决策（2026-10-19 用户批准）

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

#### 夜间备份安装与运维

每日 03:15 CST 自动备份两个库到 `/var/lib/memoria/backups/nightly/`：
- PostgreSQL：`memoria-<YYYYMMDD>-<HHMM>.dump`（pg_dump 自定义格式，约 4.4 MB）
- SQLite 控制库：`control-<YYYYMMDD>-<HHMM>.sqlite3`（约 1-2 MB）

保留策略：最近 7 天全保，满 7 天（含周日）全删。唯一例外是最新一份 dump 与最新一份 control 永远保留，即使已超过 7 天。总容量约 38-45 MB，按 7 份 dump（约 4.4 MB/份）+ 7 份 control（约 1-2 MB/份）估算。

**安装（一次性，需授权）：**

```bash
## 1. 在生产主机上创建备份目录
sudo install -d -m 0700 /var/lib/memoria/backups/nightly

## 2. 从仓库复制脚本
sudo install -d -m 0755 /opt/memoria/ops-tools
sudo install -m 0700 scripts/nightly_backup.sh scripts/nightly_backup_retention.py /opt/memoria/ops-tools/

## 3. 安装 systemd 单元
sudo cp infra/memoria-nightly-backup.service /etc/systemd/system/
sudo cp infra/memoria-nightly-backup.timer /etc/systemd/system/
sudo cp infra/memoria-backup-freshness.service /etc/systemd/system/
sudo cp infra/memoria-backup-freshness.timer /etc/systemd/system/

## 4. 启用与启动定时任务
sudo systemctl daemon-reload
sudo systemctl enable memoria-nightly-backup.timer
sudo systemctl start memoria-nightly-backup.timer
sudo systemctl enable --now memoria-backup-freshness.timer

## 5. 验证
sudo systemctl status memoria-nightly-backup.timer
sudo systemctl list-timers memoria-nightly-backup.timer
```

**监控与故障排查：**

```bash
## 查看最近备份运行状态
sudo systemctl status memoria-nightly-backup.service

## 查看完整日志
sudo journalctl -u memoria-nightly-backup.service -n 100

## 手动触发一次备份（测试）
sudo /opt/memoria/ops-tools/nightly_backup.sh

## 查看备份文件
ls -lh /var/lib/memoria/backups/nightly/

## 列出将要被删除的备份（dry-run）
python3 /opt/memoria/ops-tools/nightly_backup_retention.py --backup-dir /var/lib/memoria/backups/nightly/

## 手动检查备份新鲜度（全部新鲜退出 0，过期或缺失退出 2）
sudo python3 /opt/memoria/ops-tools/nightly_backup_retention.py --backup-dir /var/lib/memoria/backups/nightly --check-freshness

## 查看新鲜度检查状态与日志
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

#### 从备份恢复

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

## 2. 导出线上账本（只读；stdout 只有 {"exported": N}）
docker compose -f docker-compose.production.yml --profile media-runtime run --rm --no-deps \
  --user 0:0 -v "$SNAPSHOT_DIR:/snapshot" \
  control-api /app/.venv/bin/python -m scripts.replay_subject_deletions \
  --export-ledger /snapshot/subject-deletions.json

## 3. 恢复 PostgreSQL 与（如使用）SQLite 控制库（按授权执行；恢复前停 control-api）

## 4. 合并快照并 replay（stdout 只有计数；incomplete 非零时退出码 1）
docker compose -f docker-compose.production.yml --profile media-runtime run --rm --no-deps \
  -v "$SNAPSHOT_DIR:/snapshot:ro" \
  control-api /app/.venv/bin/python -m scripts.replay_subject_deletions \
  --ledger-snapshot /snapshot/subject-deletions.json --confirm-replay

## 6. 删快照
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
## 选择一份备份（备份目录是 root 0700，重定向必须在 root shell 里做，不能写在 sudo 外面）
BACKUP=/var/lib/memoria/backups/nightly/memoria-20261019-0315.dump
q() { docker exec memoria-data-postgres-1 psql -U memoria_admin -d "$1" -Atc "$2"; }

## 恢复到一个临时库，不碰生产库 memoria（库里没有 postgres 角色，用 memoria_admin）
q postgres "create database test_restore owner memoria_admin"
docker exec -i memoria-data-postgres-1 pg_restore -U memoria_admin -d test_restore --no-owner < "$BACKUP"
echo "pg_restore exit=$?"

## 对照表数（2026-10-19 实测：恢复 168 张，线上 168 张）
q test_restore "select count(*) from information_schema.tables where table_schema = 'public'"
q memoria      "select count(*) from information_schema.tables where table_schema = 'public'"

## 清理临时库
q postgres "drop database test_restore"
RESTORE
```

**恢复 SQLite 控制库到临时文件：**

```bash
## 选择一份备份
BACKUP=/var/lib/memoria/backups/nightly/control-20261019-0315.sqlite3

## 只读打开，做完整性检查并列出各表行数（主机没有 sqlite3 命令行，用 python3）
sudo python3 - "$BACKUP" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for (name,) in c.execute("select name from sqlite_master where type = 'table' order by 1"):
    print(name, c.execute(f'select count(*) from "{name}"').fetchone()[0])
PYEOF
```

### 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. **手动补一条 tag 名下的 CI 收据**。ci.yml 对 main 不再跑 push 触发，PR run 都挂在分支头，main 收下合并没有自己的 run（2026-10-23 发现 99b8d32a 名下无收据）。发布的目标提交若是 merge commit（不在任何分支），先 `git push origin <sha>:refs/heads/deploy/<tag>-ci-receipt` 再 `gh workflow run ci --ref <该分支>`（workflow_dispatch 的 `--ref` 不收裸 sha，HTTP 422）。收据是 run URL；分支留着（deploy/* 前缀约定）。
3. 在候选镜像跑 Agent 发布门（`scripts.verify_agent_release_artifact`：bridge 与提供方适配器不导入 livekit、自有媒体遥测隐私 canary、DTLN 去噪器可加载）；镜像解析显式给 expected candidate 并对齐有效 overrides。已有自动门禁接线，当前候选/生产复验要求见 P1-01。
4. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
5. dry-run→上传校验→授权切流→候选 provider smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。
5. 对外声称「我们自己的服务器不保存录音和原话」之前：每次更换服务器或改动与存储相关的代码后，都要在目标服务器上重新核查存储。最近一次核查：aginice.cn 测试服务器，2026-10-14（见 docs/compliance.md）。另外，「机器人本身不存录音」这句话依赖当前固件：不挂载 SD 卡，也不把音频写入 flash。任何固件存储改动（例如启用 SD 卡、加入离线缓存、修改分区表）都要像服务器规则一样重新核查这句话。试用机的 SD 卡槽要封住。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片，只切 `voice-core-media-bridge`；线上 bridge 可以是整栈镜像（不带 `com.memoria.release.kind`，且 version 必须等于该容器的 `MEMORIA_RELEASE_TAG`），也可以是组件或回滚恢复镜像；已存在的同名回滚 tag（`release_ops.sh` 也用 `rollback-<tag>-pre` 命名上一栈）只有与当前 bridge 镜像 image id 相同时才复用。`--dry-run` 不检查线上 bridge，这一步的拒绝只在 `--cutover` 出现，且发生在任何切换之前。默认要求运行时底座与线上 bridge 是不同镜像，否则拒绝 `runtime base image is missing, has invalid provenance, or is not independent`；手工在发布机上构建的候选（如 `memoria-agent:20261021-a94c438-pr345`）底座就是线上 bridge 本身，需显式加 `--allow-manual-base-image --reason TEXT`（必须同时给 `--cutover` 和非空理由），此时回滚到上一个镜像等于连底座一起回滚。该决定会写进本地预检日志、远端 `ROLLBACK_POINT.txt` 与 `CUTOVER_RESULT.txt`（`manual_base_image_accepted` / `manual_base_image_reason`），其余检查不变；不带该选项的行为不变。`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

`scripts/deploy_control_component.sh`（control-api 单组件发布）会让 control-api 落在组件链上。`release_ops.sh` 的 `freeze` 只认白名单（主机状态文件，见「release-ops.sh 与 PREV」）里已登记的覆盖链，并不通用地接受任意组件链；新的 control-api 单组件发布会落在基础 compose + 未登记覆盖上，下一次整栈发布会在 `freeze` 被拒。做新的 control-api（或 bridge）组件发布并停留在组件链时，切换后先 `record-override <服务> <持久路径> </tmp/原件>` 把该覆盖的持久路径与 sha256 登记进状态文件，并核对 `freeze` 认得它；整栈发布之后该服务回到纯整栈链，登记过的行留在文件里不影响整栈（只换镜像的旧覆盖仍指向旧镜像，不再出现在新 PREV 的链上）。

运行门禁需剥离本地 `OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。门禁里的 pytest 在 macOS 上同样可跑：进程采样经 `scripts/local_process_probe.py`，Linux 读 `/proc`，macOS 读 libproc 的 RSS 与存活并用一次连接探测证明监听端口已关闭，ESTABLISHED 连接数记为未测量（`null`，不是 0），不因平台被跳过。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

### 完整制品上传与校验

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

### 容器日志落盘（control-api 与 voice-core-media-bridge）

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

#### 只读预检

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

#### 安装（另行授权）

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

#### 发布时的门

`release-ops.sh env` 在第一个一次性容器之前记下两个文件的大小。声纹开关检查之后、提供方冒烟之前，它要求两个文件里新出现本次的标记行：`control-api/<12 位 id> … control_env_valid=PASS` 与 `voice-core-media-bridge/<12 位 id> … speaker_authority_disabled=PASS`。缺一个就失败，线上不动：

~~~text
no control-api line with control_env_valid=PASS reached /var/log/memoria/control-api.log; install the host log sink (docs/runbooks.md, 容器日志落盘) and rerun env
~~~

轮转把当前文件改名为 `.1` 时，门从快照偏移接着读 `.1`。再轮转一次进 `.2` 才会漏掉标记，重跑 `env` 即可。

提供方冒烟（`env`、`finish` 的 readiness 刷新、`memoria-readiness-refresh.timer`）测的是这一个部署实际会跑的引擎。`TTS_PROVIDER` 只有一个合法值，冒烟永远说豆包；ASR 由 `ASR_PROVIDER` 决定，`funasr` 走 FunASR 冒烟，`volc` 走 Volc 冒烟。

冒烟在调用任何供应商之前先打一行 `voice_engines asr=… tts=…`。发布门用它和仓里脚本按同一份 env 推出来的 PASS 行比对，Control API 拒收引擎与自身配置不符的标记（409），bridge 心跳里带的引擎与 Control 配置不一致时 `/health/ready` 报 `engine_mismatch`。所以「冒烟测了一个引擎、线上跑的是另一个」这一类错配会在发布时或第一次心跳里暴露，不会等设备说话。

2026-10-21 21:49 火山把这个 app 的 seedtts 授权收回后，冒烟每 12 小时失败一次，readiness 从 10-22 起一直是 `not_ready` / `smokes=expired`，`env` 也被挡住；授权恢复后自动回到 PASS。当时冒烟硬编码测豆包，而 StepFun 是主用，那是这段历史的起因。现在只有豆包一条路，同样的授权收回会直接让冒烟失败，这是对的。

#### 读日志

~~~bash
ssh memoria-prod 'sudo tail -F /var/log/memoria/voice-core-media-bridge.log'
ssh memoria-prod "sudo sh -c \"zgrep -h 'media turn committed' /var/log/memoria/voice-core-media-bridge.log* | sort\""
ssh memoria-prod "docker inspect -f '{{.Id}}' memoria-voice-core-media-bridge-1 | cut -c1-12"   # 当前容器写在哪个 id 下
~~~

行首是时间，同一时区偏移下 `sort` 之后就是时间顺序。

#### 事故处理：日志里出现孩子原话

1. 停写：按「回滚二」第 ① 步换上关闭版 drop-in。
2. 删除并核对：

~~~bash
sudo find /var/log/memoria -type f -delete
sudo find /var/log/memoria -type f | wc -l       # 0
~~~

3. 清 Docker 缓存。缓存随容器删除：经授权，用线上同一棵发布树重建这两个容器，`sudo env MEMORIA_RELEASE_TAG=… MEMORIA_RELEASE_COMMIT=… docker compose -f docker-compose.production.yml --profile media-runtime up -d --no-deps --no-build --force-recreate control-api voice-core-media-bridge`（与「media-edge 组件发布」第 3 步一样必须 `sudo env`）。重建后 `docker logs` 里不应再有那段原文。
4. journald：跑上面的 `journalctl -u docker … | grep -c 'Error writing log message'`，不为零另行授权处理。
5. 恢复写入：重装开启版 drop-in（「安装」一节第 3、4 行），重做安装核对。时间、范围和核对结果记进 `HANDOFF.md`。

#### 回滚

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

③ 从仓库拿掉：一个 PR 同时删去两个 `logging:` 块；`release_ops.sh` 里的 `HOST_LOG_DIR`、`host_log_size`、`assert_host_log_has`，以及 `step_env` 里记大小和两次断言的几行；两处测试；`infra/` 下的五个文件；本节，以及 `docs/compliance.md` 里对应的句子。然后走一次整栈发布。在这之前，`env` 会拒绝任何仍带 `logging:` 块的候选，因为关闭版让标记行进不了文件。这是有意的。

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

### media-edge 组件发布（手工，只换 media-edge 一个容器）

**删镜像前的核对规则（2026-10-23）**：删除任何 `memoria-*` 镜像标签之前，先核对最新 `.cutover/prev-release` 的 `overrides=` 记录里每个 image id 仍能 `docker image inspect` 成功。回滚只 recreate `services=` 名单里的服务、逐服务按记录追加覆盖 `--no-deps --no-build`，所以只要覆盖镜像还在，基础标签（整栈同名 tag）被删不影响回滚起得来；但覆盖镜像一旦丢失，`verify_prev_image_ids` 会直接拒。composite 合并配置里出现的其他标签（如 media-edge / media-slo-reporter 解析出的整栈 tag）不参与 recreate，允许被清。10-22 的清理就删掉了 `memoria-{control-api,agent,media-edge}:20261020-minor-bypass-a94c438`，14:1x 复核确认回滚仍完整可跑。

media-edge 不在 `release_ops.sh` 的整栈切流里，镜像按组件 tag 单独换，后续整栈发布没有换过它。编排链以线上容器为准：`docker inspect memoria-media-edge-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'`（2026-10-02 起是 `releases/20260930-local-stop-v2/docker-compose.production.yml` + `component-releases/<tag>-media-edge/media-edge-component.override.yml`）。收据样例见 `docs/handoff-archive.md`「2026-10-02 media-edge 组件发布 20261002-late-progress-v1」。

1. 干净 detached worktree 在要发布的提交上构建（`DOCKER_DEFAULT_PLATFORM=linux/amd64`，导出 `MEMORIA_RELEASE_TAG/COMMIT`，`docker compose -f docker-compose.production.yml --profile media-runtime build media-edge`）；核对镜像的 revision/version/role 标签，用 `docker cp` 取出 `/usr/local/bin/memoria-media-edge` 确认含新代码并记下 sha256；`docker save` 后 scp 到 `/opt/memoria/incoming/<tag>-media-edge/`，服务器上 `sha256sum -c` 通过再 `docker load`（服务器 image id 与本机 containerd 存储里的不同，以标签和二进制 sha256 为准）。
2. 在 `/opt/memoria/component-releases/<tag>-media-edge/` 写 `media-edge-component.override.yml`（`services.media-edge.image` 指向新 tag）与 `media-edge-rollback.override.yml`（当前线上的 tag），并把切前全部容器的「名字 镜像 启动时间 重启次数」快照存为 `pre-all.txt`。
3. 确认设备没在用（edge 近 10 分钟没有 `device=dev_…` 日志），再在上述 compose 树里切换：`sudo env MEMORIA_RELEASE_TAG=<该树的栈 tag> MEMORIA_RELEASE_COMMIT=<提交> docker compose -f docker-compose.production.yml -f <组件 override> --profile media-runtime up -d --no-deps --no-build media-edge`。必须 `sudo env`：`ubuntu` 读不了 `/etc/memoria-agent.env`；`docker compose config` 的渲染含 env 展开值，只取需要的行并立即删除渲染文件。
4. 验收：存 `post-all.txt` 并与 `pre-all.txt` 对比，只有 media-edge 变；healthy、restarts 0；运行中二进制的 sha256 等于构建出的（容器没有 shell，用 `docker cp` 取出）；启动日志无 WARN/ERROR；外部 readiness 200、未带凭证的设备入口 401；设备重连与真机对话等下次唤醒。
5. 回滚：同一条切换命令，把组件 override 换成 `media-edge-rollback.override.yml`。

### 数据层、备份与恢复

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

### 控制库从 SQLite 迁到 PostgreSQL（一次性，需授权）

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

### 控制库 schema 版本

`schema` 步骤先重放幂等基线 `postgres_schema.sql`，再按编号执行 `services/control_api/app/database/migrations/` 中尚未记入 `control_schema_migrations` 的文件，每个文件连同台账行在一个事务里完成，失败则整体回退、发布中止。control-api 启动时若库版本低于代码所需版本会拒绝启动，报错会提示先跑 `schema` 步骤。回滚代码不需要回退 schema：库版本高于代码时照常启动，所以新迁移必须对上一版代码保持兼容（先加列、后删列，分两次发布）。

### LiveKit 媒体链退役（首个不含 LiveKit 的整栈版本）

该版本的 `images.tar` 只含 `memoria-agent`（即 Voice Core 媒体桥镜像）、`memoria-control-api`、`memoria-speaker-model`；compose 不再定义 `agent`、`miniprogram-gateway`、`device-media-gateway`，env 分流只写 Control/Agent/Speaker Model/Media Edge 四份文件，旧 env 文件里的 LiveKit/网关键会被接受但不再分发。`20260929-livekit-retire-v1` 当时的 `release-ops.sh` 让 PREV 保留六个服务：cutover 在 bridge 健康后只停止（不删除）`memoria-agent-1`、`memoria-miniprogram-gateway-1`、`memoria-device-media-gateway-1`，rollback 可按 PREV 整体重建。主机清理完成后，其后的版本 PREV 与本版本同为 speaker-model、control-api、voice-core-media-bridge 三个角色，不再有 retire 步骤。

仓库外的主机清理不随发布自动进行，需单独授权并在回滚窗口关闭后执行（`20260929-livekit-retire-v1` 已于 2026-09-29 按下述步骤完成，收据见 `docs/handoff-archive.md`「2026-09-29 整栈发布 20260929-livekit-retire-v1」；此后整栈 `rollback` 只能回到同样不含旧媒体链的 PREV，否则按组件回滚）：停止并移除 LiveKit server compose 项目与其 sysctl 配置；安装新的 `memoria-stream.conf`、`memoria-https.conf`、IP server 片段后删除 `/etc/nginx/snippets/` 下的 `memoria-livekit.conf`、`memoria-miniprogram-media.conf`、`memoria-device-media.conf`（同时删掉 443 server 里对 `memoria-miniprogram-media.conf` 的 include），`nginx -t` 通过后 reload（若先删片段再回滚，PREV 网关的公网路由会缺失）；移除三个已停止容器、两份网关 env 文件和不再被引用的网关镜像。

### speaker-model 退役（首个不含声纹嵌入服务的整栈版本）

声纹注册与声音克隆已删（#221），说话人权威保持关闭，生产不再调用 CAM++ 嵌入服务。该版本起 `images.tar` 只含 `memoria-agent`、`memoria-control-api`；compose 不再定义 `speaker-model`，control-api 不再 `depends_on` 它，并把 `MEMORIA_SPEAKER_EMBEDDING_URL` 固定为空串：主机 `/etc/memoria-control-api.env` 里可能还留着旧的 `http://speaker-model:8001/…`，compose 的值覆盖它；空串时 control-api 不建 HTTP 嵌入客户端，`validate_production` 接受空串。readiness 去掉 `speaker_model` 核心检查；`speaker_authority` 检查与声纹相关的表保留。

`release_ops.sh` 的 `STACK` 表把 speaker-model 标为 `retired`。`freeze` 记下的 PREV 仍含它（它在上一栈的 compose 链上）；`cutover` 在 ship 角色全部健康后只停止（不删除）`memoria-speaker-model-1`；`rollback` 按 PREV 的 compose 重建 control-api、voice-core-media-bridge、speaker-model 三个服务。再下一次整栈发布时，停着的容器仍挂在旧链上，`freeze` 推出的 PREV 不再含它，`cutover` 没有可停的，不需要改脚本。

主机清理（停着的容器、`/etc/memoria-speaker-model.env`、包括 `rollback-<tag>-pre` 在内的 `memoria-speaker-model:*` 镜像）不随发布进行：须等再下一次整栈发布之后、没有回滚目标还运行 speaker-model 时单独授权。提前清理会让 `rollback` 失败，因为 PREV 的 compose 引用那份 env 文件和镜像；env 分流工具仍会写这份文件，也是为了回滚。`STACK` 里的 `retired` 行同样要等没有回滚目标运行该服务后才能删。

### 设备信任开关（仅绑定链路，无硬件 attestation）

`MEMORIA_BOUND_DEVICE_TRUST_ENABLED`（control-api env，默认 `false`）。生产设备没有 attestation，`action_device_lock_trust` 恒返回 `untrusted`，Policy 因此不给任何设备 `memory_recall_private`、`guardian_summary_view` 与 `memory_capture`。打开后，满足下列全部条件的设备按 `trusted` 对待：onboarding 记录为 `bound` 且绑定到当前 binding 与版本、fleet 未封禁、其激活证书在 fleet 里没有被吊销或过期。`trusted` 只放行这三项记忆类能力，其余敏感能力（声纹、声音克隆、数字自我、原始录音、训练、支付、转让、`memory_promotion`、家庭共享）仍要求硬件 attestation 的 `verified`。

SQL 侧只多返回事实 `device_bound`，与开关无关，所以 `schema` 步骤可以先于 cutover；旧代码忽略该字段、永远看不到 `trusted`，回滚代码不需要回退 schema。

1. 打开：整栈发布后确认设备会话正常，在 `/etc/memoria-control-api.env` 加 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED=true`，重建 control-api 并等 healthy。此后新的设备会话取新 profile；已在进行的会话保留旧 profile（设备会话最长 `DEVICE_RUNTIME_PROFILE_TTL_S`，默认 3600 s；小程序控制会话 5 分钟）。
2. 验收（只读查库）：`session_runtime_profiles` 里最新设备会话的 `payload_json->'capabilities'` 含 `memory_recall_private`；`policy_receipts_v2` 里该能力不再有 `device_untrusted` 拒绝，新 receipt 的 `device_trust` 为 `trusted`（`trusted` 只会来自这个开关，`verified` 才是硬件证明）；说一句带专名的话后 `archive_evidence_events` 出现该使用人的 `speech.utterance_finalized`，`archive_processing_outbox` 中该事件为 completed 而不是 dead。
3. 关闭：删掉该行或改回 `false`，重建 control-api。已签发的 profile 到期前仍带 `memory_recall_private`，要立刻停止归档就让机器人重新唤醒（新会话取新 profile）。设备被解绑或吊销时，无论开关如何都立即变为 `revoked`。
4. 排障：打开后 profile 仍没有 `memory_recall_private` 时，看 `policy_receipts_v2` 里该次决策的 `reason_code`；`device_untrusted` 说明 `device_bound` 为假（onboarding 记录不是 bound、绑定版本不匹配，或激活证书被吊销或过期）。

### 固件 OTA 发布与回滚演练（设备在线升级）

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

### 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。若回滚还需要恢复 `freeze` 写入的 PostgreSQL / 控制库快照，那些文件只保留 3 天（见上文「Freeze 转储保留 3 天」）；超过窗口后只能回滚镜像与配置，不能再从 `.cutover` 转储还原库。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-flash` 或误用旧 run 回滚件。

`build.sh` 不再把整片镜像复制成 `merged.bin`。需要整片包时用 `--danger-full-erase`，产物文件名带 `DANGER_FULL_ERASE`。`flash.sh` 默认拒绝烧这类文件，也拒绝任何盖住 `memoria_identity` 的写入。该分区在 `0x10000`，长 64 KiB。只有 `--allow-danger-full-erase` 才允许这一次写入。烧录前和烧录后各只读一次该分区，只判断 NVS namespace `device` 是否存在，不读取、不打印键值。烧前已有、烧后没有，脚本失败退出。烧前就没有身份时打印警告并退出 0，提示运行 `provision_identity.py`。回滚时恢复 `build.sh`、`flash.sh` 和 `check-overlay.sh` 的上一版，并删除 `identity_guard.py`。

保留身份分区。解绑、恢复出厂，以及将来还没定 GPIO 的恢复出厂按键，都不得擦除它。擦除只能按名字擦默认 `nvs` 分区，或擦具体 namespace。禁止全片擦除，也禁止不带分区名的 `nvs_flash_erase()`。`esp_partition_erase_range` 和 `esp_flash_erase_region` 默认禁止，只有 `identity_guard.py` 里按文件和函数列出的调用可以保留。`esp_ota_*` 不受这条规则限制。仓库里还没有恢复出厂擦除函数。`check-overlay.sh` 已经按这套规则扫描叠加层和脚本，以后的按键代码走同一条检查。句柄跨函数再 `nvs_erase_all` 时，这次扫描看不到，那种写法仍然不能擦身份分区。

这块板的 USB-Serial-JTAG 上 `esptool read-flash` 会在个别 4 KB 块处确定性失败（2026-10-02：`0x106000`，4 MB 槽里另有约五处，报「No more data to read from the serial port」，同样的字节分成 2 KB 读就没事），所以整槽备份和保护区比较用 `firmware/esp32/scripts/flash_backup.py`（用 IDF 的 python；先停串口记录进程）：`backup` 分 64 KB 读，失败的片重连后改读 2 KB 帧，每个备份文件都与芯片自己算的 MD5 核对；`md5` 给出各区的设备端 MD5，写前写后各跑一次，比较引导/分区表、nvs、phy_init、身份区 `0x10000` 与 assets 不变（otadata 在新 app 首次启动后会被引导程序重写成与写前相同的记录，不要求保持空白）。写入只用 `esptool write-flash 0x20000 <app> 0xd000 <8 KiB 的 0xff>`，以其 `Hash of data verified.` 为写入校验；刷完用 `esptool … --after hard-reset read-mac` 或重开串口记录进程让板子回到 app。

**空白 otadata 与任何填充文件必须先在本地验字节再上机（2026-10-06 事故）**：那次用 `head -c 8192 /dev/zero | tr '\0' '\377' > blank.bin` 生成「8 KiB 的 0xff」，那条管道把每个 0xFF 按 Latin‑1 折成 U+00FF 再按 UTF‑8 写出，文件实为 **16384 字节、每两字节 `c3 bf`**；`esptool write-flash … 0xd000 blank.bin` 于是写了 16 KiB，越过 otadata 边界，**擦掉整个 phy_init（`0xf000`–`0xffff`）与 identity 分区的前 4096 字节**（`0x10000`–`0x10fff`）。现场特征：identity 读回前 4096 字节是 `c3bf` 交替、之后与备份逐字节相同——这个精确边界就是越界写入的指纹。所以：填充文件用 Python 之类的二进制方式生成（`python3 -c "import sys; sys.stdout.buffer.write(b'\xff'*8192)"`），**写前用 `xxd` 与 `stat -f%z` 验字节与长度**，并核 `esptool` 报出的 `Wrote N bytes` 是否等于期望值（正常应为 8192；写 16384 就是这类越界）。损坏可逆的前提是先备份：`ota0-before.bin` 还了 app，`nvs-otadata-phy-before.bin` 里的 phy 段与 `identity-before.bin` 的前 4096 字节逐区写回即可复原，写后用 `flash_backup.py md5` 逐项核回刷前值（boot/分区表、phy_init、identity、assets、otadata 都应与刷前相同）。

服务器域名变了要重写身份区（NVS，`0x10000`，64 KiB）里的 `control_api_url`，否则机器人启动后还去旧域名取激活清单（2026-10-05 `aigcnice.com` → `aginice.cn` 做过一次，收据见 `HANDOFF.md`「2026-10-05 域名切换到 aginice.cn」收据 ⑥）。做法：用 `firmware/esp32/scripts/provision_identity.py` **离线**生成身份镜像（不带 `--port`；输入是原来的 device-id / certificate-id / client-id、种子文件和激活公钥，只改 `--control-api-url`），先再用**旧** URL 生成一份，与芯片里读出的身份区逐字节比较，相同才说明新镜像只改了 URL；然后与新 app、空白 otadata 一起一次 `esptool write-flash 0x10000 <身份> 0x20000 <app> 0xd000 <8 KiB 的 0xff>`（写前按上一段备份各区，写后比较 md5，身份区应等于新镜像、其余保护区不变）。身份镜像含种子，权限 0600，放在被 git 忽略的 `firmware/esp32/artifacts/dev-path2/`，不进输出和日志。这只改机器人取清单的入口；已绑定机器人的签名清单里的端点（`control_api`、`device_media`）要靠解绑再重绑才会换成新域名。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。

### 固件 bench 构建（调试镜像，不发布）

`firmware/esp32/scripts/build.sh --bench` 构建调试镜像：`config.bench.json` 是产品 `config.json` 加且仅加 `CONFIG_MEMORIA_BENCH_SERIAL=y`（patch `0035`，默认关）与 `CONFIG_LV_USE_SNAPSHOT=y`，`check-overlay.sh` 逐项核对这一点，产品清单里不得出现 bench 开关。镜像文件名带 `-bench`，内嵌标记 `MEMORIA_BENCH_BUILD=1;`：`publish_firmware_release.py` 见到它拒绝签名与上传；`flash.sh` 要求「所求类型」与树里的构建一致（不带 `--bench` 拒绝刷 bench 构建，带 `--bench` 拒绝刷产品构建）；`build.sh` 在生成的 sdkconfig 与所求类型不符时直接停下。bench 镜像不占 OTA 指针，也不产生新的 `MEMORIA_FIRMWARE_BUILD`。

bench 镜像在 USB 串口多认两条命令；产品镜像不含这两个动词，收到只记 `usb command ignored (not a command)`：

- `snap`：把屏幕当前内容（文字层在内）按 `SNAP <id8hex> <w>x<h> <seq>/<total> <crc8hex> <base64>` 行分块吐出（每块 384 B RGB565 小端），以 `SNAP <id> end …` 或 `SNAP <id> failed reason=…` 行收尾，约 6–8 s；`scripts/snap_to_png.py` 从串口记录还原 PNG，逐块验 base64、长度与 CRC-32，再对 end 行验整幅。
- `status`：吐一行 `MemoriaBench: status up_ms=… phase= mood= frame= screen_off= sleeping= captioned= frames= drawn= render_us= render_max_us= busy_us= px= composed_px= extra_ms= heap_free= psram_free= anim_stack_free=`，给出画了多少帧、每帧渲染耗时、帧调速器拉长了多少毫秒，以及堆与动画任务栈的余量；`scripts/bench_status.py`（只用标准库）解析它，字段表与固件格式串由测试逐项比对。

用法：先起常驻的 `scripts/voice_soak_serial_logger.py` 持有串口，再 `scripts/voice_soak_serial_command.py snap --log <串口记录> [--out shot.png]` 或 `… status --log <串口记录> [--after 60]`（`--after` 隔这么多秒再问一次，并算出这段时间里帧的开销）。刷入 bench 镜像和每次发命令都是对设备的一次操作，须用户当场授权；刷入前按上一节的方法备份保护区，量完**必须刷回产品镜像**。上游 `self.screen.snapshot` MCP 工具虽然链接在产品镜像里（`LV_USE_SNAPSHOT` 上游默认开），但 Memoria 的设备协议不转发 MCP（读源码得出，没在真机上试过），所以截屏只走 bench 构建。

### 固件 keep-awake 构建（临时远程测试，不发布）

产品镜像和 bench 镜像仍会深睡。`CONFIG_MEMORIA_KEEP_AWAKE` 默认关。需要 USB 串口一直在的测试机，在 `firmware/esp32` 下执行：

```bash
./scripts/build.sh --keep-awake
./scripts/flash.sh --keep-awake --port /dev/cu.usbmodem101
```

`config.keep-awake.json` 只比产品清单多 `CONFIG_MEMORIA_KEEP_AWAKE=y`。固件停在 wifi hold，不调用 `esp_deep_sleep_start()`。空闲时麦克风关闭，不上传音频。屏幕轻点或 USB `wake` 才开始会话。文件名带 `-keep-awake`，`publish_firmware_release.py` 拒绝发布。测完刷回产品镜像。`--bench` 与 `--keep-awake` 不能同时使用。

### 真机窗口：串口、句表、判据、日志包

这一节不授权刷机、播放或改音量。下面的命令都在电脑上跑。

真机窗口用 `scripts/voice_soak_serial_logger.py` 抓串口。`open()` 之前把 RTS 设为低、DTR 设为高，避免 USB-Serial-JTAG 复位板子。`--port` 和 `--baud` 可改，波特率默认 115200。每一行日志带本机时间戳。刷机前停掉这个进程。

```bash
uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py outputs/serial/robot.log \
    --port /dev/cu.usbmodem2101 --baud 115200
```

拨测句表是 `scripts/voice_soak_scenarios/child.json`。不要另建一份。`voice_soak.py --scenario` 读这一份。

窗口结束后用 run 目录算通过或失败。命令只读本地文件。尾窗必须是 `verified_idle`。静音步必须看到 idle。打断由 `voice_soak_evidence.py` 用 bridge 和 serial 日志重算。timeline 里旧的 `stopped` 字段不算数。没复核成停止就不通过。`scripts/voice_soak_verdict.py` 的 `MAX_NO_REPLY` 是 0。`MAX_REPLY_LATENCY_P90_S` 是 2.0，`MAX_REPLY_LATENCY_MAX_S` 是 3.0（2026-10-31 Monkey 定的内测首字门禁）。漏答 `no_reply` 必须为 0。首字时延 `reply_latency_p90_s` 不超过 2.0 秒，最慢一轮 `reply_latency_max_s` 不超过 3.0 秒。时延缺失时脚本判失败。判失败时脚本以退出码 1 结束。打断按 `interrupt_kind` 分开判（2026-10-31，#496 #499）：附和（`backchannel`，如「嗯」）只有确认把回复停了才失败，继续播放算通过；「停」这类 `stop` 打断开始时回复只剩不到 2.5 s，记 `interrupt_too_late_to_stop`，算无法判定，不记产品失败。专测「停」用 `devicetest/scenarios/stop_focus.json`（6 次「停」、2 次「别说了」、2 次「嗯」）；Mac 音量 30 时「停」会被机器人自己的声音盖住，专测需用户同意后用 50，测完恢复 30。共享 checkout 停在旧分支时，用 `origin/main` 的 worktree 跑测试脚本（参考 `devicetest/run_soak_9e6082c3.sh` 的路径改写），否则拿到的是旧的插入时机和判定参数。

```bash
python scripts/voice_soak_verdict.py outputs/acceptance/run-<stamp>-soak
```

同一目录里打包 `timeline.jsonl`，以及已经跟上的 `serial.log`、`bridge.log`、`edge.log`、`control.log`。缺的日志不进包。没有 `timeline.jsonl` 则命令失败。包写在 run 目录的上一级，旁边有 sha256。不连设备，不连生产。

```bash
python scripts/voice_soak_pack.py outputs/acceptance/run-<stamp>-soak
```

### 电脑语音测试的停止证据（2026-10-10 修正）

每轮真机、串口命令、刷机与调整音量仍须当场授权；本节只规定判据，不授权执行。运行脚本时显式给用户确认的 `--volume`，不靠脚本默认值；缺声学测量不报端到端声学停止延迟。

`voice_soak.py` 记录 `reply_started_ts`、`interrupt_start_ts` / `interrupt_end_ts` / `interrupt_deadline_ts`、`device_returned_ts` 与串口时区偏移。共用 `voice_soak_evidence.py` 按设备开口 generation 找唯一完整 delivery fence，要求同回复 `preempted` + 实际存在的 `media spoken stop interrupted reply`（flush=True）+ 同代播放收据和窗口内退出说话。自然播完、任务异常、断线、泛化superseded、错代/跨会话、缺日志都不算成功；证据不足为 `stopped=null`、`stop_verdict=unverified`。`voice_soak_analyze.py` 重新核对而不是信任历史布尔值；日志稍后补到可离线再算，原时间线不改。

```bash
## 只读本机既有文件：不接串口、不播放声音、不访问生产
uv run python scripts/voice_soak_evidence.py outputs/acceptance/run-20261010-verify-standard
```

停止延迟从打断素材起点到设备退出说话，`stop_delay_origin=interrupt_start`；不是DAC/麦克风测得的停止时刻。旧记录未记刺激绝对时间，只能用完整回复终态排除自然完成/异常，不可补成成功；10-09/10-10的纠正与目标路径边界见`docs/acceptance.md`。`--duration-min`只是上限，N-5长稳按时间线实际跨度计。

10-11 09:16设备窗口取得一条正向停止样本和一条自然完成反例（[专项收据](acceptance.md)）；两个停止素材音色不同，尚不构成稳定性或音量对照。开测前先保存完整音量/静音设置，退出后核对恢复；现有驱动只保存/恢复音量数值，静音需在外层恢复。选择语音库时逐一核对提问及停止素材，避免缺文件时的Tingting回退改变实验条件。

### ASR / endpoint / 关闭诊断（2026-10-10 已发布）

诊断随 PR #199 / `20261010-n8-endpoint-diag-v1` 于 2026-10-10 23:12 CST 部署。每次读新收据仍先核对发布身份；旧版日志缺字段不代表没有阻挡区间或只走词法规则。只有拿到已授权设备窗口的新收据后才能用下面的字段作设备结论，本节不授权发布或测试。23:17 中性问答轮已捕获 4 条 overlap 快照与 1 条 reopen-window `no_text` 原因；其余出口未触发，#197/#198 目标路径仍未复现，见 `docs/acceptance.md`。

- `media ASR result rejected`：`rescue_synthesized` 标记被拒稿来源；`overlap` 为 supervisor 决策当时的规范化区间和阻挡快照（最多4条，另有 `conflict_count`），含 task epoch、sample range、文字长度、rescue 标志，不含文字和 sentence id。按同 session / stream epoch 解释，不能用未知区间猜着放宽保护。
- `media turn has no text`：`text_after_endpoint` 是文字起点在提交区间之后的数量，结合 `pending_end`、`active_vad_start`、`last_playback_end`、`committed_sample` 区分“没有文字”与“文字在旧 endpoint 外”。它不证明麦克风输入质量，也不授权强制把晚到文字拼进旧回合。
- `media early conversation-close endpoint`：`rule_match` 只表示当前文本是否符合词法关闭规则；`source=final` 且规则不命中时同步判断来自已有缓存，规则命中时仍可能同时有缓存。ASR 范围 / task epoch / rescue 标志用来关联输入，原文仍不进日志；没有原文和声学采集不能断言误听了哪句话。
- `media reopen evidence window`：`armed` 记录覆盖旧 endpoint 的已知文字和时间窗；`skipped` 的 `reason` 区分 `no_text`、`endpoint_uncovered` 与 `reply_in_flight`；`held` 区分 `turn_end_advanced`、`partial_after_endpoint`、`turn_changed`、session/stream 变化与回复仍在途。`media reopened turn committing without new text` 表示到期后提交；这些日志只带 ASR 样本区间、文字长度和已提交水位，不带终稿或 token 文本。
- `services/agent/tests/unit/test_media_reopen_replay.py` 用于离线差分 t038 已记录事件与一个明确标注的合成 interim 对照。两条分支不复原历史识别内容；只有新的已授权设备收据才能判定当时实际走了哪条路径。

本地素材的 WAV RMS / peak 是数字电平；噪声文件名不等于实测电平。`afplay` 起止是进程时刻，串口和桥时间不是校准后的 ADC/DAC 时钟；要判断输入与噪声因果须取得同一窗口的实际输入证据。10-10 的留存文件读数和同期 VAD 观察见 `docs/acceptance/run-20261010-verify/input-audit.json`，不是新声学验收。

---

## 来源：`docs/runbooks/sensevoice-asr.md`

## ASR 救援 sidecar（SenseVoice）重建与验证

实时 ASR 主链是云端 FunASR。某一段语音实时识别为空时，agent 会把这段 PCM 发给本 sidecar 做一次离线转写，调用方是 `services/agent/src/providers/sensevoice.py`。sidecar 不在 compose 里，线上用 `docker run` 单独启动。本文说明如何在仓库里原样重建它，以及 2026-09-26 的验证结论（P1-02）。

### 组成

| 部分 | 仓库位置 | 与线上 `memoria-sensevoice-asr:20260901-pin-language` 的关系 |
|---|---|---|
| 服务脚本 | `scripts/run_sensevoice_asr.py` | sha256 `4935a952…` 与线上容器内一致 |
| 镜像 | `infra/sensevoice-asr/Dockerfile` | 基础镜像按 digest 锁定为 `python:3.11-slim@sha256:1042b614…`（4 层与线上逐层一致，Python 3.11.16） |
| 依赖 | `infra/sensevoice-asr/requirements.txt` | 线上容器 `pip freeze` 的 17 个包，带哈希锁定（`--require-hashes`） |
| 模型 | `infra/sensevoice-asr/models.sha256` | 只入库校验值；模型约 240 MB，运行时只读挂载，不打进镜像 |

模型四个文件（`model.int8.onnx`、`tokens.txt`、`LICENSE`、`README.md`）为 sherpa-onnx 转换的 SenseVoice-small int8 版本；按文件大小推定来自 sherpa-onnx 发布包 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`，但未逐字节核对上游。权威来源是线上主机目录 `/opt/memoria/sidecars/sensevoice-asr/models`。无论从哪里取得，都必须先通过校验。

### 重建

在仓库根目录执行。`.dockerignore` 不排除 `outputs/`，所以建议用只含所需文件的临时目录作构建上下文，避免把数 GB 发布制品发给 Docker：

```bash
docker build --platform linux/amd64 -f infra/sensevoice-asr/Dockerfile -t memoria-sensevoice-asr:<tag> .
```

准备模型并校验：

```bash
(cd <模型目录> && shasum -a 256 -c <仓库>/infra/sensevoice-asr/models.sha256)
```

### 运行（与线上一致的参数）

```bash
docker run -d --name memoria-sensevoice-asr --network memoria_default \
  --restart unless-stopped --memory 1g --cpus 2 \
  -v /opt/memoria/sidecars/sensevoice-asr/models:/models/sensevoice:ro \
  memoria-sensevoice-asr:<tag>
```

agent 通过 `SENSEVOICE_URL=http://memoria-sensevoice-asr:8001/transcribe` 调用，超时 `SENSEVOICE_TIMEOUT_S=2.5`。

### 评测

`scripts/evaluate_sensevoice_rescue.py` 做四件事：
- 把 16 kHz 单声道录音按能量切成语句，每句以原样、降 20 dB、8 倍削波三种形态各发一次；
- 另发静音和噪声探针；
- 做一轮并发请求；
- 报告非空率、外文字符（语种识别失效的特征）、降质后与原文的相似度，以及相对 2.5 s 预算的时延。

默认不输出转写文本，因为录音是私人语音。

```bash
uv run python scripts/evaluate_sensevoice_rescue.py --url http://127.0.0.1:18001 outputs/acceptance/*.wav
```

### 2026-09-26 验证结论

- **可重建**：重建镜像的依赖、Python 版本、脚本与线上逐一一致。4 段合成中文语音（1.5–16.6 s）在本地重建的 amd64 镜像、arm64 镜像和线上实例上转写逐字相同。
- **识别稳健**：用 12 段真机上行录音切出 69 句（0.5–30 s）评测：
  - 无外文输出；
  - 降 20 dB 后与原文相似度 0.971，8 倍削波后 0.984；
  - 4 路并发下 16 句与串行结果完全一致。
  - 本机 arm64 无时延参考价值，时延以线上为准。
- **幻觉（已在代码中修复，随下次 agent 发布生效）**：静音或任意强度的噪声都返回「我。」。原先 `SENSEVOICE_MIN_TEXT_CHARS=2` 按原始长度计数，这两个字符恰好过线，噪声段一旦触发救援就会变成一轮用户输入。现在只计文字字符（`SenseVoiceRescueConfig.accepts_text`），标点和空白不计。
- **空闲后首请求超时（线上问题，未改）**：sidecar 空闲约 7 小时后，1.5 s 音频的首次解码用了 12.7 s，紧接着同一段只要 0.24 s。原因是主机内存紧张时，模型所在的匿名内存被换出：容器 VmSwap 约 450 MB，RSS 约 355 MB；主机 swap 已用 1.1/1.9 GB，swappiness 60。首请求要从磁盘换回模型，远超 2.5 s 预算，所以空闲后的第一次救援必然失败。
- **长语段超预算（已在代码中修复，随下次 agent 发布生效）**：线上解码约 0.09–0.16 s 每秒音频，再加传输开销。16.6 s 音频整体耗时 3.4 s；历史日志里 27–30 s 的语段仅解码就要 2.5–2.8 s。2026-09-29 真机两次复现（`httpx.ReadTimeout`，`pcm_ms=30000`）。现在已有非空 final 时只送未覆盖的尾段（`20260929-voice-core-refactor-v1` 已上线），默认上限改为 12 s（`SENSEVOICE_MAX_AUDIO_S=12`），整段都没有 final 时只救末尾 12 s。

### 排查没有 partial 的尾超时

先检查同一会话、同一 `stream_epoch` 的 `media_asr_boundary`，不要只凭 `asr_empty_class=empty+vendor_silent` 判断厂商故障。这个分类没有证明当前回合送入过音频，旧任务的 RMS 也不是当前回合的输入证据。

- `result=duplicate` 且 `audio_admitted_watermark <= previous_finalized_watermark` 表示从上次 finalize 以来没有新增的已送音频。结合 `listening_capture_hold` 和连续冻结的水位，可以区分收麦后的设备 VAD 边沿与真正的 ASR 空结果。
- 水位有推进时，再检查 `provider_pcm_observed_samples`、`provider_pcm_rms`、`provider_pcm_peak_abs` 和 `provider_pcm_clipping_detected`。短噪声与削波不能靠延长尾超时修复。
- 收麦期间的 VAD end 不创建新的回合或尾超时，也不能删除收麦前已经接收的真实输入。收麦后设备 VAD start 一律忽略（不恢复收音，也不产生回合）；只有点屏后的 `listening.resume` 恢复收音（phase cause 为 `screen_tap`），只送恢复点之后的音频，恢复后的 VAD 仍须能正常定端点。产品固件熄屏后进 wifi hold 关麦，所以 VAD 恢复只发生在熄屏前和 keep-awake 测试机上。
- 收音窗口只在 `listening` 时到期，孩子说话时（`user_speaking`）不会收麦；说话期间由 max-user-speech watchdog 兜底。phase 显示 `listening` 但回合仍开着（有 pending turn、本 epoch 的 VAD 未结束、已准入输入或 turn commit 正在跑）时同样不收麦，判据与静音丢帧门共用 `_input_turn_open`。soak 20261031-160928 的 08:21:11 就是 VAD start 并进旧 pending turn、phase 停在 `listening`，窗口在 commit 途中到期。收麦状态下回复播完，playback ack 会先发 `listening`，standby 切回 `connecting` 时必须再发一次 `listening_closed`，否则设备以为麦开着而服务端丢掉每一帧（同一 soak 08:21:25 之后 11 次 `media vad ignored during listening capture hold`）。设备 VAD 在播放结束之后才打开时，`playback-followup` 不钉端点，由这次 VAD 的 end 定端点。soak 20261029-200043 的 t042 同时踩到这两条：逗号处的 final 被钉成端点，窗口又在说话中到期，句子被截断提交，收麦状态还延续到下一轮。
- 收音窗口（`MEDIA_LISTENING_CAPTURE_WINDOW_S`，默认 30 s）从回复播完（或本轮被接受）时起算。之后 `listening` 期间收到的第一帧上行音频若已过期，就收麦并发 `assistant_state=listening_closed`。被当作空轮回收的噪声 VAD 不重新起算窗口，所以回复播完后没人说话时，`listening_closed` 在约 30 s 后发出；窗口到期时正好有噪声段在进行，就等这段噪声被回收时立即发出。每次发送都打一行 `media listening closed session=… silence_since_reply_s=…`；本轮已被接受、还没有新回复播完时，该值为 `None`。
- 尾超时之前，同一 `task_epoch` 出现过 `reason=straddles_committed_without_timing is_final=True` 且 `text_len>0` 时，`empty+vendor_silent` 是误标。厂商把已提交的上一句和新的一句合成了一条没有词时间戳的 final。该句最后被接收的文本若是新 final 的前缀，supervisor 只收后面的新话；对不上时仍整条丢弃。

2026-10-25 排查的 37 次尾超时中，33 次发生在收麦后，没有新增的已送音频。其余 4 次有音频，另按采集质量排查。该历史结论不代表其他 `empty+vendor_silent` 都属于同一原因。

本地回归入口使用假 provider，不调用外网或播放音频：

```bash
uv run --frozen --extra dev pytest --no-cov services/agent/tests/unit/test_listening_capture_window.py
```

### 待授权的线上调整（建议，未执行）

1. **禁止 sidecar 使用 swap**：`infra/sensevoice-asr/compose.yaml` 已提交 `mem_limit=1536m`、`memswap_limit=1536m`（两值相等即不使用 swap），并带 `/healthz` healthcheck。本地实测峰值 RSS 约 570 MB，线上 RSS 与 swap 合计约 805 MB，1.5 GB 留有余量。**未在服务器执行**，重建容器要单独授权。
2. **保活探针**：`scripts/sensevoice_keepalive.py` 每次发 1 s 合成静音到 `/transcribe`，只丢弃返回文本；配套 `infra/sensevoice-asr/memoria-sensevoice-keepalive.{service,timer}` 每 15 分钟跑一次。**未安装、未启用**，安装到主机要单独授权。探针音频是程序生成的静音，不是任何录音。
3. ~~把救援音频上限降到约 12 s~~：已改为代码默认值（见上），无需再改 env。
4. **随下次 agent 发布带上「我。」修复**。

调整后按上面的评测脚本在线上用合成语音复测时延（不要上传真实录音），并在 TODOLIST P1-02 记录结果。

---

## 来源：`docs/runbooks/tts-circuit-breakers.md`

## TTS 熔断

豆包有一个进程级鉴权闩锁，阶跃有一个熔断器。两边的参数不是同一套。生产现在只有豆包一条路（`TTS_PROVIDER=doubao`），阶跃保留给回滚镜像。

### 豆包鉴权闩锁

豆包握手被拒（WebSocket 返回 HTTP 401 或 403）时，记下这个资源，`DOUBAO_TTS_AUTH_LATCH_OPEN_S` 秒内后续会话不再握手，直接返回 `DoubaoAuthRejectedError`。代码默认 300 秒，`.env.example` 和 `infra/memoria.env.production.example` 都有这一行。

窗口按 resource 分别记（`seed-tts-2.0` 与 `seed-icl-2.0` 是分开授权的）。个人音色被拒不会连累正常角色音色。窗口到点后放行一次握手，被拒就重新计时，成功就清掉。控制台改权限后不用重启 bridge。

只有鉴权拒绝会打开闩锁。网络错误仍然走池自己的 `CircuitBreaker`（3 次失败、30 秒），不跨会话。原因写在 `services/agent/src/providers/doubao_tts.py` 的 `DoubaoAuthLatch`：设备会话每次都新建自己的池，池级熔断器从来累计不到三次。生产 2026-10-25 的 bridge 日志就是每建一次会话先打一行 `TTS pool warm failed … error=InvalidStatus`，再抛 `TTSUnavailableError`，因为火山从 10-21 21:49 起就没再给这个 app 的 seedtts 授权。

计数器 `tts_auth_rejected_total{provider="volcengine_doubao"}` 只记拒绝次数，不带文本和账号。

被闩锁挡下的会话不会静默：每次跳过握手打一行 `Doubao auth latch open; skipping handshake resource_id=…`，随后照常抛 `TTSUnavailableError`，设备拿到兜底话术。恢复授权后最多等一个窗口，下一句自动回到豆包，`/health/ready` 的冒烟是更早的信号。

### 阶跃

`StepFunFailoverConfig.from_env` 读取下面三个变量。变量不存在时用代码默认值。

- `STEPFUN_TTS_BREAKER_FAILURES` 的代码默认值是 3。普通失败要累计到这个次数才打开熔断。
- `STEPFUN_TTS_BREAKER_OPEN_S` 的代码默认值是 30 秒。
- `STEPFUN_TTS_AUTH_BREAKER_OPEN_S` 的代码默认值是 300 秒。

状态码 401、402、403 在第一次失败时打开鉴权熔断，时长是 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`。供应商文案里出现鉴权或余额标记时同样处理。标记写在 `services/agent/src/providers/stepfun_tts.py` 的 `_AUTH_MARKERS`。

其他状态码仍走普通窗口。一次 500 或 503 不打开熔断。累计 3 次后打开 30 秒。到时放行一次探测。探测成功后熔断关闭，下一句回到阶跃。

进程环境如果带了 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`，用环境里的值，不用代码默认值。`.env.example` 和 `infra/memoria.env.production.example` 把这个变量设为 300，与代码默认值相同。

### 豆包传输熔断

`DoubaoTTSPool` 使用 `CircuitBreaker()`，不传入失败次数或打开时长。`DoubaoTTSConfig.from_env` 也不读这些参数。豆包传输熔断不能用环境变量配置。

固定值在 `services/agent/src/providers/reliability.py` 的 `CircuitBreaker`。

- `failure_threshold` 是 3。
- `open_seconds` 是 30。
- `half_open_successes` 是 2。半开之后要连续成功两次才关闭。

要改豆包熔断时长，改这段代码。没有对应的环境变量。这个熔断器是每个池一个，所以只对同一个会话里的连续失败有意义；跨会话的那一类由上面的鉴权闩锁负责。

豆包的失败不会打开阶跃的鉴权熔断。下一句普通回复会再试阶跃。

---

## 来源：`docs/runbooks/volc-asr-protocol-check.md`

## 火山大模型流式 ASR 协议核对

对照 `volc_protocol.py` / `volc_asr.py` 与火山引擎官方文档和官方 Demo 客户端，逐项确认帧格式、事件码、握手、结束包。核对日期 2026-10-21。

### 权威来源

- 官方文档《大模型流式语音识别API》：<https://www.volcengine.com/docs/6561/1354869>
  （渲染页读不到正文时，正文可由 `https://docs.volcengine.com/api/doc/getDocDetail?LibraryCode=DoubaoVoice&DocumentCode=LargemodelstreamingautomaticspeechrecognitionAPI` 取得。）
- 该文档附带的官方 Demo 客户端（Python / Go / Java 三份，均在文档 `# Demo` 一节）：`sauc_python.zip`、`sauc_go.zip`、`sauc.zip`。下面是 Python 版的关键实现，用于交叉验证文档正文：

  ```python
  # RequestBuilder.new_full_client_request — 握手只有一个 JSON 帧
  header = AsrRequestHeader.default_header().with_message_type_specific_flags(POS_SEQUENCE)
  payload = {"user": {...}, "audio": {...}, "request": {"model_name": "bigmodel", ...}}
  request = header.to_bytes() + struct.pack(">i", seq) + struct.pack(">I", payload_size) + gzip(payload)

  # RequestBuilder.new_audio_only_request — 最后一包用负序号
  if is_last:
      header.with_message_type_specific_flags(NEG_WITH_SEQUENCE)
      seq = -seq
  ```

### 结论摘要

评审第 4 条的怀疑成立。改之前的实现用的是 StartConnection(1)/StartSession(100)/TaskRequest(200)/TASK_STARTED(250) 这套事件帧，那是双向 TTS 与实时对话协议，**不属于** `sauc/bigmodel*`。按原实现对真实端点，每次连接都会卡在等待 `CONNECTION_STARTED`，最终 `connect_failed`；即使连上也等不到 `task-finished`，`finalize_speech_segment` 会抛 “task boundary event was not available”。

本轮已按官方文档重写，逐项对照见下表。

| 项目 | 改前 | 官方文档 | 结论 |
|---|---|---|---|
| 握手 | 发送 StartConnection(1) 事件帧并等待 `CONNECTION_STARTED(50)` | WebSocket upgrade 后**第一个请求就是 full client request**（JSON 配置），没有事件帧、没有连接级握手 | 不一致，已改 |
| 事件码 | `VolcEvent` 定义 1/2/50–52/100/102/150/152/153/200/250–252，读侧按事件分发 | 该协议**没有事件码**，也没有 `Message type specific flags = 0b0100 (WITH_EVENT)` | 不一致，已改：删除事件表与 `WITH_EVENT` |
| Full client request 位置 | 只作为 TaskRequest(200) 事件帧的 payload 发送 | 连接后第一个请求，`header + sequence + payload_size + gzip(JSON)`，`sequence=1` | 不一致，已改 |
| Full client request 序号 | 无序号（事件帧用 `WITH_EVENT` 不带 sequence） | 带正序号，官方 Demo 从 1 开始 | 不一致，已改 |
| audio-only 请求 | 只有在累积满 `chunk_ms` 时才发，余量留在缓冲区 | `header + sequence + payload_size + gzip(PCM)`，序号正数递增，`serialization=0b0000` | 结构一致；本轮补齐“永远保留一包余量”，让负包一定带音频 |
| 结束包 | 发送 `FINISH_SESSION(102)` 事件帧，等 `TASK_FINISHED(251)` | 最后一包音频用**负序号**（`flags=0b0011`，`sequence` 取负），官方 Demo 的 `is_last` 分支 | 不一致，已改：负包结束，无独立结束事件 |
| 任务边界 | 依赖 `task-finished` 事件 | 服务端最后一包 full server response 带 `is_last` 标志（`flags & 0b0010`），官方 Java Demo `if ((messageTypeSpecificFlags & 0x02) != 0) isLast = true` | 不一致，已改：以 `is_last` 帧为边界 |
| 连接复用 | 一条 WebSocket 上多次 StartSession/TaskRequest | 文档未定义会话级多任务；一次连接对应一个识别任务，结束即负包 | 不一致，已改：每段一条连接、一个任务 |
| 服务端响应的 sequence | 读侧把 `flags & 0b0011` 都当序号 | 与请求同构：`0b0001` 正序号、`0b0011` 负序号；`0b0010` 是“最后一包且无序号” | 已按位语义解析（`0b0001` 读序号，`0b0010` 记 last） |
| 错误帧 | `0b1111` + `error_code(int32)` + `payload_size` + payload | `0b1111` + error code(4B) + error message size(4B) + message(UTF8) | 一致 |
| header 字节 | 4 字节：版本/头长、类型/flags、序列化/压缩、保留 | 同左，`protocol version=0b0001`、`header size=0b0001`（×4=4 字节）、保留 1 字节 | 一致 |
| 默认 endpoint | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` | 文档列出的双向流式优化版地址，并明确推荐 | 一致 |
| 鉴权头 | `X-Api-App-Key`、`X-Api-Access-Key`、`X-Api-Resource-Id`、`X-Api-Request-Id`（UUID） | 旧版控制台四项即此；`X-Api-Request-Id` 推荐随机 UUID | 一致 |
| Resource ID 默认值 | `volc.seedasr.sauc.duration` | 豆包流式语音识别模型 2.0 小时版 | 一致 |
| `request.model_name` | `bigmodel` | 目前只有 `bigmodel` | 一致 |
| `audio` 字段 | `format=pcm, codec=raw, rate=16000, bits=16, channel=1` | `format` 必填（pcm/wav/ogg/mp3，pcm 内部须 pcm_s16le），`codec` 默认 raw，`rate` 仅支持 16000，`bits` 仅 16，`channel` 默认 1 | 一致 |
| `request.end_window_size` | 由 `VOLC_ASR_END_WINDOW_MS` 配置，默认 550 | 强制判停静音阈值，单位 ms，默认 800，最小 200 | 一致（取值在允许范围） |
| `request.enable_nonstream` | 默认 true | 仅双向流式优化版支持二遍识别；开启后默认 VAD 分句 | 一致 |
| `request.show_utterances` | 默认 true | 输出分句/分词信息，供 `extract_transcript` 取 `utterances[].words` | 一致 |
| 热词表 | 原先写在 `request.boosting_table_id` | 文档层级为 `request.corpus.boosting_table_id` | 不一致，已改 |
| `request.result_type` | 未发送（服务端默认 `full`） | 默认 `full`，即 `result.text` 为整段文本 | 一致（依赖默认值，已在代码注释说明） |
| 响应 `result.text` | 直接取 `result.text` | 识别成功时填写整段文本 | 一致 |
| 响应 `definite` | `result.definite` 或 `result_type == "full"` 视为确定分句 | `definite` 是 **utterance 级**字段；文档中 `definite: true` 的分句在二遍识别结果里出现 | 不一致，已改：只在 `result.definite` 或全部 utterance 均 `definite` 时视为确定分句；任务结束仍以最后一包为准 |
| `audio_info.duration` | 未使用 | 已处理音频时长（ms） | 不影响判定，未使用 |
| 文本帧 | 忽略 | 协议只有二进制帧 | 一致（防御性忽略） |

### 仍未验证的部分

- **没有对真实端点打过合成音**。仓库与运行环境都没有 `VOLC_ASR_APP_ID` / `VOLC_ASR_ACCESS_TOKEN`，本次只做了文档与官方 Demo 的静态核对加本地假服务端单测。打开 `ASR_FAILOVER_ENABLED` 前必须用合成音对 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` 实连一次，确认：拿到首包结果、负包后收到 `is_last` 响应、错误帧能被识别。
- `end_window_ms=550` 与 `enable_nonstream=true` 的组合会触发服务端 VAD 分句，中间结果里可能出现 `definite: true` 的分句。当前实现把 `definite` 分句当作句末结果转发，同时继续等负包边界；这一行为与 FunASR 路径的 `sentence_end` 语义一致，但没有在真实端点上观察过中间 `definite` 的时序。
- 文档未给出“一条连接能否承载第二个任务”的明确禁止。即使允许，当前实现选择一段一连接，成本是多一次 WebSocket 建连，收益是任务边界与连接生命周期一致，不依赖未写明的服务端行为。

### finalize 预算：从负包到 final

`docs/acceptance.md` 的离线 A/B 在同一端点上测到：豆包 `bigmodel_async` 的**首个 partial p50 是 3.40 s**，第二组是 4.28 s，而 final p50 只有 0.09 s、p90 是 0.18 s。

旧实现从语音起点计时，1.5 s 内没有首个 partial 就降级。这个时钟量错了对象：备选（SenseVoice 离线重识别）必须等整段音频结束才能开始，说话中途判定什么都省不下来，只会把正常朗读判成失败。现在预算从 `rotate_task` 发出负包开始计时，到 primary 返回 final 为止，`ASR_FAILOVER_FINAL_RESULT_TIMEOUT_MS` 默认 1000 ms，落在 final p90 加 0.25 s 尾部宽限之上。去掉了说话中途的判定。

计量改为 `asr_finalize_ms`（负包到 final），它和预算量的是同一段等待。打开开关前按下面两步定值：

1. 用合成音在真实端点跑第一次 smoke，读 `asr_finalize_ms` 的 p50/p90。A/B 数据来自 model 1.0，生产跑的是 2.0，不能直接沿用。
2. p90 超过预算一半时上调 `ASR_FAILOVER_FINAL_RESULT_TIMEOUT_MS`。之后再观察 `asr_failover_total{reason="final_result_timeout"}` 的占比。

---
