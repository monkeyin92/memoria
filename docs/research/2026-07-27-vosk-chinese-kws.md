# Vosk 中文短控制词 KWS 可用性调研

> 调研日期：2026-07-27
> 目标：确认 `vosk-model-small-cn-0.22` 是否能作为小程序播放期短控制词的合规、可部署声学证据通道。

## 结论

可以进入真机测试候选，但应把它定义为“受限词表的第二路短句识别”，而不是在 partial
结果上立即执行的传统 wake-word KWS：

- Vosk 代码和官方模型表中的 `vosk-model-small-cn-0.22` 都明确标为 Apache-2.0；
- PyPI `vosk==0.3.45` 提供 Python-independent Linux x86_64 wheel，已在
  Python `3.12.13` / Linux amd64 容器完成实际加载和推理；
- 小模型支持运行时 grammar，适合把解码范围限制在“停一下、等一下、等下、等等、
  别说了、暂停、先别说、停下”和 `[unk]`；
- 不能在 partial 首次等于控制词时立刻停止。实测“等一下我想问个问题”和
  “别说了这个词……”都会先产生纯控制词 partial，只有话轮终稿才包含后续 `[unk]`；
- 因此只在当前 VAD speech epoch 结束时取完整结果，且仅当结果精确等于一个纯控制词、
  不含 `[unk]` 并达到最低平均置信度时，才交给 `UtteranceRouter`；
- 模型归档不放进 Git 或 Agent 镜像。测试服务器从官方 URL 下载到宿主机只读模型目录，
  校验固定 SHA-256 后通过现有 `/data` bind 挂载。

## 官方许可与分发边界

Vosk 官方模型页将 `vosk-model-small-cn-0.22` 的许可证明确列为 `Apache 2.0`，并把它描述为
可运行时重配置词汇的小型中文模型：

- https://alphacephei.com/vosk/models

Vosk API 仓库本身包含 Apache License 2.0：

- https://github.com/alphacep/vosk-api/blob/master/COPYING

Apache-2.0 允许商业使用和再分发，但再分发时需要随附许可证并保留适用的版权、专利、
商标和 NOTICE 信息：

- https://www.apache.org/licenses/LICENSE-2.0

本次下载的模型 zip 内只有模型文件和简短 README，没有独立 `LICENSE` 或 `NOTICE`。
因此当前候选不把模型复制进仓库、制品包或 Docker 镜像，避免把“官方网页上的许可标注”
误当成已经完成再分发材料整理。若以后需要随镜像分发，必须一并保存许可证、官方来源、
归档校验值和适用 NOTICE，再做一次发布合规审查。

## Python 3.12 / Linux amd64 兼容性

PyPI `vosk==0.3.45` 声明 `Requires-Python >=3`，并发布
`py3-none-manylinux_2_12_x86_64.manylinux2010_x86_64` wheel：

- https://pypi.org/project/vosk/0.3.45/
- https://pypi.org/pypi/vosk/0.3.45/json

上游只声明 Python 3，并没有单独承诺 Python 3.12。为避免把元数据推断当成兼容性证据，
本次在生产同构容器实际验证：

```text
Python: 3.12.13
OS/arch: Linux amd64
vosk: 0.3.45
model load: success
grammar recognizer: success
16 kHz mono s16le streaming decode: success
```

PyPI 0.3.45 没有 macOS ARM64 wheel；这不阻塞 Linux amd64 生产，但意味着开发机不能直接
把本机 import 成功作为门禁，相关模型 smoke 应固定在 Linux 容器执行。

还需注意：Vosk GitHub 主分支版本高于 PyPI 0.3.45。主分支当前出现的 API 不一定存在于
0.3.45 wheel；生产实现只使用已经在 0.3.45 实测通过的
`Model / KaldiRecognizer / AcceptWaveform / Result / FinalResult / Reset`。

## Grammar 与结果语义

Vosk C API 明确支持用 JSON phrase array 创建和重配 grammar recognizer：

- https://github.com/alphacep/vosk-api/blob/master/src/vosk_api.h

Python 官方示例使用同一能力，并持续读取 partial/result：

- https://github.com/alphacep/vosk-api/blob/master/python/example/test_words.py
- https://github.com/alphacep/vosk-api/blob/master/python/vosk/__init__.py

中文小模型对本组控制词使用按字分隔的 grammar，例如：

```json
[
  "停 一 下",
  "等 一 下",
  "等 下",
  "等 等",
  "别 说 了",
  "暂 停",
  "先 别 说",
  "停 下",
  "[unk]"
]
```

`[unk]` 必须保留。若 grammar 只有控制词，普通语音会被强制映射到最接近的控制词片段，
增加误停风险。

Vosk 的运行时 grammar 是由词序列估计出的受限语言图，不应假设所有输出一定逐条等于输入
phrase。生产仍需做：

1. 去空格和标点；
2. 完整结果精确匹配控制词集合；
3. 任一 word 为 `[unk]` 时拒绝；
4. 平均置信度低于阈值时拒绝；
5. 最终仍由 `UtteranceRouter` 判定必须是纯 `INTERRUPT_COMMAND`。

## 2026-07-27 实际 smoke

官方模型归档：

```text
URL: https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
Content-Length: 43,898,754 bytes
SHA-256: 3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba
解压后: 约 65 MiB
```

普通话合成语音在 Python 3.12 / Linux amd64 容器中的代表结果：

| 输入 | 完整结果 | 判定 |
|---|---|---|
| 停一下 | `停 一 下` | 命中 |
| 等一下 | `等 一 下` | 命中 |
| 别说了 | `别 说 了` | 命中 |
| 暂停 | `暂 停` | 命中 |
| 先别说 | `先 别 说` | 命中 |
| 停下 | `停 下` | 命中 |
| 他 | 空 | 拒绝 |
| 继续说 | `说 说` | 拒绝 |
| 等一下我想问个问题 | `等 一 下 [unk] 一` | 拒绝 |
| 别说了这个词是什么意思 | `别 说 了 [unk] [unk]` | 拒绝 |
| 停一下之后继续 | `[unk]` | 拒绝 |
| 我说停一下的时候不要停 | `说 停 一 下 [unk] 一 停` | 拒绝 |

“等下/等等”对不同合成音色有明显差异：部分音色漏掉首音节，Tingting 音色在
150/185/220 三档语速都能分别得到 `等 下` 和 `等 等`。这说明模型具备词形能力，但短词
召回对音色、句首完整度和 AEC 损伤敏感，不能用合成单音色推断真机召回率。

最关键的负例结果是：

```text
“等一下我想问个问题” partial 曾精确出现 “等一下”
“别说了这个词是什么意思” partial 曾精确出现 “别说了”
```

因此 partial 只可用于诊断，不能成为立即停止证据。VAD 开始仍负责马上 duck，VAD 结束后的
完整受限词表结果负责确认纯控制命令。

## 生产候选约束

- 仅可信小程序 AEC session 启用；
- 仅助手播放期且当前 VAD epoch 有至少 80 ms AEC 后 PCM 时解码；
- 模型加载、JSON 解析、PCM 对齐或解码异常全部 fail closed，普通 FunASR 继续；
- 结果继续经过
  `UtteranceRouter → TargetSpeakerFocus → playback epoch → GenerationFence`；
- 模型只从宿主机只读目录挂载，不自动下载到镜像；
- 首次真机只对目标 session 开启 AEC 前后采样，复测后立即导出并关闭；
- 真机至少覆盖纯控制词、控制词后跟内容、助手原声回灌、访客声音、电视人声和下一段播放
  的迟到结果。
