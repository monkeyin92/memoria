# 中文全双工级联语音 Agent：可实施架构与工程设计规范

> **版本**：1.0.1  
> **基准日期**：2026-07-15  
> **目标技术栈**：FunASR Realtime API + DeepSeek API + CosyVoice Realtime API + LiveKit Agents  
> **部署前提**：无 GPU；只使用第三方模型 API；允许使用普通 CPU 云主机或托管 Agent 运行时  
> **目标语言**：普通话为主，兼容少量中英混说  
> **文档性质**：规范性设计文档。文中的 **MUST / MUST NOT / SHOULD / MAY** 分别表示必须、禁止、建议、可选。

---

## 0. 先说明本规范能保证什么、不能保证什么

本方案的目标是用级联架构实现以下主观体验：

- 助手说话时仍持续监听用户；
- 用户说“等等、不是、停一下”时快速停住；
- 用户说“嗯嗯、对、好的”时通常不误打断；
- 用户句中思考停顿时不轻易抢话；
- 用户说完后较快开始播出第一段自然语音；
- DeepSeek、搜索或业务工具在后台运行时，前台仍能进行简短互动；
- 被打断后不再播出旧回答、旧工具结果或用户未听到的内容；
- 对话历史只记录用户实际听到的助手文本。

本规范不能诚实地承诺：任何代码模型在没有运行环境、密钥、网络和第三方服务配额的情况下，一次生成代码就必然与 GPT Live 完全相同。GPT Live 是模型级全双工系统；本方案是**系统级全双工级联架构**。最终效果还受以下因素影响：

1. 用户设备的回声消除和麦克风质量；
2. 用户到 RTC 节点、RTC 节点到模型 API 的网络 RTT；
3. FunASR、DeepSeek、CosyVoice 的当时负载和限流；
4. 业务提示词、工具耗时和回答长度；
5. 中文打断与附和测试数据是否覆盖真实用户。

因此，本规范把“达到目标”定义为：**所有第 23 章验收测试通过，并满足第 21 章 SLO**。代码实现与本文冲突时，以本文的接口契约、状态机、不变量和验收测试为准。

---

## 1. 最终选型

### 1.1 固定选型，不允许实现者自行替换

| 层 | 固定选择 | 原因 |
|---|---|---|
| Web/移动 Web 媒体传输 | LiveKit WebRTC | 双向低延迟音频、重连、设备管理、服务端 Agent 接入成熟 |
| 语音 Agent 编排 | `livekit-agents==1.6.5`，Python 3.12 | 提供话轮、打断、流式 STT/LLM/TTS 插件接口、播放截断和指标 |
| VAD | Silero VAD，CPU | 无 GPU 依赖；只负责“有没有人声”，不单独决定话轮 |
| 语义/声学话轮 | LiveKit Audio Turn Detector | 结合语义与音高、语调、节奏；中文可用 |
| 自适应打断 | LiveKit Cloud Adaptive Interruption | 区分真正 barge-in 与附和/背景声；要求带时间戳的 STT |
| ASR | 阿里云百炼 `fun-asr-realtime` WebSocket | 流式中间结果、最终结果、中文、字词级时间戳 |
| 快速回答 LLM | `deepseek-v4-flash`，关闭 thinking | 低延迟口语回答、工具意图判定 |
| 深度任务 LLM | `deepseek-v4-pro`，开启 thinking | 复杂分析、RAG、工具编排；不阻塞前台交互 |
| TTS | `cosyvoice-v3-flash` + `longanyang` | 中文系统音色，支持流式输出、Instruct 和字级时间戳 |
| 控制 API | FastAPI + Pydantic v2 | 签发 LiveKit Token、会话配置、健康检查 |
| 短期状态 | 进程内内存；多实例时 Redis | 实时关键路径不得等待数据库 |
| 长期存储 | PostgreSQL，异步写入 | 对话、指标、业务事件和审计 |
| 可观测性 | OpenTelemetry + Prometheus/Grafana + Sentry | 跟踪分段延迟、失败与异常 |
| 前端 | React 19 + TypeScript + Vite + LiveKit React Components | 浏览器端 WebRTC、设备选择、字幕和状态 UI |

### 1.2 两种部署档案

代码库 MUST 同时支持以下两个配置档案，业务逻辑完全一致：

#### 档案 A：`livekit_cloud`，首选开发与首轮上线

```text
浏览器 ─ WebRTC ─ LiveKit Cloud
                       │
                 LiveKit Agent Cloud
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       FunASR       DeepSeek     CosyVoice
```

特点：

- 使用托管 Audio Turn Detector `v1`；
- 使用 Adaptive Interruption；
- Agent 运行时不需要 GPU；
- 上手最快，最接近本文预期体验；
- 必须实测目标用户所在地的 RTC 与阿里云/DeepSeek API RTT。

#### 档案 B：`cn_self_hosted`，中国大陆低 RTT 备选

```text
浏览器 ─ WebRTC ─ 自建 LiveKit SFU（CPU）
                       │
                 Agent Worker（CPU）
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       FunASR       DeepSeek     CosyVoice
```

特点：

- LiveKit SFU 和 Agent 部署在北京或上海普通 CPU 实例；
- Audio Turn Detector 固定为 `v1-mini`，本地 CPU 执行；
- Adaptive Interruption 不可用时，降级为 VAD + 中文打断守卫；
- 适合大陆用户、跨境网络不稳定或需要自行控制 RTC 的情况。

**切换规则**：上线前分别跑第 23 章同一套 E2E 录音。若档案 A 的“用户说完到首音频 P95”或“打断到停止 P95”连续三次不达标，则切换档案 B，而不是盲目改模型参数。

---

## 2. 范围与非目标

### 2.1 本期范围

- 浏览器一对一实时语音对话；
- 普通话及少量中英混说；
- 流式字幕；
- 随时打断；
- 用户附和过滤；
- DeepSeek function calling；
- 可取消的后台工具任务；
- 实际已播放文本追踪；
- 断线重连与模型 API 降级；
- 全链路指标和自动化测试。

### 2.2 本期非目标

- 不训练或微调 ASR、LLM、TTS；
- 不做多人会议或说话人分离；
- 不做医学级情绪识别；
- 不对用户情绪、人格或心理状态作敏感推断；
- 不追求助手和用户长时间同时完整说话；用户明确插话后助手应让出话权；
- 不在浏览器直接调用任何模型 API；
- 不允许把永久密钥放入前端；
- 不允许让数据库、RAG 或业务 API 阻塞实时媒体线程。

---

## 3. 总体架构

```mermaid
flowchart LR
    U[用户麦克风] -->|WebRTC Opus| LK[LiveKit SFU]
    LK --> IN[Agent Audio Input]
    IN --> VAD[Silero VAD]
    IN --> TD[Audio Turn Detector]
    IN --> ASR[FunASR Realtime Adapter]

    VAD --> ORCH[Duplex Orchestrator]
    TD --> ORCH
    ASR --> ORCH

    ORCH --> FAST[DeepSeek v4 Flash\n非思考流式]
    ORCH --> TASK[后台任务管理器]
    TASK --> DEEP[DeepSeek v4 Pro\n思考/工具/RAG]
    TASK --> BIZ[业务 API / 搜索 / 数据库]

    FAST --> SEG[中文口语分段器]
    DEEP --> SEG
    SEG --> TTS[CosyVoice Realtime Adapter]
    TTS --> OUT[Agent Audio Output]
    OUT -->|WebRTC Opus| LK
    LK --> SPK[用户扬声器]

    ORCH --> FENCE[Generation Fence\n过期结果隔离]
    ASR --> METRIC[Telemetry]
    FAST --> METRIC
    TTS --> METRIC
    LK --> METRIC
```

### 3.1 核心原则

1. **媒体流和业务流分离**：音频始终走 WebRTC；控制事件走 LiveKit Data/RPC 或 HTTPS。
2. **麦克风永远在线**：助手说话时不得停止采集或停止 ASR。
3. **VAD 不等于话轮结束**：VAD 只产生声学边界；Audio Turn Detector 决定是否提交话轮。
4. **任何输出均可取消**：LLM、TTS、播放器和工具任务都必须响应取消。
5. **所有异步结果均带代际编号**：过期结果不得进入播放、UI 或历史。
6. **实际播放是事实源**：生成文本不是“已经说过”；只有按时间戳实际播出的内容才能写入助手历史。
7. **快慢模型分离**：快速模型负责当下回应，深度模型负责复杂工作。
8. **默认短答**：首轮回答以 1–3 个口语短句为默认上限。

---

## 4. 仓库结构

实现者 MUST 创建以下单仓库，不得把关键逻辑散落在临时脚本中：

```text
voice-agent/
├── AGENTS.md
├── README.md
├── .env.example
├── .gitignore
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── package.json
├── pnpm-lock.yaml
├── Makefile
├── apps/
│   └── web/
│       ├── package.json
│       ├── vite.config.ts
│       ├── src/
│       │   ├── main.tsx
│       │   ├── App.tsx
│       │   ├── api/controlApi.ts
│       │   ├── components/VoiceRoom.tsx
│       │   ├── components/TranscriptPanel.tsx
│       │   ├── components/ConnectionBanner.tsx
│       │   ├── hooks/useVoiceSession.ts
│       │   ├── state/sessionStore.ts
│       │   └── types/events.ts
│       └── tests/
├── services/
│   ├── control_api/
│   │   ├── app/main.py
│   │   ├── app/config.py
│   │   ├── app/routes/session.py
│   │   ├── app/security.py
│   │   └── tests/
│   └── agent/
│       ├── src/main.py
│       ├── src/config.py
│       ├── src/agent.py
│       ├── src/prompts.py
│       ├── src/providers/
│       │   ├── funasr_stt.py
│       │   ├── funasr_protocol.py
│       │   ├── cosyvoice_tts.py
│       │   ├── cosyvoice_protocol.py
│       │   └── deepseek.py
│       ├── src/orchestration/
│       │   ├── state_machine.py
│       │   ├── generation_fence.py
│       │   ├── interruption_guard.py
│       │   ├── stable_prefix.py
│       │   ├── phrase_segmenter.py
│       │   ├── context_manager.py
│       │   ├── task_manager.py
│       │   ├── heard_text_tracker.py
│       │   └── prosody.py
│       ├── src/contracts/
│       │   ├── events.py
│       │   ├── ids.py
│       │   └── errors.py
│       ├── src/observability/
│       │   ├── metrics.py
│       │   ├── tracing.py
│       │   └── logging.py
│       └── tests/
│           ├── unit/
│           ├── integration/
│           └── fixtures/audio/
├── packages/
│   └── contracts/
│       ├── events.schema.json
│       └── README.md
├── tests/
│   ├── e2e/
│   ├── load/
│   └── chaos/
├── infra/
│   ├── Dockerfile.agent
│   ├── Dockerfile.control-api
│   ├── Dockerfile.web
│   ├── livekit.yaml
│   ├── prometheus.yml
│   └── grafana/
└── scripts/
    ├── verify_env.py
    ├── provider_smoke_test.py
    ├── run_e2e.py
    └── export_latency_report.py
```

### 4.1 代码质量硬约束

- Python MUST 开启 `ruff`, `mypy --strict`, `pytest`；
- TypeScript MUST 开启 `strict: true`, ESLint, Vitest；
- 所有网络调用 MUST 使用异步 API；
- 实时路径中 MUST NOT 使用 `time.sleep`、同步 `requests`、同步数据库驱动；
- 所有后台 `asyncio.Task` MUST 被任务管理器持有并能取消；
- 关键路径不得留下 `TODO`、`pass`、伪实现或“后续补充”；
- 提供 `uv.lock` 与 `pnpm-lock.yaml`；
- CI MUST 执行 lint、type-check、unit、integration 和离线 E2E。

---

## 5. 版本和依赖锁定

### 5.1 Python

```toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = [
  # LiveKit 组件必须同版本锁定，不能只锁 livekit-agents 而让插件漂移。
  "livekit-agents==1.6.5",
  "livekit-plugins-openai==1.6.5",
  "livekit-plugins-silero==1.6.5",
  "livekit-api>=1.1.1,<2",
  # livekit-agents 1.6.5 要求 openai>=2；OpenAI 插件 1.6.5 要求 openai[realtime]>=2.36。
  "openai>=2.36,<3",
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.34,<1",
  "pydantic>=2.10,<3",
  "pydantic-settings>=2.7,<3",
  "aiohttp>=3.11,<4",
  "websockets>=14,<16",
  "httpx>=0.28,<1",
  "redis>=5.2,<7",
  "asyncpg>=0.30,<1",
  "sqlalchemy[asyncio]>=2.0,<3",
  "orjson>=3.10,<4",
  "structlog>=25,<26",
  "opentelemetry-sdk>=1.30,<2",
  "prometheus-client>=0.21,<1",
  "sentry-sdk[fastapi]>=2.20,<3",
  "numpy>=2.1,<3",
]
```

说明：`livekit-agents`、`livekit-plugins-openai`、`livekit-plugins-silero` 必须一起精确锁到 `1.6.5`。该版本的 Agents 核心依赖 OpenAI Python SDK 2.x，OpenAI 插件还要求 `openai>=2.36`；不得把 OpenAI SDK 限制为 `<2`。其余依赖由首次成功构建后的 `uv.lock` 固化。升级任一 LiveKit 组件时必须重新解析锁文件并跑全套 E2E，不能只修改一个版本号上线。

安装后 CI MUST 执行以下版本断言；任一不符立即失败：

```python
from importlib.metadata import version

EXPECTED = {
    "livekit-agents": "1.6.5",
    "livekit-plugins-openai": "1.6.5",
    "livekit-plugins-silero": "1.6.5",
}
for package, expected in EXPECTED.items():
    actual = version(package)
    assert actual == expected, f"{package}: expected {expected}, got {actual}"

major = int(version("openai").split(".", 1)[0])
minor = int(version("openai").split(".")[1])
assert major == 2 and minor >= 36, "openai must be >=2.36,<3"
```


### 5.2 前端

首次创建项目时使用：

```bash
pnpm create vite apps/web --template react-ts
pnpm --dir apps/web add react@19.2.7 react-dom@19.2.7 \
  livekit-client @livekit/components-react zustand zod
pnpm --dir apps/web add -D vitest @testing-library/react \
  @testing-library/jest-dom eslint prettier typescript
```

LiveKit 前端包版本由生成时的 `pnpm-lock.yaml` 固定。后续部署 MUST 使用 `pnpm install --frozen-lockfile`。

---

## 6. 环境变量

`.env.example` MUST 包含以下全部字段：

```dotenv
# Runtime
ENVIRONMENT=development
DEPLOYMENT_PROFILE=livekit_cloud
LOG_LEVEL=INFO
PUBLIC_BASE_URL=http://localhost:8000
ALLOWED_ORIGINS=http://localhost:5173

# LiveKit
LIVEKIT_URL=wss://YOUR_PROJECT.livekit.cloud
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
LIVEKIT_AGENT_NAME=duplex-zh-agent
LIVEKIT_TURN_DETECTOR_VERSION=v1
LIVEKIT_ADAPTIVE_INTERRUPTION=true

# Alibaba Model Studio / DashScope
DASHSCOPE_API_KEY=
DASHSCOPE_WORKSPACE_ID=
DASHSCOPE_REGION=cn-beijing
DASHSCOPE_WS_URL=wss://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference

# FunASR
FUNASR_MODEL=fun-asr-realtime
FUNASR_SAMPLE_RATE=16000
FUNASR_LANGUAGE=zh
FUNASR_CHUNK_MS=80
FUNASR_MAX_SENTENCE_SILENCE_MS=650
FUNASR_SEMANTIC_PUNCTUATION=false
FUNASR_HEARTBEAT=true
FUNASR_RECONNECT_AUDIO_MS=1500
FUNASR_CONNECT_TIMEOUT_S=5
FUNASR_RESULT_TIMEOUT_S=8

# DeepSeek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_FAST_MODEL=deepseek-v4-flash
DEEPSEEK_DEEP_MODEL=deepseek-v4-pro
DEEPSEEK_FAST_FIRST_TOKEN_TIMEOUT_S=3.0
DEEPSEEK_FAST_TOTAL_TIMEOUT_S=12.0
DEEPSEEK_FAST_MAX_TOKENS=240
DEEPSEEK_FAST_TEMPERATURE=0.45
DEEPSEEK_DEEP_TOTAL_TIMEOUT_S=90

# CosyVoice
COSYVOICE_MODEL=cosyvoice-v3-flash
COSYVOICE_VOICE=longanyang
COSYVOICE_SAMPLE_RATE=24000
COSYVOICE_FORMAT=pcm
COSYVOICE_LANGUAGE=zh
COSYVOICE_RATE=1.0
COSYVOICE_PITCH=1.0
COSYVOICE_VOLUME=50
COSYVOICE_WORD_TIMESTAMPS=true
COSYVOICE_POOL_SIZE=4
COSYVOICE_CONNECT_TIMEOUT_S=5
COSYVOICE_FIRST_AUDIO_TIMEOUT_S=1.5
COSYVOICE_TOTAL_TIMEOUT_S=20

# Turn handling
VAD_MIN_SPEECH_DURATION_S=0.05
VAD_MIN_SILENCE_DURATION_S=0.30
VAD_PREFIX_PADDING_DURATION_S=0.30
ENDPOINTING_MODE=dynamic
ENDPOINTING_MIN_DELAY_S=0.30
ENDPOINTING_MAX_DELAY_S=2.20
ENDPOINTING_ALPHA=0.85
INTERRUPTION_MIN_DURATION_S=0.25
FALSE_INTERRUPTION_TIMEOUT_S=1.20
BACKCHANNEL_BOUNDARY_START_S=0.50
BACKCHANNEL_BOUNDARY_END_S=1.80
PREEMPTIVE_GENERATION=true
PREEMPTIVE_TTS=false

# Persistence and observability
DATABASE_URL=postgresql+asyncpg://voice:voice@localhost:5432/voice
REDIS_URL=redis://localhost:6379/0
OTEL_EXPORTER_OTLP_ENDPOINT=
PROMETHEUS_PORT=9090
SENTRY_DSN=

# Security
SESSION_TOKEN_TTL_S=300
JWT_ISSUER=voice-agent
AUDIO_RETENTION_ENABLED=false
AUDIO_RETENTION_DAYS=0
PII_REDACTION_ENABLED=true
```

### 6.1 启动时配置校验

`scripts/verify_env.py` MUST 在启动前验证：

- 所有必需密钥非空；
- `FUNASR_SAMPLE_RATE == 16000`；
- `COSYVOICE_SAMPLE_RATE` 是 24000；
- `VAD_MIN_SILENCE_DURATION_S >= 0.25`；
- `COSYVOICE_WORD_TIMESTAMPS=true`；
- `livekit_cloud` 档案下 `LIVEKIT_ADAPTIVE_INTERRUPTION=true`；
- `cn_self_hosted` 档案下自动将 `LIVEKIT_TURN_DETECTOR_VERSION=v1-mini`；
- DeepSeek 模型名不得使用即将弃用的 `deepseek-chat` 或 `deepseek-reasoner`；
- 生产环境不得允许 `*` CORS；
- 生产环境不得启用明文 `ws://` 或 `http://` 媒体/控制地址。

校验失败时进程必须立即退出，不允许带病启动。

---

## 7. 音频与时钟契约

### 7.1 浏览器采集

浏览器必须请求如下音频约束：

```ts
const audioConstraints: MediaTrackConstraints = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};
```

规则：

- 助手播放期间麦克风轨道 MUST 保持 `enabled=true`；
- 不得在助手说话时暂停本地采集；
- 必须显示麦克风权限、输入设备和连接状态；
- 用户点击“停止回答”时，发送控制事件，但不得关闭麦克风；
- 浏览器音频播放必须由 LiveKit 轨道完成，不自行建立第二套 WebSocket 播放器。

### 7.2 ASR 输入

Agent 将 LiveKit 音频统一转换为：

```text
编码：PCM signed 16-bit little-endian
采样率：16,000 Hz
声道：1
发送分块：80 ms，允许 60–100 ms
每块字节数：16000 × 0.08 × 2 = 2560 bytes
```

FunASR 发送器 MUST 聚合过小的 LiveKit 帧，禁止每个 10/20 ms 帧单独发一个 WebSocket 消息。

### 7.3 TTS 输出

CosyVoice 统一请求：

```text
编码：raw PCM signed 16-bit little-endian
采样率：24,000 Hz
声道：1
字级时间戳：开启
```

LiveKit TTS 插件中的 `AudioEmitter` 必须按 24 kHz、单声道、`audio/pcm` 初始化。不得先写 WAV 文件再播放。

### 7.4 时间基准

所有内部事件同时记录：

- `monotonic_ns`：进程内延迟测量的唯一事实源；
- `wall_time_utc`：跨服务日志关联；
- `audio_time_ms`：相对于该 ASR/TTS task 音频起点的时间；
- `room_time_ms`：相对于 LiveKit 房间连接时刻的时间。

禁止用系统墙上时间直接计算 100 ms 级延迟，因为 NTP 调整可能导致负值或跳变。

---

## 8. 全局标识与不可破坏的不变量

每个会话必须维护以下标识：

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class GenerationFence:
    session_id: str
    turn_id: int
    generation_id: int
    tool_epoch: int
```

### 8.1 含义

- `session_id`：一次房间会话唯一 UUID；
- `turn_id`：每次用户正式话轮提交后加 1；
- `generation_id`：每次启动、取消或替换回答时加 1；
- `tool_epoch`：用户修改工具任务条件或取消任务时加 1；
- `asr_task_id`、`tts_task_id`：每个供应商 WebSocket task 的 UUID；
- `tool_task_id`：每个业务工具调用 UUID。

### 8.2 强制不变量

1. 任意 LLM token、TTS 音频帧、工具结果进入下游前，MUST 比对完整 `GenerationFence`；
2. Fence 不匹配的结果 MUST 被丢弃并记 `stale_result_dropped_total`；
3. 用户真实打断发生后，旧 `generation_id` 不得再产生可听音频；
4. 旧 `tool_epoch` 的工具结果不得自动播报；
5. 对话历史中的助手文本长度不得超过 `HeardTextTracker` 判定的实际已播放文本；
6. 同一时刻只能有一个前台 `speech_handle` 被允许播放；
7. 同一会话可以有多个后台工具任务，但只有当前 fence 可触发语音输出；
8. 任何 API 重试不得复用已经 `task-failed` 的 WebSocket；
9. 已取消 TTS 的连接必须关闭并从连接池剔除；
10. “旧任务误播”验收结果必须为 0，不允许以概率指标接受。


---

## 9. 规范事件模型

所有模块不得直接互相调用杂乱回调；必须通过类型化事件或明确接口交互。`services/agent/src/contracts/events.py` 至少实现：

```python
from __future__ import annotations
from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class EventKind(StrEnum):
    USER_SPEECH_START = "user_speech_start"
    USER_SPEECH_END = "user_speech_end"
    ASR_INTERIM = "asr_interim"
    ASR_PREFLIGHT = "asr_preflight"
    ASR_FINAL = "asr_final"
    TURN_COMMITTED = "turn_committed"
    LLM_STARTED = "llm_started"
    LLM_TOKEN = "llm_token"
    LLM_COMPLETED = "llm_completed"
    TTS_STARTED = "tts_started"
    TTS_AUDIO = "tts_audio"
    TTS_ALIGNMENT = "tts_alignment"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_PROGRESS = "playback_progress"
    PLAYBACK_STOPPED = "playback_stopped"
    INTERRUPTION_CANDIDATE = "interruption_candidate"
    INTERRUPTION_CONFIRMED = "interruption_confirmed"
    FALSE_INTERRUPTION = "false_interruption"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    TOOL_CANCELLED = "tool_cancelled"
    PROVIDER_ERROR = "provider_error"
    STALE_RESULT_DROPPED = "stale_result_dropped"

class BasePipelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: EventKind
    session_id: str
    turn_id: int
    generation_id: int
    monotonic_ns: int
    wall_time_utc: str

class TimedWord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    punctuation: str = ""

class TranscriptEvent(BasePipelineEvent):
    kind: Literal[
        EventKind.ASR_INTERIM,
        EventKind.ASR_PREFLIGHT,
        EventKind.ASR_FINAL,
    ]
    text: str
    sentence_id: int
    begin_ms: int
    end_ms: int | None
    is_final: bool
    words: tuple[TimedWord, ...] = ()
    confidence: float | None = None

