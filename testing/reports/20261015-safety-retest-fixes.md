# Test report: safety retest fixes (20261015)

Branch `cursor/safety-retest-fixes-1440` off `c262de1a`.

## Scope

| Item | Approach |
| --- | --- |
| Sentences 2/6 post-safety extra turn | Root-cause doc first; suppress local-safe LLM fallback after fixed safety; keep echo reject for fixed-safety scripts past 10s; do not reject non-echo child speech |
| Sentence 3 「我不想或了」 | No guessed slip characters. Offline TTS→FunASR harness only. Matcher unchanged until the harness prints measured text |
| USB `sleep` / `poweroff` / `temp` | Product serial verbs; wifi hold without chip deep sleep; `temp` logs on-chip Celsius; USB still powers |
| Raw-audio route | Out of scope here — deletion owned by another agent |

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
.venv/bin/python -m pytest \
  services/agent/tests/unit/test_reply_pipeline_safety_fallback.py \
  services/common/tests/test_product_acceptance_safety_tiers.py \
  services/common/tests/test_companion_response_safety.py \
  scripts/tests/test_reproduce_safety_sentence_asr.py \
  firmware/esp32/tests/test_memoria_usb_command.py \
  firmware/esp32/tests/test_memoria_bench_serial.py \
  firmware/esp32/tests/test_memoria_keep_awake.py \
  firmware/esp32/tests/test_memoria_protocol_source.py::test_network_disconnect_actively_recovers_the_same_session \
  -q
```

Live ASR harness (needs credentials):

```bash
uv run python scripts/reproduce_safety_sentence_asr.py
```

## Result (this VM)

- Removed `MEMORIA_LOG_FINAL_ASR_TEXT` entirely (no device ASR text logging).
- Dropped the raw-audio config-switch work from this branch (route deletion is elsewhere).
- Offline harness `scripts/reproduce_safety_sentence_asr.py` exit **2**: `missing_provider_credentials` (`DASHSCOPE_API_KEY`, Doubao TTS). Sentence 3 recognized text was **not** obtained. Matcher not widened.
- Harness unit tests pass.

## Notes

- Parent/child-facing copy was not given new 「危机」 wording.
- To finish sentence 3, rerun the harness in a lab/bridge environment that has those provider secrets.
