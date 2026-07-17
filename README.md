# 中文全双工级联语音 Agent

完整 monorepo，实现规范见 `full_duplex_voice_agent_architecture_zh.md`。

**生产默认选型**：自建 LiveKit Server `1.13.3` · LiveKit Agents `1.6.5` · FunASR Realtime · 百炼 Qwen · CosyVoice Realtime · Python 3.12。默认发布不依赖任何可选 LLM 覆盖配置。

## 生产交付

- H5：<https://aginice.cn:8443/>
- Control API：<https://aginice.cn:8443/memoria-api/>
- 当前正式 release：`20260716-085218`
- 当前基础设施发布记录：`docs/releases/20260716-120146.md`
- TLS：`aginice.cn` 使用 TrustAsia 域名证书（有效至 2026-09-04）；当前公网入口为 8443。443 的 Memoria 路由已就绪，但域名 SNI 在到达 Nginx 前被上游关闭，待备案放行后可直接使用无端口 URL。公网 IP 兼容入口仍使用 Let's Encrypt 短期证书（有效至 2026-07-22）。任何 Nginx reload 前都必须先通过 `nginx -t`。

H5 使用服务端签发的匿名 Bearer 身份和短期 LiveKit participant token。消息、个人资料、偏好、会话控制和 readiness evidence 存入 SQLite，并按用户隔离；PII 在持久化或发送 Provider 上下文前统一脱敏。浏览器 bundle 不包含永久凭据。

## 快速开始

### 前置

