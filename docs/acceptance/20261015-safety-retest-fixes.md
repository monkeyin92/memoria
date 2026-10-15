# Safety retest fix results (20261015)

Recorded on branch `cursor/safety-retest-fixes-1440`. These checks ran on the VM after the behavior changes. They do not include a device flash.

## Behavior covered

After a fixed safety script has been spoken, a later turn whose planner returns `request_or_payload_invalid` does not speak a local-safe chat reply, including an identity sentence. A later turn that receives a successful planner plan still speaks. Echo of that script stays `assistant_echo` after the 10 second playback window and after the playback floor text changes. A different child sentence is still accepted.

ASR slips `我不想或`, `我不想或者了`, `我不想霍`, and `我不想货` route to `crisis_self` with or without `。` and the particles `啊` `呀` `吧`. `我不想和同学玩了` stays ordinary chat because `和` still requires `了`.

USB `sleep` and `poweroff` stop listening without calling `ToggleChatState` on that same state, abort speaking, toggle only connecting or recovering, disable codec output then input, and cancel an offline story generation. Both park in wifi hold with the hold timer disarmed. `poweroff` also stops the station. USB still powers the board. `CONFIG_MEMORIA_KEEP_AWAKE` stays off by default.

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
uv run pytest -o addopts= --tb=line -q \
  services/common/tests/test_companion_response_safety.py \
  services/common/tests/test_product_acceptance_safety_tiers.py \
  services/agent/tests/unit/test_reply_pipeline_safety_fallback.py \
  firmware/esp32/tests/test_memoria_usb_command.py \
  firmware/esp32/tests/test_memoria_bench_serial.py \
  firmware/esp32/tests/test_memoria_keep_awake.py
```

## Result

238 passed, 0 failed, in 3.53s.

| File | Result |
| --- | --- |
| `services/common/tests/test_companion_response_safety.py` | 72 passed |
| `services/common/tests/test_product_acceptance_safety_tiers.py` | 14 passed |
| `services/agent/tests/unit/test_reply_pipeline_safety_fallback.py` | 23 passed |
| `firmware/esp32/tests/test_memoria_usb_command.py` | 26 passed |
| `firmware/esp32/tests/test_memoria_bench_serial.py` | 95 passed |
| `firmware/esp32/tests/test_memoria_keep_awake.py` | 8 passed |

## Limits

`或不` with optional `了` also matches a longer phrase that merely starts with `不想或`, such as `我不想或者去学校`. `货` with optional `了` matches `我不想货比三家`. Those sentences are not product negatives in this change.

`sleep` and `poweroff` cannot remove USB power. This VM did not flash a board, so codec re-enable on the next `wake` is unmeasured.
