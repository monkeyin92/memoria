# Nameless identity prompt (20261015)

Branch `cursor/nameless-identity-prompt-b13f`. Code and offline verification only. No real device.

## Why

On qwen3.8-flash the voice agent called itself 「小智」(xiaozhi-esp32 framework prior). Product rule: the robot has no personal name, refers to itself only as 「我」, and answers who/what with 「我是陪你聊天、讲故事的 AI 助手」.

## Where names came from

| Source | DeepSeek-era behavior | Change |
| --- | --- | --- |
| `AI_IDENTITY_RULE_TRANSPARENT` | 「只能使用当前系统消息提供的机器人名称…」 | Nameless rule + fixed identity sentence |
| `CompanionStyle.prompt_fragment` | 「你对外只称自己为：{display_name}」 | 「没有个人名字；自称只用「我」」 |
| Control response-plan instructions | 「陪伴机器人“{display_name}”。对外只使用这个名字」 | Style-only; no spoken name |
| `companion_identity_reply` | 「我是{display_name}，…」 (e.g. 桃喜 / 阿序) | Fixed nameless line |
| Persona block / obligation templates | 「你当前的人格是「{name}」」 / 「我是{name}」 | Nameless self-reference |
| Welcome / Doubao preview lines | 「嗨，我是桃喜」 etc. | 「嗨，我在」 |
| Hard-coded 「小智」 in this repo | Not present | Guard test blocks seeding it |

DeepSeek-era spoken identity used companion catalog names (桃喜, 阿序, 星澜, …) via the fixed reply and prompt fragments above. 「小智」 was not hard-coded here; the model invented it when the name seam was weak.

## Preference redirect

`SAFETY_CORE` / `VOICE_SYSTEM_PROMPT` now require a one-sentence AI redirect for favorite-color/food questions, e.g. 「我是 AI，没有眼睛看颜色，你最喜欢什么颜色呀？」. No faked preferences, no 「我也喜欢」.

## Offline sample replies (fixed path, no LLM, no device)

| Query | Audience | Reply |
| --- | --- | --- |
| 你叫什么名字？ | default | 我是陪你聊天、讲故事的 AI 助手。 |
| 你是谁？ | student_minor | 我是陪你聊天、讲故事的 AI 助手。 |
| 你是什么？ | senior_companion | 我是陪您聊天、讲故事的 AI 助手。 |
| 你是千问吗？ | default | 我是陪你聊天、讲故事的 AI 助手。 |
| 你最喜欢什么颜色？ | default | *(no fixed reply; prompt rule steers the model)* |

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
uv run pytest -o addopts= --tb=line -q \
  services/agent/tests/unit/test_voice_prompt.py \
  services/agent/tests/unit/test_audience_speaking_style.py \
  services/agent/tests/unit/test_persona_renderer.py \
  services/agent/tests/unit/test_prompt_composition.py \
  services/agent/tests/unit/test_reply_limits.py \
  services/common/tests/test_companion_response_safety.py \
  services/common/tests/test_companions.py \
  services/agent/tests/unit/test_interaction_mode_agent.py::test_response_plan_failure_keeps_companion_identity_and_safety_fixed \
  services/agent/tests/unit/test_production_prompt_wiring.py::test_real_consumer_invocation_with_profile
uv run python scripts/check_module_budget.py check
uv run ruff check services/agent/src/prompts.py services/common/companion_response_safety.py \
  services/agent/src/mode_policy_client.py services/agent/src/persona_renderer.py \
  services/agent/src/prompt_composition.py services/control_api/app/routes/interaction.py \
  services/common/companions.py services/agent/src/providers/doubao_voice_catalog.py \
  services/agent/tests/unit/test_voice_prompt.py
```

## Result

Focused suite passes on this VM. Forbidden-copy stays at 39 `_PROHIBITED_CONTENT_TERMS`. Prompt surfaces carry no hard-coded assistant name (小智 or catalog display names).

## Limits

Preference redirect is prompt-steered, not a fixed reply. Full Control API identity short-circuit tests need `MEMORIA_TEST_POSTGRES_DSN` and were not run here. No real-device soak.
