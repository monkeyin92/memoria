# Memoria CAM++ 声纹 Embedding 服务实现调研

> 日期：2026-07-19  
> 范围：只研究 Memoria H5 后端所需的独立 speaker embedding 服务；不涉及 iOS。  
> 证据优先级：ModelScope 官方模型仓库、3D-Speaker/FunASR 官方源码、ONNX Runtime 与依赖项目的一手发布信息。项目实测与官方事实分开标注。

## 1. 结论

Memoria 第一版正式声纹模型建议固定为：

```text
model_id: iic/speech_campplus_sv_zh-cn_16k-common
model_revision: v1.0.0
model_revision_commit: 7e452fc19c2b0c761d591a4241c332e02dfe10d3
checkpoint: campplus_cn_common.bin
checkpoint_size: 28036335
checkpoint_sha256: 3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8
source_repository: modelscope/3D-Speaker
source_commit: 065629c313eaf1a01c65c640c46d77e61e9607b4
input: float32 feature [batch, time, 80]
output: float32 embedding [batch, 192]
```

推荐采用“两段式交付”：

1. 在固定的离线构建环境中，从官方 checkpoint 导出 ONNX，完成 PyTorch/ONNX 数值等价验证，并把最终 ONNX 的 SHA-256 写入构建清单。
2. 在线服务使用 Python 3.12、ONNX Runtime CPU 和 Kaldi-compatible FBank；运行时不安装 PyTorch、torchaudio、ModelScope，也不联网下载模型。

