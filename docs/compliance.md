# Memoria Compliance

本文件由 `scripts/consolidate_docs.py` 按类型归并生成。每个来源章节保留原文和来源路径。

## 来源文件

- `docs/compliance/child-audio-transcript-ephemeral.md`
- `docs/compliance/delete-domains.md`
- `docs/compliance/deletion-record-20261014.md`
- `docs/compliance/guardian-consent-draft.md`
- `docs/compliance/offer-child-rules-v1.md`
- `docs/compliance/p0-04-minor-safety-decisions.md`
- `docs/compliance/service-provider-checklist-fields.md`
- `docs/compliance/service-providers.md`
- `docs/compliance/weather-lookup-data-flow.md`

---

## 来源：`docs/compliance/child-audio-transcript-ephemeral.md`

## 儿童对话音频与转写用完即删（设计）

2026-10-13。本文只定设计，不改运行时代码。文中「现状」按仓库代码与运维手册核对。推荐方案里，对话摘要到期删除和 180 天档案清除由 `ChildLayerRetentionWorker` 执行，该 worker 只在 live 模式且 `MEMORIA_MEMORY_API_DATABASE_URL` 与 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL` 都已配置时启动，监护活跃存储未配置或读不到 `guardian_subject_activity` 表时跳过 180 天档案清除这一轮，其余目标尚未实现。隐私声明在其余目标实现并核对之前，只能引用现状表，不能把推荐表写成已经生效的事实。

产品范围是面向国内 3 到 10 岁孩子家长的陪伴机器人。工程上年龄档不区分 3–10 岁与其他未成年人（见 `docs/compliance.md` D1：未成年人统一要求家长同意）。本设计的删除规则覆盖全部未成年人，以及年龄不明、使用人未确认的 fail-closed 会话。是否在对外告知里把 14–17 岁写成与不满 14 周岁同一张表，见文末「已定，以及待最终确认」。

对外口径是：我们的服务器不存孩子的录音和原话，记忆家长看得见、删得掉。隐私声明按这个句子写，不把实现细节写成另一套产品承诺。

验收口径（本设计要满足、本文件不实现）：

- 服务端持久存储里没有儿童的对话音频。范围包括 PostgreSQL、MinIO、Redis、日志、归档证据、投递缓冲和备份能复原的对象。
- 儿童话轮的转写在对话结束后清掉。留下的只有两层记忆：对话摘要，以及单独同意的长期档案。规则在「两层记忆，以及删除核对盖住了什么」。
- 不同意长期档案时，对话照常。会话准入不读取这项同意。
- 白名单以外的字段写不进长期档案。健康、住址、学校、家庭矛盾一律不存。
- 儿童账号关闭声纹登记和自定义音色样本。成人如需使用，继续走现有的单独同意，不把儿童路径打开。
- 儿童账号的表达风格只保留语速、句长这类计数，不存用词片段。
- 删除要能核对。沿用 `services/governance/subject_deletion.py` 的按使用人删除和 `verified_empty`，并且遵守 `docs/compliance.md` 的表述纪律：核对为空只证明可删域无残留，不写成「已擦除」。

合规对照：《儿童个人信息网络保护规定》第十条要求告知儿童个人信息的存储地点、期限和到期后的处理方式；《个人信息保护法》对不满 14 周岁的个人信息要求监护人同意，存储不超过实现处理目的所必需的期限，并提供可核对的删除。下面两张表按这三项写，供隐私声明起草时摘用。

### 境内：代码能证明的和不能证明的

自有数据层与应用部署在同一套生产主机上。`docs/runbooks.md` 写明生产自 2026-10-05 起在 `110.42.235.198`，PostgreSQL、Redis、MinIO 使用该主机上的数据卷。对象存储 region 的代码默认值是 `cn-beijing`（`MEMORIA_ARCHIVE_OBJECT_REGION`、`MEMORIA_VOICE_OBJECT_REGION`，见 `services/control_api/app/config_fields/archive.py` 与 `voice.py`）。仓库里没有机房合同，也没有生产桶 endpoint 的值。

因此现状表和推荐表里的「境内」都写成：**代码默认与运维手册指向中华人民共和国境内的自有主机；对外写成「存储于境内服务器」之前，须用部署清单核对这台主机和 MinIO endpoint 的物理位置。** 本文件不代替那次核对。

离开自有主机的处理另计，不并进「境内自有存储」：

- 儿童上行音频做语音识别时发往阿里云百炼 FunASR（`FUNASR_MODEL` 默认 `fun-asr-realtime`，DashScope WebSocket 由 `DASHSCOPE_WS_URL` 配置）。`DASHSCOPE_API_KEY` 为这条 ASR 保留。`LIVE_SEARCH_QWEN_ENABLED` 与 `QWEN_OMNI_SESSION_ENABLED` 默认关闭。关掉它们不会去掉这把密钥，也不会停止 FunASR。
- 对话文本发往百炼兼容接口（默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`）或 DeepSeek，由 `llm_provider` 决定。
- 助手要说的文字发往阶跃星辰 StepAudio TTS（主用）或火山引擎豆包 TTS（备用）（默认阶跃 `wss://api.stepfun.com/v1/realtime/audio`；豆包 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`）。孩子的话若被复述，会夹在这段文字里。
- 记忆抽取读取已落库的证据文本，抽取模型同样走百炼。
- 声纹嵌入经 CampPlus HTTP 提交 `audio_base64`（`services/speaker/campplus_http.py`）。该服务的部署位置由环境决定，仓库没有写死地域。
- 天气查询在 `WEATHER_LOOKUP_PROVIDER=domestic` 且和风天气密钥与专属域名都已配置时，把允许表里的城市标准名发往该域名。出站字段见 `docs/compliance.md`。默认 `off`，这时没有天气客户端，也不发出这类请求。

这些厂商保存多久、存在哪一地，本仓库不能强制。告知里必须单独一行，不能写成「用完即删」。

### 数据从设备进来之后走哪

```text
ESP32-S3 内存中的 Opus 上行
    -> Go Media Edge 内存环（services/media_edge/session.go 的 audioRing）
    -> Python Voice Core：ASR、话轮文本、声纹 PCM 缓冲都在进程内
         |-- 有原始音频同意时，ArchiveSink 把 WAV 与转写写入加密磁盘缓冲，再 POST 到 Control API
         |-- 无原始音频同意时，只 POST 转写事件
         -> PostgreSQL archive_evidence_events.payload
         -> 监护人语料同意成立时，WAV 进入 MinIO，行进入 guardian_corpus_samples
         -> 编译器读取 payload.text，写入 memory_claims / timeline / knowledge / memory_records
         -> MemoryScope 出站写入 Redis 流 memoria:memory:events
