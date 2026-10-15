# Test report: safety retest fixes (20261015)

Branch `cursor/safety-retest-fixes-1440` off `c262de1a`.

## Scope

| Item | Approach |
| --- | --- |
| Sentences 2/6 post-safety extra turn | Root-cause doc first; suppress local-safe LLM fallback after fixed safety; keep echo reject for fixed-safety scripts past 10s; do not reject non-echo child speech |
| Sentence 3 「我不想或了」 | No guessed slip characters. Offline TTS→FunASR harness only. Matcher unchanged until the harness prints measured text |
| USB `sleep` / `poweroff` | Product serial verbs; wifi hold, deep sleep not entered; USB still powers |
| Raw-audio route | `MEMORIA_ARCHIVE_SESSION_RAW_AUDIO_ENABLED` default false; route code kept |

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
  services/control_api/tests/test_archive_api.py::test_session_raw_audio_route_is_disabled_by_default \
  -q
```

Live ASR harness (needs credentials):

```bash
uv run python scripts/reproduce_safety_sentence_asr.py
```

## Result (this VM)

- Removed `MEMORIA_LOG_FINAL_ASR_TEXT` entirely (no device ASR text logging).
- Offline harness `scripts/reproduce_safety_sentence_asr.py` exit **2**: `missing_provider_credentials` (`DASHSCOPE_API_KEY`, Doubao TTS). Sentence 3 recognized text was **not** obtained. Matcher not widened.
- Harness unit tests pass.

## Notes

- Parent/child-facing copy was not given new 「危机」 wording.
- To finish sentence 3, rerun the harness in a lab/bridge environment that has those provider secrets.