class ToolResultEvent(BasePipelineEvent):
    kind: Literal[EventKind.TOOL_RESULT]
    tool_epoch: int
    tool_task_id: str
    tool_name: str
    payload: dict[str, Any]
```

### 9.1 事件传输规则

- Agent 进程内部使用有界 `asyncio.Queue`；
- 队列满时，音频帧不得静默丢弃；应记录过载、取消当前会话并向客户端提示重连；
- 指标和审计事件可异步批量写入，不得反向阻塞实时路径；
- 前端只接收脱敏后的 UI 事件：状态、字幕、错误码、工具进度；
- 前端不得收到 DeepSeek `reasoning_content`、供应商原始密钥或内部堆栈。

---

## 10. Duplex Orchestrator 状态机

### 10.1 状态定义

```python
class ConversationState(StrEnum):
    CONNECTING = "connecting"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    EOT_PENDING = "eot_pending"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTION_PENDING = "interruption_pending"
    TOOL_WAITING = "tool_waiting"
    RECOVERING = "recovering"
    CLOSED = "closed"
```

### 10.2 状态转换表

| 当前状态 | 事件/条件 | 动作 | 下一状态 |
|---|---|---|---|
| CONNECTING | RTC、ASR、TTS 预热成功 | 开启麦克风监听；发布 ready | LISTENING |
| LISTENING | VAD START | 记录 `speech_start`；启动/确认 ASR task | USER_SPEAKING |
| USER_SPEAKING | VAD 暂停但 Turn Detector 判断未结束 | 保持 ASR；不提交 LLM | EOT_PENDING |
| EOT_PENDING | 用户恢复说话 | 取消待提交定时器 | USER_SPEAKING |
| EOT_PENDING | Turn Detector 判断结束或达到 max delay | 固化 ASR final；`turn_id += 1`；建立新 fence | THINKING |
| THINKING | DeepSeek 首个可说短语完成 | 启动 TTS | SPEAKING |
| THINKING | 用户再次开口 | 取消当前生成；新用户语音继续识别 | USER_SPEAKING |
| SPEAKING | 检测到用户声音 | 暂时 duck；提交 Adaptive Interruption | INTERRUPTION_PENDING |
| INTERRUPTION_PENDING | 判为附和/噪声 | 恢复播放；不新建用户话轮 | SPEAKING |
| INTERRUPTION_PENDING | 判为真实打断 | 执行第 17 章原子取消流程 | USER_SPEAKING |
| SPEAKING | 助手播放完成且无工具 | 固化实际已听文本 | LISTENING |
| SPEAKING | 前台语音结束但后台工具仍在跑 | 保持监听并允许新话轮 | TOOL_WAITING |
| TOOL_WAITING | 工具结果 fence 有效且用户未在说话 | 生成简短结果播报 | THINKING |
| TOOL_WAITING | 用户改变条件 | `tool_epoch += 1`；旧结果失效 | USER_SPEAKING |
| 任意非 CLOSED | 供应商可恢复错误 | 局部重连；UI 显示轻量状态 | RECOVERING |
| RECOVERING | 恢复成功 | 回到最近安全状态 | LISTENING |
| 任意 | 会话结束/不可恢复错误 | 取消所有任务、关闭连接 | CLOSED |

### 10.3 状态机实现规则

- 所有转换必须在单线程事件循环中的同一个 `asyncio.Lock` 或 actor mailbox 内串行化；
- 供应商回调不得直接修改状态，只能投递事件；
- 每次状态转换写结构化日志：`from_state`, `event`, `to_state`, `fence`；
- 不允许“状态 + 多个布尔值”形成隐式状态；例如不得同时维护易冲突的 `is_speaking`, `is_thinking`, `is_interrupted`；
- 状态机单元测试必须覆盖每一条合法转换和非法转换。

---

## 11. LiveKit AgentSession 配置

### 11.1 预热

```python
from livekit import agents
from livekit.plugins import silero


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=0.30,
        prefix_padding_duration=0.30,
        force_cpu=True,
    )
```

`min_silence_duration` 不得小于 0.25 秒，否则 Audio Turn Detector 无法启动。Silero 只在进程预热时加载一次，不得每个会话重新加载。

### 11.2 生产默认配置

下面代码是实现目标，不是概念伪代码。若 `livekit-agents==1.6.5` 的类型检查提示字段变化，必须以该固定版本的公开 API 为准，并保持参数语义不变：

```python
import os

import httpx
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    inference,
    room_io,
)
from livekit.plugins import openai

from .providers.funasr_stt import FunASRSTT
from .providers.cosyvoice_tts import CosyVoiceTTS

VOICE_SYSTEM_PROMPT = """
你是实时中文语音助手。像面对面聊天一样说话，不要朗读文章。
默认先直接回答最重要的一点，通常只说一到三句。
除非用户明确要求详细说明，否则不要长篇列举。
不要重复用户的问题，不要输出 Markdown、标题、项目符号或表格。
不要说“首先、其次、最后”“综上所述”“希望以上内容对你有帮助”。
用户说“嗯、对、好的”通常是附和，不需要停止原回答。
用户说“等等、不是、停一下”或提出完整的新问题时，以最新内容为准。
需要工具时，只说一句自然衔接；工具结果回来后只播报最相关部分。
数字、日期、金额、电话号码必须用适合中文口语的形式表达。
你的输出会立即进入 TTS，因此只输出要真正说出口的正文。
""".strip()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    stt = FunASRSTT.from_env()
    tts = CosyVoiceTTS.from_env()

    # DeepSeek 的兼容接口使用 max_tokens，而不是 OpenAI 新接口的
    # max_completion_tokens；因此通过 extra_body 透传，避免产生不兼容字段。
    llm = openai.LLM(
        model=os.environ["DEEPSEEK_FAST_MODEL"],
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ["DEEPSEEK_BASE_URL"],
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body={
            "thinking": {"type": "disabled"},
            "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
        },
    )

    turn_detector_version = os.getenv("LIVEKIT_TURN_DETECTOR_VERSION", "v1")

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=llm,
        tts=tts,
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(
                version=turn_detector_version,
            ),
            endpointing={
                "mode": "dynamic",
                "min_delay": 0.30,
                "max_delay": 2.20,
                "alpha": 0.85,
            },
            interruption={
                "enabled": True,
                "mode": "adaptive"
                    if os.getenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "true").lower() == "true"
                    else "vad",
                "min_duration": 0.25,
                "min_words": 0,
                "discard_audio_if_uninterruptible": True,
                "false_interruption_timeout": 1.20,
                "resume_false_interruption": True,
                "backchannel_boundary": (0.50, 1.80),
            },
            preemptive_generation={
                "enabled": True,
                "preemptive_tts": False,
                "max_speech_duration": 10.0,
                "max_retries": 2,
            },
        ),
    )

    await session.start(
        room=ctx.room,
        agent=Agent(instructions=VOICE_SYSTEM_PROMPT),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                sample_rate=24000,
                num_channels=1,
                frame_size_ms=50,
                auto_gain_control=True,
                pre_connect_audio=True,
            ),
            # 浏览器/WebRTC 客户端负责 AEC；助手说话时不得禁用输入。
            audio_output=room_io.AudioOutputOptions(
                sample_rate=24000,
                num_channels=1,
            ),
            text_output=room_io.TextOutputOptions(
                sync_transcription=True,
                json_format=True,
            ),
        ),
    )

    await session.generate_reply(
        instructions="用一句自然中文打招呼，并邀请用户直接说需求。",
    )
```

### 11.3 预生成策略

- `preemptive_generation=true`：可以基于稳定前缀提前启动 DeepSeek；
- `preemptive_tts=false`：第一版禁止在话轮正式提交前播放，以免用户还没说完就开口；
- 第 3 阶段通过验收后，才允许 A/B 测试 `preemptive_tts=true`；
- 任何预生成均绑定当时的 `generation_id`；用户继续说话时立刻作废；
- 预生成文本不得写入正式历史，直到正式提交并实际播放。

### 11.4 自建档案降级

`DEPLOYMENT_PROFILE=cn_self_hosted` 时：

```python
turn_detection = inference.TurnDetector(version="v1-mini")
interruption_mode = "vad"
```

同时启用第 16 章 `ChineseInterruptionGuard`。自建档案不得假装自己启用了 LiveKit Cloud Adaptive Interruption；指标中必须标注实际模式。

---

## 12. FunASR Realtime 适配器

### 12.1 LiveKit 能力声明

```python
from livekit.agents import stt

capabilities = stt.STTCapabilities(
    streaming=True,
    interim_results=True,
    diarization=False,
    aligned_transcript="word",
    offline_recognize=False,
    keyterms=False,
    chat_context=True,
)
```

带字级时间戳是硬要求，因为 Adaptive Interruption 和实际话轮对齐依赖它。

### 12.2 生命周期

每个 LiveKit 用户音轨对应一个 `FunASRRecognizeStream`：

1. 建立带 `Authorization: Bearer ...` 的 WebSocket；
2. 发送 `run-task`；
3. 等待服务端 `task-started`；
4. 才允许发送二进制 PCM；
5. 持续接收 `result-generated`；
6. 会话关闭时发送 `finish-task`；
7. 等待 `task-finished` 后连接可关闭或复用；
8. 收到 `task-failed` 时立即关闭，绝不放回池中。

### 12.3 `run-task` 固定请求

```json
{
  "header": {
    "action": "run-task",
    "task_id": "<UUID>",
    "streaming": "duplex"
  },
  "payload": {
    "task_group": "audio",
    "task": "asr",
    "function": "recognition",
    "model": "fun-asr-realtime",
    "parameters": {
      "format": "pcm",
      "sample_rate": 16000,
      "language_hints": ["zh"],
      "semantic_punctuation_enabled": false,
      "max_sentence_silence": 650,
      "heartbeat": true
    },
    "input": {
      "context": []
    }
  }
}
```

说明：

- 关闭语义标点，使用更低延迟的 VAD 分句；
- `max_sentence_silence=650` 只是 ASR 分句参数，不是最终话轮判定；
- 最终话轮仍由 LiveKit Audio Turn Detector 决定；
- `heartbeat=true` 防止长时间静音导致连接在 60 秒后断开；
- `speech_noise_threshold` 首版保持供应商默认值，只有真实噪声数据证明需要调整时才能修改。

### 12.4 服务端结果映射

FunASR `result-generated` 的核心字段：

```json
{
  "payload": {
    "output": {
      "sentence": {
        "begin_time": 170,
        "end_time": 920,
        "text": "好的，我明白了。",
        "heartbeat": false,
        "sentence_end": true,
        "sentence_id": 1,
        "words": [
          {
            "begin_time": 170,
            "end_time": 295,
            "text": "好",
            "punctuation": ""
          }
        ]
      }
    }
  }
}
```

映射规则：

| FunASR 条件 | LiveKit 事件 |
|---|---|
| 第一条非心跳文字出现 | `START_OF_SPEECH`，若 VAD 尚未发过 |
| `sentence_end=false` | `INTERIM_TRANSCRIPT` |
| 稳定前缀增长 | `PREFLIGHT_TRANSCRIPT` |
| `sentence_end=true` | `FINAL_TRANSCRIPT` |
| VAD 结束或最终句结束且输入静音 | `END_OF_SPEECH`，由 LiveKit/VAD 协调，避免重复 |

每个字/词转换为 LiveKit `TimedString`，时间单位从毫秒转换为秒。标点拼接到对应词之后，但时间范围沿用该词。

### 12.5 稳定前缀算法

FunASR 中间结果会改写尾部。`StablePrefixTracker` 必须按以下确定性算法生成 `PREFLIGHT_TRANSCRIPT`：

```text
输入：同一 sentence_id 最近 3 条规范化 interim 文本
1. 去除重复空格，但保留中文标点；
2. 计算三条文本的最长公共前缀 LCP；
3. 将 LCP 向左回退到最后一个完整中文字符、完整英文词或标点边界；
4. 只有当前 LCP 比上次已发布前缀至少新增 2 个中文字符
   或新增 1 个完整英文词时才考虑发布；