```

设备产品路径不把对话音频写进闪存。试验固件的 `pcmarm` / `pcmdump`（`firmware/esp32/overlay/files/main/memoria/memoria_usb_command.h`）只在 PSRAM 里录一段诊断窗，经 USB 打成日志，不是产品留存。

### 现状：存储地点、期限、到期处理

下表是今天的行为。期限写的是代码里真实存在的上限；没有删除任务的，不写成「到期删除」。

| 数据 | 存储地点（系统 / 表 / 桶；是否境内） | 保留期限 | 到期后的处理方式 |
|---|---|---|---|
| 对话音频在设备上 | ESP32-S3 内存。诊断 PCM 仅试验命令，在板载 PSRAM。不在服务器。 | 播放与采集窗口；诊断窗为一次 USB 命令的长度。 | 缓冲区被下一段覆盖或断电丢失。没有上传留存。 |
| 对话音频在接入层 | Go Media Edge 进程内 `audioRing`。不落盘。与 Voice Core 同机，境内见上文核对条件。 | 当前会话的媒体环寿命。 | 会话结束或进程退出时释放。没有对象可删。 |
| 对话音频与当轮转写在 Voice Core 进程内 | 进程内存：ASR 缓冲、`ContextManager.turns`（最多约 16 条，`services/agent/src/orchestration/context_manager.py`）、声纹缓冲 `_speaker_pcm`（上限 4 MiB，`services/agent/src/runtime_speaker.py`）。同机。 | 会话存活期间。上下文只留最近若干轮，更早的轮次从内存列表去掉。 | 进程释放。不写入独立的到期任务。 |
| 儿童对话原始音频（监护人另授语料） | 仅当 `GET /v1/archive/session-raw-voice-consent` 返回 `archive_purpose=corpus_recording` 且同意未过期。Agent `ArchiveSink.publish_owner_turn`（`services/agent/src/archive_sink.py`）把 16 kHz WAV 发到 `POST /v1/archive/session-raw-audio`。对象在归档桶，前缀默认 `archive`，用途 `authorized-child-corpus`，region 默认 `cn-beijing`。索引在 PostgreSQL `guardian_corpus_samples`（`services/guardian/postgres_schema.sql`）。加密密钥版本记在行上。境内见上文核对条件。无此同意时，这条路径不写。策略对未成年人默认拒绝 `raw_audio_retention`；账户门 `raw_voice_archive` 只放成人（`services/policy/engine.py`、`services/control_api/app/account_gate.py`）。 | 同意书上的 `expires_at`，且代码强制短于或等于创建后 30 天（`services/guardian/corpus.py` 的 `MAX_CORPUS_RETENTION`）。每个未成年人同时最多 20 条未过期样本。 | `CorpusRetentionWorker` 默认每 300 秒调用 `purge_expired`：删除对象，并把行标上 `deleted_at`。行还在，标记为已删。按使用人删除会调用 `purge_minor`，同样删对象并标记。 |
| 尚未送达的音频与转写 | Voice Core 磁盘文件 `data/archive-events.spool`（可用 `MEMORIA_ARCHIVE_SPOOL_PATH` 改），Fernet 加密，默认上限 8 MiB（`services/agent/src/config.py`）。同机磁盘。盘满时先丢掉原始音频行，留下转写行。 | 直到 HTTP 投递成功并把该行从文件删掉。没有按会话、按儿童或按时钟的期限。 | 成功则从文件删除。进程在投递完成前崩溃时，下次 `replay` 会再次写入数据库或对象存储。 |
| 儿童话轮转写 | PostgreSQL `archive_evidence_events.payload` 的 `text` / `transcript` / `user_text` / `assistant_text`。写入点是 `append_session_event`（`services/control_api/app/routes/archive.py`）。有有效 `memory_retention` 监护同意时，这些字段保留。没有同意，或主体不是成人也不是已同意的未成年人时，写入前删掉这些字段，并标 `memory_retention=ephemeral_only`（`services/guardian/retention.py`）。`archive_transcript_versions` 有表、有行级安全和删除覆盖，当前服务代码没有向它 INSERT。同机 PostgreSQL。 | 有同意时：没有过期任务。`RETENTION_TTL` 的 30 天只记在 MemoryScope 记录上，不作用在证据行（`docs/compliance.md`）。无同意时：原文不入库。 | 有同意时：对话结束不清。只有按使用人删除会按血缘和 `subject_id` 删证据行，并在 `verified_empty` 里核对。无同意时：无原文可到期。 |
| 长期记忆文字 | PostgreSQL `memory_claims.value`、`timeline_entries`、`knowledge_items`（`services/archive/postgres_memory_schema.sql`），以及 `memory_records.payload`（明文，`services/memory_scope/postgres_schema.sql`）。编译器读证据里的原文（`services/archive/postgres_memory_catalog.py`）。编译器在抽取之前先解析证据主体的类别，未成年主体（`minor`、`under_14`、`14_17`）的证据从不交给抽取器，不写 claim、timeline 或 knowledge，只写一条 `ignored_reason = minor_subject_skipped` 的 ignored 回执（`services/archive/extraction_gate.py`）。证据行按自身期限保留。此前已编译的未成年人条目不会被清除，其 `value` 可以是孩子原话的一段。家长回顾接口不返回 `ephemeral_only` 的证据。同机 PostgreSQL。 | 监护同意长期记忆时，策略义务是 `RETENTION_TTL` 30 天加 `NO_MODEL_TRAINING`（`services/policy/engine.py` 中 `retention_ttl_seconds=30 * 24 * 3600`）。该期限写入 MemoryScope 的 `retention_expires_at`。归档侧的 claim / timeline / knowledge 没有独立期限。 | MemoryScope 过期后 `MemoryRecord.is_visible` 返回不可见，读口隐藏。`kind=dialogue_summary` 的行由 control-api 的 `ChildLayerRetentionWorker`（`services/memory_scope/child_layer_retention.py`）在启动时和之后每天按 `retention_expires_at` 物理删除；其他 kind 没有按 `retention_expires_at` 做物理删除的任务。`memory_records` 有禁止 UPDATE/DELETE 的触发器；物理删除只由维护角色执行，用于按使用人删除和儿童两层的到期删除。归档 claim 随证据行，对话结束不删。Redis 里已经发出的载荷不随隐藏而撤回。 |
| 会话路由 | Redis 键，内容只有会话 id、设备、账号、流世代和过期时间（`services/control_api/app/session_directory.py` 写明不含对话历史）。无 Redis 时只在进程内存。同机。 | 默认 300 秒租约，续租则顺延。 | 键到期后 Redis 删除。不含音频或转写。 |
| 记忆事件流 | Redis Stream `memoria:memory:events`，外加去重键 `{stream}:dedup:{event_id}`（`services/memory_scope/redis_outbox.py`）。载荷是记忆出站 JSON，可以含记忆正文，不含原始音频。同机 Redis。 | 流用近似裁剪，约 10 万条。去重键 2,592,000 秒（30 天）。 | 超出长度的旧条目被裁掉，不能按某个孩子撤回已经写入的条目。去重键到期由 Redis 删除。这是 `delete-domains.md` 已记录的缺口。 |
| 应用日志 | Voice Core 与媒体会话日志在稳态只记 `text_len`、原因码和计数，不记转写原文（例如 `services/agent/src/providers/funasr_stt.py`、`services/agent/src/voice_core/media_session_commit.py`），唯一例外是播放期未采取的决策行 `reason=not_stop_word` 带一句经 `redact_pii` 脱敏的 ASR final 摘要（见下文「应用日志里有什么（稳态与失败路径）」）；失败路径会打出机器人回复句和厂商正文，见下文「应用日志里有什么（稳态与失败路径）」。回复投递缓冲 `data/media-reply-delivery.spool` 的合同是不含转写和音频（`services/agent/src/voice_core/reply_delivery.py`）。自含 `logging:` 块的整栈发布起，control-api 与 voice-core-media-bridge 的 stdout/stderr 落到同机 `/var/log/memoria/<服务>.log`（目录 0700，属主 syslog，root 也能读，登录用户读不了，不进 journald），另在 Docker 本地缓存里每个容器留至多 30 MiB。此前的发布和其他容器用 daemon 默认驱动，主机上没有核对过是哪一种。生产 journald 在同一主机；运维手册记录其体积约 1 GB，空间清理没有轮转它。 | `/var/log/memoria`：一行最长约 8 天，每个服务最坏约 160 MiB 未压缩（`maxsize 20M`、`rotate 7`、`maxage 7`；`infra/memoria-logs.logrotate`；timer 停跑的日子另计）。Docker 缓存：到容器删除。journald：由主机策略决定，仓库未配置。 | `memoria-log-rotate.timer` 每小时轮转，按份数和年龄删除旧文件；停写后照样删。没有按儿童或按对话结束的删除。若某条失败路径日志带原文，它会留到上述期限，或按事故处理提前删除（`docs/runbooks.md`「容器日志落盘」）。本设计未逐条审计全部异常分支。 |
| 声纹 | 儿童账号：`speaker_enrollment` 只允许已核验的成人（`account_gate.py`）。设备内部登记 `POST /v1/speaker/enrollments/internal` 和识别 `POST /v1/speaker/classify` 都走这道门，因此儿童 PCM 到不了声纹表。策略层仍把 `voice_profile_create` 放在未成年人可授权集合里（`minor_voice_profile_enrolled`），与账户门不一致；写入口以账户门为准。成人若登记：嵌入密文在 `speaker_profiles.template_ciphertext`，样本表 `speaker_enrollment_samples` 只存 SHA-256 和质量指标，不存 PCM（`services/speaker/postgres_schema.sql`）。登记瞬间的 PCM 会发给 CampPlus。同机 PostgreSQL；嵌入服务地域未在仓库写死。 | 无自动期限。 | 撤销时把 `template_ciphertext` 置空并标 `revoked`。按使用人删除覆盖 `speaker_*`。儿童路径今天没有行可删。 |
| 自定义音色样本 | 儿童账号：`voice_clone` 只允许成人；策略 `voice_clone_use` 对未成年人硬拒绝。小程序 profile 页的录音因此进不了儿童账号。成人样本在 `voice_samples` 加音色对象桶（region 默认 `cn-beijing`），厂商音色 id 在 `voice_profiles`，`provider_region` 由配置写入，测试与字段默认是 `cn-beijing`。 | 无产品侧自动期限。`provider_expires_at` 是厂商给出的时间，不是本仓库的删除任务。 | 撤销或按使用人删除时删除对象，并请求厂商删除音色。儿童路径今天不采集。 |
| 表达风格 | PostgreSQL `speech_style_stats`（句数、字数、时长、停顿、`tic_counts` 口头禅计数）和 `persona_traits.description`（可由口头禅生成短句，如常用某个词起句）。证据指针在 `persona_evidence`，指向 `archive_evidence_events`，本身不存整段转写。未成年人只学风格，不学价值取向和决策习惯（P0-04 D2）。同机。 | 无期限。 | 不随对话结束删除。按使用人删除会擦除并在删除末尾核对。口头禅计数是孩子用语的片段，不是整句转写，但属于儿童个人信息。 |
| 辅导记录 | PostgreSQL `tutor_practice_evidence.envelope_json` 与 `tutor_commit_outbox.archive_payload_json`。评估信封的字段是分数、技能、标准 id、时长和围栏，没有转写字段（`services/tutor/authority.py` 的 `_unsigned_payload`）。`tutor_study_progress` 是秒数、活跃日、薄弱点和已掌握技能的 JSON。同机。 | 无期限。 | 不随对话结束删除。按使用人删除覆盖这些行。实现时须用测试锁住信封键集合，防止以后把原话放进去。 |
| 危机提醒与每周小结 | `guardian_crisis_events` 存证据 id、时间、话术版本，以及布尔列 `script_interrupted`（默认 false，不含孩子原话，不含触发词）。家长文案是固定句，打开时按脚本编号现查，不入库，不含原文，也不含严重程度（`services/guardian/parent_alert.py`，P0-04）。每周小结由计数聚合投影，测试要求载荷不含转写（`services/guardian/weekly_report.py`）。同机。 | 无期限。危机提醒留作监护记录。`script_interrupted` 与危机行一起保留。 | 不随对话结束删除。按使用人删除覆盖监护行，布尔列随行删除。 |
| 厂商处理中的音频和文本 | 见上文「离开自有主机」的五个终点。不是 MinIO，也不是本仓库的表。 | 本仓库没有厂商保留期限，也不能在厂商侧到期删除。 | 不能填写「到期删除」。告知必须写明会交给哪一类厂商、目的是识别、生成回复或抽取记忆，以及拒绝后的后果是无法完成对话或无法形成长期记忆。 |
| 备份里的上述副本 | 发布手册中的 `pg_dump`，以及主机上的 `/var/backups/memoria`。WAL 归档手册记为 2026-10-06 起关闭。备份在同一台生产主机上，不在单独的异地桶描述里。 | 无儿童数据到期政策。空间清理明确不动数据卷和这份备份。 | 库内删除不会自动改写已有备份。恢复库会把已删的音频或原文带回来，除非先按 `scripts/replay_subject_deletions.py` 重放删除。对话结束没有对应动作。 |

现状里和验收直接冲突的有三处：

1. 监护人授予 `corpus_recording` 之后，儿童对话 WAV 会进入 MinIO，最长 30 天。这不满足「服务端没有儿童的对话音频」。
2. 监护人授予长期记忆之后，话轮原文留在 `archive_evidence_events.payload`，对话结束不清，也没有 30 天任务。这不满足「转写在对话结束后清掉」。
3. 留下的 `memory_claims.value` 可以是原话片段，领域也包括学习进度和日常生活，宽于长期档案白名单。归档侧没有期限。`MemoryRecord.is_visible` 到期后只是读口隐藏，没有物理删除。

声纹和自定义音色在账户门上已经对儿童关闭。策略层的 `voice_profile_create` 仍对未成年人放行，只是写入口没有跟着放行。推荐方案把这一处也关上，避免以后新增入口绕过账户门。

#### 应用日志里有什么（稳态与失败路径）

自含 `logging:` 块的整栈发布起，control-api 与 voice-core-media-bridge 的日志落到主机 `/var/log/memoria`，属主是 syslog，root 也能读，登录用户读不了，一行最长约 8 天（`docs/runbooks.md`「容器日志落盘」）。下面两种答案分开写，不能合成一句「日志不含原话」。

**稳态（正常请求、正常对话）：ASR 原文只出现在一处脱敏摘要里。**

- Voice Core：FunASR 实时识别的最终结果只记 task id、句子 id、`text_len`、`begin_ms`、`end_ms`（`services/agent/src/providers/funasr_stt.py` 第 817–824 行）。会话模块只记 `text_len`、`partial_text_len`、`partial_chars`。`FUNASR_WS_TRACE` 默认关，开了也只记 `text_len`。SenseVoice 的告警只带 JSON 解析位置，或查询串只有 `format`、`sample_rate`、`language` 的 URL，不带转写；这一条是从标准库与 httpx 的行为推断，没有实跑。
- Control API：正常请求不打日志（`--no-access-log`）。
- 稳态上有三处例外：
  1. 危机话术播放时，Voice Core 记一行 `fixed_safety_playout reply_template_id=… script_version=… tts_text=<话术全文>`（`services/agent/src/voice_core/provider_adapter.py` 第 620 行，`services/common/fixed_safety_scripts.py` 第 122–128 行）。话术是仓库里的固定稿，不是孩子说的话；但这一行带时间戳，等于记下了「这一刻播过危机话术」。
  2. Control API 的遥测行带客户端传来的 `error_type`、`error_code`、`error_param`，各截到 80 字符（`services/control_api/app/routes/session.py` 第 330–332 行）。这三个字段内容不受约束，客户端可以塞进一小段识别文字或令牌片段。
  3. 播放期未采取的决策行 `media playback-stop not taken … reason=not_stop_word … asr_text=<长度>:<文本>` 带一句 ASR final：手机号、身份证、银行卡、邮箱、密钥、地址换成 `[手机号]` 等标记，其余字符保留，最长 200 字符（`services/agent/src/voice_core/media_session_playback_stop.py`，`services/common/redaction.py`）。用途是 soak 时判断「停」被识别成了哪个字；同一行的其他 reason 记 `asr_text=-`，不带文本。

**失败路径（厂商报错、超时、任务崩溃）：下列行可能带原话或厂商返回的正文。** 应用日志按约束不改，这里只如实列出：

- 语音合成失败时，Voice Core 打出交给 TTS 的整句机器人回复：`services/agent/src/providers/tts_stream.py` 第 294 行的 `no audio frames were pushed for text: …`、第 314–319 行的重试告警，以及 `services/agent/src/voice_core/media_session_output_dispatch.py` 第 662–666 行 `logger.exception` 的 traceback。机器人的回复可以复述孩子的原话。
- 厂商错误正文不经清洗：`services/agent/src/providers/openai_chat.py` 第 231–236 行（`APIStatusError` 带响应体），CosyVoice 与豆包的 `error_message`，`services/agent/src/duplex_runtime.py` 第 1365–1369 行的后台任务错误。
- Control 响应正文最多 200 字符：`services/agent/src/session_recap_sink.py` 第 153–157 行（请求里的 `summary_text` 已过 `redact_pii`，只有 Control 回显它才会进日志），`services/agent/src/heartbeat.py` 第 125–130、161–171 行。
- `FunASR connect failed: …`（`funasr_stt.py` 第 688 行）没有经过 `sanitize_diagnostic_text`。websockets 握手错误是否带请求头，没有核对。
- FunASR 任务失败的消息经过 `sanitize_error_message`（去掉带标签的密钥、`redact_pii`、截到 400 字符）。厂商错误若引用一句不含 PII 形态的原话，仍会整句打出。
- Control API：`services/control_api/app/routes/multi_subject.py` 第 326–340 行以 `message=%r` 打出 `PersistentSessionDenied` 的原文，代码注明它可能是 PostgreSQL 错误文本；`routes/session.py` 第 634–638 行、`routes/media.py` 第 497–501 行以 `reason=%s` 打出同一串。asyncpg 是否把 SQL 或绑定值带进这段文字，没有核对。`routes/device_control.py` 第 368 行的 httpx 状态错误带请求 URL；边缘令牌在请求头里，不在 URL 里。
- 两个服务的 `logger.exception` 打 traceback 和异常消息，不带局部变量。
- 发布 `env` 步的提供方冒烟失败时打 `provider_smoke_test FAIL: <异常类型>: <异常消息>`（`scripts/provider_smoke_test.py` 第 434 行），可能带厂商响应正文；这一行也写进 `voice-core-media-bridge.log`。备份探针失败打的是 `Doubao backup probe: DEGRADED (<异常类型>: <异常消息>)`，同样是厂商正文，同样落盘。

**密钥。** 厂商 API key 和内部令牌都走请求头，没有一处应用日志格式拼接请求头，也没有找到打印 bearer token、Authorization 头或厂商 key 的日志行。剩下三条路：失败路径上的厂商与 Control 响应正文；上面 80 字符的遥测字段；任何把第三方 logger 调到 INFO 或 DEBUG 的改动。微信调用把 `secret` 和 `access_token` 放在查询串里（`services/control_api/app/wechat_auth.py` 第 119–125 行，`guardian_push.py` 第 167–170 行）。Control API 今天不输出 httpx 的 INFO 行，所以不打这些 URL；一旦调到 INFO 就会打。Voice Core 的 httpx 可能在 INFO 打出请求行（`DomesticWeather` 是否在生产里构造、从而把它压低，未核对），这类行只带方法、URL 和状态码；按仓库检索，Voice Core 调用的 URL 不把密钥放在查询串里（检索结论，没有实跑）。因此不加 `--log-config`，不去掉 `--no-access-log`，不调高 `LOG_LEVEL`。Voice Core 调到 DEBUG 可能让 websockets 打出带 Authorization 的握手请求头（未读库代码）。

**落盘之后。** 以前，这些失败路径行只活到容器下一次重建（日志驱动是 daemon 默认，主机上没有核对过是哪一种）。现在它们在 `/var/log/memoria` 里最长约 8 天，属主是 syslog，root 也能读，登录用户读不了，不进 journald，也不进 `/var/log/syslog`。发现某行带孩子原话时按事故处理：停写、删除文件并核对、重建容器清掉 Docker 缓存、再核对，步骤见运维手册「容器日志落盘」的「事故处理」。

### 两个方案

两者都只处理 `subject_category` 为 `minor` 的话轮，以及年龄不明时已经带 `DO_NOT_PERSIST` 的话轮。成人原始音频同意、成人声纹、成人音色样本保持今天的单独同意，不进这道门。

#### 方案 A：入口不落盘

儿童对话音频不进入任何持久介质。儿童话轮原文只活在 Voice Core 进程里，供本轮识别、回复和摘要使用；写入 PostgreSQL、对象存储、Redis 和磁盘缓冲之前先去掉。对话结束时丢掉进程内副本。长期记忆只接收一条有长度上限的摘要。

```python
SubjectClass = Literal["minor", "unknown", "adult"]

