# 儿童对话音频与转写用完即删（设计）

2026-10-13。本文只定设计，不改运行时代码。文中「现状」按仓库代码与运维手册核对；「推荐方案」是尚未实现的目标。隐私声明在实现并核对之前，只能引用现状表，不能把推荐表写成已经生效的事实。

产品范围是面向国内 3 到 10 岁孩子家长的陪伴机器人。工程上年龄档不区分 3–10 岁与其他未成年人（见 `docs/compliance/p0-04-minor-safety-decisions.md` D1：未成年人统一要求家长同意）。本设计的删除规则覆盖全部未成年人，以及年龄不明、使用人未确认的 fail-closed 会话。是否在对外告知里把 14–17 岁写成与不满 14 周岁同一张表，见文末待决事项。

验收口径（本设计要满足、本文件不实现）：

- 服务端持久存储里没有儿童的对话音频。范围包括 PostgreSQL、MinIO、Redis、日志、归档证据、投递缓冲和备份能复原的对象。
- 儿童话轮的转写在对话结束后清掉。留下的只有生成长期记忆所需要的文字摘要。
- 儿童账号关闭声纹登记和自定义音色样本。成人如需使用，继续走现有的单独同意，不把儿童路径打开。
- 删除要能核对。沿用 `services/governance/subject_deletion.py` 的按使用人删除和 `verified_empty`，并且遵守 `docs/compliance/delete-domains.md` 的表述纪律：核对为空只证明可删域无残留，不写成「已擦除」。

合规对照：《儿童个人信息网络保护规定》第十条要求告知儿童个人信息的存储地点、期限和到期后的处理方式；《个人信息保护法》对不满 14 周岁的个人信息要求监护人同意，存储不超过实现处理目的所必需的期限，并提供可核对的删除。下面两张表按这三项写，供隐私声明起草时摘用。

## 境内：代码能证明的和不能证明的

自有数据层与应用部署在同一套生产主机上。`docs/runbooks/operations-space-governance.md` 写明生产自 2026-10-05 起在 `110.42.235.198`，PostgreSQL、Redis、MinIO 使用该主机上的数据卷。对象存储 region 的代码默认值是 `cn-beijing`（`MEMORIA_ARCHIVE_OBJECT_REGION`、`MEMORIA_VOICE_OBJECT_REGION`，见 `services/control_api/app/config_fields/archive.py` 与 `voice.py`）。仓库里没有机房合同，也没有生产桶 endpoint 的值。

因此现状表和推荐表里的「境内」都写成：**代码默认与运维手册指向中华人民共和国境内的自有主机；对外写成「存储于境内服务器」之前，须用部署清单核对这台主机和 MinIO endpoint 的物理位置。** 本文件不代替那次核对。

离开自有主机的处理另计，不并进「境内自有存储」：

- 儿童上行音频做语音识别时发往阿里云百炼 FunASR（`FUNASR_MODEL` 默认 `fun-asr-realtime`，DashScope WebSocket 由 `DASHSCOPE_WS_URL` 配置）。
- 对话文本发往百炼兼容接口（默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`）或 DeepSeek，由 `llm_provider` 决定。
- 助手要说的文字发往火山引擎豆包 TTS（默认 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`）。孩子的话若被复述，会夹在这段文字里。
- 记忆抽取读取已落库的证据文本，抽取模型同样走百炼。
- 声纹嵌入经 CampPlus HTTP 提交 `audio_base64`（`services/speaker/campplus_http.py`）。该服务的部署位置由环境决定，仓库没有写死地域。

这些厂商保存多久、存在哪一地，本仓库不能强制。告知里必须单独一行，不能写成「用完即删」。

## 数据从设备进来之后走哪

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

## 现状：存储地点、期限、到期处理

下表是今天的行为。期限写的是代码里真实存在的上限；没有删除任务的，不写成「到期删除」。

