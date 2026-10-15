# Test report: safety retest fixes (20261015)

Branch `cursor/safety-retest-fixes-1440` off `c262de1a`.

## Scope

| Item | Approach |
| --- | --- |
| Sentences 2/6 post-safety extra turn | Root-cause doc first; suppress local-safe LLM fallback after fixed safety; keep echo reject for fixed-safety scripts past 10s; do not reject non-echo child speech |
| Sentence 3 「我不想或了」 | No guessed slip characters. TTS→FunASR harness + opt-in `MEMORIA_LOG_FINAL_ASR_TEXT` (default off). Matcher unchanged until measured text exists |
| USB `sleep` / `poweroff` | Product serial verbs; wifi hold, deep sleep not entered; USB still powers |
| Raw-audio route | `MEMORIA_ARCHIVE_SESSION_RAW_AUDIO_ENABLED` default false; route code kept |

## Commands

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /workspace
.venv/bin/python -m pytest \
  services/agent/tests/unit/test_reply_pipeline_safety_fallback.py \
  services/agent/tests/unit/test_config.py::test_log_final_asr_text_defaults_off \
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

Focused suite: **204 passed**, **2 skipped** (bench-only paths / optional).

Live ASR harness: exit 2 (`missing_provider_credentials`). No matcher widening for sentence 3 in this PR.

## Notes

- This VM has no `DASHSCOPE_API_KEY` / Doubao TTS credentials; harness exits 2 with a documented hint.
- Parent/child-facing copy was not given new 「危机」 wording.