@dataclass(frozen=True)
class TurnFacts:
    session_id: str
    turn_id: int
    subject_id: str | None
    subject_class: SubjectClass
    pcm: bytes          # 仅进程内
    transcript: str     # 仅进程内

ArchiveField = Literal["appellation", "preference", "pet_name"]

@dataclass(frozen=True)
class DialogueSummary:
    subject_id: str
    text: str           # 概括，不引原话；工作上限 200 字，待最终确认
    source_event_id: str

@dataclass(frozen=True)
class ArchiveFact:
    subject_id: str
    field: ArchiveField
    text: str           # 至多一句话，不引原话

class ChildTurnBoundary(Protocol):
    def accept_persistent_audio(self, turn: TurnFacts) -> None: ...
    def durable_event(self, turn: TurnFacts) -> dict[str, object]: ...
    def summaries(self, turn: TurnFacts) -> tuple[DialogueSummary, ...]: ...
    def archive_facts(self, turn: TurnFacts) -> tuple[ArchiveFact, ...]: ...
    def on_session_end(self, session_id: str) -> None: ...

def accept_persistent_audio(turn: TurnFacts) -> None:
    if turn.subject_class in {"minor", "unknown"}:
        raise ChildAudioForbidden(turn.session_id)
    # 成人仍走现有的 raw_voice_archive 同意

def durable_event(turn: TurnFacts) -> dict[str, object]:
    event = metadata_only(turn)  # 类型、时间、说话人类别、围栏、保留标志
    if turn.subject_class in {"minor", "unknown"}:
        return event  # 不带 text / transcript / user_text / assistant_text / audio
    return event | {"text": turn.transcript}

def summaries(turn: TurnFacts) -> tuple[DialogueSummary, ...]:
    if turn.subject_class == "unknown" or not guardian_memory_consent(turn):
        return ()
    drafted = paraphrase(turn.transcript)  # 本进程内；不把原话抄进 text
    return tuple(item for item in drafted if len(item.text) <= 200)

def archive_facts(turn: TurnFacts) -> tuple[ArchiveFact, ...]:
    if not guardian_archive_consent(turn):
        return ()  # 没有这项同意也不影响对话
    return tuple(
        fact for fact in extract_whitelist(turn.transcript)
        if fact.field in {"appellation", "preference", "pet_name"}
        and one_sentence(fact.text)
        and not quotes_speech(fact.text)
        and not forbidden_topic(fact.text)  # 健康、住址、学校、家庭矛盾
    )

def on_session_end(session_id: str) -> None:
    drop_process_buffers(session_id)  # PCM、转写、ContextManager
    refuse_spool_lines(session_id, kinds={"child_audio", "child_transcript"})
```

调用点有两个，类型相同：`ArchiveSink.publish_owner_turn` 在组包前调用 `accept_persistent_audio`；`append_session_event` 在 `record` 前调用 `durable_event`。摘要和档案事实在同一进程、同一次话轮收尾时写入，编译器不再回读证据里的原文。对话是否开始只看监护关系下的聊天同意，不看长期档案同意。

`corpus_recording` 不再作为儿童对话音频的写入理由。同意种类可以留在监护表里供历史行识别，但会话接口对未成年人一律返回 `allowed: false`。

#### 方案 B：先落盘，会话结束再集中清除

保持今天的写入：转写进证据行，语料同意成立时 WAV 进 MinIO 和 spool。新增按会话的清扫，在会话进入终态后删除这些副本，并写出核对收据。

```python
@dataclass(frozen=True)
class SweepReceipt:
    session_id: str
    audio_objects_deleted: int
    transcript_fields_cleared: int
    spool_lines_dropped: int
    verified_session_empty: bool

class SessionEndSweep(Protocol):
    async def persist(self, turn: TurnFacts) -> None: ...
    async def on_session_end(self, session_id: str) -> SweepReceipt: ...

async def persist(turn: TurnFacts) -> None:
    await archive_sink.publish_owner_turn(event_from(turn), pcm=turn.pcm, sample_rate=16_000)
    # 与今天相同：转写必达，音频在同意成立时达

async def on_session_end(session_id: str) -> SweepReceipt:
    await wait_until_compiled(session_id)          # 否则摘要来不及生成
    audio = await delete_child_audio(session_id)   # MinIO + guardian_corpus_samples
    fields = await blank_transcript_fields(session_id)
    spool = drop_spool_lines(session_id)
    redis = await redact_stream_payloads(session_id)  # 已裁剪的历史条目做不到
    ok = await verify_session_empty(session_id)
    return SweepReceipt(session_id, audio, fields, spool, ok)