5. 新增部分必须在至少 250 ms 内未被改写；
6. 数字、日期、金额、电话号码的未完成尾部不得发布；
7. 每个前缀只能发布一次；
8. final 到达时清空该 sentence_id 的 tracker。
```

示例：

```text
我想定
我想订下
我想订下周
=> 可以发布“我想订”或“我想订下周”，不得发布错误的“我想定”。
```

### 12.6 ASR 上下文

FunASR 上下文仅用于提升专有名词和连续对话识别，不代替 LLM 历史：

- 最多发送最近 5 条 user 和 5 条 assistant；
- 每条截断到 400 个字符以内；
- assistant 只发送用户实际听到的文本；
- 通过 LiveKit 的 conversation item hook 推送到适配器；
- 过滤密钥、卡号、身份证等敏感信息后再发送；
- 上下文更新失败不得中断实时识别。

### 12.7 重连和音频回放

- 内存维护最近 1500 ms PCM 环形缓冲；
- WebSocket 异常后立即创建新连接和新 task；
- 新 task `task-started` 后最多回放最近 1500 ms，避免丢掉断点附近语音；
- 回放帧标记 `replayed=true`，按音频时间去重；
- 若重连超过 2 秒，停止自动回放，向 UI 提示“我刚才没听清，请再说一次”；
- 单会话连续失败 3 次进入 circuit-open 30 秒；
- 已失败连接禁止复用。

### 12.8 适配器骨架

```python
class FunASRSTT(stt.STT):
    def __init__(self, config: FunASRConfig) -> None:
        super().__init__(capabilities=stt.STTCapabilities(
            streaming=True,
            interim_results=True,
            aligned_transcript="word",
            offline_recognize=False,
            chat_context=True,
        ))
        self._config = config
        self._context_items: deque[dict[str, object]] = deque(maxlen=10)
        self._streams: weakref.WeakSet[FunASRRecognizeStream] = weakref.WeakSet()

    @property
    def provider(self) -> str:
        return "alibaba_model_studio"

    @property
    def model(self) -> str:
        return self._config.model

    async def _recognize_impl(self, buffer, *, language, conn_options):
        # 本插件声明 offline_recognize=False。实现该抽象方法仅为满足 LiveKit
        # STT 基类契约；实时管线不得调用批量 recognize()。
        raise NotImplementedError("FunASRSTT is streaming-only; call stream()")

    def _push_conversation_item(self, ev) -> None:
        item = conversation_item_to_funasr_context(ev)
        if item is not None:
            self._context_items.append(item)
        for stream in tuple(self._streams):
            stream.update_context(tuple(self._context_items))

    def stream(self, *, language="zh", conn_options=DEFAULT_API_CONNECT_OPTIONS):
        stream = FunASRRecognizeStream(
            stt=self,
            config=self._config,
            conn_options=conn_options,
            sample_rate=16000,
            initial_context=tuple(self._context_items),
        )
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        await asyncio.gather(
            *(stream.aclose() for stream in tuple(self._streams)),
            return_exceptions=True,
        )


class FunASRRecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt,
        config,
        conn_options,
        sample_rate=16000,
        initial_context=(),
    ):
        super().__init__(
            stt=stt,
            conn_options=conn_options,
            sample_rate=sample_rate,
        )
        self._config = config
        self._pending_context = tuple(initial_context)
        self._context_updates: asyncio.Queue[tuple[dict[str, object], ...]] = asyncio.Queue(1)

    def update_context(self, context) -> None:
        self._pending_context = tuple(context)
        if self._context_updates.full():
            self._context_updates.get_nowait()
        self._context_updates.put_nowait(self._pending_context)

    async def _run(self) -> None:
        # 必须并发运行 input_sender、event_receiver、context_sender、
        # heartbeat/reconnect watcher。从 self._input_ch 读取 LiveKit AudioFrame；
        # 向 self._event_ch 写 SpeechEvent。收到上下文更新时使用供应商的
        # continue-task，而不是重启正在进行的识别任务。
        ...
```

实现者可以参考 LiveKit 官方插件的公开实现方式，但不得依赖私有字段之外的未公开行为。骨架中的 `...` 在最终代码中必须被完整实现。上述骨架还要求显式导入 `asyncio`、`weakref`、`collections.deque`；`conversation_item_to_funasr_context()` 必须按第 12.6 节做脱敏、截断与角色映射，并由单元测试覆盖。

---

## 13. DeepSeek 适配与快慢双路径

### 13.1 快速语音路径

固定参数：

```json
{
  "model": "deepseek-v4-flash",
  "thinking": {"type": "disabled"},
  "stream": true,
  "temperature": 0.45,
  "max_tokens": 240
}
```

LiveKit OpenAI 插件的构造参数名是 `max_completion_tokens`，但 DeepSeek 此接口的正式字段是 `max_tokens`。实现 MUST 像第 11.2 节那样把 `max_tokens` 放进 `extra_body`，并且不得同时发送 `max_completion_tokens`，否则 Provider Smoke Test 视为失败。

规则：

- 只把流式 `content` 送入中文分段器；
- 禁止把 `reasoning_content` 送给 TTS、前端或日志；
- 首 token 超时 3 秒；整次快速回答超时 12 秒；
- 默认最多 240 token，系统提示要求 1–3 个口语短句；
- 快速路径不得执行耗时超过 800 ms 的同步工具；
- 工具参数生成使用 strict JSON schema；
- DeepSeek 当前 Chat Completion 参数表未公开 `parallel_tool_calls`，因此请求中不得发送该字段；
- 如果响应包含多个 `tool_calls`，`ToolExecutor` MUST 按返回顺序串行校验，并且同一 `GenerationFence` 同时最多只运行一个前台工具；其余工具进入队列或被策略拒绝。

### 13.2 深度任务路径

触发条件之一：

- 需要搜索、RAG、文件分析或多个业务 API；
- 用户明确要求详细比较、推理、计划或长结果；
- 预计工具耗时超过 800 ms；
- 快速模型输出 `handoff_to_deep_task` 结构化动作。

深度路径：

```json
{
  "model": "deepseek-v4-pro",
  "thinking": {"type": "enabled"},
  "reasoning_effort": "high",
  "stream": true
}
```

工作流：

```text
用户提交复杂任务
  ├─ 快速路径立即说一句桥接语：“可以，我先帮你核对关键条件。”
  ├─ 后台 TaskManager 启动 DeepSeek Pro + 工具
  ├─ 用户仍可继续说话、增加或修改条件
  ├─ 修改条件 => tool_epoch + 1，旧任务结果自动失效
  └─ 有效结果回来 => 快速模型压缩成 1–3 句口语，再交给 TTS
```

不得直接朗读深度模型的长原始结果。必须经过 `spoken_result_summarizer`，只输出当前用户最关心的信息。

### 13.3 上下文管理

DeepSeek API 是无状态的，`ContextManager` 每次请求显式发送：

```text
1. system prompt
2. 当前业务状态摘要，最多 600 中文字
3. 最近 8 个完整话轮
4. 当前用户 final 文本
5. 当前可用工具定义
```

规则：

- 助手历史使用 `HeardTextTracker` 的实际已听文本；
- 被打断且只听到“明天下午可能有雨”时，历史不得保存后面的“建议带伞”；
- 超过 8 轮的历史由 DeepSeek Pro 异步摘要；
- 摘要失败时，退化为最近 8 轮，不阻塞对话；
- 绝不把原始音频、API Key、内部异常堆栈放入 LLM 上下文。

### 13.4 工具接口

每个工具必须声明：

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict
    cancellable: bool
    idempotent: bool
    timeout_s: float
    contains_sensitive_data: bool = False
```

执行规则：

- 非幂等写操作必须二次确认；
- 取消幂等读操作时可直接终止；
- 无法真正取消的第三方请求也必须通过 fence 隔离结果；
- 工具结果先写结构化对象，不直接写自然语言；
- 超时、失败、取消均返回可口语化的错误码；
- 不得让工具回调直接调用 TTS。

---

## 14. 中文流式口语分段器

LLM token 不得逐 token 送 TTS；也不得等待完整回答。`PhraseSegmenter` 必须按以下规则工作。

### 14.1 文本规范化

送 TTS 前：

- 去除 Markdown 标题、列表符、代码围栏、链接 URL；
- 将连续换行转为句号；
- 将英文缩写和数字按业务规则规范化；
- Emoji 转为可忽略或自然中文，不直接读 Unicode 名称；
- 保留问号、感叹号和逗号以控制韵律；
- 规范化只改变 TTS 文本，`canonical_text` 保留原始语义文本；
- 不得静默改变金额、日期、订单号等业务事实。

### 14.2 确定性切分规则

```text
强边界：。！？；\n
弱边界：，、：
目标首段：8–18 个中文字符
普通段：12–32 个中文字符
硬上限：42 个中文字符
```

算法：

1. 遇到强边界立即提交非空片段；
2. 遇到弱边界时，当前片段长度在 12–28 字之间才提交；
3. 引号、括号未闭合时禁止在弱边界切分；
4. URL、邮箱、金额、日期、版本号和小数内部禁止切分；
5. 达到 42 字时，在最近弱边界强制切分；没有弱边界则在完整词边界切分；
6. 第一非空 token 到达后，最多等待 280 ms 寻找自然边界；
7. 280 ms 时若已有至少 8 个字且语义可独立成句，则提交；否则最多等待 450 ms；
8. LLM 流结束时提交剩余内容；
9. 每段生成 `segment_id`，并携带 fence；
10. 同一生成内片段严格按序送入 TTS。

### 14.3 禁止内容

分段器或系统提示必须阻止 TTS 朗读：

- Markdown 符号；
- JSON、工具参数和内部动作名；
- “正在调用工具”“function_call”之类机器文本；
- DeepSeek 推理过程；
- 未经转换的长 URL；
- 超过 3 句的默认回答。

### 14.4 单元测试示例

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("可以，我先帮你看一下。", ["可以，我先帮你看一下。"]),
        (
            "明天下午可能有雨，出门建议带伞。晚上温度会低一些。",
            ["明天下午可能有雨，", "出门建议带伞。", "晚上温度会低一些。"],
        ),
        (
            "订单号是20260715001234，请确认。",
            ["订单号是20260715001234，请确认。"],
        ),
    ],
)
def test_phrase_segmenter(text, expected):
    assert segment_all(text) == expected
