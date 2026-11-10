# 首包延迟分布（2026-11-10）

## 数据来源

脚本读取每个 run 目录下的 `bridge.log`，提取行首 ISO8601 UTC 时间戳和以下事件：

- `first_phrase_ready`
- `doubao_session_started`
- `doubao_first_pcm`
- `first_frame_sent`

本次批量命令指定了四个目录：

| run 目录 | `bridge.log` | 事件样本数 | 四段可配对样本数 |
| --- | --- | --- | --- |
| `run-20261107-003640-soak-card5` | 有 | `first_phrase_ready=42`、`doubao_session_started=45`、`doubao_first_pcm=45`、`first_frame_sent=46` | `41 / 45 / 45 / 42` |
| `run-20261107-001440-soak-card5` | 有 | `first_phrase_ready=46`、`doubao_session_started=48`、`doubao_first_pcm=47`、`first_frame_sent=48` | `45 / 47 / 47 / 46` |
| `r02-20261107-094902` | 缺失，跳过 | — | — |
| `r02-20261108-201029` | 缺失，跳过 | — | — |

四段可配对样本数的顺序是：
`first_phrase_ready→doubao_session_started`、`doubao_session_started→doubao_first_pcm`、`doubao_first_pcm→first_frame_sent`、`first_phrase_ready→first_frame_sent`。

每段独立按日志出现顺序配对。一个新的起始事件在前一个起始事件尚未找到结束事件时，会使前一段计为缺失并丢弃；没有起始事件的结束事件计为孤儿；结束事件时间戳早于起始事件计为乱序并丢弃。连续两个起始事件各自等到一个结束事件时，无法判断哪个结束属于哪个起始，这两段都记为 `ambiguous` 且不产出样本，避免把不同轮次配成一对。`p50` 和 `p90` 使用 nearest-rank 口径，表格显示秒并四舍五入到三位小数，`--json` 保留原始浮点精度。本次两份日志的实际 `ambiguous` 计数都是 0。

抽样核对过 `last_user_audio`。它与 `first_phrase_ready` 没有稳定的一一轮次键：目标 run 为 `37` 个 `last_user_audio` 对 `42` 个 `first_phrase_ready`，相邻配对只有 `35` 组；另一份 soak 为 `26` 对 `46`，相邻配对只有 `26` 组。因此它没有参与以下统计，也不能被当作严格的“孩子说完”边界。

## 分布

### `run-20261107-003640-soak-card5`

| 段 | 样本数 | p50 | p90 | max | mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| `first_phrase_ready→doubao_session_started` | 41 | 0.050s | 0.056s | 0.059s | 0.050s |
| `doubao_session_started→doubao_first_pcm` | 45 | 0.289s | 0.398s | 0.725s | 0.319s |
| `doubao_first_pcm→first_frame_sent` | 45 | 0.002s | 0.004s | 0.009s | 0.002s |
| `first_phrase_ready→first_frame_sent` | 42 | 0.338s | 0.449s | 0.786s | 0.366s |

诊断计数：`first_phrase_ready→doubao_session_started` 丢弃 `1` 段，原因是缺少结束事件；该段有 `4` 个孤儿结束事件。`doubao_session_started→doubao_first_pcm` 没有丢弃。`doubao_first_pcm→first_frame_sent` 有 `1` 个孤儿结束事件。四段都没有时间戳乱序。

### `run-20261107-001440-soak-card5`

| 段 | 样本数 | p50 | p90 | max | mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| `first_phrase_ready→doubao_session_started` | 45 | 0.048s | 0.059s | 0.063s | 0.050s |
| `doubao_session_started→doubao_first_pcm` | 47 | 0.282s | 0.331s | 1.010s | 0.308s |
| `doubao_first_pcm→first_frame_sent` | 47 | 0.002s | 0.005s | 0.007s | 0.003s |
| `first_phrase_ready→first_frame_sent` | 46 | 0.332s | 0.406s | 1.995s | 0.391s |

诊断计数：`first_phrase_ready→doubao_session_started` 丢弃 `1` 段，原因是缺少结束事件，并有 `3` 个孤儿结束事件。`doubao_session_started→doubao_first_pcm` 丢弃 `1` 段，原因是缺少 `doubao_first_pcm`。`doubao_first_pcm→first_frame_sent` 有 `1` 个孤儿结束事件。四段都没有时间戳乱序。

### 全量汇总

全量只包含上述两个有日志的 run，共 `2` 个 run；另外 `2` 个缺日志目录被跳过。

| 段 | 样本数 | p50 | p90 | max | mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| `first_phrase_ready→doubao_session_started` | 86 | 0.049s | 0.056s | 0.063s | 0.050s |
| `doubao_session_started→doubao_first_pcm` | 92 | 0.283s | 0.362s | 1.010s | 0.313s |
| `doubao_first_pcm→first_frame_sent` | 92 | 0.002s | 0.004s | 0.009s | 0.002s |
| `first_phrase_ready→first_frame_sent` | 88 | 0.336s | 0.422s | 1.995s | 0.379s |

## 证据边界

这四段只覆盖已经进入 TTS 首句准备后的 provider 会话、首个 PCM 和发送侧首帧：

- `first_phrase_ready→doubao_session_started` 反映首句准备到豆包会话启动；
- `doubao_session_started→doubao_first_pcm` 反映 TTS 会话启动到首个 PCM；
- `doubao_first_pcm→first_frame_sent` 反映首个 PCM 到发送侧首帧；
- `first_phrase_ready→first_frame_sent` 是这份日志能直接得到的发送侧总段。

它们不包含 ASR finalize，也不包含 LLM 首 token。`first_phrase_ready` 本身已经发生在更早的 LLM 产出之后，所以这些数不等于从孩子说完到孩子感知首包的端到端延迟，也不证明真机扬声器已经发声或孩子已经听见。

## 真实运行

命令：

```sh
uv run python scripts/first_packet_latency.py devicetest/run-20261107-003640-soak-card5
```

真实输出摘要：

```text
Run: run-20261107-003640-soak-card5
events: first_phrase_ready=42, doubao_session_started=45, doubao_first_pcm=45, first_frame_sent=46, last_user_audio=37
first_phrase_ready→doubao_session_started        41    0.050s    0.056s    0.059s    0.050s
doubao_session_started→doubao_first_pcm          45    0.289s    0.398s    0.725s    0.319s
doubao_first_pcm→first_frame_sent                45    0.002s    0.004s    0.009s    0.002s
first_phrase_ready→first_frame_sent              42    0.338s    0.449s    0.786s    0.366s
runs analyzed=1, runs skipped=0
```

其中 TTS 段的 `45` 个样本与该 soak 的约 `45` 轮量级一致，并与此前手工记录的 `p50=0.289s`、`p90=0.398s`、`max=0.725s` 一致。