```

清扫必须晚于记忆编译，早于备份无法覆盖的窗口则没有意义。失败要可重试，并且不能把成人的同一张表清掉。

#### 对比

|  | 方案 A 入口不落盘 | 方案 B 会话结束清扫 |
|---|---|---|
| 接口 | 一个 `ChildTurnBoundary`，两个调用点。函数要么拒绝音频，要么返回去掉原文的事件。没有新的跨服务事务。 | 一个 `SessionEndSweep`，但要认识每一种已经写过的介质，并和编译器、spool 重放、Redis 裁剪排序。 |
| 出错面 | 摘要若在进程崩溃前没写完，这一轮没有长期记忆。音频和原文没有落盘，崩溃不会把它们留在库里。spool 若混入儿童行，重放时拒绝而不是修复。 | 崩溃发生在写入和清扫之间时，音频和原文已经在 PostgreSQL、MinIO、spool、备份和 Redis 里。清扫漏一种介质就会验收失败。证据行上的原文被清空后，编译器重试会失去输入。Redis 已发出的条目删不掉。备份不会跟着变。 |
| 和验收的关系 | 对话进行中，持久存储里也没有儿童音频。转写不进入持久存储，所以「对话结束后清掉」落在进程内存上，边界清楚。 | 对话进行中服务器上有音频和原文。「用完即删」依赖清扫成功。验收要证明所有介质都清了，包括清扫失败后的重试。 |
| 对现有记忆链路 | 编译器改吃 `DialogueSummary` 和可选的 `ArchiveFact`，不再读 `payload.text`。今天的未成年人领域白名单（学习、偏好、日常生活）比长期档案宽，不能再当写入规则。 | 编译器可以暂时不动，但必须在清扫前跑完，两套期限缠在一起。 |

推荐方案 A。儿童音频用「不写入」满足「服务器上没有」；转写用「不进入持久存储、进程随会话释放」满足「对话结束后清掉」。方案 B 的清扫列表会跟着每一种新缓存增长，而且补不上备份和已经裁剪不掉的 Redis 历史。方案 A 仍保留一个很窄的失败关闭动作：发现 spool 或对象键属于儿童音频或儿童转写时，删除并拒绝重放，不把它们当成正常路径。

### 推荐方案下的告知表

下表是方案 A 实现之后可以放进隐私声明的目标表述。实现和核对完成之前，它不是现状。

成人声纹、成人自定义音色、成人原始音频不在本表。它们继续各自的单独同意；儿童账号不提供这些开关。

| 数据 | 存储地点（系统 / 表 / 桶；是否境内） | 保留期限 | 到期后的处理方式 |
|---|---|---|---|
| 孩子说话的录音 | 只在机器人体内和服务器内存中处理，用于听清这一句并生成回复。不写入 PostgreSQL、对象存储、Redis、磁盘缓冲或日志。服务器若在境内，以部署清单为准，条件与现状节相同。 | 这一轮对话期间。 | 对话结束（道别、连续无语音进入待机、连接断开或进程退出）时从内存释放。不保留、不备份。 |
| 交给语音识别厂商的录音 | 阿里云百炼 FunASR，用于把这一句转成文字。不进入我们的数据库。 | 我们的服务器不留这份录音。厂商保存多久按合同。这一句待最终确认，见文末。 | 我们的服务器上没有副本，所以没有我们自己的到期删除。合同未写明期限前，不能写成厂商也会到期删除。 |
| 这一轮孩子说的原话，以及机器人复述进去的原话 | 只在生成回复和生成摘要的服务器内存中。不写入 `archive_evidence_events` 的文本字段，不写入 `archive_transcript_versions`。正常运行时只在播放期未采取的决策行写一句脱敏摘要（见「应用日志」一行）；语音合成出错时，服务器日志会记下机器人那一句回复，其中可能复述孩子的原话（见「日志」一行）。 | 这一轮对话期间。出错时写进日志的那一句，最长约 8 天。 | 对话结束时从内存删除。若磁盘缓冲里出现这类原文，重放时丢弃该行，不入库。日志里的那一句到期由轮转删除，或按事故处理提前删除。 |
| 交给对话模型的文字 | 百炼或 DeepSeek，用于生成当轮回复。可含这一轮的原话。 | 我们的服务器不留原文。厂商按合同。待最终确认。 | 同语音识别。 |
| 对话摘要 | 家长同意「记住最近的对话」之后，写入 PostgreSQL `memory_records`，`kind=dialogue_summary`。一条不超过 200 字，是概括，不引用原话。这条字数上限待最终确认。同机数据库，境内以部署清单为准。Redis 事件流只携带摘要的出站副本。家长能在小程序里看见并删除。 | 30 天。这是物理删除的期限，不是读口隐藏。 | 到期后由维护角色执行 `DELETE`（control-api 的 `ChildLayerRetentionWorker` 启动时跑一次，之后每天一次；须先设置 `app.memory.subject_erase`，否则 `memory_records_immutable` 会拒绝），只删已到期的摘要行，然后计数剩余的到期摘要，不为零即失败。家长提前删除走同一条物理删除。Redis 里尚未被约 10 万条裁剪掉的副本，告知里写明会随裁剪消失。 |
| 长期档案 | 另一项告知、另一项同意。写入同一张 `memory_records`，`kind=child_archive`，字段只允许称呼、喜好、宠物名。每条至多一句话，不引用原话。健康、住址、学校、家庭矛盾不写。不同意时不写这些行，对话照常。同机数据库。家长看得见、删得掉。 | 没有 30 天上限。保留到家长删除、解绑，或连续 180 天没有对话且家长也没有打开小程序。清除前 30 天、7 天各提醒一次。另外每 90 天提醒复核一次，家长不确认也不删除。 | 清除时只物理删除 `kind=child_archive` 的行，并计数确认为零。180 天无活跃的清除由 `ChildLayerRetentionWorker` 每天执行；读不到 `guardian_subject_activity` 时这一轮不清除档案。清除前的提醒今天还没有发送，清除不等提醒。整份按使用人删除仍走现有 `verified_empty`。解绑必定清除长期档案：2026-10-21 起解绑没有保留开关（issue #283）。 |
| 没有对话摘要同意时的对话 | 内存。 | 这一轮对话。 | 结束即丢。不写摘要，也不写长期档案。对话照常。 |
| 声纹 | 不采集，不存储。没有对应的表行、对象或厂商音色嵌入。 | 不适用。 | 不适用。账号关闭此功能。 |
| 自定义音色样本 | 不采集，不存储。没有 `voice_samples` 行，也没有音色桶对象。 | 不适用。 | 不适用。账号关闭此功能。 |
| 表达风格 | PostgreSQL `speech_style_stats` 只保留语速和句长能算出来的计数：句数、字数、说话时长、停顿。儿童行的 `tic_counts` 保持空对象，不写入用词。`persona_traits.description` 不再写入含孩子用词的短句。同机。 | 账号存续期间，直到家长撤销学习或删除该使用人。 | 撤销或按使用人删除时擦除。`speech_style_stats` 已在 `persona.forget_subject` 和 `verified_empty` 的人格计数里。 |
| 学习进度与每周小结 | PostgreSQL 辅导进度与监护小结。内容是时长、技能和趋势，不含原话。同机。 | 账号存续期间。 | 按使用人删除时擦除并核对。 |
| 危机提醒 | PostgreSQL 监护危机行，以及发给家长的固定微信文案。不含孩子原话。同机；微信侧按微信规则投递。 | 作为监护通知记录保留，直到该使用人被删除。 | 按使用人删除时擦除并核对。固定文案本身不含儿童原话。 |
| 日志 | 服务器日志在正常运行时只记长度、原因和计数，不记原话，不记录音。出错时（语音合成失败、厂商报错），日志可能记下机器人那一句回复（可能复述孩子的原话）或厂商返回的错误文字。两个对话服务的日志文件在同一主机的 `/var/log/memoria`，登录账号读不了，主机管理员和系统日志账号能读。 | 最长约 8 天，按大小和天数轮转。应用不延长它。 | 到期由轮转自动删除。若核对发现某条日志含孩子原话，按事故处理：停写、删除日志文件并核对、清掉 Docker 本地缓存、再核对（运维手册「容器日志落盘」）。 |
| 备份 | 数据库备份在同一台主机。方案 A 上线之后，新备份不应含儿童对话音频和话轮原文。 | 与主机备份策略相同。 | 上线前已有的语料、原话和含这些内容的备份，工作决定是物理删除并核对。这一项待最终确认。恢复流程今天会把删除前的行带回来，所以清理必须包含备份，并保持删除重放。 |

第十条还要求说明目的、方式和拒绝的后果。与本表配套的句子是：录音和原话只为完成当轮对话；对话摘要只为家长已同意的最近对话记忆；长期档案是另一项同意，拒绝后对话照常，只是机器人不再记住称呼、喜好和宠物名；拒绝把录音交给识别厂商则无法进行语音对话。家长可以在小程序里看见并删除这两层记忆。安全保障沿用现有做法：对象加密、数据库行级隔离、内部写入令牌、按使用人删除收据。

### 两层记忆，以及删除核对盖住了什么

产品已定：记忆分成对话摘要和长期档案。摘要 30 天后物理删除，可核对，不是 `MemoryRecord.is_visible` 那种读口隐藏。长期档案只存白名单字段，单独告知、单独同意。

| 层 | 写入条件 | 内容 | 期限 | 清除 |
|---|---|---|---|---|
| 对话摘要 | 沿用绑定页「长期记忆」同意。不同意则不写，对话照常。 | 概括，不引原话。工作上限每条 200 字，待最终确认。 | 30 天 | 到期物理删除；家长也可以提前删 |
| 长期档案 | 新的同意项。不同意则不写，对话照常。 | 称呼、喜好、宠物名。每条至多一句话，不引原话。健康、住址、学校、家庭矛盾不存。 | 家长删除、解绑，或连续 180 天无对话 | 清除前 30 天和 7 天各提醒一次。家长打开小程序算恢复活跃，180 天重新计算。每 90 天另提醒复核一次，不确认不删除 |

活跃的定义是：有一次儿童对话，或家长打开小程序。两者都把 180 天的时钟清零。今天没有这个时钟。解绑接口是 `POST /v1/devices/{device_id}/binding/unbind`（`services/control_api/app/routes/identity_lifecycle.py`）。2026-10-21 起解绑一律清除长期档案和对话摘要，并清除使用人的昵称和年龄段；请求体里已没有保留开关，旧客户端仍发 `purge_subject_data` 会被忽略。

两层都写在已有的 `memory_records` 里，用 `kind` 区分，不新建表。原因是这张表已经在按使用人删除里面。

`memory_subject_erase`（`services/memory_scope/postgres_schema.sql`）会物理删除该使用人的 `memory_records`、`memory_status_events`、共享提案和投票、`memory_capture_evidence`，并清空 `memory_outbox` 与 `memory_audit_events` 里仍含该使用人的载荷。`memory_subject_remaining` 对上述表计数。`subject_deletion` 在 `memory_scope_erased` 之后调用剩余计数，非零就在 `verified_empty` 失败（`services/governance/subject_deletion.py`）。因此：

- 家长删除整个使用人时，只要长期档案和摘要都在 `memory_records`，它们已经在现有删除和 `verified_empty` 范围内。不需要新表，也不需要给 `verified_empty` 再加一张表。
- 只删长期档案、留下未到期摘要时，现有 `memory_subject_erase` 做不到。它按使用人整份删除，不按 `kind`。180 天清除和解绑若只动档案，要另写一条按 `kind=child_archive` 的删除，并在删除后把该层剩余行数计为零。这不是把 `verified_empty` 再跑一遍，因为整份核对会把还没到期的摘要也算成残留。
- 30 天摘要同理：只删到期的 `kind=dialogue_summary`。`memory_records_immutable` 在 `app.memory.subject_erase` 未指向该使用人时会拒绝任何 `DELETE`。到期任务必须走这条维护角色路径，不能只把 `is_visible` 改成 false。删完之后计数「到期摘要仍在的行」，为零才算这一轮核对通过。
- 归档侧的 `memory_claims`、`person_entities`、`knowledge_items`、`timeline_entries` 会随证据血缘被 `subject_archive` 删除，并出现在剩余计数里。儿童内容如果还写进这些表，整份删除盖得住，但 30 天和 180 天的分层删除盖不住。所以儿童摘要和档案只进 `memory_records`，编译器不再为儿童写入 claim、时间线、知识条目和人物别名。
- 不在核对里、也不能假装被核对的：Redis 流 `memoria:memory:events` 已发出的载荷；`memory_shared_membership_snapshots`（`memory_subject_erase` 和 `memory_subject_remaining` 都没有它）；主机上的 `pg_dump`。长期档案不要写入成员快照。告知里写明 Redis 裁剪。备份按文末待确认项在上线前清理。
- `memory_audit_events` 在整份删除时被清空载荷，并留下一条不含正文的 `memory.subject.erased` 行。剩余计数只看载荷是否还非空，所以这条空行不会让 `verified_empty` 失败。分层删除不要把档案正文写进审计载荷。

白名单是写入门，不是删除核对。`verified_empty` 只能证明行没有了，不能证明剩下的行没有健康或学校。验收「白名单外字段写不进档案」要单独测写入拒绝：学校、住址、健康、家庭矛盾、原话引用、超过一句话，都不得插入 `kind=child_archive`。

表达风格：`speech_style_stats.tic_counts` 今天存口头禅计数（`services/persona/postgres_engine.py` 读取并写回该 JSON）。儿童行停止写入用词，只累加句数、字数、时长和停顿，这些数可以算出语速和句长。`persona_traits.description` 不再为儿童生成含用词的短句。整份删除时，这张表已经由 `persona.forget_subject` 删掉，并由 `remaining_subject_rows` 计入 `verified_empty`。

提醒是新的，今天还没有发送；180 天清除不等提醒，照常执行。清除前 30 天和 7 天各一次，每 90 天复核一次。复核没有回应不删除。小程序打开要写一条活跃时间，否则 180 天只能看见设备对话。这些提醒走现有监护通知通道，文案不含原话。

### 波及与如何确认没有弄坏

方案 A 不动设备协议，也不改成人声纹、成人音色、成人原始音频的同意模型。

| 已有功能 | 会碰到什么 | 怎样确认没有弄坏 |
|---|---|---|
| 声纹门禁 | 未成年人 `voice_profile_create` 从策略允许集移出，与 `speaker_enrollment` 的成人门对齐。成人登记、识别、撤销仍走 `services/control_api/app/routes/speaker.py`。 | 现有成人声纹测试保持通过。加一条：未成年人内部登记和识别返回与账户门相同的拒绝码，且 `speaker_profiles` 无新行。 |
| 自定义音色 | 儿童路径今天已被 `voice_clone` 拒绝。方案 A 不新开入口。 | `services/control_api/app/routes/voice.py` 的未成年人用例仍为 403。成人登记、撤销和厂商删除用例不改断言。 |
| 档案与回顾 | 儿童证据不再带原话。家长看见的是摘要和长期档案，不是话轮原文。 | `test_archive_subject_scope.py`：有记忆同意的儿童话轮，证据行无 `text`。回顾接口不出现原话。不同意长期档案时，会话仍能开始，且 `kind=child_archive` 为零行。 |
| 记忆生成 | 编译器在抽取之前先解析证据主体的类别。未成年主体的证据不交给抽取器，因此不写入 `memory_claims`、时间线或知识条目（`services/archive/extraction_gate.py`）。 | 投影测试改为：学校、住址、健康、家庭矛盾、原话引用、超长句子都写不进档案；200 字以上的摘要被拒绝；无摘要同意时零条摘要。 |
| 表达风格 | 儿童行停止写 `tic_counts` 里的用词，也停止写含用词的 `persona_traits.description`。句数、字数、时长、停顿保留。 | 人格测试仍断言未成年人 `style_only` 不产生价值取向和决策习惯。另断言儿童 `tic_counts` 为空。 |
| 删除核对 | 整份按使用人删除已经覆盖 `memory_records`。要补的是按 `kind` 的到期删除和 180 天删除，以及删除后的分层计数。语料对象和证据文本键仍要纳入儿童会话核对。 | `test_subject_deletion.py` 保持整份 `verified_empty`。另加：只删档案后摘要还在、到期摘要计数为零、白名单外写入被拒绝。不把分层计数叫成现有的 `verified_empty`。 |
| 辅导与危机 | 信封不含原话，方案 A 不改字段。归档投影若把原话抄进辅导 outbox，应在写入边界被去掉。 | `services/tutor/tests/test_evidence.py` 锁住信封键。危机用例继续断言家长文案无原文。 |
| 评测与验收脚本 | 真机 findings 用 `speech.utterance_finalized` 的原文做相似度。实现后生产库不再有这段原文，脚本不能再向儿童会话要原文。 | 脚本改为断言儿童事件无文本字段、语料对象数为 0，相似度改在测试夹具里做，不读生产儿童库。不改历史 `docs/acceptance/` 收据。 |
| 文档预算 | 本文件放在 `docs/compliance/`，属于 `tests/test_documentation_budget.py` 允许的前缀。 | `uv run pytest tests/test_documentation_budget.py`。不新增其他文档类别。 |
| 既有 30 天记忆决定 | 30 天仍然是对话摘要的期限，但从读口隐藏改成物理删除。长期档案不使用这 30 天。 | 策略测试里未成年人记忆义务仍有 `RETENTION_TTL` 与 `NO_MODEL_TRAINING`。档案行没有 30 天到期。 |
| 解绑 | 已实现（issue #283）：解绑一律清除长期档案、未到期摘要和 `identity_persons` 里的昵称与年龄段，不再有保留开关。 | `test_unbind_deletes_every_child_layer_without_a_data_switch` 锁住解绑后各层行数为零；`test_unbind_clears_the_child_and_ignores_the_legacy_data_switch` 锁住旧开关被忽略。 |
| 小程序活跃 | 打开小程序要记下时间，用来重置 180 天，并承接 30 天、7 天和 90 天提醒。今天没有这个时钟。 | 只打开小程序、没有新对话时，清除日向后推。90 天不回应时档案还在。提醒文案不含原话。 |

### 工作量

实现集中在一条边界上，不改 ESP32 协议，也不改 Go Media Edge 的环缓冲。

- 新增 `ChildTurnBoundary` 的判定与两个调用点：`ArchiveSink.publish_owner_turn`、`append_session_event`。儿童和年龄不明的会话不再接收持久音频，事件载荷去掉文本字段。
- 关闭未成年人的 `corpus_recording` 会话写入。这是工作决定，待最终确认。表和历史行留到上线前清理。
- 记忆编译改为消费 `DialogueSummary` 与 `ArchiveFact`。儿童不再写入 claim、时间线、知识条目。摘要概括且不超过 200 字；档案只收三个字段。字数和「不引原话」待最终确认，实现前按这个上限做。
- 摘要到期删除和档案分层删除各一条维护角色语句，删完计数。整份按使用人删除不改语义。
- 解绑固定清除长期档案。小程序打开写入活跃时间。30 天、7 天、90 天提醒走现有监护通知，不含原话。
- 策略把 `voice_profile_create` 从未成年人可授权集合移出。账户门保持成人专用。
- 儿童 `tic_counts` 不再写入用词；风格描述不再含用词片段。
- 会话结束时释放进程内 PCM 和转写。spool 重放遇到儿童音频或儿童原文则丢弃。
- 上线前物理删除并核对已有语料、证据原文和含这些内容的备份。待最终确认；确认前不把清理写成已经授权。
- 测试改动落在归档主体范围、语料、记忆投影、白名单拒绝、分层删除计数、声纹拒绝、解绑。文档预算测试只因本文件位于 `docs/compliance/` 而继续通过。

不在范围内：厂商合同谈判、隐私声明排版、成人路径改造、设备协议。小程序只增加同意开关、档案列表、删除按钮和活跃上报，不在本文件里画界面。

### 风险

- 语音识别和对话模型仍会在当轮收到录音或原话。我们的服务器没有副本，不等于厂商没有副本。告知用「我们的服务器不留、厂商按合同」，不用「全链路用完即删」。这一句待最终确认。
- 200 字和不引原话会让第二天的回忆变短。放宽成原话摘录就会退回现状。实现时不得自行放宽。
- `memory_records` 的触发器会拒绝普通 `DELETE`。到期任务若只改 `is_visible`，库和备份里仍有正文。物理删除必须带上维护角色的 erase 目标。
- 分层删除若误调用整份 `memory_subject_erase`，会把未到期摘要和档案一起删掉。
- 上线前已经入库的语料和原文还在对象存储、证据表和旧备份里。只改写入路径不会把它们清掉。工作决定是上线前物理删除并核对，待最终确认。
- Redis 流不能按孩子删除。摘要出站之后，最坏会留到约 10 万条裁剪。告知要写这句话。
- 日志在稳态只记长度。失败路径会把机器人的回复句和厂商正文打进日志，方案 A 的边界罩不住；日志落盘之后，这些行在 `/var/log/memoria` 最长留约 8 天。实现时要有一条扫描测试：儿童会话的日志记录不含 `transcript` 字段值。
- 生产主机和桶是否在境内，仓库不能证明。告知写错地域是合规风险，不是实现风险。

### 已定，以及待最终确认

已经写进本设计、不再作为选项的：

- 记忆分两层。对话摘要 30 天后物理删除并可核对。长期档案只存称呼、喜好、宠物名，每条至多一句话，不引原话；健康、住址、学校、家庭矛盾不存。
- 长期档案单独告知、单独同意。不同意时对话照常。
- 长期档案在家长删除、解绑，或连续 180 天无对话且家长未打开小程序时清除。清除前 30 天和 7 天各提醒一次。家长打开小程序算恢复活跃。每 90 天提醒复核一次，不确认不删除。
- 整份按使用人删除时，这两层都在现有 `memory_records` 的删除和 `verified_empty` 里。只删其中一层要补按 `kind` 的删除和计数，见上一节。
- 儿童表达风格只留语速、句长一类计数，不存用词片段。
- 对外只说：我们的服务器不存孩子的录音和原话，记忆家长看得见、删得掉。

下面四项按已定建议写进上文的表和工作量，实现前待 Monkey In 最终确认。确认前不据此清理生产数据：

1. 废止儿童语料录音。`corpus_recording` 不再成为儿童对话音频的写入理由。
2. 对话摘要每条不超过 200 字，概括，不引用原话。
3. 已入库的儿童语料、证据原文，以及含这些内容的备份，在上线前物理删除并核对。恢复会把旧行带回来，所以备份必须一起处理。
4. 告知写「我们的服务器不留、厂商按合同」。不写「全链路用完即删」。

下面几件不改变上面的结构，仍要单独定：

- 告知的年龄范围。工程按全部未成年人和年龄不明处理。对外是只写不满 14 周岁，还是与 D1 一致写全部未成年人。
- 解绑时未到期的对话摘要是否跟随长期档案一起删除：已按 issue #283 定为一起删除，解绑不再有第三态。
- 会话结束是否就用现有待机条件：连续无主人语音、明确道别、断开。崩溃由不落盘覆盖。
- 境内表述是否在核对机房和桶 endpoint 之前就写进隐私声明。

口头禅不再是待决项。30 天作为摘要期限也不再是待决项；变的是到期后必须物理删除。

### 不在本文件里做的事

不提交实现、不改策略常量、不清理生产数据、不改 `HANDOFF.md` 或 `TODOLIST.md`。实现时以本文的方案 A 和拍板结果为准；与 `delete-domains.md` 冲突时，删除核对的表述纪律优先。

---

## 来源：`docs/compliance/delete-domains.md`

## 删除域与 seal 契约

本文是从 `HANDOFF.md` 迁出的合规参考，保留 2026-09-20 删除范围结论和 17 表 seal 契约分析。这里记录已验证边界与已知缺口，不把 `verified_empty` 写成“已擦除”，也不把未授权的窄片实现误报为完成。

### 2026-09-26 按使用人删除覆盖面复核（当前权威）

按使用人删除（`services/governance/subject_deletion.py`）的覆盖面按存储逐项复核，结论取代下方 09-20 结论中已过时的部分（`memory_scope` 已由 P1-11 的维护角色函数按使用人擦除，不再是"无法物理删除"）。

- **已删除并在 `verified_empty` 核对为空**：归档证据（按血缘与 `subject_id`）、转写、证据对象行、处理 outbox、编译收据、所有以 `source_event_id` 关联的投影；监护/导师与危机行；语料活行；`memory_scope` 记录、状态事件、共享提案与投票、采集证据（outbox/审计载荷清空）；人格（特征、风格统计、版本，2026-09-26 起按使用人记账并在删除末尾核对）。
- **向量域（2026-10-24 补充）**：`memory_vector_documents` 是搜索投影的一部分，不是独立留存域。删除时按血缘走 `_delete_order`，条件 `source_event_id IN :ids OR CAST(item_id AS TEXT) IN :items`；末尾的 `verified_empty` 另有两条按主体的计数核对（`about_subject` 直接计数，以及经 `memory_claim_subjects` 反查 `item_id` 计数），都进 `verified_empty` 结果。撤回与修正单条记忆时的向量清理走 `services/archive/memory_vector_cleanup.py`（#326，2026-10-19 合入）：撤回删除向量并把搜索行置 `status='retracted'`，重新确认恢复同一行的向量，修正删除旧措辞的每份副本并在同一事务内为改正后的文本重新嵌入。恢复重放（`replay_subject_deletions.py`）在正常删除之后再跑一次 `delete_subject_vectors` 的按证据 join，只为覆盖「从早于 #326 的备份恢复、向量又指向已删除证据」这一种情况；对证据行已不存在的孤立向量它无能为力，留待投影重建。
- **已删除但未在末尾复核**：归档对象（每次删除后立即回读确认）、语料对象、身份脱敏、会话终止、按 id 收集的投影（通过 `source_event_id` 间接核对）。
- **有意保留的内容无关审计**：同意要约/证据/快照、`policy_receipts_v2`、会话 runtime profile 与事件（签名载荷只含 id、年龄段、类别与 `persona_id`，无人格文本与姓名）、身份关系与绑定、`guardian_person_consents`、删除台账本身。
- **2026-09-26 已修复的来源**：数字分身与进化学习此前只校验"主人说话人/成年账号"，会把孩子或老人的话轮写入账号本人的数字分身快照与学习信号；现在两者只接收主体为空（早于主体归属）或等于账号本人的证据。response-plan 缓存在删除前按使用人失效。线上两张表当时为空，无存量需要清理。
- **恢复后再删除（2026-09-26 已实现，2026-10-26 扩展覆盖账号注销）**：删除台账在控制库，使用人与账号数据在 PostgreSQL/MinIO；恢复数据层后必须先运行 `scripts/replay_subject_deletions.py --confirm-replay` 重放全部已完成主体删除与账号注销（`account_deletions`），再恢复流量与重建投影（见发布运维手册）。
- **仍存在的已知缺口（明确接受，不宣称已擦除）**：
  1. Redis 流 `memoria:memory:events` 中已派发的载荷无法撤回，只随约 10 万条的近似裁剪老化。
  2. 使用人显示名：`/minors/{id}/delete` 仍被设备服务的孩子保留昵称和年龄段（绑定引用该行，解绑才清除）；账号注销会在 `identity_delete_account_identity` 口子里删除该账号自己的人行。设备解绑时（`release_committed_binding`），`device_onboarding_activations.manifest_json` 中的主要使用人显示名已脱敏置空（保留签名与哈希用于审计记录，避免明文泄露个人信息）。
  3. `session_runtime_tool_effect_intents/outbox` 的 agent 载荷可按会话归属，但现有角色无删除权限（只读 + `ON DELETE RESTRICT`）。
  4. 仅 SQLite 部署中执行过的迁移备份表保存源行完整 JSON。
  5. 账号本人记忆中残留的 `entity_ids` 悬空 UUID（无文本），以及由账号本人话轮建立的关于孩子的 `person_entities`/关系画像（只能按姓名关联）。
  6. `subject_id` 为空的历史证据（早于主体列）对删除与核对都不可见；2026-09-26 线上 2336 条证据均为空主体，来自只服务账号本人的声纹时代。

### 2026-09-20 删除范围：已闭合部分与已知缺口（结论）（部分已被上方 2026-09-26 复核取代）

- 已闭合（有收据）：可删域按 saga 9 步推进，`verified_empty` 覆盖 `_delete_order` 内且含 `account_id` 列的表；本轮补齐 guardian 的 `tutor_practice_evidence`/`tutor_commit_outbox`（删除/计数/导出 + RLS 前置 policy + 最小授权，提交 `6e853ef`；测试含真 PG 的“另一主体行不受影响”与幂等断言）。
- **已知合规缺口（明确记录；不做封存实现）**：① 明文残留——`memory_records.payload`、`memory_shared_proposals.content` 明文且不可就地改写，追加 tombstone **不构成擦除**；② 不可归属面四处——`memory_outbox`（无主体列）、`session_runtime_events`（`actor_id` 可空且无 subject 列）、`session_runtime_outbox`（无 subject 列）、`policy_receipts_v2.subject_id IS NULL`；③ append-only/零 DELETE 域（`memory_records`/`memory_status_events`/`memory_shared_votes`、`session_runtime_profiles`/`events`/`profile_receipts`、`policy_receipts_v2`）**无法物理删除**，且在不加迁移的前提下**无法形成可信封存标记**（`memory_status_events.status='revoked'` 非单调、后续合法事件可恢复可见性，故不能据此判 `verified_sealed`）。
- **表述纪律**：不得对上述域使用“封存/已擦除”表述；`verified_empty` 与删除收据只证明“可删域无残留”，**不证明“已擦除”**。若未来定义合规要求（留存期限、合规接受者、主体哈希/去标识方案），再走“各域新增独立封存登记 + 读口 join + 计数口”的最小迁移路径（届时需同时定义消费者与审计留存）。
- 未开工专项：identity（`identity_delete_guard` 依赖的 GUC 全仓从未设置、SQLite 无 delete/remaining 实现）、device_fleet/onboarding（无 person 列，须经 `identity_device_bindings.account_owner_person_id` 解析归属）。

### 2026-09-20 seal 契约持久化（17 表；供下一轮审查 D1–D10 的依据）

**逐域关键列与约束**（M=`services/memory_scope/postgres_schema.sql`、S=`services/session_runtime/postgres_schema.sql`、P=`services/policy/postgres_receipt_schema.sql`）

| 表 | 归属键 | 约束（不可删/不可改） | 现有可用的封存机制（只 INSERT 下） | 残留 |
|---|---|---|---|---|
| `memory_records` | `subject_id` M:96 / `resource_owner_id` M:97 / `created_by_actor_id` M:122；共属 `family_space_id`+`co_subject_ids` M:99-100,125-126 | 无条件 BEFORE UPDATE OR DELETE 触发器 M:288-293/309-312；仅 SELECT+INSERT M:348-349 | 只可追加 `memory_status_events`；`payload` 明文不可改写 | **明文 payload 永久保留** |
| `memory_status_events` | 经 `record_id` M:142 继承 | 无条件触发器 M:295-299/314-317；仅 SELECT+INSERT M:350-351 | 追加封存事件；**但读派生取最后一条**（PS:808-818/1719-1727）→ **非单调，可被后续合法事件覆盖** | status 判定不可靠 |
| `memory_shared_votes` | `subject_id` M:225（PK 成员 M:256） | 无条件触发器 M:302-306/319-322；仅 SELECT+INSERT M:354-355 | 不可重写；只能读层去标识 | 票面保留（且是他人晋升 fence 的授权证据 M:1414-1463） |
| `memory_shared_proposals` | `proposer_subject_id` M:156 / `family_space_id` M:155 | **可变**（有 UPDATE M:352-353，无触发器） | 全域唯一可就地去标识：`title` M:180 / `content` M:181；**不得触碰** `proposal_revision`/`capture_evidence_hash`/consent 三件套 M:192-203 | canonical evidence 必须保留以复验 |
| `memory_outbox` | **无主体列** M:259-266 | 无触发器；worker 可 UPDATE | **无法按主体定位** | 投递证据保留 |
| `memory_audit_events` | `actor_subject_id` M:274 / `subject_id` M:275 | 无触发器；仅 INSERT M:356-357 | 只能追加 | 审计原名保留 |
| `memory_shared_membership_snapshots` | `family_owner_subject_id` M:1007 + `subject_ids` JSONB M:1009 | RLS 仅 owner M:1034-1041；零运行时授权 | 追加新 revision + `status='revoked'/'expired'` M:1006；**不能按单一成员封存整行** | 历史 revision 保留（fence 复验） |
| `memory_capture_evidence` | `subject_id` M:1022 | 零运行时授权，经端口 M:1053-1208 / M:1310-1374 | **追加式封存语义现成**：追加 `status='revoked'` 的新 revision，锁端口按 `status='active'`+`revision DESC` 取行 M:1346-1355 → 旧 revision 自然失效 | 证据链保留（`canonical_hash` M:1020） |
| `session_runtime_contexts` | `actor_id` S:103 / `active_subject_id` S:107-110 | 可变（CAS 更新 S:797-800）；REVOKE ALL + 仅 SELECT S:376-401 | 用既有 `session_runtime_close_session` S:1152-1258 置终态；不可删（是 profiles/events 的 FK 目标） | 会话生命周期保留 |
| `session_runtime_profiles` | `actor_id` S:135 / `active_subject_id` S:138-141 | 无条件触发器 S:334-341/344-347；DEFERRABLE FK S:153-166 | 只能追加后继 profile 换主体；`signature` S:144 覆盖主体 id → **真去标识化不可能** | 历史 profile 仍被收据引用（S:171-172） |
| `session_runtime_profile_receipts` | `actor_id` S:175 | 无条件触发器 S:353-357 | 只能停止新增 | 与 `policy_receipts_v2` 跨域留存链 |
| `session_runtime_events` | `actor_id` S:188（**可空**）；**无 subject 列** | 无条件触发器 S:348-352；仅 SELECT | 只能追加；**历史主体归属不可证明** | 审计流保留 |
| `session_runtime_outbox` | `actor_id` S:204；**无 subject 列** | 无触发器；无 UPDATE/DELETE 授权 | 只能追加；投递端口未实装 | 投递证据保留 |
| `session_runtime_idempotency` | `actor_id` S:227 | 可变；仅 owner/maintenance | 置空结果列会破坏重放去重语义 | 去重证据保留 |
| `session_runtime_tool_effect_intents` | `actor_id` S:273 + `subject_id` S:274 | 无触发器；`status` 可变 S:298-300 | 用 reconcile 端口转终态 S:1856-1895；就地清 payload 会破坏 fence S:295-296 | 执行证据保留 |
| `session_runtime_tool_effect_outbox` | `actor_id` S:313 + **`subject_id` S:314** | 无触发器；claim/complete 端口 S:1897-1940 / 1942-2038 | **唯一可按 subject 在投递前干净过滤的面**（须把封存检查内建进 claim） | 投递证据保留 |
| `policy_receipts_v2` | `actor_id` P:127；`subject_id` 可空 P:128-130；`resource_owner_id` P:131-134；共享身份在 `action_resource_fence` JSONB P:168-201 | **零 UPDATE/DELETE/TRUNCATE**（REVOKE ALL 后仅 SELECT,INSERT P:357-369；文件头 P:15-16 声明 immutable audit record） | 只能停止签发 + 读层过滤；无 sealed 列 | 收据永久保留（不变量级审计记录） |

**封存后必须过滤的读口（谁必须改）**：memory → `services/memory_scope/postgres_store.py:696-806`（注意 `include_revoked=True` 是审计开关，须先于它生效）、RLS `M:369-398`（不含封存维度，owner-grant 分支 M:388-398 会照常放行）、`services/memory_scope/service.py:764-941`、operator 读口 `services/governance/subject_postgres_reads.py:249-300`（**必须改**）、SQLite 侧 `services/memory_scope/migrations/legacy_archive.py:2950-2980`；session → `services/session_runtime/postgres_store.py:875-1030,1252-1306,1399-1435`、RLS `S:443-466`（**按 binding 判定、无主体维度**）、`S:1897-1940`（投递前门）、`S:542-605/821-848`；policy → `services/policy/postgres_receipt_repository.py:222-278,401-435`、`P:257-294`（失败关闭，封存后本人也读不回）、全局读者 `P:436-463` 与 `S:2500-2514`。

**残余泄漏面（需显式接受，不得当作全链路不可见）**：① 产品召回走 archive 目录而非 `memory_records`（`services/control_api/app/routes/interaction.py:1719-1743`）⇒ 封存 memory_scope 不影响轮次内容；operator/导出面（`subject_postgres_reads.py`、governance `build_subject_export`）与 Redis 派发（`redis_outbox.py:83-112`，at-least-once）、搜索/向量物化各自需消费封存事件；③ D4=A 明确放弃 `session_runtime_events`/`outbox` 封存；④ D6=A 保留原始审计（只收窄可读者）。

**不可归属面（必须显式报 `unattributable`，不得计 0）**：`memory_outbox`、`session_runtime_events`、`session_runtime_outbox`、`policy_receipts_v2.subject_id IS NULL`。

**现有约束（不改授权/不迁移/append-only）下的可交付边界**：三域**统一去标识不可实现**；仅两处窄片可行——`memory_capture_evidence`（追加 revoked revision，锁端口语义现成）与 `session_runtime_tool_effect_outbox`（按 `subject_id` 在 claim 前过滤）；其余必须保持**未闭合**。用户已决策：**不做封存实现，记为已知缺口**（不授权上述窄片）。

---

## 来源：`docs/compliance/deletion-record-20261014.md`

## 测试服务器数据删除记录（2026-10-14）

- 服务器：aginice.cn 测试服务器（ssh memoria-prod），release 20261014-subscribe-optout-v1
- 批准：Monkey In，2026-10-14 15:05（UTC+8）私聊回复「开始清理」
- 数据来源：自动语音测试，无真人儿童
- 执行：memoria技术部，所有时间为 UTC+8

### (a) 上行录音
| 删除内容 | 数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/lib/memoria-agent/diagnostics/pcm-tap/*.wav`（08-19 至 08-22） | 57 个 WAV，约 135M | 2026-10-14 15:08:47 | 0 个文件（目录 12K 空壳保留） |
| `/etc/memoria-agent.env` 中 `MEDIA_PCM_TAP_DIR=/tmp/media-pcm-tap` 及 `MEDIA_PCM_TAP_MAX_BYTES=4194304` | 2 行 | 2026-10-14 15:08:47 | 0 行；重建后的 bridge 容器内无 PCM_TAP 环境变量 |
| `/tmp/media-pcm-tap`（宿主机与容器内） | 原本就不存在，0 个 | 2026-10-14 15:08:47 | 不存在 |

