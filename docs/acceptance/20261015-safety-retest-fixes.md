# Safety retest fix results (20261015)

Recorded on branch `cursor/safety-retest-fixes-1440`. These checks ran on the VM after the behavior changes. They do not include a device flash.

## Behavior covered

After a fixed safety script has been spoken, a later turn whose planner returns `request_or_payload_invalid` does not speak a local-safe chat reply, including an identity sentence. A later turn that receives a successful planner plan still speaks. Echo of that script stays `assistant_echo` after the 10 second playback window and after the playback floor text changes. A different child sentence is still accepted.

Sentence 3 matching is unchanged. The offline TTS→FunASR harness (`scripts/reproduce_safety_sentence_asr.py`) must print measured ASR text before any slip characters are widened. This VM lacked provider credentials (`missing_provider_credentials`).

USB `sleep` and `poweroff` stop listening without calling `ToggleChatState` on that same state, abort speaking, toggle only connecting or recovering, disable codec output then input, and cancel an offline story generation. Both park in wifi hold with the hold timer disarmed. `poweroff` also stops the station. USB still powers the board. Product USB also accepts `temp` for on-chip Celsius. `CONFIG_MEMORIA_KEEP_AWAKE` stays off by default.

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
uv run pytest -o addopts= --tb=line -q \
  services/common/tests/test_companion_response_safety.py \
  services/common/tests/test_product_acceptance_safety_tiers.py \
  services/agent/tests/unit/test_reply_pipeline_safety_fallback.py \
  scripts/tests/test_reproduce_safety_sentence_asr.py \
  firmware/esp32/tests/test_memoria_usb_command.py \
  firmware/esp32/tests/test_memoria_bench_serial.py \
  firmware/esp32/tests/test_memoria_keep_awake.py
```

## Result

Focused suite passes on this VM after reverting guessed ASR slip widenings.

## Limits

`sleep` and `poweroff` cannot remove USB power. This VM did not flash a board, so codec re-enable on the next `wake` is unmeasured. Raw-audio route deletion is owned by #247, not this branch.
