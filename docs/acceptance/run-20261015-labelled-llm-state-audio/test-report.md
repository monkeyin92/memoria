# Test report: restore labelled speech path / TTS fallback

Date: 2026-10-15

## Command

```bash
uv run pytest --no-cov \
  services/common/tests/test_labelled_lines.py \
  services/agent/tests/unit/test_media_session_llm_thinking_filler.py \
  services/agent/tests/unit/test_phrase_segmenter.py \
  services/agent/tests/unit/test_provider_adapter.py \
  firmware/esp32/tests/test_memoria_offline_menu.py -q
```

## Result

| Case | Outcome |
|---|---|
| Scaffold provenance rows are not loaded into the play catalogue | pass |
| Synthetic tone + scaffold provenance still excluded from play | pass |
| Doubao provenance + AIGC ogg + s16le loads for play | pass |
| Thinking filler still works with override catalogue (no TTS) | pass |
| Offline pack: 25 production / 2 pending AIGC placeholders restored | pass |
| Chunk join does not emit doubled 「。。」 | pass |

See also `verification-restore.md`.