- Python **3.12.x**
- [uv](https://github.com/astral-sh/uv)
- Node 20+ / pnpm 9+
- Xcode 16.3+（iOS 客户端；部署目标 iOS 17）
- （可选）LiveKit CLI、Docker

### 安装

```bash
cp .env.example .env
# 无真实密钥时：
echo 'OFFLINE_MOCK=true' >> .env

uv sync --all-extras
pnpm --dir apps/web install
```

### 开发（三终端）

```bash
# 1. 控制 API
uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload

# 2. Agent worker（需要 LiveKit 密钥）
uv run python -m services.agent.src.main dev

# 3. 前端
pnpm --dir apps/web dev --host 0.0.0.0
```

或使用 `make dev-api` / `make dev-agent` / `make dev-web`。

### iOS

打开 `apps/ios/MemoriaVoice.xcodeproj`，选择 `MemoriaVoice` scheme 后运行。App 默认访问模拟器宿主机的 `http://127.0.0.1:8000`，也可在首屏修改控制 API 地址。真机应使用可访问的 HTTPS 地址。

若使用 macOS 27 测试版，需要匹配的 Xcode 27 工具链；Xcode 26.x 虽可做目标级编译，但可能无法解析 scheme 的 iOS Simulator destination。

iOS App 只从控制 API 获取短期 participant token；不得在 App、Info.plist 或构建配置中写入 LLM、DashScope 或 LiveKit API secret。

### H5

新的移动 Web 客户端位于 `apps/h5`，包含动态情绪吉祥物、实时语音、每日回顾和个人资料：

```bash
npm --prefix apps/h5 ci
npm --prefix apps/h5 run dev
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
```

本地默认使用 `/memoria-h5/` base path；生产 Control API 通过同源 `/memoria-api` 访问，永久密钥不会进入浏览器 bundle。H5 会缓存匿名身份，并通过 `/v1/auth/me` 验证：只有服务端返回 401/403 才换发身份，临时网络故障不会导致用户数据被切换到新身份。

当前交付已通过 H5 38 项测试、production build 和 390×720 浏览器回归；其中 `useVoiceSession` 生产边界测试为 18/18。

会话只有收到当前 Agent 在 `voice-agent.ui` topic 发布的显式 `assistant_state: ready` 后才进入可用态；LiveKit transport 已连接但 45 秒内未收到该事件时，H5 会断开并恢复为可重试状态。Agent 在音频输出与 UI publisher 就绪后先发布并等待 `ready`，随后才生成首次欢迎语，避免欢迎语先于客户端可用态。

### 离线质量门（无需供应商密钥）

```bash
uv run ruff check .
uv run mypy services --strict
uv run pytest
pnpm --dir apps/web lint
pnpm --dir apps/web test --run
uv run python scripts/run_e2e.py --profile offline
uv run python scripts/provider_smoke_test.py   # 缺密钥时 SKIP 并打印变量名
```

### Docker

```bash
docker compose up -d postgres redis
# 构建镜像前需已有 uv.lock / pnpm-lock.yaml
docker compose build
```

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

API secret 永远只放控制 API 与 Agent 服务端；Web/iOS 只接收短期 participant token。

## 仓库结构

见规范第 4 章。关键路径：

| 路径 | 说明 |
|---|---|
| `services/agent/src/orchestration/` | 状态机、Fence、HeardText、分段、打断 |
| `services/agent/src/providers/` | FunASR / CosyVoice / OpenAI-compatible LLM 协议与适配 |
| `services/control_api/` | Session token / stop-response / health |
| `apps/web/` | React 19 + LiveKit 客户端 |
| `apps/h5/` | 面向移动浏览器的 Memoria 三页产品 |
| `apps/ios/` | SwiftUI + LiveKit Swift SDK 原生客户端 |
| `docs/requirements_traceability_matrix.md` | MUST → 代码 → 测试 |

## 部署档案

- `DEPLOYMENT_PROFILE=livekit_cloud`：Adaptive Interruption + Turn Detector `v1`
- `DEPLOYMENT_PROFILE=cn_self_hosted`：Turn Detector `v1-mini` + `ChineseInterruptionGuard`

H5 的生产 Compose、自建 LiveKit、域名与公网 IP TLS、Nginx 路由、Provider 门禁、SQLite 备份和回滚步骤见 `docs/production-deployment.md`。`https://aginice.cn:8443/` 直接交付 H5；同一端口还通过 Nginx stream 复用为 LiveKit RTC/TCP 回退。静态资源与 API 继续使用 `/memoria-h5/`、`/memoria-api/` 独立路径。应用发布证据见 `docs/releases/20260716-085218.md`，自建 LiveKit 切换证据见 `docs/releases/20260716-120146.md`。

## 实现偏差

1. **工作区布局**：规范示例为 `voice-agent/` 根目录；本仓库以 monorepo 根目录直接承载同等树结构（`apps/`、`services/`、`packages/`、`infra/`、`scripts/`）。
2. **LiveKit STT/TTS 基类**：FunASR/CosyVoice 提供完整协议会话与连接池，并在 `agent.py` 中挂入 `AgentSession`；若固定版本 `livekit-agents==1.6.5` 的 `stt.STT`/`tts.TTS` 抽象字段与骨架略有差异，以该版本公开 API 为准，协议语义保持规范第 12/15 章。
3. **规模化设备门槛**：LiveKit、FunASR、Qwen、CosyVoice 的生产实网 smoke 已通过。200 条真实设备录音、AEC 矩阵和第 21 章 SLO 属于后续规模化门禁，不作为当前 H5 成品交付的阻塞项。
4. **预生成桥接 PCM 缓存**：桥接语文本在 `prompts.BRIDGE_PHRASES`；二进制 PCM 缓存可在生产预热任务中填充，离线路径使用 mock TTS。

## 故障排查

| 现象 | 顺序 |
|---|---|
| 反应慢 | endpointing → LLM TTFT → phrase wait → TTS TTFB → playback |
| 总抢话 | VAD min silence ≥0.25；Turn Detector；勿把 FunASR sentence_end 当对话 EOT |
| 嗯一声就停 | Adaptive Interruption / backchannel / aligned_transcript=word / AEC |
| 打断后仍播旧内容 | generation_id 先递增；CosyVoice 连接关闭；fence 比对；tool_epoch |

## 验收

当前后端质量门为 Ruff、mypy strict 和 204 项 pytest；H5 质量门为 38 项测试（含 hook 18/18）与 production build。完整追踪矩阵见 `docs/requirements_traceability_matrix.md`。

只要出现 **旧 generation 误播** 或 **旧 tool epoch 误播**，发布结论必须是 **REJECT**。