| 数据 | 存储地点（系统 / 表 / 桶；是否境内） | 保留期限 | 到期后的处理方式 |
|---|---|---|---|
| 对话音频在设备上 | ESP32-S3 内存。诊断 PCM 仅试验命令，在板载 PSRAM。不在服务器。 | 播放与采集窗口；诊断窗为一次 USB 命令的长度。 | 缓冲区被下一段覆盖或断电丢失。没有上传留存。 |
| 对话音频在接入层 | Go Media Edge 进程内 `audioRing`。不落盘。与 Voice Core 同机，境内见上文核对条件。 | 当前会话的媒体环寿命。 | 会话结束或进程退出时释放。没有对象可删。 |
| 对话音频与当轮转写在 Voice Core 进程内 | 进程内存：ASR 缓冲、`ContextManager.turns`（最多约 16 条，`services/agent/src/orchestration/context_manager.py`）、声纹缓冲 `_speaker_pcm`（上限 4 MiB，`services/agent/src/runtime_speaker.py`）。同机。 | 会话存活期间。上下文只留最近若干轮，更早的轮次从内存列表去掉。 | 进程释放。不写入独立的到期任务。 |
| 儿童对话原始音频（监护人另授语料） | 仅当 `GET /v1/archive/session-raw-voice-consent` 返回 `archive_purpose=corpus_recording` 且同意未过期。Agent `ArchiveSink.publish_owner_turn`（`services/agent/src/archive_sink.py`）把 16 kHz WAV 发到 `POST /v1/archive/session-raw-audio`。对象在归档桶，前缀默认 `archive`，用途 `authorized-child-corpus`，region 默认 `cn-beijing`。索引在 PostgreSQL `guardian_corpus_samples`（`services/guardian/postgres_schema.sql`）。加密密钥版本记在行上。境内见上文核对条件。无此同意时，这条路径不写。策略对未成年人默认拒绝 `raw_audio_retention`；账户门 `raw_voice_archive` 只放成人（`services/policy/engine.py`、`services/control_api/app/account_gate.py`）。 | 同意书上的 `expires_at`，且代码强制短于或等于创建后 30 天（`services/guardian/corpus.py` 的 `MAX_CORPUS_RETENTION`）。每个未成年人同时最多 20 条未过期样本。 | `CorpusRetentionWorker` 默认每 300 秒调用 `purge_expired`：删除对象，并把行标上 `deleted_at`。行还在，标记为已删。按使用人删除会调用 `purge_minor`，同样删对象并标记。 |
| 尚未送达的音频与转写 | Voice Core 磁盘文件 `data/archive-events.spool`（可用 `MEMORIA_ARCHIVE_SPOOL_PATH` 改），Fernet 加密，默认上限 8 MiB（`services/agent/src/config.py`）。同机磁盘。盘满时先丢掉原始音频行，留下转写行。 | 直到 HTTP 投递成功并把该行从文件删掉。没有按会话、按儿童或按时钟的期限。 | 成功则从文件删除。进程在投递完成前崩溃时，下次 `replay` 会再次写入数据库或对象存储。 |
| 儿童话轮转写 | PostgreSQL `archive_evidence_events.payload` 的 `text` / `transcript` / `user_text` / `assistant_text`。写入点是 `append_session_event`（`services/control_api/app/routes/archive.py`）。有有效 `memory_retention` 监护同意时，这些字段保留。没有同意，或主体不是成人也不是已同意的未成年人时，写入前删掉这些字段，并标 `memory_retention=ephemeral_only`（`services/guardian/retention.py`）。`archive_transcript_versions` 有表、有行级安全和删除覆盖，当前服务代码没有向它 INSERT。同机 PostgreSQL。 | 有同意时：没有过期任务。`RETENTION_TTL` 的 30 天只记在 MemoryScope 记录上，不作用在证据行（`docs/compliance/p0-04-minor-safety-decisions.md`）。无同意时：原文不入库。 | 有同意时：对话结束不清。只有按使用人删除会按血缘和 `subject_id` 删证据行，并在 `verified_empty` 里核对。无同意时：无原文可到期。 |
| 长期记忆文字 | PostgreSQL `memory_claims.value`、`timeline_entries`、`knowledge_items`（`services/archive/postgres_memory_schema.sql`），以及 `memory_records.payload`（明文，`services/memory_scope/postgres_schema.sql`）。编译器读证据里的原文（`services/archive/postgres_memory_catalog.py`）。未成年人只保留自述、低敏、且领域属于 `study_progress` / `learning_preference` / `daily_life` 的条目（`services/archive/memory_write_policy.py`）。`value` 必须能从当轮原文追溯，因此可以是孩子原话的一段，不是改写后的摘要。家长回顾接口不返回 `ephemeral_only` 的证据。同机 PostgreSQL。 | 监护同意长期记忆时，策略义务是 `RETENTION_TTL` 30 天加 `NO_MODEL_TRAINING`（`services/policy/engine.py` 中 `retention_ttl_seconds=30 * 24 * 3600`）。该期限写入 MemoryScope 的 `retention_expires_at`。归档侧的 claim / timeline / knowledge 没有独立期限。 | MemoryScope 过期后 `MemoryRecord.is_visible` 返回不可见，读口隐藏。没有找到按 `retention_expires_at` 做物理删除的任务。`memory_records` 有禁止 UPDATE/DELETE 的触发器；物理删除只在按使用人删除时由维护角色执行。归档 claim 随证据行，对话结束不删。Redis 里已经发出的载荷不随隐藏而撤回。 |
| 会话路由 | Redis 键，内容只有会话 id、设备、账号、流世代和过期时间（`services/control_api/app/session_directory.py` 写明不含对话历史）。无 Redis 时只在进程内存。同机。 | 默认 300 秒租约，续租则顺延。 | 键到期后 Redis 删除。不含音频或转写。 |
| 记忆事件流 | Redis Stream `memoria:memory:events`，外加去重键 `{stream}:dedup:{event_id}`（`services/memory_scope/redis_outbox.py`）。载荷是记忆出站 JSON，可以含记忆正文，不含原始音频。同机 Redis。 | 流用近似裁剪，约 10 万条。去重键 2,592,000 秒（30 天）。 | 超出长度的旧条目被裁掉，不能按某个孩子撤回已经写入的条目。去重键到期由 Redis 删除。这是 `delete-domains.md` 已记录的缺口。 |
| 应用日志 | Voice Core 与媒体会话日志记录 `text_len`、原因码和计数，当前路径不记录转写原文（例如 `services/agent/src/providers/funasr_stt.py`、`services/agent/src/voice_core/media_session_commit.py`）。回复投递缓冲 `data/media-reply-delivery.spool` 的合同是不含转写和音频（`services/agent/src/voice_core/reply_delivery.py`）。生产 journald 在同一主机上；运维手册记录其体积约 1 GB，空间清理没有轮转它。 | 应用没有日志保留期限。journald 期限由主机策略决定，仓库未配置。 | 没有按儿童或按对话结束的删除。若某条异常响应把原文打进日志，它会留到主机日志策略淘汰为止。本设计未逐条审计全部异常分支。 |
| 声纹 | 儿童账号：`speaker_enrollment` 只允许已核验的成人（`account_gate.py`）。设备内部登记 `POST /v1/speaker/enrollments/internal` 和识别 `POST /v1/speaker/classify` 都走这道门，因此儿童 PCM 到不了声纹表。策略层仍把 `voice_profile_create` 放在未成年人可授权集合里（`minor_voice_profile_enrolled`），与账户门不一致；写入口以账户门为准。成人若登记：嵌入密文在 `speaker_profiles.template_ciphertext`，样本表 `speaker_enrollment_samples` 只存 SHA-256 和质量指标，不存 PCM（`services/speaker/postgres_schema.sql`）。登记瞬间的 PCM 会发给 CampPlus。同机 PostgreSQL；嵌入服务地域未在仓库写死。 | 无自动期限。 | 撤销时把 `template_ciphertext` 置空并标 `revoked`。按使用人删除覆盖 `speaker_*`。儿童路径今天没有行可删。 |
| 自定义音色样本 | 儿童账号：`voice_clone` 只允许成人；策略 `voice_clone_use` 对未成年人硬拒绝。小程序 profile 页的录音因此进不了儿童账号。成人样本在 `voice_samples` 加音色对象桶（region 默认 `cn-beijing`），厂商音色 id 在 `voice_profiles`，`provider_region` 由配置写入，测试与字段默认是 `cn-beijing`。 | 无产品侧自动期限。`provider_expires_at` 是厂商给出的时间，不是本仓库的删除任务。 | 撤销或按使用人删除时删除对象，并请求厂商删除音色。儿童路径今天不采集。 |
| 表达风格 | PostgreSQL `speech_style_stats`（句数、字数、时长、停顿、`tic_counts` 口头禅计数）和 `persona_traits.description`（可由口头禅生成短句，如常用某个词起句）。证据指针在 `persona_evidence`，指向 `archive_evidence_events`，本身不存整段转写。未成年人只学风格，不学价值取向和决策习惯（P0-04 D2）。同机。 | 无期限。 | 不随对话结束删除。按使用人删除会擦除并在删除末尾核对。口头禅计数是孩子用语的片段，不是整句转写，但属于儿童个人信息。 |
| 辅导记录 | PostgreSQL `tutor_practice_evidence.envelope_json` 与 `tutor_commit_outbox.archive_payload_json`。评估信封的字段是分数、技能、标准 id、时长和围栏，没有转写字段（`services/tutor/authority.py` 的 `_unsigned_payload`）。`tutor_study_progress` 是秒数、活跃日、薄弱点和已掌握技能的 JSON。同机。 | 无期限。 | 不随对话结束删除。按使用人删除覆盖这些行。实现时须用测试锁住信封键集合，防止以后把原话放进去。 |
| 危机提醒与每周小结 | `guardian_crisis_events` 只存证据 id、时间、话术版本。家长文案是固定句，不含原文，也不含严重程度（`services/guardian/crisis.py`，P0-04）。每周小结由计数聚合投影，测试要求载荷不含转写（`services/guardian/weekly_report.py`）。同机。 | 无期限。危机提醒留作监护记录。 | 不随对话结束删除。按使用人删除覆盖监护行。 |
| 厂商处理中的音频和文本 | 见上文「离开自有主机」的四个终点。不是 MinIO，也不是本仓库的表。 | 本仓库没有厂商保留期限，也不能在厂商侧到期删除。 | 不能填写「到期删除」。告知必须写明会交给哪一类厂商、目的是识别、生成回复或抽取记忆，以及拒绝后的后果是无法完成对话或无法形成长期记忆。 |
| 备份里的上述副本 | 发布手册中的 `pg_dump`，以及主机上的 `/var/backups/memoria`。WAL 归档手册记为 2026-10-06 起关闭。备份在同一台生产主机上，不在单独的异地桶描述里。 | 无儿童数据到期政策。空间清理明确不动数据卷和这份备份。 | 库内删除不会自动改写已有备份。恢复库会把已删的音频或原文带回来，除非先按 `scripts/replay_subject_deletions.py` 重放删除。对话结束没有对应动作。 |

