# Test report: labelled online `llm_thinking` / `llm_unavailable`

Date: 2026-10-15

## Command

```bash
uv run pytest --no-cov \
  services/common/tests/test_labelled_lines.py \
  services/agent/tests/unit/test_media_session_llm_thinking_filler.py \
  services/agent/tests/unit/test_provider_adapter.py \
  services/agent/tests/unit/test_llm_failover.py \
  firmware/esp32/tests/test_memoria_offline_menu.py -q
```

## Result

| Case | Outcome |
|---|---|
| Shipped online ogg assets embed Appendix-E AIGC tags | pass |
| Catalogue rejects clips missing `AI_GENERATED` | pass |
| Slow reply plays filler PCM with `generate_output_calls == 0` and AIGC flag | pass |
| Thinking timing: 3s from voiced end, once/turn, skipped when reply starts first | pass |
| `generate_reply` for both fixed lines: `stream_calls == 0`, AIGC retained | pass |
| `generate_output(tts_source=llm_unavailable)` skips streaming TTS | pass |
| Offline pack: 27 production clips, 0 placeholders; both lines AIGC-labelled | pass |
| LLM failover unit suite still green | pass |

All listed cases green. Ruff clean on touched Python paths.