```

---

## 15. CosyVoice Realtime 适配器

### 15.1 LiveKit 能力声明

```python
capabilities = tts.TTSCapabilities(
    streaming=True,
    aligned_transcript=True,
)
```

`longanyang` 在 `cosyvoice-v3-flash` 下支持中文、Instruct 和时间戳。若改换音色，启动时必须执行 smoke test 验证字级时间戳非空；否则 Adaptive Interruption 和实际已听文本追踪不通过，服务不得进入 ready。

### 15.2 WebSocket 连接池

- 进程启动后预建 `COSYVOICE_POOL_SIZE` 条连接；
- 默认池大小为预计峰值并发 TTS 数的 1.5–2 倍，向上取整；
- 每条连接同一时刻只服务一个 task；
- 正常 `task-finished` 后可复用；
- `task-failed`、协议错误、超时或取消后必须关闭并剔除；
- 后台异步补齐连接池，但补池不得阻塞当前会话；
- 连接空闲接近供应商超时前主动重建；
- 连接池耗尽时最多等待 300 ms，之后使用新建连接；仍失败则文字降级。

### 15.3 `run-task` 固定请求

```json
{
  "header": {
    "action": "run-task",
    "task_id": "<UUID>",
    "streaming": "duplex"
  },
  "payload": {
    "task_group": "audio",
    "task": "tts",
    "function": "SpeechSynthesizer",
    "model": "cosyvoice-v3-flash",
    "parameters": {
      "text_type": "PlainText",
      "voice": "longanyang",
      "format": "pcm",
      "sample_rate": 24000,
      "volume": 50,
      "rate": 1.0,
      "pitch": 1.0,
      "enable_ssml": false,
      "word_timestamp_enabled": true,
      "language_hints": ["zh"],
      "instruction": "你正在进行闲聊互动，你说话的情感是 neutral。"
    },
    "input": {}
  }
}
```

必须等 `task-started` 后才发送文本。

### 15.4 文本发送

每个 `PhraseSegmenter` 片段发送：

```json
{
  "header": {
    "action": "continue-task",
    "task_id": "<同一 UUID>",
    "streaming": "duplex"
  },
  "payload": {
    "input": {
      "text": "明天下午可能有雨，"
    }
  }
}
```

LLM 结束且所有片段已发送后，必须发送 `finish-task`。正常完成必须等到服务端 `task-finished`。

### 15.5 服务端事件和音频映射

CosyVoice 会返回：

- `sentence-begin`：记录句子 index 与原始文本；
- `sentence-synthesis`：其后紧跟一个二进制音频帧；
- `sentence-end`：含字级 `begin_time/end_time/text`；
- `task-finished`：正常完成；
- `task-failed`：立即关闭连接。

每个二进制 PCM 帧通过：

```python
output_emitter.initialize(
    request_id=tts_task_id,
    sample_rate=24000,
    num_channels=1,
    mime_type="audio/pcm",
    frame_size_ms=20,
    stream=True,
)
output_emitter.push(pcm_bytes)
```

字级时间戳通过 `output_emitter.push_timed_transcript(...)` 送入 LiveKit。时间戳是相对于当前 TTS task 音频起点；适配器必须加上已完成片段的累计音频偏移，保证整个回答单调递增。

### 15.6 时间戳晚到处理

供应商可能先发送音频，再在 `sentence-end` 返回对齐：

1. 按 `sentence.index` 缓存该句已送出的音频字节数；
2. 根据采样率计算该句音频时长；
3. `sentence-end` 到达后，校验最后一个词 `end_time` 与实际 PCM 时长差值；
4. 误差 ≤120 ms：直接发布；
5. 误差 120–300 ms：按比例缩放该句内部词时间戳并记录 warning；
6. 误差 >300 ms：该句标记 `alignment_degraded`，禁止用于精确 heard-text 截断，并使用 PCM 播放进度 + 字符比例保守估计；
7. 连续 3 句严重退化时，将服务标记 unready 并换新连接。

### 15.7 取消语义

CosyVoice 协议没有可依赖的跨供应商统一“取消当前 task”动作，因此用户打断时必须：

1. LiveKit 立即停止向用户播放；
2. `generation_id += 1`，使后续帧失效；
3. 取消读取/写入协程；
4. 关闭当前 CosyVoice WebSocket；
5. 不发送 `finish-task` 等待正常复用；
6. 从连接池剔除；
7. 后台补一条新连接。

这是故意以连接成本换取正确性。不得为了复用连接继续接收并冒险误播旧音频。

### 15.8 适配器骨架

```python
class CosyVoiceTTS(tts.TTS):
    def __init__(self, config: CosyVoiceConfig, pool: CosyVoicePool) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(
                streaming=True,
                aligned_transcript=True,
            ),
            sample_rate=24000,
            num_channels=1,
        )
        self._config = config
        self._pool = pool

    @property
    def provider(self) -> str:
        return "alibaba_model_studio"

    @property
    def model(self) -> str:
        return self._config.model

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        # CosyVoice 的非流式 LiveKit 接口也复用同一个 WebSocket 流实现，
        # 避免维护两套协议代码。
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return CosyVoiceSynthesizeStream(
            tts=self,
            config=self._config,
            pool=self._pool,
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts, config, pool, conn_options):
        super().__init__(tts=tts, conn_options=conn_options)
        self._config = config
        self._pool = pool

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        # 从 self._input_ch 读取文本和 FlushSentinel；
        # 发送 run/continue/finish-task；
        # 将二进制 PCM 与 TimedString 推给 output_emitter；
        # 取消时关闭并丢弃连接。
        ...
```

最终代码中的 `...` 必须完整实现，并由协议模拟集成测试覆盖。

---

## 16. 中文打断守卫与附和处理

### 16.1 LiveKit Cloud 档案

主判断使用 Adaptive Interruption。该模型在 VAD 检测到用户说话后，根据声学信号区分真实打断和 backchannel；FunASR 的字级时间戳用于对齐。应用层仍保留以下规则作为安全补充，但不能覆盖模型的明显相反结论。

### 16.2 自建档案 `ChineseInterruptionGuard`

在 VAD 模式下，按三层决策：

#### 层 1：0–80 ms 快速 duck

检测到播放期间用户人声：

- 立即把助手播放音量降低到原来的 25%；
- 不立即销毁生成；
- 开始 250 ms 判定窗口；
- 若用户发出高置信度关键词“停、等等、不是、别说了”，立即确认打断。

#### 层 2：80–350 ms 声学/文本判定

附和词白名单，仅在总时长 ≤900 ms、没有后续完整句时成立：

```text
嗯、嗯嗯、对、对的、是、是的、好、好的、行、可以、明白、知道了、哦、啊
```

明确打断前缀：

```text
等等、等一下、停、停一下、不是、不对、先别、你听我说、我的意思是、我问的是、换一个
```

规则：

- 命中明确打断前缀 => 确认打断；
- 仅命中附和词且后续 350 ms 无新增稳定文本 => 恢复播放；
- 文本长度 ≥5 个中文字符，或出现新问题语气 => 确认打断；
- 咳嗽、键盘、碰撞声且无 ASR 文本 => 恢复播放；
- 不确定时优先让出话权，但记录 `uncertain_interruption`。

#### 层 3：误打断恢复

若已暂停但 1.2 秒内没有形成有效用户文本：

- 视为 false interruption；
- 从最近自然短语边界恢复，不从被砍断的音素中间恢复；
- 若无法安全恢复原音频，则重新合成剩余当前句；
- 不重复已经实际播放的文字。

### 16.3 业务特定短答优化

对于只允许“是/否/确认”的业务节点，不完全依赖通用 Turn Detector：

- 一旦 ASR final 属于允许答案集合，立即提交话轮；
- endpoint max delay 临时降到 0.8 秒；
- 节点结束后恢复默认参数；
- 该优化必须由显式业务状态触发，不能全局开启。


---

## 17. 原子打断与取消流程

真实打断确认后，`Orchestrator.confirm_interruption()` 必须在一个状态机临界区内按以下顺序执行：

```text
T0  记录 barge_in_detected
1. 立即提升 generation_id，使所有旧输出失效
2. 请求 LiveKit 停止当前 speech_handle / 清空待播放队列
3. 客户端收到事件后停止当前音轨播放并记录 playback_stopped
4. 取消 DeepSeek 流式生成 task
5. 取消 PhraseSegmenter 与 TTS sender/receiver task
6. 关闭并丢弃当前 CosyVoice WebSocket
7. 对 cancellable 工具发送取消；不可取消工具只作 fence 隔离
8. 根据 TTS 对齐和播放进度计算实际已听文本
9. 把助手历史截断到实际已听文本
10. 新建用户 turn buffer，状态进入 USER_SPEAKING
```

伪代码：

```python
async def confirm_interruption(self, cause: str) -> None:
    async with self._state_lock:
        old = self._fence
        self._fence = GenerationFence(
            session_id=old.session_id,
            turn_id=old.turn_id,
            generation_id=old.generation_id + 1,
            tool_epoch=old.tool_epoch,
        )

        heard_text = self._heard_tracker.snapshot()
        await self._playback.stop_and_flush()
        await cancel_and_wait(self._active_llm_task)
        await cancel_and_wait(self._active_tts_task)
        await self._cosyvoice_pool.discard_active_connection(old)
        await self._task_manager.cancel_cancellable(old)
        self._context.commit_interrupted_assistant_text(heard_text)
        self._state = ConversationState.USER_SPEAKING
```

### 17.1 延迟预算

```text
VAD 检测与 duck           30–80 ms
打断模型/规则确认         40–100 ms
服务端停止发布            10–30 ms
客户端缓冲清空            10–40 ms
--------------------------------
目标 P95 完全停止         <=180 ms
```

### 17.2 禁止实现

- 只暂停浏览器播放器，但让 LLM/TTS 在后台继续；
- 先等待 ASR final 再停止；
- 用户打断后仍把完整助手回答写入历史；
- 为了复用 CosyVoice 连接而继续消费旧 task；
- 工具结果回来时不检查 fence；
- 用户新问题已开始仍恢复旧音频。

---

## 18. 实际已听文本 `HeardTextTracker`

### 18.1 数据源

Tracker 同时使用：

1. CosyVoice 字级时间戳；
2. LiveKit 实际播放起始时刻；
3. 客户端或服务端的播放进度/停止时刻；
4. RTC jitter buffer 安全余量。

### 18.2 计算方法

```text
heard_audio_ms = max(
    0,
    playback_stopped_monotonic - playback_started_monotonic
    - OUTPUT_SAFETY_MARGIN_MS
)
```

默认 `OUTPUT_SAFETY_MARGIN_MS=80`。只保留 `word.end_ms <= heard_audio_ms` 的字词。若时间戳严重退化，使用保守字符比例，并向前回退到自然标点边界。

### 18.3 例子

生成文本：

```text
明天下午可能有雨，建议你带一把伞，晚上温度会更低。
```

用户在 1.32 秒打断，时间戳显示实际完整播完的内容是：

```text
明天下午可能有雨，
```

写入历史的 assistant 内容必须正好是上面这一段；后半句不得保存为已说出内容。

### 18.4 对齐不确定时的原则

宁可少记，不可多记。多记会让下一轮模型错误假设用户已经听到信息；少记最多导致助手稍后自然补充。

---

## 19. 四阶段实施计划

四阶段必须顺序执行。前一阶段验收未通过，不得通过堆叠后续功能掩盖问题。

### 阶段 1：能持续监听、能正确取消

#### 必做项

- LiveKit 房间和浏览器麦克风；
- AEC/NS/AGC；
- 助手说话时麦克风和 FunASR 持续工作；
- FunASR interim/final/时间戳适配；
- DeepSeek 非思考流式回答；
- CosyVoice 流式 PCM；
- 打断时同时停止播放、LLM、TTS；
- `GenerationFence`；
- `HeardTextTracker`；
- 基础指标。

#### 阶段 1 验收

- 明确说“停一下”后 P95 180 ms 内完全停止；
- 连续打断 100 次，旧音频误播为 0；
- 被打断后历史只含实际听到内容；
- 助手播放期间麦克风有效率 100%；
- 任一模型 API 断开时不会出现孤儿任务。

### 阶段 2：不乱抢话、区分附和

#### 必做项

- Silero VAD + Audio Turn Detector；
- LiveKit Adaptive Interruption，或自建中文守卫；
- dynamic endpointing；
- 附和/明确打断词规则；
- false interruption 恢复；
- 中句停顿、咳嗽、键盘和背景人声测试；
- 业务短答节点参数覆盖。

#### 阶段 2 验收

- “我想订……下周三的票”中间 800 ms 停顿，误提交率 <3%；
- 助手说话时用户说“嗯嗯/对/好的”，误停止率 <3%；
- 用户说“不是，我问的是下周三”，打断成功率 >95%；
- 纯背景噪声导致的误打断率 <2%；
- E2E 测试至少覆盖 100 条中文录音。

### 阶段 3：反应自然、工具期间仍可聊

#### 必做项

- `PREFLIGHT_TRANSCRIPT` 稳定前缀；
- DeepSeek 预生成；
- 中文流式口语分段器；
- 首段短句策略；
- 快速模型和深度任务模型分离；
- 可取消工具与 `tool_epoch`；
- 结果口语压缩；
- CosyVoice 连接池预热。

#### 阶段 3 验收

- 说完到首个可听音频 P50 ≤700 ms，P95 ≤1200 ms；
- 工具运行期间用户可以增加条件，旧条件结果误播为 0；
- 默认回答 90% 不超过 3 个口语句；
- 第一段语音不以半个数字、半个英文词或未闭合括号结束；
- 预生成错误猜测从未直接播出。

### 阶段 4：轻量韵律自适应

此阶段不是“情绪诊断”，只调整说话节奏。

#### CPU 可计算信号

- 用户话轮 RMS/峰值 dBFS；
- 根据 ASR 字时间戳计算语速；
- 话轮内停顿比例；
- 可选基频中位数和方差；
- 最近三轮是否频繁打断；
- 用户明确语言内容，例如“说慢一点”。

#### 输出样式

```python
class SpeakingStyle(StrEnum):
    NEUTRAL = "neutral"
    CALM = "calm"
    HESITANT = "hesitant"
    URGENT = "urgent"
    EXCITED = "excited"
