# 小程序短命令 KWS 资产

仓库只保存纯控制词，不保存或下载模型权重。当前候选使用 Vosk
`vosk-model-small-cn-0.22`，官方模型页标记为 Apache-2.0。

```text
infra/kws/keywords.txt
```

Agent 会把每个中文控制词转换成按字分隔的 Vosk grammar，并自动加入 `[unk]`。只有当前
VAD 话轮的完整结果精确等于一个控制词、不含 `[unk]` 且达到最低平均置信度时，才会把它
作为 Router 证据；partial 不执行停止。

模型由 operator 从官方地址下载到宿主机
`/var/lib/memoria-agent/models/vosk-model-small-cn-0.22`，归档固定为：

```text
URL: https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
SHA-256: 3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba
```

模型 zip 不进入仓库、镜像或发布包。完整许可、兼容性和正负例记录见
`docs/research/2026-07-27-vosk-chinese-kws.md`。
