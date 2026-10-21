# 火山大模型流式 ASR 协议核对

对照 `volc_protocol.py` / `volc_asr.py` 与火山引擎官方文档和官方 Demo 客户端，逐项确认帧格式、事件码、握手、结束包。核对日期 2026-10-21。

## 权威来源

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

## 结论摘要

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

## 仍未验证的部分

- **没有对真实端点打过合成音**。仓库与运行环境都没有 `VOLC_ASR_APP_ID` / `VOLC_ASR_ACCESS_TOKEN`，本次只做了文档与官方 Demo 的静态核对加本地假服务端单测。打开 `ASR_FAILOVER_ENABLED` 前必须用合成音对 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` 实连一次，确认：拿到首包结果、负包后收到 `is_last` 响应、错误帧能被识别。
- `end_window_ms=550` 与 `enable_nonstream=true` 的组合会触发服务端 VAD 分句，中间结果里可能出现 `definite: true` 的分句。当前实现把 `definite` 分句当作句末结果转发，同时继续等负包边界；这一行为与 FunASR 路径的 `sentence_end` 语义一致，但没有在真实端点上观察过中间 `definite` 的时序。
- 文档未给出“一条连接能否承载第二个任务”的明确禁止。即使允许，当前实现选择一段一连接，成本是多一次 WebSocket 建连，收益是任务边界与连接生命周期一致，不依赖未写明的服务端行为。

## 与首结果预算的关系（需要决策）

`docs/acceptance/run-20260929-asr-ab/findings.md` 的离线 A/B 在同一端点上测到：豆包 `bigmodel_async` 的**首个 partial p50 是 3.40 s**，而 final p50 只有 0.09 s。默认 `ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS=1500` 比这个首 partial 分布还紧，正常朗读也可能在第一次出字前就触发 `first_result_timeout`。

本轮把计时的起点从“任务第一帧”改到“首个有能量的帧”，只消除了静音导致的误判；**说话中的 1.5 s 预算本身仍可能偏紧**。打开开关前建议按下面两步定值，不要沿用默认值：

1. 用合成音在真实端点测首个 partial 的 p50/p90（`asr_first_result_ms` 已经按语音起点计量）。
2. 把 `ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS` 设到 p90 之上，再观察 `asr_failover_total{reason="first_result_timeout"}` 是否落到可接受范围。

这条没有在本轮改动默认值：默认值的调整会影响降级覆盖率，属于上线参数决策，需要真实端点的数据支撑。

