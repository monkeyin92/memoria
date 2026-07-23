# 豆包个人音色接入 Memoria 实时级联 TTS 官方资料核验

> 核验日期：2026-07-23  
> 范围：火山引擎/豆包声音复刻 2.0、V3 语音合成接口、合规要求、音色生命周期、配额监控，以及 Memoria 当前 `voice_profile` 与豆包 TTS 代码接缝。  
> 证据边界：只把火山引擎当前官方文档和当前仓库源码视为事实。未在官方公开文档中确认的删除接口、字段映射、地域约束、固定并发和首包 SLA 一律标记为未知，不作推断。  
> 本文是 S8 实施前研究，不代表已经调用真实声音复刻服务、完成本人授权、主观盲测、真实设备验收或生产发布。

## 1. 执行结论

豆包当前公开能力足以支持“本人批准的个人音色进入 Memoria 级联实时语音”，但不能通过删除现有 409、把 `speaker_id` 填进当前 `seed-tts-2.0` 配置来完成。

官方已确认：

1. 个人音色可通过 `POST https://openspeech.bytedance.com/api/v3/tts/voice_clone` 上传样本并训练；通过 `POST https://openspeech.bytedance.com/api/v3/tts/get_voice` 查询训练状态。[官方：音色训练HTTP](https://www.volcengine.com/docs/6561/2534906) [官方：音色查询HTTP](https://www.volcengine.com/docs/6561/2535742)
2. V3 双向流式合成入口为 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`，面向实时交互，支持文本流式输入和音频流式输出，声音复刻也在该接口能力范围内。[官方：双向流式语音合成WebSocket](https://www.volcengine.com/docs/6561/2532486) [官方：HTTP Chunked/SSE单向流式-V3 中的 API 列表](https://www.volcengine.com/docs/6561/1598757)
3. 通用 TTS 2.0 与声音复刻 2.0 使用不同的 `X-Api-Resource-Id`：前者为 `seed-tts-2.0`，后者为 `seed-icl-2.0`。[官方：HTTP Chunked/SSE单向流式-V3](https://www.volcengine.com/docs/6561/1598757)
4. 音色状态存在 `Training`、`Success`、`Active`、`Expired`、`Reclaimed` 等阶段，并能查询到 `ExpireTime`。[官方：音色查询HTTP](https://www.volcengine.com/docs/6561/2535742) [官方：音色管理HTTP](https://www.volcengine.com/docs/6561/2235883)
5. 官方要求业务方取得用户隐私信息收集同意；处理个人生物特征还应按适用法律取得处理同意，不得处理他人声音数据或侵犯他人权利。[官方：语音模型体验服务免责声明和服务规则说明](https://www.volcengine.com/docs/6561/1866421)

当前仍有两个实施阻塞项：

1. 本次核验的当前公开训练、查询和音色管理文档中，没有找到声音复刻音色的公开删除/撤销 API。Memoria 可以先做到本地撤销立即生效，但不能把 provider 物理删除伪装成已确认能力。
2. 官方文档同时出现训练侧 `speaker_id` / `custom_speaker_id`、管理侧 `IclSpeakerId`、合成侧 `speaker`。现有公开正文没有把三者的映射契约说明完整，实装前必须用真实 provider smoke 确认“哪个值才是 `seed-icl-2.0` 合成请求里的 speaker”。

## 2. 官方能力与明确未知项

| 主题 | 已确认官方事实 | 不能猜测的部分 |
|---|---|---|
| 训练 | `POST /api/v3/tts/voice_clone`；请求包含唯一音色代号和 base64 音频 | 响应中的哪个字段可直接作为实时 TTS `speaker` |
| 查询 | `POST /api/v3/tts/get_voice`；状态 2/4 可用于 TTS；`demo_audio` 在成功时返回且一小时有效 | 查询接口是否始终返回 synth-ready `IclSpeakerId` |
| 管理 | `BatchListMegaTTSTrainStatus` 可查状态、到期时间、`ModelTypeDetails`、`IclSpeakerId`、`ResourceID` | 是否必须经管理接口才能完成 `speaker_id -> IclSpeakerId` 映射 |
| 模型 | 通用 TTS 2.0 为 `seed-tts-2.0`；声音复刻 2.0 为 `seed-icl-2.0` | 账号实际开通的是字符版、并发版还是其他售卖形态 |
| 实时合成 | V3 双向 WebSocket 面向实时交互，支持声音复刻、文本流式输入和音频流式输出 | 当前账号的首包延迟、稳定并发、字级时间戳精度 |
| 韵律 | 支持语速、音量、音高；部分音色支持 emotion；2.0/表现力增强版本支持 `context_texts` | 个人复刻音色实际支持哪些 emotion；`context_texts` 是否满足 Memoria 时间戳门禁 |
| 水印 | V3 文档公开 `aigc_watermark` 与 `aigc_metadata` 能力 | 双向接口当前完整参数结构、PCM 输出是否承载 metadata 水印 |
| 删除 | 未使用的后付费试听音色 7 天内未正式调用会由系统删除 | 用户主动撤销后的公开 provider 删除 API |
| 配额 | 可通过 `QuotaMonitoring` / `UsageMonitoring` 查询账号资源的限制和用量 | 固定 QPS、固定并发、固定首包 SLA |

## 3. 训练与查询接口

### 3.1 音色训练

官方当前训练接口：

```text
POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
Content-Type: application/json
X-Api-Key: <api key>
X-Api-Request-Id: <uuid>
```

来源：[音色训练HTTP，更新于 2026-07-08](https://www.volcengine.com/docs/6561/2534906)

已确认的关键请求约束：

- `speaker_id` 为必填的唯一音色代号。
- 后付费自定义名称使用固定 `speaker_id="custom_speaker_id"`，实际名称放在 `custom_speaker_id`。
- 自定义名称为 8–256 字符，需以英文字母开头，只支持数字、大小写字母、中划线和下划线，并受官方前后缀冲突规则限制。
- 音频支持 `wav`、`mp3`、`ogg`、`m4a`、`aac`、`pcm`。
- PCM 仅支持 24 kHz、单声道。
- 上传文件最大 10 MB。
- 音频数据以 base64 传入。

这些约束与当前 Memoria domain 不完全一致：

- `services/voice_profile/domain.py` 当前允许样本最大 15 MiB，豆包训练接口上限是 10 MB。
- 当前请求只记录 `sample_rate`，没有记录声道数，无法在进入 provider 前证明 PCM 为单声道。
- 当前 `VoiceEnrollmentProvider.create_voice()` 接收 `sample_url`，豆包接口需要 base64 音频和格式，不直接消费 URL。

S8 最小实现可以由 Doubao adapter 在 control-api 内下载现有短期签名 `sample_url`、校验大小/格式/PCM 24 kHz 单声道后转 base64，暂不扩大公共 domain 接口。若之后继续接多个需要原始字节的 provider，再把 provider port 深化为显式样本对象。

### 3.2 样本质量

官方声音复刻 2.0 最佳实践建议：

- 使用 14–30 秒 WAV；
- 低噪声、单人、单轨；
- 人声清晰；
- 情绪尽量一致，避免过大起伏或过于平淡；
- 中英混合场景最好在样本中同时覆盖中英文。

来源：[声音复刻2.0最佳实践，更新于 2026-04-21](https://www.volcengine.com/docs/6561/2298705)

这里是效果建议，不应被写成未经证实的 provider 硬限制。Memoria 可以继续把 10–60 秒作为产品录制边界，但提交豆包前应将 14–30 秒作为质量门禁或显著提示，并用真人盲测决定是否批准。

### 3.3 查询状态

官方当前查询接口：

```text
POST https://openspeech.bytedance.com/api/v3/tts/get_voice
Content-Type: application/json
X-Api-Key: <api key>
X-Api-Request-Id: <uuid>
```

来源：[音色查询HTTP，更新于 2026-07-07](https://www.volcengine.com/docs/6561/2535742)

已确认状态：

| 数值 | 状态 | 含义 |
|---|---|---|
| 0 | `NotFound` | 未找到 |
| 1 | `Training` | 训练中 |
| 2 | `Success` | 训练成功，可调用 TTS |
| 3 | `Failed` | 训练失败 |
| 4 | `Active` | 已激活，可调用 TTS |

其他已确认字段：

- `available_training_times`：剩余训练次数；
- `model_type=5`：复刻 2.0；
- `demo_audio`：`Success` 时返回的试听音频链接，一小时有效。

Memoria 不应在首次训练 HTTP 200 后直接把 profile 标记为 candidate。正确流程应是：

```text
provider_submitted
    -> Training
    -> Success
    -> 拉取并持久化 synth-ready speaker/resource/expiry
    -> candidate
    -> blind trial + subjective evaluation + objective probe
    -> active
```

训练超时、查询失败或未知状态继续留在可 reconciliation 的中间态，不能视为失败后重新创建另一个 provider 资产。

## 4. 生命周期、收费与到期

官方使用指南区分预付费和后付费音色：

- 预付费音色可在未激活时再次训练，文档说明至多共 15 次；启用后不能再次训练。
- 后付费音色首次用于正式语音合成时才收取音色槽位费并固定音色，此后不能再次训练。
- 后付费试听音色如果 7 天内没有正式调用，系统会删除。

来源：[声音复刻下单及使用指南，更新于 2026-06-26](https://www.volcengine.com/docs/6561/1167802)

因此，Memoria 的盲测和质量探针会产生真实调用、可能触发“转正/收费”。实现前必须明确：

1. 使用预付费还是后付费产品；
2. 哪一种试听调用不会触发正式转正；
3. 自动质量探针是否会产生槽位费；
4. 测试账号与生产账号如何隔离。

这些属于账号实际开通和计费契约，不能仅凭通用文档推断。

官方音色管理接口：

```text
Host: open.volcengineapi.com
Region: cn-north-1
Service: speech_saas_prod
Version: 2023-11-07
Action: BatchListMegaTTSTrainStatus
```

来源：[音色管理HTTP，更新于 2026-06-23](https://www.volcengine.com/docs/6561/2235883)

它公开的状态包括：

| 状态 | 官方含义 |
|---|---|
| `Unknown` | SpeakerID 尚未训练 |
| `Training` | 训练中 |
| `Success` | 训练成功，可 TTS |
| `Active` | 已激活，无法再次训练 |
| `Expired` | 控制台实例过期或账号欠费 |
| `Reclaimed` | 控制台实例已回收 |

返回示例还包含 `ExpireTime`、`AvailableTrainingTimes` 和 `ModelTypeDetails`；后者示例中包含 `IclSpeakerId` 与 `ResourceID`。S8 应把 provider 到期时间写入现有 `provider_expires_at`，并在 `Expired` / `Reclaimed` 时立刻停止解析个人音色。

## 5. `speaker_id`、`IclSpeakerId` 与 `resource_id`

### 5.1 已确认的资源区分

V3 语音合成通过连接请求头 `X-Api-Resource-Id` 选择模型效果和计费产品：

| 资源 ID | 官方用途 |
|---|---|
| `seed-tts-2.0` | 豆包语音合成模型 2.0 |
| `seed-tts-1.0` | 豆包语音合成模型 1.0 字符版 |
| `seed-tts-1.0-concurr` | 豆包语音合成模型 1.0 并发版 |
| `seed-icl-2.0` | 豆包声音复刻模型 2.0 字符版 |
| `seed-icl-1.0` | 声音复刻 1.0 字符版 |
| `seed-icl-1.0-concurr` | 声音复刻 1.0 并发版 |

来源：[HTTP Chunked/SSE单向流式-V3，更新于 2026-05-25](https://www.volcengine.com/docs/6561/1598757)

当前 Memoria 默认伙伴音色使用 `seed-tts-2.0` 是正确的；个人复刻音色不能继续伪装成同一个 model/resource。

### 5.2 不应猜测的 ID 映射

官方训练/查询文档使用：

- `speaker_id`
- `custom_speaker_id`

官方管理接口示例使用：

- `SpeakerID`
- `ModelTypeDetails[].IclSpeakerId`
- `ModelTypeDetails[].ResourceID`

官方合成文档使用：

- `speaker`

本次公开文档核验不能证明以上字段值相同。S8 provider smoke 必须记录脱敏后的字段名、状态和相等关系，确认：

1. 训练请求使用的 `speaker_id` 是否也是合成 `speaker`；
2. 如果不是，是否应使用 `IclSpeakerId`；
3. `IclSpeakerId` 是否会因模型版本变化；
4. `ResourceID` 是否必须与该 ID 成对绑定；
5. 升级、续费或回收后 ID 是否变化。

在确认前，数据库中的 `provider_voice_id` 应被理解为“最终 synth-ready speaker ID”，不能直接等同于训练请求 ID。

## 6. 实时双向合成

官方当前 API 列表将以下接口定义为实时交互场景：

```text
wss://openspeech.bytedance.com/api/v3/tts/bidirection
```

它支持文本实时流式输入、音频流式输出，并覆盖语音合成、声音复刻和混音。

来源：[双向流式语音合成WebSocket](https://www.volcengine.com/docs/6561/2532486) [HTTP Chunked/SSE单向流式-V3 中的 API 列表](https://www.volcengine.com/docs/6561/1598757)

需要保留的证据限制：

- 当前环境能确认页面标题、入口、鉴权头和高层能力，但没有稳定取得该页面完整、当前的帧级正文。
- 因此本文不重新抄写或推断 EventType、消息序列、取消事件、时间戳事件的完整契约。
- 当前仓库已经实现的 `StartConnection`、session、incremental text、cancel、subtitle 等协议仍需用 `seed-icl-2.0` 真实 smoke 逐项验证，不能因为 `seed-tts-2.0` 已工作就自动视为 clone 资源等价。

### 6.1 当前连接池的关键接缝

`services/agent/src/providers/doubao_tts.py` 在 WebSocket 建连时把 `resource_id` 写入 `X-Api-Resource-Id` 请求头。当前热池是在默认 `seed-tts-2.0` 下创建的。

因此，只在 `apply_voice_profile()` 里把可变配置改成 `seed-icl-2.0` 不会改变已经建立的 WebSocket 请求头。这会形成“内存显示 clone resource，真实连接仍按 baseline resource 鉴权”的错误。

S8 最小正确实现应满足其一：

1. 连接池按 `resource_id` 分桶，至少维护 `seed-tts-2.0` baseline pool 与 `seed-icl-2.0` personal clone pool；或
2. resource 变化时丢弃旧池连接并按新请求头重建。

推荐第一种。撤销或过期后的下一 generation 可以直接回到已经预热的 baseline pool，避免回退首包突然变慢。

每条池连接还应记录其建连时的 `resource_id`，acquire 时断言目标 resource 一致；不能只读取当前可变 config。

### 6.2 generation fence

当前 runtime 会在用户开始说话时刷新 voice profile，并在回复生成前等待最新 refresh，再应用缓存音色。这个接缝可以复用：

- `services/agent/src/duplex_runtime.py::on_user_voice_started`
- `services/agent/src/agent.py::_apply_cached_voice_profile`

但个人音色选择必须复制进当前 generation/stream 的不可变快照，至少包含：

```text
profile_id
provider
resource_id
speaker_sha256
provider_expires_at
resolution_epoch
```

后续异步任务不得读取“当前最新 profile”替代当前 fence 的声音归属。撤销在下一 generation 生效；已取消的旧 generation 继续由现有 `GenerationFence` 和连接丢弃机制阻止尾音。
raw speaker ID 只在服务端解析和 TTS 发送链路内短暂使用，manifest、H5 与 Archive 均只持有
SHA-256。Self Preview 还必须单独冻结所选伙伴的设计音色作为安全回退；该回退不加载伙伴人格。

## 7. 韵律、情感与发音

V3 文档公开的通用控制包括：

- `speech_rate`：`[-50, 100]`，对应约 0.5–2.0 倍速；
- `loudness_rate`：`[-50, 100]`；
- 后处理 `pitch`：`[-12, 12]`；
- `emotion`：仅部分音色支持，范围因音色而异；
- `emotion_scale`：1–5；
- `context_texts`：用于对话式合成、情绪、语气、速度、音量等控制。

来源：[HTTP Chunked/SSE单向流式-V3](https://www.volcengine.com/docs/6561/1598757) [语音指令与标签，更新于 2026-07-17](https://www.volcengine.com/docs/6561/1871062)

声音复刻 2.0 最佳实践明确给出 `context_texts` 用法：可以传一句语音指令，也可以传 LLM 上文 query，帮助回复语音更符合对话情境。[官方：声音复刻2.0最佳实践](https://www.volcengine.com/docs/6561/2298705)

但 Memoria 当前对 `seed-tts-2.0` 的实测发现 `context_texts` 会造成字幕时间戳明显漂移，因此代码主动禁用。S8 首版应继续禁用个人音色的 `context_texts`，只保留当前受控 rate；之后单独对 `seed-icl-2.0` 做：

- 首包；
- 字幕/音频偏差；
- 长句完整率；
- 打断取消尾音；
- 情绪遵循度；
- 专有词发音；
- 弱网与连接重建。

只有这些探针通过后，才允许按个人音色/模型能力逐步启用 emotion 或 `context_texts`。不能把通用 TTS 音色列表的 emotion 标签自动套到个人复刻音色。

## 8. 水印与反滥用

V3 单向流式正文公开：

- `aigc_watermark`：在合成结尾增加音频节奏标识；
- `aigc_metadata`：在支持的 mp3/wav/ogg_opus header 中加入隐式 metadata 水印；
- metadata 可携带内容制作方、制作编号、传播方和传播编号。

来源：[HTTP Chunked/SSE单向流式-V3](https://www.volcengine.com/docs/6561/1598757)

当前需要明确区分：

- 官方已公开 V3 产品具备水印能力；
- 本次未确认双向 PCM 流是否支持与单向接口完全相同的参数和承载方式；
- PCM 没有 mp3/wav 文件 header，不能假设 `aigc_metadata` 会自动生效；
- 节奏水印是否影响打断尾音、听感或字级对齐，需要真实探针。

官方合规说明要求用户同意和不得处理他人声音，但本次没有找到“provider 自动验证声源本人授权”或“自动反冒用检测”的公开 API 契约。[官方：语音模型体验服务免责声明和服务规则说明](https://www.volcengine.com/docs/6561/1866421)

因此，Memoria 仍需自己承担：

- step-up 身份确认；
- 明示用途、保存期、撤销方式；
- 本人对样本权利和授权的确认；
- 禁止上传他人声音；
- 训练、试听、激活、合成、撤销的最小审计；
- 敏感日志不记录音频、完整 speaker ID 或凭据；
- 可选水印策略的真实兼容性验证。

## 9. 删除与撤销

### 9.1 已确认

- 本地 consent 可撤销后不再解析 active profile，这是 Memoria 当前已经具备的产品控制面。
- 官方说明后付费试听音色 7 天未正式调用会由系统删除。[官方：声音复刻下单及使用指南](https://www.volcengine.com/docs/6561/1167802)
- 官方状态接口存在 `Expired` 和 `Reclaimed`。[官方：音色管理HTTP](https://www.volcengine.com/docs/6561/2235883)

### 9.2 未确认

本次核验的当前公开文档中没有找到：

- `DELETE /voice_clone/...`；
- `delete_voice`；
- `DeleteSpeaker`；
- 用户主动撤销后立即物理删除 provider 资产的公开 Action。

因此 S8 不应让一个未经确认的 provider 删除操作阻塞本地撤销。建议状态拆分为：

```text
product_status = revoked
runtime_resolution = fallback
provider_cleanup_status = unsupported | pending | completed | failed
```

用户撤销后：

1. 当次控制面立即标记 revoked；
2. 下一 generation 必须回退批准的伙伴音色；
3. provider cleanup 按官方最终确认的 API 或人工流程执行；
4. 未确认物理删除前，UI 和审计不得显示“云端声音资产已删除”。

当前 `VoiceProfileManager.revoke_profile()` 期望 `provider.delete_voice()` 完成才返回 deletion completed。Doubao provider 接入时需要调整为上述双状态语义，不能用空实现返回成功。

## 10. 配额、调用量与延迟

官方提供：

- `QuotaMonitoring`：查询 qps、concurrency、qpm、tpm 等 quota 类型和 limit；
- `UsageMonitoring`：查询调用量；
- `QPS/并发查询接口说明`：声音复刻字符版/并发版的监控 ResourceID 分别为 `volc.megatts.default` / `volc.megatts.concurr`。

来源：

- [QuotaMonitoring - Quota查询接口](https://www.volcengine.com/docs/6561/1801956)
- [调用量查询接口说明](https://www.volcengine.com/docs/6561/1476625)
- [QPS/并发查询接口说明](https://www.volcengine.com/docs/6561/1476626)

公开文档没有给所有账号通用的固定并发数或首包 SLA。实际限制取决于项目、商品和购买资源，S8 应在预发布/生产账号上读取 quota，并把下面指标作为 Memoria 自己的发布门禁，而不是厂商 SLA：

- 首音频 `<= 1500 ms`；
- cancel tail `<= 250 ms`；
- 时间戳误差 `<= 250 ms`；
- 200 字以上长句完成率 `>= 0.98`；
- 连接池达到配置 warm size；
- quota 余量满足预估峰值；
- 限流时立即回退 baseline，不重试叠加并发。

这些阈值来自当前 Memoria `voice_profile` domain，不是火山引擎公开承诺。

## 11. 当前仓库接缝

### 11.1 可直接复用

当前 `services/voice_profile` 已经有：

- consent grant/revoke；
- 样本加密存储；
- 幂等 enrollment operation；
- provider reconciliation；
- blind trial；
- 主观评分；
- 首包、尾音、时间戳、长句质量门禁；
- active/revoked/expired fallback；
- SQLite/PostgreSQL 双实现；
- `provider_expires_at`。

这些正是 S8 需要的生命周期骨架，不应另建一套 Doubao 专用 profile 表。

### 11.2 当前仍锁定 CosyVoice

以下位置需要 provider 化：

- `services/control_api/app/config.py`
  - 默认 enrollment URL 为 DashScope；
  - 默认 target model 为 `cosyvoice-v3.5-flash`；
  - production validator 明确要求 CosyVoice v3.5。
- `services/control_api/app/main.py`
  - 只根据 DashScope API key 构造 `CosyVoiceEnrollmentClient`。
- `services/voice_profile/cosyvoice_enrollment.py`
  - provider port 的唯一真实实现是 CosyVoice。
- `services/voice_profile/manager.py` 与 `postgres_manager.py`
  - enrollment operation/profile 的 provider 字段硬编码为 `alibaba_model_studio`。

S8 应新增 Doubao adapter 和 provider-specific config，不应把旧 CosyVoice 记录改写为 Doubao。

### 11.3 当前 runtime 会拒绝个人音色

`services/agent/src/voice_profile_client.py` 的 active 解析仍要求：

- model 必须为 `seed-tts-2.0`；
- voice 必须出现在 approved companion catalog。

`services/agent/src/providers/doubao_tts.py::apply_voice_profile()` 也要求 voice 出现在同一 catalog。

这两个白名单对默认伙伴音色是正确安全边界，但不能同时承担个人音色授权。S8 应保留：

```text
designed -> 只允许版本化 approved catalog
active personal -> 只允许 control-api 返回的已 consent + eval + quality + unexpired profile
```

不能把个人 speaker 加进静态 `infra/voices/doubao_voice_ids.json`；该文件是伙伴原生音色发布目录，不是用户敏感资产库。

### 11.4 当前 provenance 会在中途回退后失真

`services/agent/src/agent.py` 在 agent 构造时只计算一次 `actual_voice_profile_id`。之后即使 refresh 使 TTS 回退 baseline，response provenance 仍可能记录启动时的旧 profile。

S8 必须让 `actual_voice_profile_id`、`tts_model/resource_id` 和实际 speaker 绑定到当前 generation 的不可变 voice snapshot，再写入 Archive。否则无法证明某条回答实际用了本人声音还是伙伴声音。

### 11.5 现有 409 应保留

当前 control-api 拒绝在 Doubao runtime 激活历史 CosyVoice clone，这是正确的跨 provider 防线。S8 应增加“Doubao-native personal clone”激活路径，而不是放开所有 legacy active profile。

## 12. 最小实施顺序

1. **Provider smoke**
   - 在隔离项目购买/开通声音复刻 2.0；
   - 用已授权测试者样本调用 train/query/status；
   - 确认 `speaker_id`、`IclSpeakerId`、`ResourceID`、`ExpireTime`；
   - 确认试听、正式调用、收费和 7 天回收行为；
   - 向火山引擎确认公开删除路径。
2. **Doubao enrollment adapter**
   - 复用现有 sample storage、consent 和 enrollment operation；
   - 严格校验 10 MB、格式、PCM 24 kHz 单声道；
   - 训练后轮询，不在 HTTP 200 时直接 candidate；
   - 持久化 synth-ready speaker、resource 和 expiry。
   - expiry 字段路径和格式必须由账号 smoke 明确配置；缺失、无时区或已过期均拒绝激活，
     不能把 NULL 当作永久有效；
   - clone API key 与实时 TTS key 必须独立，生产环境拆分前拒绝同值 secret。
3. **Provider-neutral config/schema**
   - 去掉 production 对 CosyVoice v3.5 的硬要求；
   - provider 字段不再硬编码；
   - 保留旧 CosyVoice profile，不迁移成 Doubao。
4. **Resource-aware TTS pool**
   - baseline `seed-tts-2.0` 与 clone `seed-icl-2.0` 分池；
   - pool connection 记录建连 resource；
   - 回退复用 baseline warm pool。
5. **Fence-bound voice resolution**
   - 生成前刷新；
   - voice snapshot 绑定 generation；
   - provenance 记录实际 profile/resource；
   - 撤销/过期下一 generation 生效。
6. **安全首版**
   - 暂不启用 clone `context_texts`/自由 emotion；
   - 先通过首包、尾音、时间戳、长句、专有词、弱网；
   - provider 删除未知时，本地 revoke 与 provider cleanup 分状态。
7. **真实设备验收**
   - 本人相似度/自然度；
   - 盲选；
   - 情绪与指令遵循；
   - 打断无旧 generation 尾音；
   - 撤销后下一 generation 使用伙伴批准音色；
   - Archive 中实际声音 provenance 正确。

## 13. 实施前必须回答的问题

- 生产使用预付费还是后付费音色？
- 自动质量探针是否触发首次正式合成和槽位费？
- 合成 `speaker` 是训练 `speaker_id` 还是 `IclSpeakerId`？
- `seed-icl-2.0` 双向流式的完整事件与时间戳契约是什么？
- 当前账号的地域、项目隔离和实际 quota 是什么？
- 官方支持的用户主动删除 API 或人工删除 SLA 是什么？
- 双向 PCM 是否支持节奏水印，如何验证不会破坏尾音和对齐？
- 个人复刻音色支持哪些 emotion/context 能力？

以上问题未得到官方或真实 provider 响应前，不应在代码、UI 或文档里填入假字段或承诺。

## 14. 官方来源

| 官方文档 | 本次用途 |
|---|---|
| [音色训练HTTP](https://www.volcengine.com/docs/6561/2534906) | 训练入口、鉴权、speaker/custom speaker、音频格式、10 MB、状态 |
| [音色查询HTTP](https://www.volcengine.com/docs/6561/2535742) | 查询入口、状态枚举、试听链接、model type |
| [音色管理HTTP](https://www.volcengine.com/docs/6561/2235883) | `BatchListMegaTTSTrainStatus`、到期、回收、`IclSpeakerId`/`ResourceID` 示例 |
| [声音复刻下单及使用指南](https://www.volcengine.com/docs/6561/1167802) | 预付费/后付费、首次正式合成、重训、7 天未调用删除 |
| [声音复刻2.0最佳实践](https://www.volcengine.com/docs/6561/2298705) | 14–30 秒样本、单人单轨、`context_texts` |
| [双向流式语音合成WebSocket](https://www.volcengine.com/docs/6561/2532486) | 实时双向接口入口与高层能力 |
| [HTTP Chunked/SSE单向流式-V3](https://www.volcengine.com/docs/6561/1598757) | API 列表、resource_id、韵律、watermark、错误/限流语义 |
| [语音指令与标签](https://www.volcengine.com/docs/6561/1871062) | 当前语音指令能力范围 |
| [ListSpeakers - 大模型音色列表（新接口）](https://www.volcengine.com/docs/6561/2160690) | 通用 TTS 音色按 resource 查询；仅用于 baseline catalog，不用于存个人音色 |
| [QPS/并发查询接口说明](https://www.volcengine.com/docs/6561/1476626) | 声音复刻/大模型 TTS quota ResourceID |
| [QuotaMonitoring - Quota查询接口](https://www.volcengine.com/docs/6561/1801956) | 账号实际 quota/limit 查询 |
| [调用量查询接口说明](https://www.volcengine.com/docs/6561/1476625) | 调用量监控 |
| [语音模型体验服务免责声明和服务规则说明](https://www.volcengine.com/docs/6561/1866421) | 生物特征处理同意、不得处理他人数据 |