```

规则：

- 用户明确要求优先于推断；
- 置信度低于 0.7 时使用 `neutral`；
- 三轮指数平滑，禁止一帧抖动导致风格突变；
- 语速只在 0.90–1.10 范围内调整；
- 音高首版保持 1.0；
- CosyVoice `longanyang` 的 instruction 使用其规定格式，例如：

```text
你正在进行闲聊互动，你说话的情感是 neutral。
```

#### 阶段 4 验收

- 用户说“慢一点”后下一句语速下降但内容不变；
- 风格切换不导致首音频延迟增加超过 100 ms；
- 不存储“用户焦虑/抑郁”等敏感标签；
- 不因声学推断改变业务决定。

---

## 20. 前端设计

### 20.1 页面状态

前端只显示下列用户可理解状态：

```text
正在连接
可以说话
正在听
正在思考
正在回答
已被打断
正在处理任务
连接恢复中
```

不得暴露“VAD、EOT、TTS task-failed”等内部术语。

### 20.2 控制 API

#### `POST /v1/sessions`

请求：

```json
{
  "user_id": "anonymous-or-auth-user-id",
  "locale": "zh-CN",
  "client": {
    "platform": "web",
    "timezone": "Asia/Shanghai"
  }
}
```

响应：

```json
{
  "session_id": "uuid",
  "livekit_url": "wss://...",
  "room_name": "voice-<uuid>",
  "participant_token": "short-lived-jwt",
  "expires_in": 300,
  "agent_name": "duplex-zh-agent",
  "config": {
    "locale": "zh-CN",
    "allow_text_fallback": true
  }
}
```

Token 只能连接指定 room，TTL 300 秒。永久 LiveKit API Secret 仅在服务端。

#### `POST /v1/sessions/{session_id}/stop-response`

语义：用户主动按下“停止回答”。服务端必须走与真实语音打断相同的原子取消流程，但不创建新的用户话轮。

#### `GET /health/live`

进程活着即 200。

#### `GET /health/ready`

只有以下条件全部满足才 200：

- LiveKit 可连接；
- FunASR smoke task 成功；
- DeepSeek 轻量请求成功；
- CosyVoice 连接池至少有一条 ready；
- 所选 CosyVoice 音色返回有效时间戳；
- 配置校验通过。

### 20.3 前端行为

- 进入页面后先请求 session，再连接 LiveKit；
- 显式用户手势后启用音频播放，处理浏览器 autoplay 限制；
- 连接前做设备和权限检查；
- 实时显示 ASR interim，但 final 到达前用较浅样式；
- 助手字幕按实际播放进度逐字/逐词高亮；
- 被打断后立即截断屏幕上的助手字幕；
- 网络恢复后不重复播放旧音频；
- 提供“停止回答”“结束会话”“切换麦克风”；
- 不提供会在助手说话时自动 mute 麦克风的按钮逻辑；
- 页面隐藏或设备休眠后恢复时，重新校验轨道状态。

### 20.4 LiveKit Data 事件

前后端共享 `packages/contracts/events.schema.json`。至少包括：

```json
{
  "type": "assistant_state",
  "session_id": "uuid",
  "state": "speaking",
  "turn_id": 3,
  "generation_id": 5,
  "at": "2026-07-15T00:00:00Z"
}
```

```json
{
  "type": "transcript_delta",
  "speaker": "assistant",
  "text": "明天下午可能有雨，",
  "final": false,
  "heard": true,
  "turn_id": 3,
  "generation_id": 5
}
```

客户端收到较小 `generation_id` 的事件必须丢弃。

---

## 21. 可观测性和 SLO

### 21.1 必须记录的时间点

每个事件记录 `monotonic_ns`：

```text
rtc_connected
speech_start
asr_first_audio_sent
asr_first_partial
last_user_audio
vad_end
eot_prediction_received
turn_committed
llm_request_started
llm_first_content_token
first_phrase_ready
tts_task_started
tts_first_audio_received
first_audio_published
client_first_playback
barge_in_candidate
barge_in_confirmed
playback_stopped
tool_started
tool_finished
```

### 21.2 派生指标

```text
asr_partial_latency
endpointing_latency = turn_committed - last_user_audio
llm_ttft = llm_first_content_token - llm_request_started
phrase_wait = first_phrase_ready - llm_first_content_token
tts_ttfb = tts_first_audio_received - tts_task_started
speech_to_speech = client_first_playback - last_user_audio
barge_in_stop = playback_stopped - barge_in_confirmed
```

### 21.3 工程 SLO

这些是本项目验收目标，不是供应商 SLA：

| 指标 | 目标 |
|---|---:|
| 用户明确打断到开始 duck，P95 | ≤80 ms |
| 用户明确打断到完全停止，P95 | ≤180 ms |
| 用户实际说完到话轮提交，P50 | ≤420 ms |
| 用户实际说完到首音频，P50 | ≤700 ms |
| 用户实际说完到首音频，P95 | ≤1200 ms |
| 附和导致误停止 | <3% |
| 中句停顿导致误抢话 | <3% |
| 背景噪声导致误打断 | <2% |
| 明确打断成功率 | >95% |
| 默认回答超过 3 句比例 | <10% |
| 旧 generation 音频误播 | 0 |
| 旧 tool epoch 结果误播 | 0 |
| 播放期间麦克风有效率 | 100% |
| 无时间戳 TTS 进入生产 | 0 |

### 21.4 Prometheus 指标

至少暴露：

```text
voice_sessions_active
audio_input_seconds_total
asr_requests_total{status,model}
asr_reconnects_total
llm_requests_total{model,status,thinking}
tts_requests_total{status,model,voice}
tts_pool_available
tts_connections_discarded_total{reason}
interruption_candidates_total
interruptions_confirmed_total
false_interruptions_total
stale_result_dropped_total{source}
tool_tasks_active{tool}
voice_latency_seconds{stage,quantile}
state_transition_total{from,to,event}
```

### 21.5 结构化日志

每条日志至少包含：

```json
{
  "level": "info",
  "event": "state_transition",
  "session_id": "...",
  "turn_id": 3,
  "generation_id": 5,
  "from_state": "speaking",
  "to_state": "user_speaking",
  "cause": "adaptive_interruption",
  "monotonic_ns": 123456789
}
```

不得记录：完整 API Key、Authorization header、未经脱敏的身份证/银行卡、DeepSeek 推理内容、默认原始音频。

---

## 22. 错误恢复与降级

### 22.1 FunASR

| 故障 | 动作 |
|---|---|
| 连接建立失败 | 指数退避 200/500/1000 ms，最多 3 次 |
| 运行中断开 | 新 task + 最多 1500 ms 音频回放 |
| 2 秒未恢复 | UI 提示用户重说；不猜测内容 |
| task-failed | 关闭连接，禁止复用 |
| 时间戳缺失 | 当前句仍显示文字，但禁用精确打断对齐；连续发生则 unready |

### 22.2 DeepSeek

| 故障 | 动作 |
|---|---|
| 首 token >3 s | 取消快速请求，重试一次；向用户说预合成桥接语 |
| 总时长 >12 s | 取消快速请求；返回简短失败提示 |
| 429 | 尊重 Retry-After；会话内不忙循环 |
| 5xx | 抖动退避重试一次 |
| 工具 JSON 不合法 | strict schema 校验失败则重新请求一次 |
| 深度任务超时 | 保留前台会话；告诉用户当前无法完成，不阻塞新话轮 |

预合成桥接语可本地缓存 PCM：

```text
“我在看，稍等一下。”
“可以，我先帮你核对。”
“刚才没有听清，可以再说一遍吗？”
“现在连接不太稳定，我们再试一次。”
```

缓存语句使用同一 CosyVoice 音色预生成，并标注版本。不可用固定桥接语冒充真实业务结果。

### 22.3 CosyVoice

| 故障 | 动作 |
|---|---|
| 连接池无连接 | 等待最多 300 ms，再新建连接 |
| 首音频 >1.5 s | 取消并用新连接重试一次 |
| task-failed | 关闭并丢弃连接 |
| 字级时间戳为空 | 该连接丢弃；尝试一次；仍失败则文字降级 |
| TTS 全部失败 | UI 显示文本；用耳标提示，不播放系统浏览器 TTS |

不建议在同一次会话中自动切换到浏览器 `speechSynthesis`，因为音色、时间戳和取消行为不可控，会破坏一致性。

### 22.4 LiveKit/RTC

- 短暂重连期间暂停新生成；
- 恢复后不得补播连接中断前积压的旧音频；
- 重连成功后 `generation_id += 1`；
- 10 秒未恢复则结束会话并允许用户一键重新连接；
- UI 显示“连接恢复中”，但麦克风权限不自动重置。

### 22.5 Circuit Breaker

每个供应商维护独立 breaker：

```text
CLOSED -> 连续 3 次失败 -> OPEN 30 秒
OPEN -> 仅允许健康探测 -> HALF_OPEN
HALF_OPEN -> 成功 2 次 -> CLOSED
HALF_OPEN -> 任意失败 -> OPEN
```

Breaker 状态不得跨供应商连锁。例如 TTS 故障时仍允许文字对话。

---

## 23. 测试与验收规范

### 23.1 单元测试

必须覆盖：

- `GenerationFence` 过期结果丢弃；
- 状态机所有合法/非法转换；
- 中文分段器；
- 稳定前缀；
- 附和词与打断词；
- `HeardTextTracker`；
- 工具 epoch；
- 配置校验；
- 时间戳缩放与退化；
- 文本规范化不改变金额/日期。

覆盖率要求：

```text
services/agent/src/orchestration >= 90%
services/agent/src/providers/protocol parsing >= 90%
整体 Python >= 85%
前端状态逻辑 >= 80%
```

### 23.2 协议模拟集成测试

不得依赖真实供应商才能测试协议。实现本地 mock WebSocket server，按录制的匿名事件夹具模拟：

#### FunASR

- 正常 `task-started -> interim -> final -> task-finished`；
- interim 反复改写；
- heartbeat；
- 缺失时间戳；
- task-failed；
- 中途断线与回放；
- 慢事件和乱序保护。

#### CosyVoice

- sentence-begin；
- sentence-synthesis + 紧随二进制 PCM；
- sentence-end + words；
- 多句累计时间戳；
- 时间戳晚到；
- task-failed；
- 用户取消时连接被关闭且不回池。

#### DeepSeek

- 流式 content；
- thinking 字段存在但不进入 TTS；
- function call；
- 非法 JSON；
- 首 token 超时；
- 429/5xx；
- 用户打断后 stream 被取消。

### 23.3 中文 E2E 音频数据集

至少 200 条，按真实目标设备录制，不使用纯 TTS 合成替代全部数据：

| 类别 | 至少条数 | 示例 |
|---|---:|---|
| 普通问答 | 30 | “明天适合去公园吗” |
| 句中停顿 | 30 | “我想订……下周三的票” |
| 犹豫改口 | 20 | “订周二，不对，周三” |
| 附和 | 30 | 助手说话时“嗯嗯、对、好的” |
| 明确打断 | 30 | “等等，不是这个意思” |
| 噪声 | 20 | 键盘、咳嗽、门响、电视声 |
| 中英混说 | 15 | “帮我查一下 API latency” |
| 数字日期 | 15 | 日期、金额、订单号、手机号 |
| 工具改条件 | 10 | 搜索中追加筛选条件 |

每条夹具包含：

```json
{
  "id": "interrupt-001",
  "audio_file": "interrupt-001.wav",
  "assistant_audio_overlap": "assistant-sample-003.wav",
  "expected": {
    "turn_commit_window_ms": [250, 700],
    "should_interrupt": true,
    "expected_text_contains": ["不是", "下周三"],
    "max_stop_latency_ms": 180
  }
}
```

### 23.4 AEC 真实设备矩阵

至少测试：

- macOS Chrome 内置扬声器；
- Windows Chrome 普通笔记本；
- iPhone Safari；
- Android Chrome；
- 有线耳机；
- 蓝牙耳机；
- 外放 50%、80% 音量；
- 安静房间、办公室、街边。

同一设备助手播放期间，FunASR 不应大量识别出助手自己的声音。若发生，先修 AEC/音频路由，不得用关键词规则掩盖。

### 23.5 Chaos 测试

随机注入：

- FunASR 连接每 30–120 秒断开；
- DeepSeek 首 token 延迟 5 秒；
- CosyVoice 在第二句 task-failed；
- RTC 丢包 5%、10%；
- 工具延迟 20 秒后返回旧结果；
- 客户端断线 3 秒重连；
- Redis/Postgres 暂时不可用。

系统必须保证：

- 不误播旧内容；
- 不死锁；
- 不无限重试；
- 实时路径在数据库故障时仍可继续；
- 会话结束后所有 task 和 WebSocket 被回收。

### 23.6 Definition of Done

只有以下全部成立才算完成：

- [ ] `uv run ruff check .` 通过；
- [ ] `uv run mypy services --strict` 通过；
- [ ] `uv run pytest` 通过；
- [ ] `pnpm --dir apps/web lint` 通过；
- [ ] `pnpm --dir apps/web test` 通过；
- [ ] provider smoke test 通过；
- [ ] 200 条中文 E2E 全部运行；
- [ ] 第 21 章所有 SLO 达标；
- [ ] 100 次连续打断没有旧音频；
- [ ] 100 次工具条件修改没有旧结果误播；
- [ ] CosyVoice 时间戳 smoke test 非空；
- [ ] 助手说话时麦克风轨道始终开启；
- [ ] 安全扫描未发现前端密钥；
- [ ] 生产构建使用锁文件；
- [ ] README 包含从零启动与故障排查。


---

## 24. 部署、网络和容量规划

### 24.1 网络优先于模型参数

上线前必须从真实目标地区测量以下 RTT 和首包：

```text
用户 -> LiveKit RTC 节点
Agent -> FunASR WebSocket
Agent -> CosyVoice WebSocket
Agent -> DeepSeek HTTPS
```

原则：

- Agent 应尽可能靠近 FunASR/CosyVoice 的阿里云地域；
- 用户和 Agent 跨境时必须以实测为准；
- 不要为了少 100 ms 模型生成时间，容忍 300 ms 额外网络 RTT；
- DNS、TLS 握手和冷连接必须通过连接复用/预热从关键路径移除；
- 每次会话开始前预热 ASR/TTS，不要等用户第一句话后再建全部连接。

### 24.2 LiveKit Cloud 部署

开发机：

```bash
curl -sSL https://get.livekit.io/cli | bash
lk cloud auth
lk project set-default "<project-name>"
```

Agent 初次创建：

```bash
cd voice-agent/services/agent
lk agent create
lk agent status
lk agent logs
```

后续部署：

```bash
lk agent deploy
```

LiveKit Cloud 构建时不会上传 `.env.*`，生产密钥必须通过其 Secrets 管理注入。部署后使用 `lk agent status` 和 `lk agent logs` 验证，不以“构建成功”代替运行验收。

### 24.3 自建 LiveKit

自建生产环境至少需要：

- 独立域名与可信 TLS 证书；
- UDP 媒体端口；
- TURN/TLS 以兼容企业网络；
- Redis；
- LiveKit Server；
- 独立 Agent Worker；
- 公网 IP 和正确的防火墙配置。

官方 VM 生成器可作为基础：

```bash
docker pull livekit/generate
docker run --rm -it -v "$PWD:/output" livekit/generate
```

不得在不开放 UDP/TURN 的普通反向代理后直接声称 WebRTC 已生产可用。

### 24.4 容器入口

`services/agent/src/main.py`：

```python
import os