- 修改前的 env 已复制为 `/etc/memoria-agent.env.pre-pcmtap-removal-20261014`（只有配置，没有录音）。
- 15:08:57 重建 voice-core-media-bridge；15:09:53 和 15:10:46 两次复查都是 healthy，近 2 分钟日志里没有 error 或 traceback。其他容器也都正常。

### (b) 旧数据库
| 删除内容 | message 行数 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/backups/memoria/migrations/20260720-140053/memoria.sqlite3`（含 -wal/-shm） | 380 | 2026-10-14 15:08:47 | 文件不存在 |
| `/var/lib/memoria-candidate-20260719-204618/memoria.sqlite3`（含 -wal/-shm） | 376 | 2026-10-14 15:08:47 | 文件不存在，目录为空 |

- `frozen-state.tar.zst` 见下方「追加删除」。

### (d) 现网 Postgres（memoria 库）派生数据
| 表 | 删除行数 | 时间 | 复查后剩余 |
|---|---|---|---|
| daily_summaries | 13 | 2026-10-14 15:08:57 | 0 |
| memory_claims | 4 | 2026-10-14 15:08:57 | 0 |
| life_episodes | 2 | 2026-10-14 15:08:57 | 0 |
| memory_search_documents | 8 | 2026-10-14 15:08:57 | 0 |
| memory_search_document_sources（外键级联） | 8 | 2026-10-14 15:08:57 | 0 |
| episode_evidence（外键级联） | 2 | 2026-10-14 15:08:57 | 0 |
| timeline_entries（外键级联） | 2 | 2026-10-14 15:08:57 | 0 |

- 临时备份：删除前在 15:08:47 用 pg_dump 导出上述 7 张表（data-only，28074 字节，root 600 权限），存放在 `/var/backups/memoria/derived-20261014/derived.sql`。服务确认稳定后，于 2026-10-14 15:10:46 删除，复查确认已不存在。
- 旧 SQLite 中的 daily_summaries 见下方「追加删除」。各 memoria.pre-*.sqlite3 文件里也各有 13 行，会随 (c) 一起处理。

### 追加删除（Monkey In 于 2026-10-14 15:12（UTC+8）私聊批准）
| 删除内容 | 数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/backups/memoria/migrations/20260720-140053/frozen-state.tar.zst`（2026-07-20 打包，含 2026-07-16 至 07-19 的旧 memoria-pre-*.sqlite3） | 1 个归档，7.0M，共 1604 个条目 | 2026-10-14 15:12:36 | 文件不存在，目录为空 |
| `/var/lib/memoria/memoria.sqlite3` 中 daily_summaries 表的行（只删行，文件保留给 (c) 使用） | 13 行 | 2026-10-14 15:12:48 | 0 行；文件仍在（2027520 字节），属主仍为 65532 |

