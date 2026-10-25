# ASR 救援 sidecar（SenseVoice）重建与验证

实时 ASR 主链是云端 FunASR。某一段语音实时识别为空时，agent 会把这段 PCM 发给本 sidecar 做一次离线转写，调用方是 `services/agent/src/providers/sensevoice.py`。sidecar 不在 compose 里，线上用 `docker run` 单独启动。本文说明如何在仓库里原样重建它，以及 2026-09-26 的验证结论（P1-02）。

## 组成

| 部分 | 仓库位置 | 与线上 `memoria-sensevoice-asr:20260901-pin-language` 的关系 |
|---|---|---|
| 服务脚本 | `scripts/run_sensevoice_asr.py` | sha256 `4935a952…` 与线上容器内一致 |
| 镜像 | `infra/sensevoice-asr/Dockerfile` | 基础镜像按 digest 锁定为 `python:3.11-slim@sha256:1042b614…`（4 层与线上逐层一致，Python 3.11.16） |
| 依赖 | `infra/sensevoice-asr/requirements.txt` | 线上容器 `pip freeze` 的 17 个包，带哈希锁定（`--require-hashes`） |
| 模型 | `infra/sensevoice-asr/models.sha256` | 只入库校验值；模型约 240 MB，运行时只读挂载，不打进镜像 |

模型四个文件（`model.int8.onnx`、`tokens.txt`、`LICENSE`、`README.md`）为 sherpa-onnx 转换的 SenseVoice-small int8 版本；按文件大小推定来自 sherpa-onnx 发布包 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`，但未逐字节核对上游。权威来源是线上主机目录 `/opt/memoria/sidecars/sensevoice-asr/models`。无论从哪里取得，都必须先通过校验。

## 重建

在仓库根目录执行。`.dockerignore` 不排除 `outputs/`，所以建议用只含所需文件的临时目录作构建上下文，避免把数 GB 发布制品发给 Docker：

```bash
docker build --platform linux/amd64 -f infra/sensevoice-asr/Dockerfile -t memoria-sensevoice-asr:<tag> .
```

准备模型并校验：

```bash
(cd <模型目录> && shasum -a 256 -c <仓库>/infra/sensevoice-asr/models.sha256)
```

## 运行（与线上一致的参数）

```bash
docker run -d --name memoria-sensevoice-asr --network memoria_default \
  --restart unless-stopped --memory 1g --cpus 2 \
  -v /opt/memoria/sidecars/sensevoice-asr/models:/models/sensevoice:ro \
  memoria-sensevoice-asr:<tag>
```

agent 通过 `SENSEVOICE_URL=http://memoria-sensevoice-asr:8001/transcribe` 调用，超时 `SENSEVOICE_TIMEOUT_S=2.5`。

## 评测

`scripts/evaluate_sensevoice_rescue.py` 做四件事：
- 把 16 kHz 单声道录音按能量切成语句，每句以原样、降 20 dB、8 倍削波三种形态各发一次；
- 另发静音和噪声探针；
- 做一轮并发请求；
- 报告非空率、外文字符（语种识别失效的特征）、降质后与原文的相似度，以及相对 2.5 s 预算的时延。

默认不输出转写文本，因为录音是私人语音。

```bash
uv run python scripts/evaluate_sensevoice_rescue.py --url http://127.0.0.1:18001 outputs/acceptance/*.wav
```

## 2026-09-26 验证结论

- **可重建**：重建镜像的依赖、Python 版本、脚本与线上逐一一致。4 段合成中文语音（1.5–16.6 s）在本地重建的 amd64 镜像、arm64 镜像和线上实例上转写逐字相同。
- **识别稳健**：用 12 段真机上行录音切出 69 句（0.5–30 s）评测：
  - 无外文输出；
  - 降 20 dB 后与原文相似度 0.971，8 倍削波后 0.984；
  - 4 路并发下 16 句与串行结果完全一致。
  - 本机 arm64 无时延参考价值，时延以线上为准。