CAM++ 适合这一版的原因是：中文通用模型、权重仅约 28 MB、输出 192 维，官方 CPU 基准中 CAM++ 的 3 秒音频端到端 RTF 为 `0.049`，低于 ERes2NetV2 的 `0.142`。这只是官方 Xeon 8163 单次基准，不等同于 Memoria 生产延迟承诺。[3D-Speaker README](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/README.md#L146-L159)

必须同时保留三条安全边界：

- CAM++ 只产生 speaker embedding，不能识别姓名，也不能单独证明账户权限。
- 该模型没有 replay/synthetic anti-spoof 能力，不能伪造 `replay_risk=0` 或 `synthetic_risk=0`。
- 官方没有发布可直接下载的 CAM++ ONNX，也没有官方证明 Python 3.12 + CAM++ Python ONNX 服务可运行；这两项必须由 Memoria 自己构建和验收。

## 2. 模型、版本与许可证

### 2.1 首选模型

| 项目 | 固定值 | 一手证据 |
|---|---|---|
| Model ID | `iic/speech_campplus_sv_zh-cn_16k-common` | [ModelScope 模型页](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/summary) |
| Revision | `v1.0.0` | [固定 revision 文件清单](https://modelscope.cn/api/v1/models/iic/speech_campplus_sv_zh-cn_16k-common/repo/files?Revision=v1.0.0&Recursive=true) |
| Annotated tag object | `e55509c273d2342b2988e282fb5b938a1062ed34` | ModelScope Git `refs/tags/v1.0.0` |
| Peeled commit | `7e452fc19c2b0c761d591a4241c332e02dfe10d3` | ModelScope Git `refs/tags/v1.0.0^{}` |
| 权重 | `campplus_cn_common.bin`，28,036,335 bytes | [固定 commit 下载](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/7e452fc19c2b0c761d591a4241c332e02dfe10d3/campplus_cn_common.bin) |
| 权重 SHA-256 | `3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8` | ModelScope 文件 API；本次下载后独立复算一致 |
| 采样率 | 16 kHz | [固定模型配置](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/configuration.json) |
| 特征/输出 | 80-bin FBank / 192 维 embedding | [3D-Speaker infer_sv.py](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/bin/infer_sv.py#L49-L56) |
| 训练语料边界 | 约 20 万中文说话人 | [固定模型卡](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/README.md) |
| 模型许可证 | Apache License 2.0 | [ModelScope 官方元数据 API](https://www.modelscope.cn/api/v1/models/iic/speech_campplus_sv_zh-cn_16k-common) |

这里选 `v1.0.0`，不是随 `master` 漂移，也不是自行假定最新 tag 更好。原因是固定的 3D-Speaker 推理和 ONNX exporter 都明确为该模型选择 `v1.0.0`；`v2.0.2` 主要增加 FunASR 配置，核心 checkpoint 的 SHA-256 与 `v1.0.0` 相同。[官方 exporter 模型表](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/bin/export_speaker_embedding_onnx.py#L47-L58)

### 2.2 源码固定点

- 3D-Speaker：[`065629c313eaf1a01c65c640c46d77e61e9607b4`](https://github.com/modelscope/3D-Speaker/commit/065629c313eaf1a01c65c640c46d77e61e9607b4)
- 3D-Speaker License：[Apache-2.0](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/LICENSE)
- FunASR 参考实现：`v1.3.20` / [`f2122795a414865e14dca29d3a949ad327f56048`](https://github.com/modelscope/FunASR/commit/f2122795a414865e14dca29d3a949ad327f56048)，MIT License。

不能只写 `main`、`master` 或 `campplus-v1`。建议服务返回不可变版本串，例如：

```text
campplus-cn-common@v1.0.0+ckpt.3388cf5f+onnx.<最终ONNX前12位>+fbank.v1
```

声纹模板必须保存完整 `model_id`、revision commit、checkpoint SHA、ONNX SHA、embedding 维度和 preprocessing version；任一项变化都产生新模板版本，不能静默混用。

### 2.3 备选模型

| 使用场景 | Model ID | 固定 revision / commit | 权重 SHA-256 | 输出维度 |
|---|---|---|---|---:|
| 中英混合明显 | `iic/speech_campplus_sv_zh_en_16k-common_advanced` | `v1.0.0` / `8f015938038d619e4e94227f320c6c78e5321315` | `92f29b94e6948786a26778c9e302525d185bb08c8b9f5252ed98776902840199` | 192 |
| 英语为主 | `iic/speech_campplus_sv_en_voxceleb_16k` | `v1.0.2` / `f3fc0e04b50ddac7ad1ca48ab1c49e8e69f2ce15` | `5b1a88b6f8d85826fabef804779c3372b42f3af21457fa48bd5c097c0686b2de` | 512 |

Memoria 当前主要是中文家庭对话，不建议为“未来可能双语”先换成更宽泛模型。以后若真实数据证明中文模型跨语言退化，再用独立模板版本 A/B，不能把 192/512 维向量放进同一模板。

## 3. 精确输入与预处理合同

### 3.1 HTTP 边界先固定 PCM

当前 `CampPlusHTTPEmbeddingAdapter` 发送：

```json
{
  "audio_base64": "...",
  "encoding": "pcm_s16le",
  "sample_rate": 16000
}
```

模型服务 v1 应只接受：

- signed PCM 16-bit little-endian；
- 单声道；
- 16,000 Hz；
- 字节长度为偶数；
- 有界时长与请求体大小。

虽然 3D-Speaker Python 示例会重采样且多声道只取第一路，FunASR 当前 loader 的多声道策略又是求平均，3D-Speaker C++ runtime 则要求单声道。为了让模板可复现，服务边界应直接拒绝非 16 kHz/非单声道输入，而不是让不同 runtime 隐式决定。[3D-Speaker Python 音频加载](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/bin/infer_sv.py#L265-L276) [FunASR PCM 加载](https://github.com/modelscope/FunASR/blob/f2122795a414865e14dca29d3a949ad327f56048/funasr/utils/load_utils.py#L217-L237)

PCM 转 float32 使用：

```text
float_pcm = int16_pcm / 32768.0
```

### 3.2 FBank 与 CMN

官方 Python 推理的实际流程是：

1. 16 kHz 单声道 waveform；
2. 80-bin Kaldi FBank；
3. `dither=0`；
4. 对整条 utterance 的每个频带减均值；
5. 增加 batch 维，形成 `[1, T, 80]`。

来源：[infer_sv.py](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/bin/infer_sv.py#L276-L284) 与 [FBank 实现](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/process/processor.py#L133-L158)。

完整参数按官方 C++ runtime 固定为：

| 参数 | 值 |
|---|---:|
| sample frequency | 16,000 Hz |
| frame length | 25 ms |
| frame shift | 10 ms |
| Mel bins | 80 |
| dither | 0 |
| spectrum | power |
| output | log FBank |
| pre-emphasis | 0.97 |
| remove DC offset | true |
| window | Povey |
| snip edges | true |
| low frequency | 20 Hz |
| high frequency | Nyquist |

来源：[fbank_config.json](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/assets/fbank_config.json)、[默认参数](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/feature/feature_basic.h#L20-L45) 和 [均值扣除](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/bin/extract_speaker_embedding.cpp#L94-L103)。

准确术语是 utterance-level CMN，不是完整 CMVN：官方只减均值，没有除以方差，也没有全局 `cmvn_file`。

## 4. ONNX 合同与导出风险

### 4.1 官方张量合同

官方 exporter 使用：

```text
opset: 11
input name: feature
input dtype: float32
input shape: [batch_size, frame_num, 80]
dynamic axes: batch_size, frame_num
output name: embedding
output shape: [batch_size, 192]
```

来源：[export_speaker_embedding_onnx.py](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/bin/export_speaker_embedding_onnx.py#L177-L191)；C++ runtime 也硬编码相同节点名：[speaker_embedding_model.cpp](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/model/speaker_embedding_model.cpp#L31-L69)。

模型返回原始 embedding，不在模型内做 L2 normalize。Memoria 当前 `SpeakerAuthority` 会在模板形成和 cosine 计算时归一化，因此 HTTP 服务应返回有限的原始 192 维向量，不必悄悄改变模型输出。

### 4.2 没有官方 ONNX 权重

ModelScope 模型仓库只发布 PyTorch checkpoint，没有预构建 `.onnx` 和官方 ONNX SHA。任何“官方 CAM++ ONNX 下载地址/校验值”的说法都不准确。

因此必须生成两层校验：

- 上游校验：官方 checkpoint SHA-256，固定为 `3388cf...5ada8`；
- 下游校验：Memoria 自己构建并验收后的 ONNX SHA-256，在实现阶段写入 lock/build manifest 和镜像标签。

ONNX 不应在容器启动时临时导出，也不应在生产启动时从 ModelScope 下载。

### 4.3 本次实测发现：直接导出存在 AveragePool 数值陷阱

这是 Memoria 本次复现实验，不是官方声明：

- 使用固定 checkpoint 与固定 3D-Speaker 源码导出后，ONNX 中 52 个 `AveragePool` 节点的 `count_include_pad=1`。
- 对动态长度输入，尾部不足 100 帧的 segment pooling 会与 PyTorch 结果偏离；三条官方 WAV 的 PyTorch/ORT embedding 余弦最低约 `0.9795`，不能视为严格等价。
- 把这些节点改为 `count_include_pad=0` 后，随机长度 `100/200/300/345/370/400/489/529/1000` 帧的最大绝对误差不超过 `7.4e-6`，余弦均约为 `1.0`。

根源对应 CAM++ 的 `F.avg_pool1d(..., ceil_mode=True)`：[layers.py](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/speakerlab/models/campplus/layers.py#L97-L108)。官方 README 给了 parity 检查思路，但没有覆盖多种动态长度：[ONNX README](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/README.md#L69-L100)。

实现阶段不要手工修改生产 ONNX 二进制。建议在固定的 export-only 源码副本中把 `count_include_pad=False` 显式写入 `avg_pool1d`，重新导出，再把以下门禁全部跑通：

1. 所有 `AveragePool` 的 `count_include_pad` 都为 `0`；
2. `onnx.checker.check_model` 通过；
3. PyTorch 与 ORT 在多种长短输入上 `cosine >= 0.99999` 且 `max_abs <= 1e-4`；
4. 三条官方 WAV 的输出维度、有限值、同/异人排序通过；
5. 最终才记录 ONNX SHA-256。

本次实验产物没有写入仓库，其 SHA 也不能当作正式交付 SHA；正式值必须由项目固定构建环境重新生成。

## 5. Python 3.12 / Linux CPU 可行性

### 5.1 官方能够证明什么

3D-Speaker 官方资料明确证明：

- 主仓 README 标注 Python `>=3.8`，但 quickstart 实际创建 Python 3.8 环境；
- ONNX C++ runtime 只明确测试 Linux；
- 默认 `USE_CUDA=OFF`，CPU 可运行；
- 官方 Python 导出/对照环境为 `torch==1.13.1`、`onnx==1.14.1`、`onnxruntime==1.16.1`。

来源：[3D-Speaker README](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/README.md#L11-L52) 与 [ONNX runtime README](https://github.com/modelscope/3D-Speaker/blob/065629c313eaf1a01c65c640c46d77e61e9607b4/runtime/onnxruntime/README.md#L38-L100)。

FunASR `v1.3.20` 的 package metadata 声明 Python 3.12 classifier，并且 CAM++ `inference()` 返回 `spk_embedding` `[1,192]`：[setup.py](https://github.com/modelscope/FunASR/blob/f2122795a414865e14dca29d3a949ad327f56048/setup.py#L135-L149) [CAM++ model.py](https://github.com/modelscope/FunASR/blob/f2122795a414865e14dca29d3a949ad327f56048/funasr/models/campplus/model.py#L142-L195)。但其 Python 3.12 ONNX CI 没有覆盖 CAM++ embedding，所以不能把它扩写为“官方已验证 CAM++ Python 3.12 ONNX”。

### 5.2 Memoria 本次容器 smoke

本次在 `python:3.12-slim`、Linux ARM64 CPU 容器中使用：

```text
Python 3.12.13
onnxruntime 1.27.0
kaldi-native-fbank 1.22.3
numpy 2.4.3
CPUExecutionProvider
```

固定模型经上述 pool semantics 修正后，三条官方 WAV 都返回 192 维有限向量：

| 对比 | cosine |
|---|---:|
| `speaker1_a` vs `speaker1_b`（同一说话人） | `0.693604` |
| `speaker1_a` vs `speaker2_a`（不同说话人） | `-0.084175` |

这证明该最小依赖路线能在本次 Python 3.12/Linux CPU ARM64 容器运行，不代表官方承诺，也不代替未来生产 `linux/amd64` 镜像 smoke。PyPI 当前为 ONNX Runtime 1.27.0 和 kaldi-native-fbank 1.22.3 同时提供 CPython 3.12 的 manylinux x86_64、aarch64 wheels；镜像锁文件仍需固定 wheel hash。[ONNX Runtime PyPI](https://pypi.org/project/onnxruntime/1.27.0/) [kaldi-native-fbank PyPI](https://pypi.org/project/kaldi-native-fbank/1.22.3/)

### 5.3 最小在线依赖

模型运行层建议只新增：

```text
onnxruntime==1.27.0
kaldi-native-fbank==1.22.3
numpy==2.4.3
```

HTTP 层复用项目已有 FastAPI、Pydantic、Uvicorn。运行镜像不需要：

- `torch`
- `torchaudio`
- `modelscope`
- `funasr`
- `onnx`（只在构建/校验阶段需要）
- ffmpeg/SoX（v1 严格只接受 16 kHz mono PCM）

`kaldi-native-fbank` 自称是无外部依赖的 Kaldi-compatible online FBank，并提供 Linux、x86、arm、aarch64 与 Python API：[官方仓库](https://github.com/csukuangfj/kaldi-native-fbank/tree/b09e686fe2084732ddd30d1ef80acfc0f13eaf01)。它不是 3D-Speaker 官方 runtime 的直接依赖，因此 Memoria 必须用官方 PyTorch FBank golden vectors 做数值等价门禁，不能只凭“Kaldi-compatible”名称认定完全相同。

## 6. 可复核的官方 WAV

| 文件 | 格式/时长 | SHA-256 | 固定下载 |
|---|---|---|---|
| `speaker1_a_cn_16k.wav` | PCM s16le、mono、16 kHz、3.715250 s | `5f20ce0ddc378ca3239d3ce864b1142726a46a1221ae553912e4e142045df58b` | [下载](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/examples/speaker1_a_cn_16k.wav) |
| `speaker1_b_cn_16k.wav` | PCM s16le、mono、16 kHz、4.906688 s | `20745dc08a4281894d146140b99b9ef7417ac681119b7f7202f553cdf1a85f65` | [下载](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/examples/speaker1_b_cn_16k.wav) |
| `speaker2_a_cn_16k.wav` | PCM s16le、mono、16 kHz、5.312000 s | `8a6cffa452df32ef10503f7992f22ffcdd7f16c4e0273d13311bc5cdcb13abf4` | [下载](https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v1.0.0/examples/speaker2_a_cn_16k.wav) |

这些文件适合做可重复 smoke，但不能用来确定生产主人阈值。模型卡示例曾使用 `0.31`，旧配置又出现过 `0.5`，官方资料并不一致；Memoria 仍必须按已有方案用不少于 200 条授权的主人、访客、噪声与重放样本校准 FAR、FRR、EER 和 unknown rejection。

## 7. 与 Memoria 现有 HTTP 契约的差距

当前 `CampPlusHTTPEmbeddingAdapter` 除 `embedding` 外，还强制读取：

```text
speech_ms
snr_db
quality_score
replay_risk
synthetic_risk
```

CAM++ 官方模型只能直接提供 `embedding`。其余字段的诚实来源应是：

- `speech_ms`：VAD 后有效语音时长，不应简单等于请求 PCM 总时长；
- `snr_db` / `quality_score`：独立、版本化的信号质量算法；
- `replay_risk` / `synthetic_risk`：独立 anti-spoof/liveness 模型或服务。

3D-Speaker/CAM++ 官方资料没有 anti-spoof 输出。第一版若尚未接 anti-spoof，应让该能力显式为 unavailable，并保持 profile 仅 shadow、分类不升级 owner。不能为通过当前 `classification_quality_reason()` 而返回假零。由于现有数据类没有 `unavailable` 状态，实现前需要在以下两种方案中明确选择：

1. 同步接入独立 anti-spoof provider，只有真实结果才允许通过登记质量门；
2. 扩展内部契约表达 `risk_assessment_unavailable`，允许收集 shadow embedding，但始终 fail-closed 为 `uncertain`，直到 anti-spoof 与真人评估完成。

建议选第 2 种完成低风险 shadow 纵向切片，同时把“active owner”继续锁在 anti-spoof + 200 条真人评估之后。

## 8. 建议的服务交付形态

```text
H5 16k mono PCM
  -> Control API / SpeakerAuthority
  -> internal bearer HTTP
  -> speaker-model
       1. 严格校验 PCM 合同
       2. FBank + utterance CMN
       3. ONNX Runtime CPU embedding
       4. shape/finite/model digest 校验
       5. 独立质量与 anti-spoof 状态
  -> 192-d embedding + 可审计元数据
  -> encrypted speaker template / shadow decision
```

服务实现建议：

- 独立 `speaker-model` 容器，加入 production Compose；不塞进 H5，也不把模型加载进实时 Agent。
- 启动时只创建一个 `InferenceSession`，显式使用 `CPUExecutionProvider`；线程数和并发上限固定并压测。
- 启动先核验 ONNX SHA；不一致直接 readiness fail。
- readiness 至少校验 input/output 名、dtype、维度与一条内置 golden feature 的 digest。
- 接口只在内部网络开放，沿用独立 bearer token；日志不记录 PCM、embedding 或声纹模板正文。
- `model_version` 必须包含 checkpoint、ONNX 和 preprocessing 三层版本。
- 先 shadow：超时、短音频、低 SNR、anti-spoof 未知、模型版本不匹配一律 `uncertain`，不能 fail-open 为 owner。

## 9. 实现阶段验收清单

- [ ] 模型固定为 `v1.0.0` / commit `7e452fc...`，checkpoint SHA 复算通过。
- [ ] 3D-Speaker 固定 commit `065629c...`，许可证随镜像归档。
- [ ] ONNX 在固定 builder 中生成，记录构建工具版本、源码补丁和最终 SHA。
- [ ] 多长度 PyTorch/ORT parity 达到 `cosine >= 0.99999`、`max_abs <= 1e-4`。
- [ ] 三条官方 WAV 的 SHA、192 维、finite、同人分数高于异人分数均通过。
- [ ] `python:3.12-slim` 的目标生产架构（至少 `linux/amd64`）CPU smoke 通过。
- [ ] 非 16 kHz、奇数字节、空音频、超长输入、NaN/Inf 输出全部 fail-closed。
- [ ] 运行容器无 PyTorch、torchaudio、ModelScope 和在线模型下载。
- [ ] anti-spoof 未接入时不会伪造低风险，也不会激活 owner。
- [ ] Compose、health/readiness、资源限制和独立 token 接线完成。
- [ ] 使用授权真人集重新校准阈值，不采用模型卡 `0.31/0.5` 作为生产门槛。

## 10. 尚不能确认的事项

- 官方没有预构建 CAM++ ONNX 与官方 ONNX SHA。
- 官方没有 Python 3.12 + CAM++ + Python ONNX Runtime 的端到端证明。
- 官方没有 Linux ARM64 C++ runtime 测试；其 CMake 下载脚本在 Linux 下硬编码 x64。
- 官方没有发布 CAM++ Python ONNX 服务的最小 requirements。
- 官方没有证明 PyTorch FBank、3D-Speaker C++ FBank 与 kaldi-native-fbank 对所有输入逐元素一致。
- 官方没有给出生产并发、内存峰值、线程安全和长时间运行数据。
- 官方没有 replay、合成音或声纹转换攻击的防护承诺。
- 官方示例阈值互有差异，不能直接用于 Memoria 的主人识别。

这些都应保留为实现/验收项，不能在产品文案中写成已具备能力。