- 删除后 control-api 与 bridge 都是 healthy。

### (c) 机器人回复文本（代码修复已部署后执行）
- 前提：PR #235（squash `5333d1a`，CI 通过）已合并，并按常规发布流程部署为 `20261014-child-playout-ephemeral-v1`。各步骤均 PASS：verify-load 16:38:32、freeze 16:38:45、env 16:39:27、schema 16:39:29、cutover 16:40:07、finish 16:41:25。
- 部署后：bridge 与 control-api 均 healthy，重启 0，revision 都是 5333d1a；外部 readiness 200 `ready`；bridge 中 `ENVIRONMENT=production`，`MEDIA_PCM_TAP_DIR` 为空；近 3 分钟日志无 error。修复相关的 4 个测试文件在发布 worktree 中全部通过。回滚目标是 `20261014-subscribe-optout-v1`。
- 部署前检查：`/etc/memoria-*.env` 和 `/etc/memoria-media-runtime/` 下都没有 MEDIA_PCM_TAP。

| 删除内容 | 方式与数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| Postgres `archive_evidence_events` 中 `assistant.playout_stopped` 的回复文本 | 只从 payload 删掉 `text` 键，保留行和表结构（删行会级联到二十多张记忆表）。共 1,255 行带文本，另 7 行原本就没有文本，1,262 行全部保留 | 2026-10-14 16:34:25 | 1,262 行，带 `text` 的 0 行（16:43 复查） |
| agent 加密 spool `/var/lib/memoria-agent/archive-events.spool` | 384 行积压（未送达）事件，全部是 `assistant.playout_stopped` 且带文本（2026-09-30 至 10-13）。加锁后清空 | 2026-10-14 16:34:15 | 0 字节（部署后复查仍为 0） |
| 5 个旧切流 dump：`/opt/memoria/releases/{20261013-speaker-retire-v1, 20261014-night-window-v1, 20261014-night-default-2100-v1, 20261014-safety-subscribe-v1, 20261014-subscribe-optout-v1}/.cutover/memoria-pre-*.dump` | 5 个，每个都含 1,255 条带文本的回复 | 2026-10-14 16:42:44 | 不存在 |
| `/var/lib/memoria/memoria.pre-{上述 5 个 tag}.sqlite3`，含 -shm/-wal | 5 个库加 10 个附属文件，共 15 个（库内无回复文本，但每个有 13 行 daily_summaries） | 2026-10-14 16:42:44 | 不存在 |

- 新的回滚备份：先清掉文本，再运行 freeze，所以本次 freeze 生成的 `memoria-pre-20261014-child-playout-ephemeral-v1.dump`（3,924,871 B，16:38）中带文本的 playout 行为 0（已用 pg_restore 核对）。配套的 `memoria.pre-20261014-child-playout-ephemeral-v1.sqlite3` 中 daily_summaries 为 0。这两份作为当前唯一的回滚点保留。
- 全盘复查：除上面这一份新 dump 外，再没有 memoria 的 dump；`/opt/pocketsparks/backups` 属于共用主机上的另一个项目，没有动。
- 剩余事项：HANDOFF 中「备份已全删」一句需要加注更正（不删原句），由技术部在仓库中处理。注意现在仍保留一份不含文本的回滚 dump。

### 补记（2026-10-17）：保留的回滚备份删除与复查、旧机器

