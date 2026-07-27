# 中文全双工级联语音 Agent

完整 monorepo，实现规范见 `full_duplex_voice_agent_architecture_zh.md`。

**代码默认选型**：自建 LiveKit Server `1.13.3` · LiveKit Agents `1.6.5` · FunASR Realtime · 百炼 Qwen · 豆包 Seed-TTS 2.0 双向流式 · Python 3.12。当前线上 release 与候选发布状态以 `HANDOFF.md` 为准；默认发布不依赖任何可选 LLM 覆盖配置。

## 生产交付

- H5：<https://122.51.108.140:8443/>
- Control API：<https://122.51.108.140:8443/memoria-api/>
- 当前 runtime release：`20260721-224804`
- 当前 H5 release：`20260721-224804`
- 最新发布记录：`docs/releases/20260721-224804.md`；历史证据保留在 `docs/releases/`
- TLS：公网 IP `122.51.108.140` 使用 Let's Encrypt 短期证书；当前公网入口为 8443，443 继续由既有 WMS 使用。任何 Nginx reload 前都必须先通过 `nginx -t`。

当前线上 H5 使用用户名/密码稳定账号和短期 LiveKit participant token；旧匿名身份仍可在注册时原地升级。消息、个人资料、偏好、会话控制和 readiness evidence 按用户隔离；PII 在持久化或发送 Provider 上下文前统一脱敏。浏览器 bundle 不包含永久凭据。

> 生产状态边界：P0.5、P1-P6 工程能力底座随 `20260720-140053` 部署，当前 runtime/H5 为 `20260721-224804`；“过滤明显旁人（实验）”默认开启，只拒绝明确 guest，ambiguous 为避免误静音主人可聊天但无主人历史或私人权限。正式声纹仍为 shadow-only，复刻声音尚未通过授权真人盲测。同机 PostgreSQL/MinIO 没有异地副本/KMS/PITR，不能宣传为已完成规模化声纹验收或“永不丢失”。

P0.5～P6 工程切片已完成并部署；声纹模型使用独立 `speaker-model` 容器承载固定 CAM++ ONNX 版本，Control API readiness 会校验模型健康与版本。CAM++ 不提供 anti-spoof，因此当前只允许 shadow/`uncertain` 结果，不能把模型冒烟当作生产主人识别。

## 快速开始

### 前置

