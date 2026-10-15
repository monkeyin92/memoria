# Fixed-reply matcher expand (20261015)

Branch `cursor/fixed-reply-matcher-expand-b13f`. Depends on #251 (`cursor/identity-affection-fixed-replies-b13f`). Code and offline verification only. No real device.

Reply texts are unchanged from #251 (`NAMELESS_IDENTITY_REPLY`, `AFFECTION_FIXED_REPLY`).

## Normalization

1. Strip punctuation and whitespace.
2. Lowercase (so `AI` / `ai` / `Ai` match).
3. Strip trailing particles 呀 / 啊 / 呢 / 嘛 / 哦 repeatedly.
4. Exact match against the closed sets.

## Identity list (16)

你是谁、你到底是谁、你是什么、你叫什么、你叫什么名字、你叫啥、你叫啥名字、你有名字吗、你是真人吗、你是不是真人、你是真的人吗、你是人吗、你是不是人、你是机器人吗、你是不是机器人、你是AI吗

## Affection list (9)

你喜欢我吗、你喜不喜欢我、你爱我吗、你爱不爱我、你想我吗、你想不想我、你会想我吗、你是我的朋友吗、你是我的好朋友吗

## Unmatched identity phrasing

When the fixed list does not match, `VOICE_SYSTEM_PROMPT` / `AI_IDENTITY_RULE_TRANSPARENT` still require an explicit answer that the assistant is AI and not a real person.

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
uv run pytest -o addopts= --tb=line -q \
  services/common/tests/test_companion_response_safety.py \
  services/agent/tests/unit/test_voice_prompt.py \
  services/agent/tests/unit/test_reply_limits.py
uv run python scripts/check_module_budget.py check
```