- **幻觉（已在代码中修复，随下次 agent 发布生效）**：静音或任意强度的噪声都返回「我。」。原先 `SENSEVOICE_MIN_TEXT_CHARS=2` 按原始长度计数，这两个字符恰好过线，噪声段一旦触发救援就会变成一轮用户输入。现在只计文字字符（`SenseVoiceRescueConfig.accepts_text`），标点和空白不计。
- **空闲后首请求超时（线上问题，未改）**：sidecar 空闲约 7 小时后，1.5 s 音频的首次解码用了 12.7 s，紧接着同一段只要 0.24 s。原因是主机内存紧张时，模型所在的匿名内存被换出：容器 VmSwap 约 450 MB，RSS 约 355 MB；主机 swap 已用 1.1/1.9 GB，swappiness 60。首请求要从磁盘换回模型，远超 2.5 s 预算，所以空闲后的第一次救援必然失败。
- **长语段超预算（已在代码中修复，随下次 agent 发布生效）**：线上解码约 0.09–0.16 s 每秒音频，再加传输开销。16.6 s 音频整体耗时 3.4 s；历史日志里 27–30 s 的语段仅解码就要 2.5–2.8 s。2026-09-29 真机两次复现（`httpx.ReadTimeout`，`pcm_ms=30000`）。现在已有非空 final 时只送未覆盖的尾段（`20260929-voice-core-refactor-v1` 已上线），默认上限改为 12 s（`SENSEVOICE_MAX_AUDIO_S=12`），整段都没有 final 时只救末尾 12 s。

## 排查没有 partial 的尾超时

先检查同一会话、同一 `stream_epoch` 的 `media_asr_boundary`，不要只凭 `asr_empty_class=empty+vendor_silent` 判断厂商故障。这个分类没有证明当前回合送入过音频，旧任务的 RMS 也不是当前回合的输入证据。

- `result=duplicate` 且 `audio_admitted_watermark <= previous_finalized_watermark` 表示从上次 finalize 以来没有新增的已送音频。结合 `listening_capture_hold` 和连续冻结的水位，可以区分收麦后的设备 VAD 边沿与真正的 ASR 空结果。
- 水位有推进时，再检查 `provider_pcm_observed_samples`、`provider_pcm_rms`、`provider_pcm_peak_abs` 和 `provider_pcm_clipping_detected`。短噪声与削波不能靠延长尾超时修复。
- 收麦期间的 VAD 不应创建新的回合或尾超时，也不能删除收麦前已经接收的真实输入。点屏后的 `listening.resume` 恢复收音，恢复后的 VAD 仍须能正常定端点。

2026-10-25 排查的 37 次尾超时中，33 次发生在收麦后，没有新增的已送音频。其余 4 次有音频，另按采集质量排查。该历史结论不代表其他 `empty+vendor_silent` 都属于同一原因。

本地回归入口使用假 provider，不调用外网或播放音频：

```bash
uv run --frozen --extra dev pytest --no-cov services/agent/tests/unit/test_listening_capture_window.py
```

## 待授权的线上调整（建议，未执行）

1. **禁止 sidecar 使用 swap**：`infra/sensevoice-asr/compose.yaml` 已提交 `mem_limit=1536m`、`memswap_limit=1536m`（两值相等即不使用 swap），并带 `/healthz` healthcheck。本地实测峰值 RSS 约 570 MB，线上 RSS 与 swap 合计约 805 MB，1.5 GB 留有余量。**未在服务器执行**，重建容器要单独授权。
2. **保活探针**：`scripts/sensevoice_keepalive.py` 每次发 1 s 合成静音到 `/transcribe`，只丢弃返回文本；配套 `infra/sensevoice-asr/memoria-sensevoice-keepalive.{service,timer}` 每 15 分钟跑一次。**未安装、未启用**，安装到主机要单独授权。探针音频是程序生成的静音，不是任何录音。
3. ~~把救援音频上限降到约 12 s~~：已改为代码默认值（见上），无需再改 env。
4. **随下次 agent 发布带上「我。」修复**。

调整后按上面的评测脚本在线上用合成语音复测时延（不要上传真实录音），并在 TODOLIST P1-02 记录结果。