- 删除：(c) 里保留的唯一回滚点 `/opt/memoria/releases/20261014-child-playout-ephemeral-v1/.cutover/memoria-pre-20261014-child-playout-ephemeral-v1.dump`（3,924,871 B）和 `/var/lib/memoria/memoria.pre-20261014-child-playout-ephemeral-v1.sqlite3`（2,027,520 B，另 -shm 32,768 B、-wal 0 B），已于 2026-10-16 16:02:59（UTC+8）删除，不是定时清理删的。批准：Monkey In 2026-10-16 16:01（UTC+8）群内回复。同一批共删 29 个文件、81,280,926 B，还包括 2026-10-14 至 10-15 各次发布 `.cutover/` 下的 `memoria-pre-*.dump` 和 `memoria.pre-*.sqlite3`，以及当时的 `/var/lib/memoria/memoria.sqlite3`。逐个文件的路径、sha256、字节数在服务器 `/root/memoria-phone-clear-20261016/backup-deletion.txt`（root，600）。
- 复查（2026-10-17 10:36，UTC+8，只读）：`/var/lib/memoria/` 下没有 `memoria.pre-*.sqlite3`；全盘（`find / -xdev`）查 `memoria-pre-*.dump` 和 `*.pre-*.sqlite3`，2026-10-14、10-15 的一个都没有了。还在的只有 2026-10-16 三次发布 freeze 生成的回滚点：`20261016-session-cap-fallback-v1`（13:06）、`20261016-log-redaction-v1`（19:04，`20261016-readiness-b54189c` 树里有同名的一份）、`20261016-device-registration-v1`（22:26），共 4 个 dump、4 个 sqlite3。
- 定时清理：`memoria-freeze-dump-cleanup.timer` 每 24 小时跑一次 `cleanup_freeze_dumps.py --retention-days 3 --apply`。2026-10-14 17:28、10-15 17:35、10-16 17:41 三次都是 `removed=0`，10/14 那份 dump 在到期（10-17 16:38）前已被上面那次手动删除，所以这次不是靠定时清理删掉的。2026-10-16 那批由它在超过 3 天后清掉，最早一份 10-19 13:06 后到期。
- 旧机器 memoria-prod-old：实例、云盘已释放，无快照（Monkey 10/14 在控制台确认）。

---

## 来源：`docs/compliance/guardian-consent-draft.md`

## 监护人同意书（草稿）

仓库里原先没有单独的监护人同意书。本文件从 `docs/compliance.md` 摘出可以对家长阅读的段落。小程序 `pages/guardian-consent/index` 的正文与下面各段逐字相同。该合规说明写明，推荐方案的告知表在生产核对完成前不是现状，所以正文不写那些期限，也不写未经核对的存储地点。

### 正文

这是内测版，待律师审核，还不是已经生效的同意书。

我是孩子的监护人。我已阅读《隐私保护指引》和本同意书。

我同意为完成当轮对话、按天查看的摘要，以及称呼、喜好、宠物名这三项长期记录，处理孩子的对话。不勾选登录页的同意框，就不能完成本次登录。

本说明采用的对外说法是：我们的服务器不存孩子的录音和原话，记忆家长看得见、删得掉。

对话经过我们的服务器。孩子说的话会经服务器处理后再回复。

识别和生成回复时，孩子说的这一句会交给服务商。服务商保存多久按合同。这里不承诺全链路用完即删，也不承诺服务商到期就删除。

家长可以按天查看对话摘要。原话在入库前就去掉。

长期只记三项：孩子的称呼、喜好、宠物名。

回应由 AI 生成，设备里说话的是合成语音，不是真人。离线故事和睡前音频的音频文件里带有 AI 生成标记。

设备没有摄像头，也不采集图像。

手机不录音。机器人本身不存录音。使用人由设备绑定确定，不通过声音识别身份。

### 来源

`docs/compliance.md`

---

## 来源：`docs/compliance/offer-child-rules-v1.md`

## offer_child_rules_v1

Date: 2026-10-24

Offer id: `offer_child_rules_v1`

Mode: `parent_for_child`