现状里和验收直接冲突的有三处：

1. 监护人授予 `corpus_recording` 之后，儿童对话 WAV 会进入 MinIO，最长 30 天。这不满足「服务端没有儿童的对话音频」。
2. 监护人授予长期记忆之后，话轮原文留在 `archive_evidence_events.payload`，对话结束不清，也没有 30 天任务。这不满足「转写在对话结束后清掉」。
3. 留下的 `memory_claims.value` 可以是原话片段。它比整段转写窄，但还不是「文字摘要」，而且归档侧没有期限。

声纹和自定义音色在账户门上已经对儿童关闭。策略层的 `voice_profile_create` 仍对未成年人放行，只是写入口没有跟着放行。推荐方案把这一处也关上，避免以后新增入口绕过账户门。

## 两个方案

两者都只处理 `subject_category` 为 `minor` 的话轮，以及年龄不明时已经带 `DO_NOT_PERSIST` 的话轮。成人原始音频同意、成人声纹、成人音色样本保持今天的单独同意，不进这道门。

### 方案 A：入口不落盘

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

@dataclass(frozen=True)
class MemorySummary:
    subject_id: str
    text: str           # 有字符上限；不是当轮原文
    domain: Literal["study_progress", "learning_preference", "daily_life"]
    source_event_id: str

