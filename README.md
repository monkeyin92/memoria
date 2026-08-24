# Memoria

Memoria 是以 ESP32-S3 为第一等语音终端的中文陪伴与长期档案系统。当前唯一交互权威链是：

```text
ESP32-S3 -> Go Media Edge -> Python Voice Core / Agent
                                      |
                                      +-> Control API -> PostgreSQL / Redis / MinIO
H5 / 微信小程序 -------------------------> 控制面、档案与设备管理
```

小程序不是实时媒体终端：不采集麦克风、不播放实时 TTS、不建立媒体 WSS、不加入 LiveKit 房间。H5 保留浏览器实时语音能力；ESP32 是机器人产品的实时语音入口。未完成真实 AEC、双讲和连续轮次验收前，不得宣称全双工。

## 文档与权威

仓库长期只保留三份文档：

- `README.md`：产品、架构、开发、协议和固件入口。
- `AGENTS.md`：长期协作规则、产品约束和工程纪律。
- `HANDOFF.md`：当前线上状态、发布/回滚/备份和待验收事项。

机器可执行事实放在 schema、proto、配置、锁文件和测试中，不另建说明文档；历史发布过程不在仓库累积。

## 技术栈与目录

- Python 3.12、uv、FastAPI、LiveKit Agents、FunASR、百炼兼容 LLM、豆包 Seed-TTS。
- Go Media Edge：设备 WSS、generation fence、gRPC Voice Core bridge、Pion WebRTC。
- PostgreSQL 17 + pgvector、Redis、MinIO。
- H5：`apps/h5`；微信小程序：`apps/miniprogram`；ESP32 overlay：`firmware/esp32`。
- 共享契约：`packages/contracts`；Media Edge/Voice Core proto：`packages/proto`。
- 发布、验收和运维脚本：`scripts`；生产 Compose/Nginx/模型 registry：`infra`。

共享 JSON/schema/proto 是兼容性契约，不是运行时配置。修改契约时必须在同一提交中修改全部生产者、消费者、生成物和跨语言测试。

## 快速开始

前置：Python 3.12、uv、Node 20+、npm；容器开发另需 Docker。

```bash
cp .env.example .env
echo 'OFFLINE_MOCK=true' >> .env  # 没有供应商密钥时
uv sync --all-extras
npm --prefix apps/h5 ci
```

三个终端分别启动：

```bash
uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload
uv run python -m services.agent.src.main dev
npm --prefix apps/h5 run dev -- --host 0.0.0.0
```

本地数据层与自建 LiveKit：

```bash
docker compose up -d postgres redis
docker compose --profile self-hosted up -d redis livekit
```

`devkey/devsecret` 只允许本机开发。生产 API secret 永远只进入 Control API/Agent 的 root-only 环境文件；H5 只接收短期 participant token。

## 质量门

提交前按改动范围运行，发布前运行完整门禁：

```bash
uv run ruff check .
uv run python scripts/check_module_budget.py check
uv run mypy services --strict
uv run pytest
npm --prefix apps/h5 test
npm --prefix apps/h5 run build
npm --prefix apps/miniprogram test
uv run python scripts/run_e2e.py --profile offline
uv run python scripts/provider_smoke_test.py
```

Media Edge：

```bash
./scripts/generate_media_go_proto.sh
cd services/media_edge
go test ./...
go test -race ./...
```

多主体契约：

```bash
uv run python scripts/generate_multi_subject_contracts.py --check
node --test apps/miniprogram/tests/*.test.js
```

只要出现旧 generation/tool epoch 误播、主人数据越权、危机回复被普通提示覆盖或危机事件未进入监护通知 outbox，发布结论必须是 REJECT。

## 产品与数据边界

- 账号身份只证明客户端持有账号凭据，不证明麦克风前的人是账户主人。
- `Speaker Classification` 是 `owner / guest / uncertain` 的概率结论，不是法律身份认证。
- 明确 guest 可以被实验性 Target Speaker Focus 拒绝；ambiguous 不可获得主人历史、私人记忆、工具或敏感权限。
- 只有 Agent 权威终稿中的 `history_eligible=true` 可以进入主人长期历史，且资格必须绑定原话轮 generation fence。
- Evidence Event 是不可变观察；Memory Claim 具有 `candidate / confirmed / disputed / retracted` 生命周期；投影可重建，不能替代原始证据。
- Speaker Profile 用于判断谁在说话；Voice Profile 用于授权合成声音，两者不得混称。
- Companion Mode、Self Preview、Archive Mode、Legacy Mode 是不同权限边界；模拟输出不得反哺主人证据。
- 浏览器 bundle、日志、发布清单和仓库不得包含永久凭据、设备私钥或供应商 secret。

学生账号能力以 `services/control_api/app/account_gate.py` 为唯一规则表。任何端点必须在读取私有资源或产生写副作用前完成 capability 检查；未声明能力默认拒绝。

