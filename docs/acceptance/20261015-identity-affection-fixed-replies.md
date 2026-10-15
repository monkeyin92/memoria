# Identity and affection fixed replies (20261015)

Branch `cursor/identity-affection-fixed-replies-b13f`. Code and offline verification only. No real device.

## Product copy

| Constant | Exact text |
| --- | --- |
| `NAMELESS_IDENTITY_REPLY` | 我是 AI 助手，不是真人，可以陪你聊天、讲故事。你今天想聊什么呀？ |
| `AFFECTION_FIXED_REPLY` | 我是 AI，没有喜欢的感觉，不过我会认真听你说话。你今天想聊点什么呀？ |

Offline `OFFLINE_LINES` in `firmware/esp32/scripts/build_offline_stories.py` has network/menu lines only. No identity or affection product line was present, so the affection constant uses the product text above. No audio manifest update. If these lines are later pre-recorded, AIGC metadata is required.

## Closed matchers (normalized exact match)

Identity routes: 你是谁 / 你叫什么名字 / 你是真人吗 / 你是人吗 / 你是机器人吗

Affection routes: 你喜欢我吗 / 你爱我吗 / 你想我吗

Nearby wording (你是不是真人, 你喜不喜欢我, 你是千问吗, who are you) is not a fixed reply.

## Offline samples (no LLM, no device)

| Query | Reply |
| --- | --- |
| 你是谁？ | NAMELESS_IDENTITY_REPLY |
| 你叫什么名字？ | NAMELESS_IDENTITY_REPLY |
| 你是真人吗？ | NAMELESS_IDENTITY_REPLY |
| 你是人吗？ | NAMELESS_IDENTITY_REPLY |
| 你是机器人吗？ | NAMELESS_IDENTITY_REPLY |
| 你喜欢我吗？ | AFFECTION_FIXED_REPLY |
| 你爱我吗？ | AFFECTION_FIXED_REPLY |
| 你想我吗？ | AFFECTION_FIXED_REPLY |

Neither fixed reply contains 我喜欢你 / 我爱你 / 好朋友. Crisis scripts are unchanged.

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
uv run pytest -o addopts= --tb=line -q \
  services/common/tests/test_companion_response_safety.py \
  services/agent/tests/unit/test_voice_prompt.py \
  services/agent/tests/unit/test_audience_speaking_style.py \
  services/agent/tests/unit/test_reply_limits.py \
  services/agent/tests/unit/test_interaction_mode_agent.py::test_response_plan_failure_keeps_companion_identity_and_safety_fixed
uv run python scripts/check_module_budget.py check
```