from livekit import agents

from .agent import entrypoint, prewarm

server = agents.AgentServer(setup_fnc=prewarm)
server.rtc_session(
    entrypoint,
    agent_name=os.getenv("LIVEKIT_AGENT_NAME", "duplex-zh-agent"),
)

if __name__ == "__main__":
    agents.cli.run_app(server)
```

`WorkerOptions` 在 1.6.5 中仍是兼容别名，但新实现必须使用上面的 `AgentServer` 入口，避免继续依赖旧式启动接口。

`infra/Dockerfile.agent`：

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY services/agent ./services/agent
COPY packages ./packages

USER 65532:65532
ENTRYPOINT ["uv", "run", "python", "-m", "services.agent.src.main"]
CMD ["start"]
```

### 24.5 初始资源上限

以下只是防止过载的初始保守值，最终必须由负载测试调整：

#### LiveKit Cloud + 托管 Turn Detector

```text
Agent Worker：2 vCPU / 2–4 GB RAM
单 Worker 会话软上限：8
CPU 70% 或活跃会话 7 时开始扩容
```

#### 自建 + `v1-mini`

```text
Agent Worker：4 vCPU / 8 GB RAM
单 Worker 会话软上限：4
必须使用持续性能 CPU，不使用可突发但会耗尽积分的实例
```

控制 API：

```text
1 vCPU / 1 GB 起步；无状态；至少 2 副本
```

单 Worker 的会话上限必须通过 30 分钟持续负载测试确认，测试内容包含同时说话、打断和 TTS。不得仅用静默连接测试容量。

### 24.6 CosyVoice 连接池容量

```text
pool_size = ceil(峰值同机并发 TTS × 1.5)
```

例如 Worker 最多 4 个会话，通常同一时刻最多 4 路 TTS，则初始池大小 6。若供应商连接配额不足，应降低 Worker 会话上限，而不是共享一条正在工作的 TTS 连接。

### 24.7 数据库不在实时关键路径

会话事件写入：

```text
实时事件 -> 内存有界队列 -> 100–500 ms 批量写 PostgreSQL
```

数据库故障时：

- 实时对话继续；
- 队列在内存保留有限数量；
- 超限时丢弃低优先级指标而非音频/状态；
- 记录审计缺口；
- 不允许实时协程等待数据库恢复。

---

## 25. 安全、隐私和合规

### 25.1 密钥

- FunASR、CosyVoice、DeepSeek、LiveKit Secret 只存在服务端；
- 前端只收到短期 LiveKit participant token；
- 密钥通过云 Secrets 注入，不写进镜像、仓库或日志；
- 不同环境使用不同密钥和 Workspace；
- 定期轮换；
- Git hooks 和 CI 使用 secret scanner；
- 发现泄漏必须立即吊销，而不是只删除提交。

### 25.2 最小权限

- LiveKit token 只允许加入单个指定房间；
- 匿名用户每 IP/设备限制并发会话和每分钟创建次数；
- 工具服务使用独立服务账户；
- 写操作工具必须有明确授权和确认；
- 数据库账号按读写职责拆分；
- 管理接口与用户 API 分离。

### 25.3 音频保留

默认：

```text
AUDIO_RETENTION_ENABLED=false
```

也就是原始音频不落盘。若业务需要质量分析：

- 必须获得用户明确同意；
- 对测试/生产分别配置；
- 使用加密对象存储；
- 设置明确自动删除期限；
- 访问留审计；
- 不把录音作为默认调试手段；
- 评测数据尽可能使用自愿录制的专用样本。

### 25.4 文本脱敏

持久化前检测并遮盖：

- 手机号；
- 身份证号；
- 银行卡号；
- 邮箱；
- 地址；
- API Key 或 token 形态字符串。

业务确实需要原值时，只在加密的专用字段保存，并与对话日志分离。

### 25.5 Prompt Injection 与工具安全

- 用户说出的任何内容都视为不可信输入；
- 工具 schema 与授权逻辑由代码控制，不允许 LLM 自行扩大权限；
- 工具结果同样视为不可信文本；
- 禁止把工具返回的隐藏指令提升为 system 指令；
- 高风险写操作采用“展示计划 -> 用户确认 -> 执行”；
- 日志中记录工具名和结果摘要，不记录敏感完整载荷。

### 25.6 音色使用

- 使用系统音色或已获得授权的复刻音色；
- 不复刻未经同意的真实人物声音；
- 产品 UI 明确说明为 AI 语音；
- 按所在地法规决定是否开启 AIGC 标识参数。

---

## 26. 本地开发和启动命令

### 26.1 前置工具

```bash
python --version   # 必须为 3.12.x
uv --version
node --version
pnpm --version
lk --version
```

### 26.2 首次安装

```bash
git clone <your-repo>
cd voice-agent
cp .env.example .env

uv sync --frozen
pnpm install --frozen-lockfile

docker compose up -d postgres redis
uv run python scripts/verify_env.py
uv run python scripts/provider_smoke_test.py
```

### 26.3 开发模式

终端 1：

```bash
uv run uvicorn services.control_api.app.main:app \
  --host 0.0.0.0 --port 8000 --reload
```

终端 2：

```bash
uv run python -m services.agent.src.main dev
```

终端 3：

```bash
pnpm --dir apps/web dev --host 0.0.0.0
```

### 26.4 测试

```bash
uv run ruff check .
uv run mypy services --strict
uv run pytest -q
pnpm --dir apps/web lint
pnpm --dir apps/web test --run
uv run python scripts/run_e2e.py --profile offline
uv run python scripts/run_e2e.py --profile provider-smoke
```

### 26.5 Makefile 目标

```makefile
.PHONY: install verify dev-api dev-agent dev-web test e2e smoke

install:
	uv sync --frozen
	pnpm install --frozen-lockfile

verify:
	uv run python scripts/verify_env.py

smoke:
	uv run python scripts/provider_smoke_test.py

dev-api:
	uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload

dev-agent:
	uv run python -m services.agent.src.main dev

dev-web:
	pnpm --dir apps/web dev

test:
	uv run ruff check .
	uv run mypy services --strict
	uv run pytest
	pnpm --dir apps/web lint
	pnpm --dir apps/web test --run

e2e:
	uv run python scripts/run_e2e.py --profile provider-smoke
```

---

## 27. Provider Smoke Test

`scripts/provider_smoke_test.py` 必须真实验证，而不是只检查 HTTP 200。

### 27.1 FunASR

发送一段固定 16 kHz 中文 WAV：

```text
“你好，这是实时语音测试。”
```

通过条件：

- 收到 task-started；
- 至少一个 interim；
- 收到 final；
- final 包含“实时语音测试”；
- words 非空且时间单调；
- 正常 task-finished。

### 27.2 DeepSeek

发送：

```text
请只回答“连接正常”。
```

通过条件：

- 使用 `deepseek-v4-flash`；
- thinking disabled；
- stream=true；
- 3 秒内收到 content；
- 输出含“连接正常”；
- 没有把 reasoning_content 当 content。

### 27.3 CosyVoice

合成：

```text
“你好，这是语音合成测试。”
```

通过条件：

- `cosyvoice-v3-flash` + `longanyang`；
- 24 kHz raw PCM；
- 收到二进制音频；
- 收到 sentence-end；
- words 非空；
- 时间戳单调且末尾与 PCM 时长误差 ≤300 ms；
- 正常 task-finished。

任何 smoke test 失败，`/health/ready` 必须失败。

---

## 28. 上线步骤与运行手册

### 28.1 上线前

1. 冻结依赖锁文件；
2. 跑完整单元、集成、E2E；
3. 从目标城市跑网络 RTT；
4. 检查供应商配额和并发连接限制；
5. 验证音色时间戳；
6. 用真实设备矩阵测试 AEC；
7. 运行 30 分钟峰值并发负载；
8. 检查无密钥进入前端 bundle；
9. 设置告警和 dashboard；
10. 保存上一版本镜像和配置以便回滚。

### 28.2 灰度

```text
内部用户 -> 5% -> 20% -> 50% -> 100%
```

每阶段至少观察：

- speech-to-speech P50/P95；
- barge-in stop P95；
- false interruption；
- FunASR/CosyVoice 连接失败；
- stale result dropped；
- 会话异常结束；
- 用户主动停止回答频率；
- 默认回答时长。

若旧结果误播出现 1 次，立即停止扩量并回滚；该指标没有容忍区间。

### 28.3 告警

立即告警：

```text
stale audio playback detected > 0
stale tool playback detected > 0
ready replicas < 1
CosyVoice timestamp empty rate > 1%
barge-in stop P95 > 300 ms for 10 min
speech-to-speech P95 > 1800 ms for 10 min
provider error rate > 5% for 5 min
```

警告：

```text
false interruption > 5%
TTS pool utilization > 80%
Agent CPU > 75%
ASR reconnects > 3% sessions
```

### 28.4 故障排查顺序

#### “反应慢”

按指标逐段定位：

```text
endpointing -> LLM TTFT -> phrase wait -> TTS TTFB -> RTC playback
```

不得直接假设 DeepSeek 慢。

#### “总抢话”

检查：

1. VAD min silence 是否小于 0.25；
2. Turn Detector 是否实际启用；
3. ASR 是否把短暂停顿当 final；
4. endpointing 是否过短；
5. 目标录音中语气和停顿分布；
6. 是否把 FunASR sentence_end 当最终对话话轮。