## UI 产品约束

新账号先选择星澜、桃喜、绵绵、阿序或玄墨，再完成自然、轻声、带笑、认真四种说话状态的可撤销声纹登记。当前登记只生成 shadow 档案，不得宣传为已启用主人认证，也不得与声音克隆混为一谈。

称呼仅在注册时设置，字段为“怎么称呼你？”；“我的/个人信息”不再提供称呼或陪伴方式编辑入口。伙伴音色只通过服务器批准的稳定目录键解析供应商 `voice_id`。

吉祥物的用户情绪只消费权威 `emotion_observation`；助手说话时只消费 Agent 发布且与 `session_id + turn_id + generation_id + tool_epoch` 匹配的 `assistant_expression`。客户端不得从字幕猜表情，回答结束、断线或中断时必须清除。动效需尊重 `prefers-reduced-motion`。

## ESP32 固件

固件以固定 `78/xiaozhi-esp32` upstream 加小型 overlay 维护。锁定版本、commit、ESP-IDF 和传递依赖分别以 `firmware/esp32/upstream.lock` 与 `overlay/files/dependencies.lock` 为准；缓存、工具链和构建产物不提交。

```bash
cd firmware/esp32
./scripts/bootstrap.sh            # 可加 --no-idf-install
./scripts/build.sh --clean
./scripts/check-overlay.sh
./scripts/flash.sh --list
./scripts/flash.sh --port /dev/cu.usbmodemXXXX --monitor
```

默认启用本地唤醒词“梅莫里亚”（`mei mo li ya`）；普通“你好你好”不是唤醒词。短按 BOOT 可启动会话；播放期间 BOOT 是本地物理硬停止权威。只有排查媒体问题时才构建 `./scripts/build.sh --wake-word disabled`。

Mac 进入下载模式：按住 BOOT，轻按 RESET，松开 RESET，再松开 BOOT，然后重试。monitor 使用 `Ctrl+]` 退出。

### 身份分区安全

Flash `0x10000..0x1ffff` 是独立 `memoria_identity` NVS。普通固件更新不得写入该区：优先 app-only 刷写 `0x20000`，整包刷写前后都必须回读身份区并逐字节比较。任何身份摘要变化、Manifest v2 验签失败或设备 SKU 不符都必须停止验收，禁止靠重置身份绕过。

长按 5 秒只重置网络配置，不清除身份分区，也不解除云端绑定。当前没有启用 Secure Boot、Flash Encryption 或 eFuse，量产安全不可由研发身份验收替代。

### 设备媒体协议

固件请求 `supported_protocol_versions: [2, 1]`，首选 v2：

- direct Edge 是 v2-only；direct WSS 不接受 v1。
- v1 只由 legacy livekit_compat Gateway 承接滚动发布或明确回滚。
- the direct edge is v2-only and never accepts a v1 hello.

v2 的 `session_epoch + turn_id + generation_id + tool_epoch` 完整 fence 贯穿 generation、PCM 和 playback 回执。下行 sequence/sample 时钟按 generation 从 0/0 开始；旧 generation 帧必须在连续性检查前丢弃。`playback.ended` 只能在 completion 到达且真实解码/播放队列排空后上报；网络收到帧不是 Actual Heard。

当前板无 AEC reference、无自然双讲、无精确 DAC 采样计数器。回执水位属于保守播放边界；`full_duplex_verified` 只能由真实声学验收产生。

## 模型与第三方来源

DTLN 降噪固定到 `breizhn/DTLN` commit `1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc`，MIT License。运行契约为 16 kHz mono、512-sample block、128-sample shift、每会话独立 recurrent state。机器可读来源和摘要在 `services/agent/models/dtln/provenance.json`，许可证保留在同目录 `LICENSE`。

第三方模型、声音和上游代码必须带固定来源、版本/commit、摘要和许可证；二进制模型不因位于仓库中而变成 Memoria 自有代码。生成物和预览资产若可由权威源重建，不作为长期文档或发布证据保留。

## 排障顺序

- 无响应：设备状态/票据 -> WSS epoch -> VAD -> ASR final -> Agent generation -> 首个 0/0 下行帧 -> playback terminal。
- 说话距离过近：板端输入增益/codec -> 原始 PCM RMS/peak -> DTLN 输入输出 -> VAD 阈值；不要只调云端识别阈值。
- 回答中断后失联：同一 fence 检查 stop epoch、successor cancel、迟到旧帧、first-frame 连续性和 WSS close cause。
- 打断后仍播旧内容：先推进 generation，再取消 provider/session，并在 Edge、设备和投影处比较完整 fence。
- H5 transport 已连但不可用：必须等当前 Agent 的显式 `assistant_state: ready`，不能把 LiveKit connected 当业务 ready。

当前线上镜像、证据层级、发布与剩余真实设备验收见 `HANDOFF.md`。