Status: known, not required by the server. A parent_for_child binding that omits this offer still succeeds. The catalog records a warning that the offer was not presented. A later version of this offer must be able to show this text as what `_v1` said. The mini program does require it before submit since 2026-10-24 (PR #417); the server-side requirement is a later step.

### Rules the parent acknowledges

The device is an AI companion for the child.

Replies are generated by AI, and the voice in the device is synthesized, not a person.

Conversations are summarized for the parent.

The child can stop at any time.

### Text change before first release

The first version said a tone plays at the start of each conversation. That tone is off by default since 2026-10-24 (`AIGC_NOTICE_ENABLED` in `services/agent/src/config.py`), so the sentence promised something the device no longer does. `_v1` had not reached any family when the sentence changed. This file records what `_v1` ships with.

---

## 来源：`docs/compliance/p0-04-minor-safety-decisions.md`

## P0-04 未成年人安全闭环：产品决定

2026-09-26 整理，同日用户拍板，结论见下表；各项原先的现状、选项与建议保留在后文，作为决策依据。现状核查基于 main `feeb277` 的代码。

### 结论（用户 2026-09-26 决定）

| 项 | 决定 |
|---|---|
| D1 | 不区分 14 岁以下与 14–17 岁，未成年人统一要求家长同意 |
| D2 | 未成年人只学表达风格，不学价值取向与决策习惯；已学到的这两类特征清除 |
| D3 | 单次时长与夜间休息在运行时强制执行（固定话术道别或拒绝进入对话）；危机对话不受限 |
| D4 | 以家长申报为准，不做证件核验；设备页显示当前年龄段并可修改，修改后立即刷新会话授权；改为成人时要求本人手机号核验 |
| D5 | **年龄不明时也允许学习辅导**，仍不留存、不读私人记忆（与建议不同） |
| D6 | 删除绑定页「每周小结」勾选项，在「长期记忆」说明中写明同时开通每周小结 |
| D7 | **孩子与老人触发危机都推送**：孩子推给家长，老人推给代为同意的子女（老人部分与建议不同）；推送走微信订阅消息，需开通模板并授权启用（2026-10-04 起产品聚焦儿童青少年，老人部分暂不适用，代码暂留） |
| D8 | 话术加入全国统一心理援助热线 12356（国家卫健委指定，2025 年 5 月前各省已开通），对外发布前请专业人士审核定稿 |

### 实现状态（2026-09-26，已随 `20260926-minor-safety-v1` 上线，设备未验）

| 项 | 实现 | 边界 |
|---|---|---|
| D1 | 无需改动：策略本来就不区分年龄档 | — |
| D2 | 人格学习证据带 `style_only`；孩子的对话在抽取后丢弃决策习惯与价值取向（两个引擎） | 线上现无任何未成年人或此两类特征，无需清理 |
| D3 | 策略用绑定时家长填的单次时长与夜间时段签入授权（缺省回落 1800s、21:30–06:30）；设备会话在夜间时段唤醒时播固定晚安并待机，超时后等当前回复说完播固定道别并待机；本次会话说过危机安抚话术后不再按时长结束 | 按单次会话计时（重新唤醒重新计时，与「单次使用时长」一致）；时区取服务端 `MEMORIA_TIMEZONE`（Asia/Shanghai），不是设备所在时区；家长页若撤回再重开语音会话，同意参数回落默认值 |
| D4 | `GET /v1/persons/{id}/age-evidence`；设备页显示当前年龄段；修改后为该使用人的设备重签授权（下次会话生效） | — |
| D5 | 年龄不明（含使用人未确认）允许学习辅导，义务与聊天相同：不留存、不写进度 | 练习会话接口（持久化路径）仍要求确认的使用人 |
| D6 | 绑定页删除「每周小结」，长期记忆说明写明同时开通 | 后端目录仍接受旧版客户端提交的该 id |
| D7 | 孩子（已声明或已认定的监护人）与老人（绑定人的代理认定）的危机提醒都入队；设备页为「给父母使用」的绑定显示提醒与微信订阅；文案改为中性「TA」。同时修复：2026-09-25 之后绑定的孩子，提醒入队在 PostgreSQL 必然失败 | 推送需开通订阅消息模板并设置 `MEMORIA_GUARDIAN_PUSH_ENABLED`（需授权）；离线兜底话术不入队 |
| D8 | 心理援助热线 12356 改由家长端告警承接；孩子端 `crisis-self-v1` 只做安抚与陪伴（另有 `victim-support-v1` / `support-other-v1`） | 2026-10-27 定稿；对外发布前仍可再请专业人士复核 |

#### 长期记忆留存（2026-09-30 补充，未上线，设备未验）

- **对齐**：2026-09-25 的决定是家长绑定时勾选长期记忆，孩子即被记住；Policy 引擎里未成年人 `memory_capture` 的旧口径却是 `PERSIST_AGGREGATE_ONLY`（`minor_memory_minimized`），归档因此对孩子只能存聚合。现在有效监护同意、有效 `guardian_of` 关系、绑定 fence、受信设备、使用人本人说话同时满足时，决定改为与成人相同的 `RETENTION_TTL`（30 天）加 `NO_MODEL_TRAINING`（`minor_memory_guardian_authorized`）；缺任一项仍拒绝，测试逐项覆盖。
- **Agent 闸门**：孩子的 profile 只在家长长期记忆同意有效时才列出 `memory_recall_private`，闸门凭它归档；撤销同意后下一份 profile 不再有。服务端 `/v1/archive/session-events` 仍独立按家长 `memory_retention` 同意决定是否保留原文。catalog 编译时先解析证据主体的类别，从不把未成年主体的证据交给抽取器，孩子的证据不形成长期记忆投影（`services/archive/extraction_gate.py`）。家长仍看不到原文。
- **没有做的**：不给归档逐轮申请动作时收据。Control 的动作围栏每次授权只能前进一步，与 Agent 的计数不同步，逐事件申请会大面积 409；服务端落库时本来就独立重新授权。
- **边界**：归档证据行没有过期任务，`RETENTION_TTL` 目前只有 MemoryScope 记录执行；设备信任分两档：`verified`（硬件 attestation）放行全部敏感能力，`trusted`（onboarding 绑定链路、无硬件证据）只放行 `memory_capture`、`memory_recall_private`、`guardian_summary_view`。生产设备没有 attestation，需打开 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED` 后孩子的对话才会入库，见 TODOLIST P1-11。

### 已定，不再讨论

- 设备只服务绑定时选定的使用人；家长给孩子绑定并勾选长期记忆，监护小结随之授予（2026-09-25、09-26 决定）。
- 同意长期有效，直到撤销；解绑时撤销同意，并询问是否删除数据。
- 家长只看摘要、趋势和风险提醒，不看孩子原文。危机提醒的家长文案是固定的，不含原文，也不含严重程度（`services/guardian/crisis.py`）。
- 未成年人不开放声音复刻、数字自我、传承、设备转让；付费、原始音频留存、模型训练默认拒绝（`services/policy/engine.py`）。
- 年龄不明或使用人未确认时降级为 `unknown_safe`：只允许聊天和英语练习，不留存（fail closed）。

### 决策依据（各项现状、选项与当时的建议）

#### D1 14 岁以下和 14–17 岁是否区别对待

- **现状**：绑定时可以选年龄段，但策略只认"未成年"一类，两个年龄段的能力完全相同，而且都要求家长同意。
- **选项**：
  - A. 保持一致：所有未成年人都按"需要家长同意"处理。
  - B. 14 岁以下更严。例如长期记忆留存更短，或人格只学表达风格（见 D2）。
- **建议**：A。国内法规把 14 岁以下个人信息列为敏感信息、要求监护人同意，而我们现在对全部未成年人都已要求家长同意，已经覆盖这条线。另外分两档会让家长端的开关变复杂。

#### D2 未成年人的人格学习范围（对外发布前复核，你之前的决定要求这一步）

- **现状**：未成年人和成人一样完整学习，包括"价值取向""决策习惯"这类可以用来画像的特征（`services/persona/domain.py`）。
- **选项**：
  - A. 维持完整学习。
  - B. 未成年人只学表达风格（语速、句长、口头禅、语气），不学价值取向和决策习惯。
  - C. 未成年人完全不学人格。
- **建议**：B。孩子仍能感到"它懂我说话的方式"，但不形成对孩子观念和决策的长期画像，对外说明也更容易讲清楚。已经学到的这类特征，按选项一次性清除。

#### D3 单次时长和夜间休息时段是否真正执行

- **现状**：绑定表单里有单次时长（默认 30 分钟）和夜间休息时段（默认 21:30–06:30），策略也会下发，但它们只写进了提示词。机器人可能会提一句，运行时并不执行（`services/agent/src/prompt_composition.py`）。
- **选项**：
  - A. 维持仅提示。
  - B. 运行时强制：到时长后，机器人用固定话术道别并结束会话；夜间时段唤醒时，机器人用固定话术说明已到休息时间，然后不进入对话。
  - C. 只强制夜间时段。
- **建议**：B。家长在绑定页设的值应该真的生效，否则就是误导。危机相关的对话不受这两个限制：危机话术照常播出，提醒照常入队。

#### D4 孩子的年龄怎么确认、怎么变更

- **现状**：孩子的年龄段由家长在绑定时申报，之后一直是"未核验"，系统里没有把未成年人变成"已核验"的途径。这不影响功能，因为策略按年龄段判断是否未成年。设备页有"年龄资料申报"入口，但它不显示当前年龄段，改完也不会刷新会话授权。
- **选项**：
  - A. 家长申报即为准，不做证件核验；设备页显示当前年龄段，家长可以修改；孩子满 18 岁由家长或本人改为成人，改为成人时要求本人手机号核验。
  - B. 引入证件核验。需要第三方服务、费用和额外的合规评估。
- **建议**：A。显示当前年龄段、修改后立即刷新授权，这两处属于工程修复，不管选哪个都要做。

#### D5 年龄不明时是否允许学习辅导

- **现状**：年龄不明或使用人未确认时只允许聊天和英语练习，不允许学习辅导，也不留存。
- **选项**：
  - A. 维持。
  - B. 放开不留存的学习辅导。
- **建议**：A。学习辅导通常意味着使用人是孩子。放开就等于在没有家长同意的情况下向孩子提供辅导，和"不能决定时 fail closed"的原则冲突。

#### D6 绑定页的"每周小结"勾选项

- **现状**：绑定页有一个默认勾选的"每周小结"，后端并不处理。实际的监护小结随"长期记忆"一起授予（你 09-26 的决定）。
- **选项**：
  - A. 删掉这个勾选项，在"长期记忆"的说明里写明"同时开通每周小结"。
  - B. 做成独立授权。但小结需要长期记忆的数据，单独开小结没有内容可汇总。
- **建议**：A，避免家长以为勾了就有、不勾就没有。

#### D7 危机提醒怎么送到家长

- **现状**：孩子触发危机时，机器人播放固定安抚话术，同时给家长写一条提醒。提醒默认只在小程序家长页可见。微信订阅消息推送的代码已写好，但默认关闭（`MEMORIA_GUARDIAN_PUSH_ENABLED=false`），需要开通公众号或小程序订阅消息模板。成人和老人触发危机时不写提醒。
- **选项（孩子）**：
  - A. 维持只在家长页可见。
  - B. 开通微信订阅消息推送：家长需要事先点一次"开启提醒"，微信的一次性订阅限制是点一次推一次。
  - C. 短信或电话，需要服务商、费用和号码授权。
- **选项（老人）**：
  - A. 不通知子女，保护成年人隐私。
  - B. 通知代为同意的子女，需要在绑定时单独征得同意。
- **建议**：孩子选 B（代码已就绪，只差开通模板和授权）；老人选 A，以后如有需求再加单独的同意项。

#### D8 危机话术的内容与审核

- **现状（决策当时）**：话术版本是 `crisis-transfer-draft-v1`，名字里就带着 draft。内容是：表达关心、请远离危险物品、联系身边可信的人、紧急时联系急救或报警，最后问对方是否正准备伤害自己。话术里没有心理援助热线。
- **选项**：
  - A. 维持现稿。
  - B. 加入全国心理援助热线（例如 12356），并在对外发布前请心理或儿童保护方面的专业人士审核定稿。
- **建议**：B。热线号码和措辞以专业审核结果为准，本文不替你定稿。
- **后续（2026-10-14，PR #238）**：产品与合规定稿三套固定话术，id 分别为 `crisis-self-v1`、`victim-support-v1`、`support-other-v1`；去掉句末追问，热线与急救号码在展示文案里用阿拉伯数字。

### 不需要决定、直接实现的工程项

- 设备页年龄资料显示当前年龄段；修改后重签 Runtime Profile。
- 会话记忆按使用人迁入 `services/memory_scope`，覆盖切人、撤销、删除和旧缓存。
- 两条策略入口的软件测试矩阵：14 岁以下、14–17、成人、年龄不明，以及权威数据不可用或过期、profile 与会话错绑、同意撤销或过期、管理账号更换、切人并发。
- 安全专项设备链验收：身份与年龄 → 有效同意 → 准入或受限能力 → 固定话术真实播出 → 提醒入队、幂等、家长读回。通过之前 `student_safety_loop_verified=false` 保持不变。

---

## 来源：`docs/compliance/service-provider-checklist-fields.md`

## 服务商出网清单的字段

这份文件只规定清单里每一行必须有哪些字段。具体服务商写在 `docs/compliance.md`。那份文件还没有。

每一行都要有下面六项。少一项，这一行不算完成。

- 服务商。发出请求的那一家的名字。
- 用途。这次出站在产品里干什么。
- 出站数据类型。请求里实际带出去的字段。写字段名，不写「对话数据」这种笼统词。
- 是否含儿童内容。只写是或否。儿童语音、转写、昵称算是。
- 保留依据。对方留多久，依据哪份协议或哪一段代码。没有依据就写未知。不能空着。
- 审核日期。合规看过这一行的日期。还没看就写未审。

填表时抄这一行当表头。

| 服务商 | 用途 | 出站数据类型 | 是否含儿童内容 | 保留依据 | 审核日期 |
| --- | --- | --- | --- | --- | --- |

---

## 来源：`docs/compliance/service-providers.md`

## Memoria 服务商出网清单

这份清单列出生产链路和代码库里实际出站的服务商。字段规则见 `service-provider-checklist-fields.md`，每一行六项，少一项不算完成。表里没写的服务，代表没有这条出站链路。

| 服务商 | 用途 | 出站数据类型 | 是否含儿童内容 | 保留依据 | 审核日期 |
| --- | --- | --- | --- | --- | --- |
| 火山引擎（北京字节跳动科技有限公司） | 语音识别（Doubao/Volc ASR） | PCM 音频帧，返回转写文本 | 是（儿童语音流） | 企业协议约定时效，处理完毕即释放 | 2026-10-25 |
| 火山引擎（北京字节跳动科技有限公司） | 语音合成（Doubao/Volc TTS） | 回复文本（合成请求的 prompt），返回音频流 | 是（回复文本包含儿童互动内容） | 无持久存储，流式下发即销毁 | 2026-10-25 |
| 阿里云（阿里云计算有限公司） | 语言大模型（百炼 DashScope / Qwen） | 对话 prompt、记忆上下文，返回生成回复 | 是（对话及画像上下文） | 阿里云服务协议，企业级不保留训练数据 | 2026-10-25 |
| 阿里云（阿里云计算有限公司） | 文本嵌入向量（Text Embedding） | 记忆文本，返回向量数据（历史上用于检索） | 是 | 向量存本地 PGVector，远端接口无保留 | 2026-10-25 |
| 深度求索（杭州深度求索人工智能基础技术研究有限公司） | 语言大模型（DeepSeek API，api.deepseek.com） | 对话 prompt，返回生成回复 | 是（儿童对话内容） | 开发者协议，API 数据不用于模型训练 | 2026-10-25 |
| 阶跃星辰（上海阶跃星辰智能科技有限公司） | 预置安全音频合成（StepFun stepaudio-2.5-tts） | 离线安全话术文本，研发期一次性调用并固定落盘 | 否（仅固定安全话术，无儿童真实数据） | 仅离线单次生成，生产运行不出站 | 2026-10-25 |
| LiveKit, Inc. | 实时音视频信令与流媒体传输 | WebRTC 音频流、Room Token、Session ID | 是（实时音频包） | 实时中继传输，端到端转发不持久化 | 2026-10-25 |
| 腾讯科技（深圳）有限公司 | 微信小程序开放接口 | code2Session 的 js_code 与 openid，监护人模板消息通知参数 | 否（仅监护人微信凭据及状态通知） | 微信开放平台开发者协议 | 2026-10-25 |
| 和风天气（和风天气有限公司） | 实时天气查询 | 城市名称、经纬度（`QWEATHER_*` 配置，数据流见 `docs/compliance.md`） | 否（仅地理位置参数） | 公共查询接口，无用户数据保留 | 2026-10-25 |

### 说明

含儿童内容的出站集中在四条链路：ASR 上行语音流、TTS 上行回复文本、LLM 上行对话与记忆上下文、LiveKit 实时音频中继。这四条都是流式或请求级传输，服务商侧不落长期存储；儿童内容在服务商处的停留时间以各家的企业协议或开发者协议条款为准。ASR 转写的临时性另见 `docs/compliance.md`。

不含儿童内容的链路有四条：阶跃星辰只在研发期合成过固定安全话术，产物已落盘为本地资源，生产运行不再调用该接口；腾讯微信侧只传监护人凭据和通知参数；和风天气只传城市名称和经纬度，不带用户标识。

嵌入向量一行标注了「历史上用于检索」。当前检索走向量时，向量数据留在本地 PGVector，远端只提供文本到向量的转换。

保留依据一列写的是对方协议的说法。协议文本不在本仓库里，逐条核验依赖商务留存的企业协议与各家控制台条款。清单整体审核日期为 2026-10-25，新增或变更服务商时要补行并重审该行。

---

## 来源：`docs/compliance/weather-lookup-data-flow.md`

## 天气查询出站字段

`WEATHER_LOOKUP_PROVIDER` 默认是 `off`。`LIVE_SEARCH_QWEN_ENABLED` 默认是 `false`。`build_domestic_weather` 在 `off` 时返回 `None`，也在 `domestic` 缺少 `QWEATHER_API_KEY` 或 `QWEATHER_API_HOST` 时返回 `None`。实时检索开关关闭，且没有可用的和风天气数据源时，规则层对天气问句以及下一轮只报城市的话固定回答「我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。」文稿是产品部定稿 weather-fallback-v1（2026-10-16）。这时没有天气客户端，也不调用通义实时检索，请求数是 0。这句话只作为文字回复，本变更不合成音频。

`LIVE_SEARCH_DEEPSEEK_ENABLED=true` 且进程持有 `DEEPSEEK_API_KEY` 时，实时问句（天气、新闻、比分、汇率等）交给 DeepSeek 联网搜索，它优先于通义。先 `POST {DEEPSEEK_BASE_URL}/v1/tools/search`，正文只有 `query`（经 `redact_pii` 脱敏后的孩子原话，截到 128 字）和 `count`（5）。有结果时再 `POST /chat/completions`，正文是系统提示、北京时间、每条结果的标题、日期和摘要，以及同一句脱敏原话。不发送网址、身份、设备号、过往对话或记忆。没有结果或任一步失败时返回 `None`，孩子听到「暂时没查到」。天气问句例外：会话内的天气线索（`services/agent/src/weather_thread.py`，存在本会话的 `ContextManager` 上，换主体或清空上下文时一起丢弃）记住允许表命中的城市标准名和日期词。问句没说地点、本会话也没有城市时，直接回答「你想查哪个城市呀？」，不出站。孩子随后报出允许表城市，或在天气问句之后接「那明天呢」「后天呢」「那上海呢」时，`query` 改为「城市标准名 + 日期词 + 天气」（例如「杭州明天天气」），不含孩子原话；问句里自己说了允许表城市时仍发脱敏原话。允许表以外的地点照旧发脱敏原话，不记城市。日志只写模型名、结果条数、回答字数和耗时，不写原话。按 DeepSeek 官方价格，每 1,000 次成功搜索 $0.75。

`LIVE_SEARCH_QWEN_ENABLED=true` 且进程持有 `DASHSCOPE_API_KEY` 时，本地规则答不了的天气问句交给通义实时检索。`domestic` 且密钥与域名都存在时，即使实时检索开关关闭，`DomesticWeather.resolve` 仍先在本地决定。不是天气问句，或日期窗口超过 7 天、单位不是「天」、窗口含糊或互相矛盾时，返回 `None`，请求数是 0。实时检索开着则把原话交给通义。关着则不再出站。是天气问句但允许表不能确定唯一城市时，返回 `你想查哪个城市呀？`，请求数是 0，通义不被调用。允许表命中且窗口可回答时，才发出下面的 GET。

域名是配置里的 `QWEATHER_API_HOST`，必须是裸主机名 `*.qweatherapi.com`。方法是 GET。正文为空。密钥放在请求头 `X-QW-Api-Key`。`httpx` 还会带上 `Host`、`Accept`、`Accept-Encoding`、`Connection`、`User-Agent`。客户端以 `trust_env=False` 创建，不读取 `HTTP_PROXY` 一类环境变量。

构造 `DomesticWeather` 时把进程内 `httpx` 日志级别升到 WARNING，并丢掉消息里含有 `qweatherapi.com` 的记录。这个 logger 是进程级的，因此同一进程里其他 `httpx` 客户端也不再打出 INFO 请求行。应用自己的日志只写 `model`、`start_offset`、`days`、`elapsed_ms` 和固定的 `reason`。日志里没有 URL、密钥、城市名、原话或异常文本。

### 出站字段

| 路径 | 位置 | 字段 |
|---|---|---|
| `/geo/v2/city/lookup` | query | `location` |
| `/geo/v2/city/lookup` | query | `range` |
| `/geo/v2/city/lookup` | query | `number` |
| `/v7/weather/3d` | query | `location` |
| `/v7/weather/7d` | query | `location` |
| `/v7/weather/now` | query | `location` |

`/geo/v2/city/lookup` 的 `location` 是允许表 `City.standard_name`。区县别名命中时仍然发送所属城市的标准名。`range` 是 `cn`。`number` 是 `10`。返回行要同时满足 `country` 为 `中国`、`name` 等于标准名、`adm1` 等于该城市的省，并且 `id` 是 9 位数字。这个 id 记在进程内，同一城市后面的查询不再请求地理接口。

预报需要的天数是从今天起算的结束日（含起始偏移）。1 到 3 天走 `/v7/weather/3d`，4 到 7 天走 `/v7/weather/7d`。这两处的 `location` 是上面的 9 位 id。问句里有「现在」或「当前」，并且窗口从今天开始时，再请求 `/v7/weather/now`，`location` 同样是这个 id。

请求不携带原话、抽取到的地片段、经纬度、设备 id、儿童 id 或会话 id。连接的源地址是服务器自己的地址。

口头回答里的城市名用标准名。这个查询不另写天气表。说出去的句子随后走原来的回复路径。

---