#### “用户嗯一声就停”

检查：

1. Adaptive Interruption 是否因 STT 无时间戳降级为 VAD；
2. `capabilities.aligned_transcript` 是否正确为 `word`；
3. backchannel boundary；
4. 自建守卫白名单；
5. AEC 是否把助手声音和用户附和混在一起。

#### “被打断后又继续说旧内容”

检查：

1. generation_id 是否先递增；
2. speech handle 是否清空；
3. CosyVoice 连接是否真正关闭；
4. token/audio 帧是否比对 fence；
5. 客户端是否丢弃旧 generation 事件；
6. 工具结果是否检查 tool_epoch。

---

## 29. 实施顺序：编码模型不得改变

实现者必须按以下 PR/提交顺序工作，每一步可运行、可测试：

### PR 1：脚手架与控制 API

- 仓库结构；
- 配置和 `.env.example`；
- LiveKit token endpoint；
- React 房间连接；
- CI 和锁文件。

### PR 2：FunASR 插件

- 协议模型；
- WebSocket 生命周期；
- PCM 重采样/聚合；
- interim/final/word timestamps；
- mock server 和测试。

### PR 3：CosyVoice 插件

- WebSocket 生命周期；
- 连接池；
- 流式 PCM；
- 字级对齐；
- 取消丢连接；
- mock server 和测试。

### PR 4：DeepSeek 与基本语音链路

- DeepSeek 非思考流；
- 系统提示；
- 中文分段器；
- ASR -> LLM -> TTS；
- 端到端 smoke。

### PR 5：状态机、Fence、实际已听文本

- actor/状态机；
- 原子取消；
- generation/tool epoch；
- heard text；
- 连续打断测试。

### PR 6：话轮与附和

- Silero；
- Audio Turn Detector；
- Adaptive Interruption/中文守卫；
- false interruption；
- 中文音频集。

### PR 7：快慢路径和工具

- DeepSeek Pro；
- TaskManager；
- bridge phrase；
- 工具取消和过期；
- 结果口语压缩。

### PR 8：可观测性、负载和部署

- metrics/tracing/logging；
- dashboards/alerts；
- Docker；
- Agent Cloud 或自建部署；
- 负载与 chaos。

### PR 9：韵律自适应

- CPU 声学特征；
- 用户显式语速控制；
- 风格平滑；
- 隐私检查。

任何 PR 不得通过删除测试或放宽硬指标来“修复”失败。

---

## 30. 交给 GPT、Grok 或其他编码 Agent 的实施指令

将本节和整份文档一起提供给编码模型。以下指令是规范的一部分：

```text
你是一名资深实时语音系统工程师。你必须依据随附的
《中文全双工级联语音 Agent：可实施架构与工程设计规范》
创建一个完整、可运行、可测试、可部署的 monorepo。

最高优先级规则：
1. 规范中的 MUST、MUST NOT、状态机、不变量、接口和验收测试不可改写。
2. 固定使用 FunASR Realtime、DeepSeek、CosyVoice 和 LiveKit Agents；
   不得擅自替换模型供应商或改成传统“等整句后再处理”的实现。
3. Python 固定 3.12；`livekit-agents`、`livekit-plugins-openai`、`livekit-plugins-silero` 固定 1.6.5；`openai` 固定在 `>=2.36,<3`。
4. 不得在关键路径留下 TODO、pass、mock-only、伪代码或未实现异常。
5. 不得把供应商密钥放入前端；必须生成 .env.example。
6. 所有异步结果必须经过 GenerationFence；旧音频和旧工具结果误播必须为零。
7. 助手说话时麦克风、VAD 和 FunASR 必须持续运行。
8. 必须实现 CosyVoice 字级对齐和 HeardTextTracker；历史只保存实际已听文本。
9. 用户打断必须同时取消播放、LLM、TTS 和可取消工具；被取消的
   CosyVoice WebSocket 必须关闭并从池中剔除。
10. 必须实现本地协议 mock server，使单元/集成测试不依赖真实 API。
11. 必须生成 uv.lock、pnpm-lock.yaml、Dockerfile、Makefile、README、CI。
12. 必须运行并修复 lint、type check、unit、integration 和离线 E2E。
13. 不得通过降低测试断言、延长规范中的 SLO 或删除用例来让测试通过。
14. 第三方公开 API 若与记忆冲突，以固定版本的官方文档和安装包源码为准；
    保持本文规定的协议语义，并在 README 的“实现偏差”中精确记录必要差异。
15. 未提供真实密钥时，也必须完成全部代码和 mock 测试；真实 provider smoke
    测试应自动 skip 并明确提示缺少哪个环境变量，不得伪造成功。

工作方式：
A. 先读取完整规范，建立 requirements traceability matrix：
   每项 MUST -> 对应代码文件 -> 对应测试。
B. 按规范第 29 章的 PR 顺序实现。
C. 每完成一步，运行相关测试并修复后再继续。
D. 最终运行：
   uv run ruff check .
   uv run mypy services --strict
   uv run pytest
   pnpm --dir apps/web lint
   pnpm --dir apps/web test --run
   uv run python scripts/run_e2e.py --profile offline
E. 输出最终目录树、启动命令、测试结果、尚未运行的真实 provider 测试及原因。
F. 不要只输出代码片段；要创建全部文件。
G. 不要重新询问规范已经给出的选择。只有密钥、域名和业务工具定义属于外部输入。

完成定义：
- 所有离线测试通过；
- 没有关键路径 TODO/pass；
- 前端能连接 LiveKit；
- mock 模式能完整跑通 ASR -> LLM -> TTS；
- 连续 100 次模拟打断没有旧 generation 输出；
- 连续 100 次工具条件修改没有旧 tool epoch 输出；
- README 能让新开发者从空环境启动；
- 真实密钥存在时，provider_smoke_test 能验证 FunASR final/时间戳、
  DeepSeek 流式 content、CosyVoice PCM/字级时间戳。
```

### 30.1 编码 Agent 必须生成的追踪矩阵

示例：

| 规范要求 | 实现文件 | 测试文件 | 状态 |
|---|---|---|---|
| 旧 generation 音频不得播放 | `generation_fence.py`, `cosyvoice_tts.py` | `test_generation_fence.py`, `test_cancel_tts.py` | PASS |
| 助手历史只含实际已听文本 | `heard_text_tracker.py` | `test_heard_text_tracker.py` | PASS |
| FunASR 字级时间戳 | `funasr_stt.py` | `test_funasr_protocol.py` | PASS |
| CosyVoice 取消后丢连接 | `cosyvoice_tts.py` | `test_cosyvoice_pool.py` | PASS |

追踪矩阵缺失即视为交付不完整。

---

## 31. 推荐的首轮参数与调优顺序

首轮上线固定使用：

```yaml
vad:
  min_speech_duration: 0.05
  min_silence_duration: 0.30
  prefix_padding_duration: 0.30

turn_detector:
  version: v1

endpointing:
  mode: dynamic
  min_delay: 0.30
  max_delay: 2.20
  alpha: 0.85

interruption:
  mode: adaptive
  min_duration: 0.25
  false_interruption_timeout: 1.20
  backchannel_boundary: [0.50, 1.80]

funasr:
  semantic_punctuation_enabled: false
  max_sentence_silence_ms: 650
  chunk_ms: 80
  heartbeat: true

preemptive_generation:
  enabled: true
  preemptive_tts: false

cosyvoice:
  sample_rate: 24000
  rate: 1.0
  pitch: 1.0
  word_timestamp_enabled: true
```

调优必须一次只改一类参数：

1. 先确保 AEC 正常；
2. 再调 VAD；
3. 再调 endpointing；
4. 再调 interruption；
5. 再调分段器首段长度；
6. 最后才调 LLM/TTS 风格。

每次修改用相同固定录音集对比，不允许凭几次主观聊天决定。

---

## 32. 关键设计决策记录

### ADR-001：为什么使用 LiveKit 而不是自写 WebSocket 音频

自写 WebSocket 仍需处理 AEC、Opus、jitter、重连、设备、播放打断和 RTC 网络问题。LiveKit 把媒体层与 Agent 管线连接起来，减少非核心工作。

### ADR-002：为什么 FunASR 的 sentence_end 不能直接当用户话轮结束

ASR 分句与对话话权不是同一问题。用户可能在一个短暂停顿后继续补充；最终提交由 Audio Turn Detector + endpointing 决定。

### ADR-003：为什么先关闭 preemptive TTS

预生成可以隐藏 LLM 延迟，但在话轮完成前播放会增加抢话风险。第一版只预生成文本，等正式提交后再播放。

### ADR-004：为什么取消 CosyVoice 时直接关闭连接

正确停止旧音频比连接复用更重要。没有可靠取消确认时，关闭连接是最明确的隔离边界。

### ADR-005：为什么保存实际已听文本

生成完成不等于用户听到。被打断后保存完整回答会污染上下文，导致下一轮助手引用用户从未听过的信息。

### ADR-006：为什么 DeepSeek 分快慢两路

语音交互需要迅速反馈，复杂任务需要深度推理。让同一请求同时承担两者会造成长时间沉默或低质量回答。

### ADR-007：为什么不使用数据库做实时状态源

数据库 RTT 和故障会直接进入语音延迟。会话关键状态由单 Agent actor 持有，数据库仅异步持久化。

---

## 33. 官方参考资料

以下链接用于实现者核对固定版本的公开 API。阅读顺序按重要性排列：

### LiveKit

- LiveKit Agents Python：<https://pypi.org/project/livekit-agents/>
- Audio Turn Detector：<https://docs.livekit.io/agents/logic/turns/turn-detector/>
- Turn Handling Options：<https://docs.livekit.io/reference/agents/turn-handling-options/>
- Adaptive Interruption：<https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/>
- Silero VAD：<https://docs.livekit.io/agents/logic/turns/vad/>
- Python STT API：<https://docs.livekit.io/reference/python/livekit/agents/stt/>
- Python TTS API：<https://docs.livekit.io/reference/python/livekit/agents/tts/>
- OpenAI-compatible LLM plugin：<https://docs.livekit.io/reference/python/livekit/plugins/openai/>
- Agent Deployment：<https://docs.livekit.io/deploy/agents/>
- Self-hosting：<https://docs.livekit.io/transport/self-hosting/>

### 阿里云百炼 / Model Studio

- FunASR Client Events：<https://help.aliyun.com/en/model-studio/fun-asr-client-events>
- FunASR Server Events：<https://help.aliyun.com/en/model-studio/fun-asr-server-events>
- CosyVoice Client Events：<https://help.aliyun.com/en/model-studio/cosyvoice-client-events>
- CosyVoice Server Events：<https://help.aliyun.com/en/model-studio/cosyvoice-server-events>
- CosyVoice WebSocket API：<https://help.aliyun.com/zh/model-studio/cosyvoice-websocket-api>
- CosyVoice 音色列表：<https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list>

### DeepSeek

- API Quick Start：<https://api-docs.deepseek.com/>
- Chat Completion：<https://api-docs.deepseek.com/api/create-chat-completion/>
- Thinking Mode：<https://api-docs.deepseek.com/guides/thinking_mode>
- Tool Calls：<https://api-docs.deepseek.com/guides/tool_calls>

---

## 34. 最终验收结论模板

上线评审必须填写，不能只写“体验不错”：

```markdown
# Voice Agent Release Acceptance

版本：
日期：
部署档案：livekit_cloud / cn_self_hosted
测试城市与网络：
设备矩阵：

## Provider Smoke
- FunASR final + timestamps: PASS/FAIL
- DeepSeek stream content: PASS/FAIL
- CosyVoice PCM + timestamps: PASS/FAIL

## Latency
- Speech-to-speech P50:
- Speech-to-speech P95:
- Barge-in stop P50:
- Barge-in stop P95:

## Conversation Quality
- False early turn commit:
- Backchannel false stop:
- Background noise false interrupt:
- Explicit interruption success:

## Correctness
- Stale generation playback: 0 / NOT ZERO
- Stale tool result playback: 0 / NOT ZERO
- Heard-text mismatch count:

## Reliability
- 30-minute load test:
- Chaos suite:
- Memory/task leaks:

## Decision
APPROVE / REJECT

拒绝原因或风险接受人：
```

只要 `Stale generation playback` 或 `Stale tool result playback` 非零，结论必须是 `REJECT`。

---

# 文档结束