- Python **3.12.x**
- [uv](https://github.com/astral-sh/uv)
- Node 20+ / npm
- （可选）LiveKit CLI、Docker

### 安装

```bash
cp .env.example .env
# 无真实密钥时：
echo 'OFFLINE_MOCK=true' >> .env

uv sync --all-extras
npm --prefix apps/h5 ci
```

### 开发（三终端）

```bash
# 1. 控制 API
uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload

# 2. Agent worker（需要 LiveKit 密钥）
uv run python -m services.agent.src.main dev

# 3. H5
npm --prefix apps/h5 run dev -- --host 0.0.0.0
```

或使用 `make dev-api` / `make dev-agent` / `make dev-h5`。当前交付客户端为 H5 与微信小程序；
Web 端只保留 `apps/h5`，legacy Web 与原生 iOS 客户端源码均已移除。

### H5

新的移动 Web 客户端位于 `apps/h5`，包含五个可选陪伴伙伴、动态情绪表情、实时语音、每日回顾和个人资料：

```bash
npm --prefix apps/h5 ci
npm --prefix apps/h5 run dev
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
```

本地默认使用 `/memoria-h5/` base path；生产 Control API 通过同源 `/memoria-api` 访问，永久密钥不会进入浏览器 bundle。H5 通过 `/v1/auth/me` 恢复稳定账号身份；只有服务端返回 401/403 才清理失效身份，临时网络故障不会切换用户数据归属。

当前本地交付已通过 H5 232 项测试、production build 和移动端浏览器回归；注册后选角、
表情与设计音色试听、三段声纹登记、匿名注册原地升级、跨账号 Profile 隔离、默认
“过滤明显旁人（实验）”、主人历史 fail-closed、无横向溢出和 console 0 warning/error
均已验收。声纹登记仍为 shadow-only，不代表已启用强身份认证。

会话只有收到当前 Agent 在 `voice-agent.ui` topic 发布的显式 `assistant_state: ready` 后才进入可用态；LiveKit transport 已连接但 45 秒内未收到该事件时，H5 会断开并恢复为可重试状态。Agent 在音频输出与 UI publisher 就绪后先发布并等待 `ready`，随后才生成首次欢迎语，避免欢迎语先于客户端可用态。

### 微信小程序

微信小程序位于 `apps/miniprogram`，媒体面通过
`RecorderManager → PCM/WSS → MiniProgramMediaGateway → LiveKit Agent` 接入同一业务与智能链路。
共享媒体契约位于 `packages/contracts/miniprogram-media.json`：

```bash
npm --prefix apps/miniprogram test
find apps/miniprogram -type f -name '*.js' ! -path '*/node_modules/*' -print0 \
  | xargs -0 -n1 node --check
```

### 离线质量门（无需供应商密钥）

```bash
uv run ruff check .
uv run mypy services --strict
uv run pytest
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
npm --prefix apps/miniprogram test
uv run python scripts/run_e2e.py --profile offline
uv run python scripts/provider_smoke_test.py   # 缺密钥时 SKIP 并打印变量名
```

### 本地 Docker 开发

```bash
docker compose up -d postgres redis
# 仅在本地开发机执行；当前 Compose 只构建后端
docker compose build control-api agent
# H5 使用唯一锁文件单独生成静态产物
npm --prefix apps/h5 ci
npm --prefix apps/h5 run build
```

生产服务器约 3.6 GiB 内存，不在服务器构建镜像。正式发布必须在本机生成并校验 `linux/amd64` 镜像和 H5 静态产物，上传后由服务器执行 `docker load` 与 `docker compose up --no-build`；完整步骤见 `docs/production-deployment.md`。

本地自建 LiveKit：

```bash
# .env 中使用 ws://localhost:7880、devkey/devsecret，并选择 cn_self_hosted
docker compose --profile self-hosted up -d redis livekit
```

`devkey/devsecret` 仅用于本机开发。生产环境必须生成独立随机 key/secret，并通过 secret manager 注入。

### LiveKit 为什么开源还需要 key

LiveKit Server 是开源的，但任何客户端加入房间都必须提交服务端签发的 JWT，因此部署方始终需要一对 API key/secret：

- **LiveKit Cloud**：在 Cloud 控制台创建 Project，Project Settings / Keys 中生成；
- **自建 LiveKit**：不需要向 LiveKit 申请，直接在部署配置的 `keys:` 中自行定义，或用 LiveKit 配置生成器生成。

API secret 永远只放控制 API 与 Agent 服务端；H5 只接收短期 participant token。

## 仓库结构

见规范第 4 章。关键路径：

| 路径 | 说明 |
|---|---|
| `services/agent/src/orchestration/` | 状态机、Fence、HeardText、分段、打断 |
| `services/agent/src/providers/` | FunASR / 豆包 TTS / OpenAI-compatible LLM 协议与适配；CosyVoice 仅保留历史兼容代码 |
| `services/control_api/` | 账号、会话、档案、人格、声纹、声音、导出/删除与 health |
| `apps/h5/` | 面向移动浏览器的 Memoria 三页产品 |
| `apps/miniprogram/` | 微信小程序 UI、媒体会话、PCM 播放器与体验版配置 |
| `docs/requirements_traceability_matrix.md` | MUST → 代码 → 测试 |

## 部署档案

- `DEPLOYMENT_PROFILE=livekit_cloud`：Adaptive Interruption + Turn Detector `v1`
- `DEPLOYMENT_PROFILE=cn_self_hosted`：Turn Detector `v1-mini` + `ChineseInterruptionGuard`

H5 的生产 Compose、自建 LiveKit、IP TLS、Nginx 路由、Provider 门禁、备份和回滚步骤见
`docs/production-deployment.md`。`https://122.51.108.140:8443/` 直接交付 H5；同一端口还通过
Nginx stream 复用为 LiveKit RTC/TCP 回退，`/wms/` 保留给既有 WMS。静态资源与 API 继续
使用 `/memoria-h5/`、`/memoria-api/` 独立路径。当前生产证据见
`docs/releases/20260727-170628.md`；历史迁移、热修与回滚证据保留在 `docs/releases/`。

## 实现偏差

1. **工作区布局**：规范示例为 `voice-agent/` 根目录；本仓库以 monorepo 根目录直接承载同等树结构（`apps/`、`services/`、`packages/`、`infra/`、`scripts/`）。
2. **LiveKit STT/TTS 基类**：FunASR/豆包提供完整协议会话与连接池，并在 `agent.py` 中挂入 `AgentSession`；若固定版本 `livekit-agents==1.6.5` 的 `stt.STT`/`tts.TTS` 抽象字段与骨架略有差异，以该版本公开 API 为准，协议语义保持规范第 12/15 章。
3. **规模化设备门槛**：LiveKit、FunASR、Qwen 与豆包仍须以每次 release 的生产实网 smoke 为准。200 条真实设备录音、AEC 矩阵和第 21 章 SLO 属于后续规模化门禁，不作为当前 H5 成品交付的阻塞项。
4. **预生成桥接 PCM 缓存**：桥接语文本在 `prompts.BRIDGE_PHRASES`；二进制 PCM 缓存可在生产预热任务中填充，离线路径使用 mock TTS。

## 故障排查

| 现象 | 顺序 |
|---|---|
| 反应慢 | endpointing → LLM TTFT → phrase wait → TTS TTFB → playback |
| 总抢话 | VAD min silence ≥0.25；Turn Detector；勿把 FunASR sentence_end 当对话 EOT |
| 嗯一声就停 | Adaptive Interruption / backchannel / aligned_transcript=word / AEC |
| 打断后仍播旧内容 | generation_id 先递增；豆包 session 取消并丢连接；fence 比对；tool_epoch |

## 验收

当前本地工程质量门为 Ruff、mypy strict、全量 pytest、Doubao 离线 E2E、Control API/Agent 镜像构建；本 release 的 `uv run pytest -q` 退出码为 0（`730 tests collected`），H5 为 `138/138`。最近一次覆盖率证据 `82.05%` 仍低于既有 85% 门槛，必须保留 waiver。完整追踪矩阵见 `docs/requirements_traceability_matrix.md`，终身记忆架构与阶段状态见 `docs/memory-persona-architecture-v1.md` 和 `docs/memory-persona-implementation-plan.md`。

只要出现 **旧 generation 误播** 或 **旧 tool epoch 误播**，发布结论必须是 **REJECT**。
