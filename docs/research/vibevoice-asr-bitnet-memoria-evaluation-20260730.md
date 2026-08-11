# VibeVoice-ASR-BitNet 用于 Memoria 的可行性与成本评估

> 核验日期：2026-07-30
>
> 目标：评估 `microsoft/VibeVoice-ASR-BitNet` 是否适合替换 Memoria 当前收费的 `fun-asr-realtime` 主 ASR。
>
> 资料边界：Microsoft 官方 Hugging Face 模型卡、官方技术报告、官方 `VibeASR.cpp` 源码与许可证，以及阿里云百炼官方 Fun-ASR 文档；未使用社区跑分或二手文章。
>
> 版本边界：模型仓库提交 `66e78021ab8f5f06133d1ab421ba4d348bda97c9`；运行时源码提交 [`da6991219d83dcc5e13be487ab71cc0cdb1bf226`](https://github.com/microsoft/VibeASR.cpp/tree/da6991219d83dcc5e13be487ab71cc0cdb1bf226)。官方性能数字保持原始测试口径；§2.3 另列 Apple M2 Pro 上两个音频样本的三个本机补充 smoke。未做 Linux 生产机、真实用户、真机声学或并发实测，推论会明确标注。

## 结论

**当前不适合直接替换 Memoria 的主 FunASR Realtime。** 关键障碍不是中文是否“能识别”，而是官方 CPU 运行时目前并非 Memoria 所需的在线流式 ASR：它接收一个已经写完的 WAV 文件，完整读取并完成两路 VAE 编码后，才逐 token 输出文字；所谓 `RTF < 1` 表示“处理一段已经存在的音频所需时间小于音频时长”，不等于边说边识别、低首字延迟或提供可修订 interim。[服务协议与整段文件输入](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L1-L10) [完整加载和 VAE 编码](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L134-L208) [token 输出发生在 prefill 之后](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L231-L318)

这与 Memoria 的核心交互契约直接冲突：当前适配器持续发送 16 kHz PCM、接收 interim/final、保留词级对齐、做重连音频回放和重复终稿去重；播放期间还依赖非 final 文本尽早识别“等一下”等控制语义。[当前 FunASR 流式适配器](../../services/agent/src/providers/funasr_stt.py#L47-L117) [PCM 与 interim/final 映射](../../services/agent/src/providers/funasr_stt.py#L447-L565) [LiveKit 能力声明](../../services/agent/src/providers/funasr_stt.py#L568-L581) [播放期 partial 打断路径](../../services/agent/src/duplex_runtime.py#L3817-L3873)

**它可以进入“离线/影子评测候选”，不应进入主链切换候选。** 合理用途是对用户已结束的话轮做 shadow 转写、离线回归或批量素材转写；如果未来上游提供真正的增量声学流接口、并发服务和受验证的时间戳，再重新评估主链替换。

成本方面也不能只看“模型免费”。当前 `fun-asr-realtime` 华北 2 官方原价是 `0.00033 元/秒`，即 `1.188 元/音频小时`，且官方明确说明不含账户优惠。[阿里云官方价格](https://help.aliyun.com/zh/model-studio/fun-asr-realtime) 在没有当前账单音频小时数、折扣和峰值并发前，不能证明自建更省。Memoria runbook 记录生产机“约 3.6 GiB 内存”，2026-07-23 的发布预检另记录当时“约 4.1 GiB 可用”；本轮 SSH 未能登录，不能把任一数字写成实时现状。[runbook 资源记录](../production-deployment.md#1-本机构建打包并增量上传固定工件) [历史发布预检](../releases/20260723-192611.md#生产只读预检与迁移预演)

## 1. 模型到底是什么

VibeVoice-ASR-BitNet 继承 VibeVoice-ASR 的“语音 tokenizer + 自回归语言模型”架构，但为了 CPU 部署把原始 Qwen2.5-7B 解码器换成 Qwen2.5-1.5B。语音前端包含 acoustic encoder 与 semantic encoder，两者均为 7-stage ConvNeXt，从 24 kHz 音频做 `3200×` 时间降采样到 7.5 Hz；解码器是 28 层 Transformer。[技术报告 §2.1](https://arxiv.org/html/2607.21075v2#S2.SS1) [官方模型配置](https://huggingface.co/microsoft/VibeVoice-ASR-BitNet/blob/main/config.json)

量化不是“整个模型统一 2 bit”，而是异构量化：

| 组件 | 量化方式 | 官方文件大小 | 说明 |
|---|---:|---:|---|
| VAE tokenizer | `I8_S` | 0.65 GB | 权重、激活和中间缓冲区走完整 INT8 数据通路，并配合算子融合 |
| LM decoder | `I2_S` + `Q6_K` | 0.92 GB | 主体权重为 `{-1, 0, +1}` 三值 2-bit；激活逐 token 量化为 INT8；embedding 与 LM head 保留 Q6_K |
| 合计 | 异构 | 1.58 GB | 相对 4.62 GB FP16 基线压缩 2.9 倍 |

来源：[技术报告 §2.2](https://arxiv.org/html/2607.21075v2#S2.SS2)、[模型卡量化与文件表](https://huggingface.co/microsoft/VibeVoice-ASR-BitNet#quantization-strategy)。模型仓库还提供约 10.7 GB 的原始 SafeTensors 供转换，运行预量化 GGUF 不需要把它们一并部署。[模型文件说明](https://huggingface.co/microsoft/VibeVoice-ASR-BitNet#model-files)

## 2. CPU 性能：官方数字与正确口径

### 2.1 官方 RTF

技术报告的主实验平台是 AMD EPYC 7V13：24 核、3.1 GHz、无 SMT、216 GB DDR4、96 MB L3，支持 AVX2/FMA。RTF 定义为“推理时间 / 音频时长”，小于 1 只表示离线处理速度超过音频播放速度。[实验平台与指标](https://arxiv.org/html/2607.21075v2#S3.SS1)

| 音频时长 | 1 线程 | 2 线程 | 3 线程 | 4 线程 | 6 线程 | 8 线程 |
|---:|---:|---:|---:|---:|---:|---:|
| 5 s | 2.11 | 1.22 | 0.89 | 0.76 | 0.60 | 0.54 |
| 10 s | 2.05 | 1.13 | 0.82 | 0.69 | 0.53 | 0.47 |
| 20 s | 1.98 | 1.08 | 0.77 | 0.63 | 0.49 | 0.42 |
| 40 s | 1.96 | 1.05 | 0.76 | 0.61 | 0.47 | 0.41 |

来源：[技术报告 Table 3](https://arxiv.org/html/2607.21075v2#S3.SS2)。官方仓库还给出两组补充数据：Apple M4 16 GB 在 2/3/4 线程下为 `0.68/0.52/0.43`；Intel i7-13700 32 GB 在 2/3/4 线程下为 `0.97/0.78/0.71`。EPYC 与 M4 使用 20 秒音频，i7 使用 10.3 秒片段。[官方 README 性能表](https://github.com/microsoft/VibeASR.cpp#inference-performance)

### 2.2 不能从 RTF 推出的结论

- 不能推出低首字延迟：源码先读取整段 WAV、完成 acoustic/semantic VAE 与 LM prefill，之后才开始 token 输出。
- 不能推出适合播放期打断：以 EPYC 5 秒、3 线程的官方 RTF 0.89 为例，**推论**是音频结束后还需约 4.45 秒完成整段推理；这不包含用户录音本身的 5 秒。20 秒音频对应约 15.4 秒推理。
- 不能推出中文延迟相同：报告的 Whisper.cpp 对比明确使用 20 秒英语语音；持续时长表没有公布中文独立数据。自回归输出 token 数也可能随语言和内容变化。[技术报告 §3.4](https://arxiv.org/html/2607.21075v2#S3.SS4)
- 不能推出廉价 ARM 板都达标：官方 ARM 数据来自 Apple M4，不是树莓派或低功耗云 ARM；x86 数据来自 EPYC 7V13 与 i7-13700。
- 不能推出并发能力：官方没有并发数、吞吐、P95/P99、首 token 延迟或持续压测结果。

因此，官方“3 线程实时”是一个有价值的离线吞吐指标，但不能作为 Memoria 实时交互验收证据。

### 2.3 本机补充实测（不代表生产）

为校准官方口径，本轮在 Apple M2 Pro（12 核、32 GiB）上对官方运行时 HEAD
`da6991219d83dcc5e13be487ab71cc0cdb1bf226` 做了两个最小 smoke：4 线程、greedy、24 kHz
单声道 WAV，音频为 macOS Tingting 合成中文。结果如下：

| 入口 / 样本 | 总推理时间 | RTF | maximum RSS | 文本结果 |
|---:|---:|---:|---:|---|
| `asr_infer` / 7.425 s | 6,885.6 ms | 0.9273 | 3,871,440,896 bytes（约 3.61 GiB） | 可识别，但有轻微错字 |
| `asr_infer` / 2.271 s | 2,744.6 ms | 1.2088 | 2,763,390,976 bytes（约 2.57 GiB） | 漏掉句首“请” |
| 常驻 `asr_stream_server` / 同一 2.271 s | 2,219.5 ms | 0.9775 | 2,765,275,136 bytes（约 2.58 GiB） | token 仍在完整 WAV 处理后才输出 |

一次性 CLI 的模型加载约 0.45–0.50 秒；常驻 server 消除了后续 chunk 的重复模型加载，令同一
2.271 秒样本从 RTF 1.2088 降到 0.9775，但没有改变完整文件输入和整段 VAE/prefill。运行时仍然
等待完整文件，再执行整段推理；因此 7.425 秒样本的 `RTF < 1` 和常驻 server 的 0.9775 都不能
转写为“说话期间已得到 interim”。另一次对
2.271 秒样本使用 `--prompt-format json` 时，只生成终止 token，没有 JSON、说话人或时间戳。
这与源码中“1.5B 默认纯文本、JSON prompt 注释对应 7B”的边界一致，但单一样本只能证明该
路径当前不可直接依赖，不能据此估计所有音频的失败率。[prompt format 源码](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/utils/prompt_builder.h#L155-L180)

以上只是开发机补充 smoke，不代表 Linux 生产、真实用户普通话、AEC/回声/噪声、并发、长时间
稳定性或总体 CER。它的意义是：

- 验证“短音频更容易受固定开销影响，RTF 不一定小于 1”；
- 验证进程 RSS 明显高于 1.58 GB 模型文件大小；
- 验证当前 JSON/时间戳路径不能作为 Memoria 迁移前提；
- 不应把 M2 Pro 的单路结果外推成生产容量或成本结论。

## 3. 内存、磁盘与现有生产机

官方只给出约 2 GB 磁盘需求，没有给出最低 RAM 或峰值 RSS。[官方部署要求](https://github.com/microsoft/VibeASR.cpp#requirements) “1.58 GB 模型”也不等于“进程只占 1.58 GB”。当前 VAE 源码按输入长度预留计算 arena：I8_S 路径为 `n_samples × 10240 bytes + 512 MiB`，且输入按 24 kHz 处理；这是源码中的分配公式，不是本文实测 RSS。[VAE arena 公式](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/vae.cpp#L649-L670)

按该公式计算：

| 单个完整 WAV | 仅 VAE arena 预留 | 还未包含 |
|---:|---:|---|
| 5 s | 约 1.64 GiB | 1.58 GB 模型权重、LM KV cache、音频/特征、程序和系统 |
| 10 s | 约 2.79 GiB | 同上 |
| 20 s | 约 5.08 GiB | 同上 |
| 40 s | 约 9.66 GiB | 同上 |

这说明：

1. 无论采用 runbook 的约 3.6 GiB 总内存描述，还是 2026-07-23 已记录的约 4.1 GiB 当时可用量，都不能把现有多容器生产机当作“再塞一个 1.58 GB 模型”的可信同机升级。本机单进程 smoke 的 maximum RSS 已约 2.57–3.61 GiB，但这只是 macOS 单路测量；它支持“没有可信共机余量”，不等于当前生产 Linux 的精确内存值。本轮 SSH 未能登录，实时总量/可用量保持未核验。
2. 独立试验机建议从 4–8 个现代 CPU 线程、16 GiB RAM 起步，这是根据源码分配和官方 16/32 GB 测试机作出的**工程试验建议**，不是 Microsoft 公布的最低配置。
3. 音频分块越长，VAE arena 近似线性增长；必须在目标硬件上测峰值 RSS、OOM、冷启动和并发，而不能只看 GGUF 文件大小。

## 4. 中文、普通话和中英混说

模型元数据明确列出 7 种语言：英语、中文、法语、意大利语、韩语、葡萄牙语和越南语；技术报告对 MLC 六种语言以及中文 AISHELL4、AliMeeting、Fleurs-zh 做了评测。[模型卡语言列表](https://huggingface.co/microsoft/VibeVoice-ASR-BitNet/blob/main/README.md#vibevoice-asr-bitnet) [技术报告数据集](https://arxiv.org/html/2607.21075v2#S3.SS1)

中文结果应按 CER 看：

| 中文集 | VibeVoice-ASR-BitNet | 报告中的 FunASR | 绝对差 |
|---|---:|---:|---:|
| AISHELL4 | 27.45 | 20.41 | +7.04（更差） |
| AliMeeting | 40.58 | 39.27 | +1.31（更差） |
| Fleurs-zh | 8.35 | 7.00 | +1.35（更差） |

来源：[技术报告 Table 4](https://arxiv.org/html/2607.21075v2#S3.SS3)。报告自己总结 SenseVoice 和 FunASR 在中文集上受益于中文专项训练。[技术报告准确率讨论](https://arxiv.org/html/2607.21075v2#S3.SS3)

这里有两个必须保留的边界：

- 报告脚注中的对手是 `FunASR-Nano`，**不是** Memoria 当前购买的阿里云 `fun-asr-realtime`，因此不能把表格当成两条生产链的直接 A/B。[Table 4 模型脚注](https://arxiv.org/html/2607.21075v2#S3.SS3)
- BitNet 模型卡没有提供中英 code-switch 专项集或逐 utterance 混说结果。基础版 VibeVoice-ASR 声称支持 50+ 语言和 code-switch，但 BitNet 版本换了更小 LM、缩短训练序列并重新做量化训练，不能自动继承成已验证事实。[基础版声明](https://huggingface.co/microsoft/VibeVoice-ASR#key-features) [BitNet 训练变化](https://arxiv.org/html/2607.21075v2#S2.SS3.SSS2)

官方还明确提醒：准确率表使用标准口音语料，训练分布未覆盖的口音或方言可能显著退化。[官方已知口音限制](https://github.com/microsoft/VibeASR.cpp#accuracy-wer) 相比之下，当前 Fun-ASR 官方页面明确宣称支持中英文自由切换、多地区方言与噪声场景，并给出同一 `0.00033 元/秒` 的服务价格；这些也是服务方声明，仍需以 Memoria 真机音频为准。[Fun-ASR 官方能力与价格](https://help.aliyun.com/zh/model-studio/fun-asr-realtime)

## 5. 流式、interim/final、时间戳与长音频

| 能力 | 官方 VibeASR.cpp 当前状态 | 对 Memoria 的判断 |
|---|---|---|
| 持续 PCM 输入 | 不支持；stdin 收 WAV 文件路径，`load_wav` 一次读完并转 mono/24 kHz | 不满足现有 80 ms PCM 流协议，需要另写落盘/内存分块服务 |
| 在线 acoustic streaming | 未实现；每个完整 chunk 重跑两路 VAE 和 LM prefill；论文也把 edge 用法描述为 chunked audio，而不是一次处理完整长录音 | 不满足边说边识别 |
| token streaming | 支持，但发生在整段音频编码后，每 token 一行 | 不能等同于 ASR interim |
| interim 可修订假设 | 无正式事件或 revision 语义 | 不满足现有 stable-prefix / preflight 路径 |
| final | `---END---` 表示当前文件完成 | 可映射为 chunk final，但延迟和分块边界需自建 |
| 词级时间戳 | 没有经验证的词级 timestamp API | 不满足当前 `aligned_transcript="word"` |
| 句段时间戳/说话人 | 源码有 `json` prompt，让模型生成 `Start/End/Speaker/Content`，但注释说明这是 7B 模型格式；1.5B BitNet 默认是纯文本，且代码没有 schema 校验或时间戳质量评测 | 不能视为可靠生产契约 |
| VAD / endpoint | 模型服务器没有 VAD；官方 Gradio “Online”固定 `FIRST_CHUNK_DURATION=5s`、`DEFAULT_CHUNK_DURATION=10s`、`OVERLAP=2.5s`，再用静音搜索和 LCS 去重模拟，本质仍是分块批处理 | 可复用 Memoria 现有 VAD 切话轮，但会变成 endpoint 后离线识别 |
| 长音频 | BitNet LM 训练只用小于 4 分钟的片段；性能只测 5–40 秒。基础版的 60 分钟单次能力不能移植过来 | 只按短 chunk 候选评估，不声明 4 分钟或 60 分钟生产能力 |

源码证据：[WAV 一次读完与转 mono/24 kHz](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/utils/audio_io.h#L81-L148)、[每 chunk 清 KV 并重新 prefill](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L187-L208)、[token 与 END 协议](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L231-L318)、[text/json prompt 注释](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/utils/prompt_builder.h#L95-L180)、[官方 Demo 的“在线”分块](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/demo/gradio_asr_demo.py#L441-L528)、[分块重识别与文本去重](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/demo/gradio_asr_demo.py#L609-L679)、[小于 4 分钟训练边界](https://arxiv.org/html/2607.21075v2#S2.SS3.SSS2)。

## 6. 并发、部署与运维成熟度

官方 `asr_stream_server` 是一个常驻进程，通过 stdin 顺序读取文件路径；每次同步执行 `process_chunk`，处理完再读下一行。它只有一个 LM context，并在每个 chunk 前清 KV。官方没有 HTTP/gRPC/WebSocket 多客户端服务、请求取消、超时隔离、队列背压、租户上下文隔离、并发 worker 或负载测试。[串行主循环](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/src/asr_server.cpp#L380-L449)

因此生产接入至少还需要：

1. 独立 ASR worker/service，封装 PCM→WAV、VAD chunk、超时、取消、健康检查和指标；
2. 每 session 独立的上下文和 generation fence；官方 server 的 `CONTEXT:` 修改的是进程全局字符串，直接共享会有跨会话污染风险；
3. 进程池/副本级并发与 admission control；测清每 worker 的 CPU、RSS 和峰值话轮长度；
4. 失败回退到 FunASR，不能让本地 OOM 或积压阻塞 LiveKit Agent；
5. 适配 24 kHz 模型输入，同时确认现有 16 kHz 上行和 AEC 音频的重采样质量。

官方构建要求 Python ≥3.9、CMake ≥3.14、GCC/Clang 与 C++11；CPU 优化针对 x86 AVX2/FMA 和 ARM NEON。Windows 不支持 MSVC，需 GCC/Clang；Linux/macOS 更适合当前试验。[官方 Requirements 与 Windows 限制](https://github.com/microsoft/VibeASR.cpp#quick-start)

成熟度方面，截至核验日，技术报告是 2026-07-25 的 arXiv v2，官方运行时仓库仍处于快速迭代期；没有公开的并发或生产 SLO 证据。[arXiv 版本记录](https://arxiv.org/abs/2607.21075) 这不代表代码不可用，但不能按成熟托管 ASR 的运维能力估算替换成本。

## 7. 许可证与商用边界

- 模型卡声明 MIT License；官方 `VibeASR.cpp` 仓库也提供完整 MIT LICENSE。[模型卡许可证](https://huggingface.co/microsoft/VibeVoice-ASR-BitNet#license) [运行时 LICENSE](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/LICENSE)
- MIT 条款允许使用、修改、再分发、再许可和销售，要求在软件副本或主要部分中保留版权与许可声明，并明确无担保。
- 官方运行时包含一个定制 llama.cpp submodule；如果把二进制/镜像对外分发，仍需做第三方 notices 与依赖许可证清单，不能只复制顶层 LICENSE。[官方 submodule 声明](https://github.com/microsoft/VibeASR.cpp/blob/da6991219d83dcc5e13be487ab71cc0cdb1bf226/.gitmodules)
- BitNet 模型卡和技术报告没有给出训练数据逐项清单、个人信息/声音数据权利证明或特定行业合规承诺。MIT 许可不替代隐私、数据跨境、声音生物信息和训练数据合规审查。

工程上可认为“许可证允许商业使用”，但正式上线前仍应由法务核对模型权重、第三方组件和业务音频处理边界；以上不是法律意见。

## 8. 成本测算

### 8.1 当前 API 基准

按阿里云官方原价 `0.00033 元/秒`：

| 每月送入 ASR 的实际音频 | FunASR 原价/月 |
|---:|---:|
| 100 小时 | 118.8 元 |
| 500 小时 | 594 元 |
| 1,000 小时 | 1,188 元 |
| 3,000 小时 | 3,564 元 |

实际账单可能因免费额度、活动、折扣和发送静音时长不同而更低或更高，必须以百炼账单为准。[官方价格说明](https://help.aliyun.com/zh/model-studio/fun-asr-realtime)

### 8.2 自建盈亏点

若独立 ASR 服务的完整月成本为 `C` 元，先忽略开发、运维和冗余，自建相对 API 的理论盈亏点为：

```text
每月音频小时 > C / 1.188
```

示例仅用于理解数量级：100/300/500/1,000 元月成本对应约 84/253/421/842 音频小时。真实 `C` 必须包含：合适的 CPU/RAM 实例、至少一份冗余或回退、监控、模型分发、存储、故障处理，以及为缺失的 streaming/interim/timestamp/concurrency 所做的开发和维护。

由于官方 server 串行、并发和峰值 RSS未知，不能用单路 RTF 直接推导“每台机器支持多少在线用户”。在低到中等用量下，`1.188 元/音频小时` 的托管价格很难被一台常驻专用服务器加运维成本稳定击穿；只有以下情况才值得继续做成本 PoC：

- 实际账单音频量已经达到每月数百至上千小时；
- 有可复用的闲置 16 GiB 级 CPU 资源，边际成本远低于新购实例；
- 可接受 endpoint 后离线识别的延迟，或用途本身就是批处理；
- 隐私/离线部署价值本身高于纯成本收益。

## 9. 建议路线

### P0：不要改主链，先补齐决策数据

1. 从百炼账单取得最近 30 天 `fun-asr-realtime` 的实际音频秒数、费用、折扣、峰值并发和静音占比。
2. 记录当前 FunASR 的 endpoint→首 interim、endpoint→final、短控制词召回、中英混说 CER/WER 与断线率基线。
3. 明确目标到底是“降低现金 API 费用”，还是“音频不出域/离线可用”；两者的最优方案可能不同。

### P1：若账单足够高，再做独立 shadow PoC

- 使用独立 Linux x86 AVX2/FMA 试验机，不部署到当前共享生产机；初始建议 4–8 vCPU、16 GiB RAM。
- 只旁路复制获得授权的同一份 AEC 后 PCM，不改变权威 transcript，不写主人历史，不触发 LLM/TTS/工具。
- 以 5/10/20/40 秒真实话轮测：峰值 RSS、冷/热启动、RTF、音频结束到首 token/final 的 P50/P95/P99、单 worker 串行积压、多个进程并发和 OOM 恢复。
- 测试集必须覆盖普通话近讲/远讲、播放回声、噪声、短控制词、控制词+内容、中英品牌名混说、数字/日期/金额、方言口音、空音频和截断音频。
- 中文效果必须与当前付费 API 做同音频盲测；报告中的 FunASR-Nano 数字不能代替这个 A/B。

### P2：只有同时过门槛，才讨论小流量 canary

- 普通话、中英混说、短控制词和专名准确率达到当前 FunASR 约定的非劣门槛；
- endpoint→final P95 满足产品等待预算；若仍无在线 interim，则播放期打断必须保留独立、已验证的低延迟识别路径，不能让离线 final 接管；
- 词级时间戳若不能提供，需证明所有消费方可安全降级，而不是伪造时间戳；
- 并发、内存、超时、取消、跨 session 隔离、late result 与 generation fence 全部通过故障注入；
- 以真实账单和实例成本复算后有明确净节省，并计入至少一条可立即回切 FunASR 的回滚路径。

## 最终建议

| 方案 | 建议 | 原因 |
|---|---|---|
| 直接替换主 FunASR Realtime | **否** | 缺持续 PCM online streaming、正式 interim/revision、可靠词级时间戳和并发服务；播放期打断会明显退化 |
| 在现有生产机同机自建 | **否** | 已记录的内存边界、本机 2.57–3.61 GiB 单进程峰值和按音频长度增长的 VAE arena 都表明没有可信共机余量；当前实时内存未核验 |
| 作为已结束话轮的影子 ASR | **可以做 PoC** | 不影响权威链路，可获得真实中文、混说、延迟和内存数据 |
| 离线/批量转写 | **有条件可用** | CPU 吞吐和 MIT 许可有吸引力，但仍需中文效果、内存和长音频分块实测 |
| 为降低成本立即开发生产适配器 | **暂缓** | 先看实际月账单；托管价低，缺失能力的工程与运维成本可能高于 API 节省 |

一句话结论：**VibeVoice-ASR-BitNet 是不错的 CPU 离线 ASR 技术样本，但目前不是 Memoria 的实时主 ASR 替代品；先量账单，再做完全旁路的短周期 PoC，主链继续保留 FunASR Realtime。**