class ChildTurnBoundary(Protocol):
    def accept_persistent_audio(self, turn: TurnFacts) -> None: ...
    def durable_event(self, turn: TurnFacts) -> dict[str, object]: ...
    def summaries(self, turn: TurnFacts) -> tuple[MemorySummary, ...]: ...
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

def summaries(turn: TurnFacts) -> tuple[MemorySummary, ...]:
    if turn.subject_class == "unknown" or not guardian_memory_consent(turn):
        return ()
    drafted = draft_summaries(turn.transcript)  # 本进程内，写库前完成
    return tuple(item for item in drafted if summary_allowed(item))

def on_session_end(session_id: str) -> None:
    drop_process_buffers(session_id)  # PCM、转写、ContextManager
    refuse_spool_lines(session_id, kinds={"child_audio", "child_transcript"})
```

调用点有两个，类型相同：`ArchiveSink.publish_owner_turn` 在组包前调用 `accept_persistent_audio`；`append_session_event` 在 `record` 前调用 `durable_event`。摘要在同一进程、同一次话轮收尾时写入现有记忆编译入口，编译器不再回读证据里的原文。

`corpus_recording` 不再作为儿童对话音频的写入理由。同意种类可以留在监护表里供历史行识别，但会话接口对未成年人一律返回 `allowed: false`。

### 方案 B：先落盘，会话结束再集中清除

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

### 对比

|  | 方案 A 入口不落盘 | 方案 B 会话结束清扫 |
|---|---|---|
| 接口 | 一个 `ChildTurnBoundary`，两个调用点。函数要么拒绝音频，要么返回去掉原文的事件。没有新的跨服务事务。 | 一个 `SessionEndSweep`，但要认识每一种已经写过的介质，并和编译器、spool 重放、Redis 裁剪排序。 |
| 出错面 | 摘要若在进程崩溃前没写完，这一轮没有长期记忆。音频和原文没有落盘，崩溃不会把它们留在库里。spool 若混入儿童行，重放时拒绝而不是修复。 | 崩溃发生在写入和清扫之间时，音频和原文已经在 PostgreSQL、MinIO、spool、备份和 Redis 里。清扫漏一种介质就会验收失败。证据行上的原文被清空后，编译器重试会失去输入。Redis 已发出的条目删不掉。备份不会跟着变。 |
| 和验收的关系 | 对话进行中，持久存储里也没有儿童音频。转写不进入持久存储，所以「对话结束后清掉」落在进程内存上，边界清楚。 | 对话进行中服务器上有音频和原文。「用完即删」依赖清扫成功。验收要证明所有介质都清了，包括清扫失败后的重试。 |
| 对现有记忆链路 | 编译器改吃 `MemorySummary`，不再读 `payload.text`。未成年人窄投影可以留在摘要准入里。 | 编译器可以暂时不动，但必须在清扫前跑完，两套期限缠在一起。 |

推荐方案 A。儿童音频用「不写入」满足「服务器上没有」；转写用「不进入持久存储、进程随会话释放」满足「对话结束后清掉」。方案 B 的清扫列表会跟着每一种新缓存增长，而且补不上备份和已经裁剪不掉的 Redis 历史。方案 A 仍保留一个很窄的失败关闭动作：发现 spool 或对象键属于儿童音频或儿童转写时，删除并拒绝重放，不把它们当成正常路径。

## 推荐方案下的告知表

下表是方案 A 实现之后可以放进隐私声明的目标表述。实现和核对完成之前，它不是现状。

成人声纹、成人自定义音色、成人原始音频不在本表。它们继续各自的单独同意；儿童账号不提供这些开关。

| 数据 | 存储地点（系统 / 表 / 桶；是否境内） | 保留期限 | 到期后的处理方式 |
|---|---|---|---|
| 孩子说话的录音 | 只在机器人体内和服务器内存中处理，用于听清这一句并生成回复。不写入 PostgreSQL、对象存储、Redis、磁盘缓冲或日志。服务器若在境内，以部署清单为准，条件与现状节相同。 | 这一轮对话期间。 | 对话结束（道别、连续无语音进入待机、连接断开或进程退出）时从内存释放。不保留、不备份。 |
| 交给语音识别厂商的录音 | 阿里云百炼 FunASR，用于把这一句转成文字。不进入自有数据库。 | 由与厂商的合同约定；本产品不在自有服务器保存这份录音。 | 自有侧没有副本。告知需附上厂商名称和合同中的保存期限；合同未约定前，这项不能写成「到期删除」。 |
| 这一轮孩子说的原话，以及机器人复述进去的原话 | 只在生成回复和生成摘要的服务器内存中。不写入 `archive_evidence_events` 的文本字段，不写入 `archive_transcript_versions`，不写入日志正文。 | 这一轮对话期间。 | 对话结束时从内存删除。若磁盘缓冲里出现这类原文，重放时丢弃该行，不入库。 |
| 交给对话模型的文字 | 百炼或 DeepSeek，用于生成当轮回复。可含这一轮的原话。 | 由与厂商的合同约定。 | 同语音识别：自有侧不留原文；厂商期限以合同为准。 |
| 长期记忆摘要 | 家长在绑定中同意长期记忆之后，写入 PostgreSQL 的记忆记录（摘要文本，有字符上限，不是逐字对话）。同机数据库，境内以部署清单为准。Redis 事件流只携带这条摘要的出站副本。 | 沿用已定的 30 天（`RETENTION_TTL`），除非产品另行改期。不超过该期限。 | 到期后从产品中不再展示。物理删除使用与按使用人删除相同的维护角色删除，并留下可核对的计数。30 天以前已经发出、尚未被 Redis 裁掉的摘要副本，随约 10 万条的流裁剪消失；告知里写明这段裁剪，不写成按人即时撤回。 |
| 没有长期记忆同意时的对话 | 内存，同上。 | 这一轮对话。 | 结束即丢。不生成摘要，不写入记忆表。 |
| 声纹 | 不采集，不存储。没有对应的表行、对象或厂商音色嵌入。 | 不适用。 | 不适用。账号关闭此功能。 |
| 自定义音色样本 | 不采集，不存储。没有 `voice_samples` 行，也没有音色桶对象。 | 不适用。 | 不适用。账号关闭此功能。 |
| 表达风格（语速、句长、口头禅统计） | PostgreSQL `speech_style_stats` 与风格类 `persona_traits`。不含整句原话。同机。 | 账号存续期间，直到家长撤销学习或删除该使用人。本设计不新增长期期限。 | 撤销或按使用人删除时擦除，并在删除核对中计数。 |
| 学习进度与每周小结 | PostgreSQL 辅导进度与监护小结。内容是时长、技能和趋势，不含原话。同机。 | 账号存续期间。 | 按使用人删除时擦除并核对。 |
| 危机提醒 | PostgreSQL 监护危机行，以及发给家长的固定微信文案。不含孩子原话。同机；微信侧按微信规则投递。 | 作为监护通知记录保留，直到该使用人被删除。 | 按使用人删除时擦除并核对。固定文案本身不含儿童原话。 |
| 日志 | 服务器日志只记长度、原因和计数，不记原话，不记录音。journald 在同一主机。 | 主机日志策略。应用不延长它。 | 不含儿童原话或录音，因此没有儿童对话内容可删。若核对发现某条日志含原文，按事故处理：停写、清理、再核对。 |
| 备份 | 数据库备份在同一台主机。方案 A 下新备份不应含儿童对话音频和话轮原文。 | 与主机备份策略相同。 | 上线前已有的备份若含旧语料或旧原文，按待决事项做一次清理或隔离，避免恢复时写回。之后的删除重放流程保持不变。 |

第十条还要求说明目的、方式和拒绝的后果。与本表配套、但不必另建存储设计的句子是：录音和原话只为完成当轮对话；摘要只为家长已同意的长期记忆；拒绝长期记忆则只做当轮对话，不记摘要；拒绝把录音交给识别厂商则无法进行语音对话。安全保障沿用现有做法：对象加密、数据库行级隔离、内部写入令牌、按使用人删除收据。这些句子等产品定稿后写入隐私声明，不在本文件展开成另一份政策。

## 波及与如何确认没有弄坏

方案 A 不动设备协议，也不改成人声纹、成人音色、成人原始音频的同意模型。

| 已有功能 | 会碰到什么 | 怎样确认没有弄坏 |
|---|---|---|
| 声纹门禁 | 未成年人 `voice_profile_create` 从策略允许集移出，与 `speaker_enrollment` 的成人门对齐。成人登记、识别、撤销仍走 `services/control_api/app/routes/speaker.py`。 | 现有成人声纹测试保持通过。加一条：未成年人内部登记和识别返回与账户门相同的拒绝码，且 `speaker_profiles` 无新行。 |
| 自定义音色 | 儿童路径今天已被 `voice_clone` 拒绝。方案 A 不新开入口。 | `services/control_api/app/routes/voice.py` 的未成年人用例仍为 403。成人登记、撤销和厂商删除用例不改断言。 |
| 档案与回顾 | 儿童证据不再带原话。时间线已经跳过 `ephemeral_only`；实现后儿童行应没有文本字段，而不是只靠过滤器掩盖。 | `services/control_api/tests/test_archive_subject_scope.py` 扩展为：有长期记忆同意的儿童话轮，证据行仍在，`text` 等字段不在。家长回顾接口不出现原话。 |
| 记忆生成 | 编译器今天读 `payload.text`。要改成只接受 `MemorySummary`，并继续用 `filter_extraction_for_subject` 的未成年人窄域。 | `services/archive/tests/test_memory_catalog.py` 里未成年人投影用例改为喂摘要。确认敏感句、非自述、域外内容仍然丢弃。无同意时零条摘要。 |
| 表达风格 | 风格统计今天可以读整句来数口头禅。摘要化之后，统计只能来自会话内存里的当轮文本，不能回读已清掉的证据。 | 人格测试里未成年人 `style_only` 仍不产生价值取向和决策习惯。统计行不含整句。 |
| 删除核对 | `verified_empty` 已覆盖证据、转写表、记忆和人格。要增加：儿童主体没有 `guardian_corpus_samples` 的未删除对象，证据 payload 没有文本键，spool 重放不会把儿童音频写回去。 | `services/governance/tests/test_subject_deletion.py` 与 `test_subject_archive.py`。核对结果仍不称为「已擦除」。append-only 且无法按主体定位的表继续按 `delete-domains.md` 报缺口。 |
| 辅导与危机 | 信封不含原话，方案 A 不改字段。归档投影若把原话抄进辅导 outbox，应在写入边界被去掉。 | `services/tutor/tests/test_evidence.py` 锁住信封键。危机用例继续断言家长文案无原文。 |
| 评测与验收脚本 | 真机 findings 用 `speech.utterance_finalized` 的原文做相似度。实现后生产库不再有这段原文，脚本不能再向儿童会话要原文。 | 脚本改为断言儿童事件无文本字段、语料对象数为 0，相似度改在测试夹具里做，不读生产儿童库。不改历史 `docs/acceptance/` 收据。 |
| 文档预算 | 本文件放在 `docs/compliance/`，属于 `tests/test_documentation_budget.py` 允许的前缀。 | `uv run pytest tests/test_documentation_budget.py`。不新增其他文档类别。 |
| 既有 30 天记忆决定 | 摘要的期限仍是 P0-04 的 30 天。本设计不把它改成会话级。 | 策略测试里 `minor_memory_guardian_authorized` 的义务仍是 `RETENTION_TTL` 与 `NO_MODEL_TRAINING`。 |

## 工作量

实现集中在一条边界上，不改 ESP32 协议，也不改 Go Media Edge 的环缓冲。

- 新增 `ChildTurnBoundary` 的判定与两个调用点：`ArchiveSink.publish_owner_turn`、`append_session_event`。儿童和年龄不明的会话不再接收持久音频，事件载荷去掉文本字段。
- 关闭未成年人的 `corpus_recording` 会话写入。保留表和历史行，供一次性清理和删除核对。
- 记忆编译改为消费 `MemorySummary`。摘要在 Voice Core 进程内、话轮结束时生成，再进入现有的窄域过滤。
- 策略把 `voice_profile_create` 从未成年人可授权集合移出。账户门保持成人专用。
- 会话结束时释放进程内 PCM 和转写。spool 重放遇到儿童音频或儿童原文则丢弃。
- 到期摘要的物理删除接到已经存在的维护角色删除上，并给出计数。这是为了让 30 天不只是读口隐藏。
- 上线前的一次性清理：已有 `guardian_corpus_samples` 对象、证据 payload 里的儿童原文。清理设计在实现阶段写，本文件不带脚本。
- 测试改动落在归档主体范围、语料、记忆投影、声纹拒绝、删除核对。文档预算测试只因本文件位于 `docs/compliance/` 而继续通过。

不在范围内：厂商合同谈判、隐私声明排版、小程序文案、成人路径改造、设备协议。

## 风险

- 语音识别和对话模型仍会在当轮收到录音或原话。自有服务器没有副本，不等于厂商没有副本。在合同写明期限之前，隐私声明不能使用「全链路用完即删」。
- 摘要若写得太短，第二天的回忆会变差；若把原话整句放进摘要，就退回现状。字符上限和可否引用原话是产品决定，不是实现时自行放宽。
- `memory_records` 默认不能改、不能删。摘要若只靠 `is_visible` 隐藏，数据库和备份里仍有正文，达不到「不超过必要期限」的可核对删除。所以推荐方案要求走到维护角色的物理删除，而不是只改读口。
- 上线前已经入库的语料和原文还在对象存储、证据表和旧备份里。只改写入路径不会把它们清掉。
- Redis 流不能按孩子删除。摘要出站之后，最坏会留到约 10 万条裁剪。告知要写这句话。
- 日志现状是记长度。一旦某条异常把原文打出来，方案 A 的边界罩不住。实现时要有一条扫描测试：儿童会话的日志记录不含 `transcript` 字段值。
- 生产主机和桶是否在境内，仓库不能证明。告知写错地域是合规风险，不是实现风险。

## 待决事项

需要产品或负责人拍板之后才能实现。未拍板的项，实现时保持更严的一侧（不落音频、不落原文、儿童不开声纹和音色）。

1. 对外告知的年龄范围。工程按全部未成年人和年龄不明处理。告知是只写不满 14 周岁，还是与 D1 一致写全部未成年人。3 到 10 岁是产品聚焦，不是代码里的单独年龄档。
2. `corpus_recording` 是否废止。本设计把它从儿童对话路径拿掉。若仍要做研究语料，那是另一项同意和另一份期限，不属于这次验收。
3. 摘要可以多像原话。现状的 claim 必须能从原话追溯，所以会留下原话片段。验收写的是「文字摘要」。需确定字符上限，以及摘要是否允许整句引用。
4. 机器人说的话算不算儿童个人信息。若回复复述了孩子的话，推荐方案把这句也当转写，不入库。不含孩子原话的回复是否也要在对话结束时从内存丢掉，建议是丢掉，只把摘要留下。
5. 30 天是否仍然是摘要的期限。本设计不改 P0-04。若要改短或改长，应单独决定，并同时改策略参数。
6. 厂商期限。FunASR、对话模型、豆包、记忆抽取的合同期限和地域，是否接受「自有侧不留、厂商按合同」作为告知写法。不接受则不能上线语音对话。
7. 境内表述。是否以 `110.42.235.198` 和 `cn-beijing` 默认值作为「境内服务器」的依据，还是先核对机房和桶 endpoint 再写进隐私声明。
8. 历史数据。已入库的儿童语料、证据原文、含这些内容的 `pg_dump`，是上线前物理删除并核对，还是隔离到不再恢复。恢复流程今天会把删除前的行带回来。
9. 口头禅统计是否继续。`tic_counts` 和风格描述含孩子用词的片段。继续保留、还是儿童账号只保留语速和句长这类不含用词的计数。
10. 会话结束的定义是否就用现有待机条件：连续无主人语音、明确道别、断开。崩溃由「未落盘」覆盖，不依赖清扫任务跑完。

## 不在本文件里做的事

不提交实现、不改策略常量、不清理生产数据、不改 `HANDOFF.md` 或 `TODOLIST.md`。实现时以本文的方案 A 和拍板结果为准；与 `delete-domains.md` 冲突时，删除核对的表述纪律优先。
