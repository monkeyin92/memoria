# CosyVoice designed voices (v3.5)

`cosyvoice-v3.5-flash` / `plus` **没有系统音色**（不能用 `longanyang`）。  
Memoria 通过 **声音设计（Voice Design）** 创建自定义音色，再用于实时合成。

## 目录

| 文件 | 说明 |
|------|------|
| 代码目录 `cosyvoice_voice_catalog.py` | 5 套陪伴向 `voice_prompt` |
| `designed_voice_ids.json` | enrollment 后写入的 `profile → voice_id`（本机/密钥相关，勿硬编码密钥） |
| `previews/*.wav` | 设计接口返回的试听音 |

## 创建音色

```bash
export DASHSCOPE_API_KEY=sk-...
# 推荐北京 Workspace 专属域名（更稳）
# export DASHSCOPE_WORKSPACE_ID=<你的 WorkspaceId>
# 或 export DASHSCOPE_VOICE_DESIGN_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization

# 只看描述
python scripts/design_cosyvoice_voices.py --dry-run

# 创建全部（target 须与合成 model 一致）
python scripts/design_cosyvoice_voices.py --target-model cosyvoice-v3.5-flash

# 只建默认陪伴声
python scripts/design_cosyvoice_voices.py --profiles warm_companion
```

创建免费（CosyVoice 声音设计）。试听 `infra/voices/previews/`，不满意可删了重建。

## 启用 v3.5

```bash
export COSYVOICE_MODEL=cosyvoice-v3.5-flash
export COSYVOICE_VOICE_PROFILE=warm_companion
# 或直接：
# export COSYVOICE_VOICE=cosyvoice-v3.5-flash-vd-warmboy-xxxxxx
export COSYVOICE_INSTRUCT_STYLE=auto   # v3.5 自动用 freeform 指令
export COSYVOICE_WORD_TIMESTAMPS=true

uv run pytest services/voice_profile/tests/test_cosyvoice_preview.py \
  services/agent/tests/integration/test_cosyvoice_mock.py
# 生产主链 smoke 只验证豆包；历史 CosyVoice 音色还必须单独做授权真人盲听。
```

## 内置 profile

| profile_id | 名称 | 用途 |
|------------|------|------|
| `warm_companion`（默认） | 暖阳青年 | 主陪伴，接近原 longanyang 定位 |
| `soft_confidante` | 温柔知己 | 共情 / 深夜 |
| `calm_guide` | 沉稳向导 | 规划 / 步骤说明 |
| `bright_peer` | 元气搭子 | 轻松闲聊 |
| `low_magnetic` | 低音笃定 | 稳重托底 |

## 注意

1. **声音设计 / 合成 model 必须一致**（例如都是 `cosyvoice-v3.5-flash`）。
2. **v3.5 仅北京**；新加坡系统音色路径请继续用 `cosyvoice-v3-flash` + `longanyang`。
3. 设计音色走 **freeform Instruct**，不再使用 longanyang 固定句式。
4. 1 年未用于合成的音色可能被平台清理；生产需监控 / 定期触达。
5. 相同 prompt 每次设计结果可能不同，请试听后固化 `voice_id`。
