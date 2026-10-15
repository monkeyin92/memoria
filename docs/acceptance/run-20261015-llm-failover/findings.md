# 2026-10-15 LLM failover（软件门）

Feature flag `LLM_FAILOVER_ENABLED` defaults off. Primary DeepSeek; when on, first-token timeout cancels and retries the same turn on Beijing DashScope `qwen3.8-flash` with `enable_thinking=false`. Mid-stream stall after the first token does **not** switch to qwen; it ends the turn and speaks the fixed `llm_unavailable` line. Both pre-content failures use the same closing path. No real-device dialogue in this run.

## Result

| Case | Outcome |
|---|---|
| Flag off returns the bare primary model | pass |
| Timeout → backup with byte-identical messages | pass |
| Mid-stream stall → `LlmUnavailableError`, no backup stitch | pass |
| Both down → `LlmUnavailableError` / fixed copy | pass |
| Backup forbidden-copy gate | pass |
| Beijing URL only when DashScope is in use (agent + control-api) | pass |
| DeepSeek primary + flag off skips backup URL validation | pass |
| DeepSeek thinking body exactly `{"thinking":{"type":"disabled"}}` | pass |
| Qwen reply body has `enable_thinking=false` | pass |
| Offline `OnLlmUnavailable(play_line=false)` enters menu while online | pass |
| Pending `llm_unavailable.ogg` placeholder with Appendix-E AIGC tags | pass |

Command:

```bash
uv run pytest --no-cov \
  services/agent/tests/unit/test_llm_failover.py \
  services/agent/tests/unit/test_semantic_endpoint.py \
  services/control_api/tests/test_text_models.py \
  firmware/esp32/tests/test_memoria_offline_menu.py -q
```

All cases green. Module budgets and ruff on the touched Python paths pass. No production env change. `direct_real_device_verified` stays false.

## Enable checklist (unchecked)

- [ ] Operator chooses DeepSeek primary and backup model id `qwen3.8-flash`
- [ ] 合规/备案 review signed for Bailian backup path
- [ ] Privacy copy shows 「DeepSeek，故障时切到通义千问」
- [ ] `DASHSCOPE_API_KEY` belongs to a pay-as-you-go (按量付费) account, not Token Plan / Coding Plan
- [ ] About page shows qwen filing number `ZheJiang-TongYiQianWen-20230901` when the flag is enabled
- [ ] Prompt/safety passthrough tests green on the release candidate
- [ ] Forbidden-words gate exercised on a live backup sample
- [ ] `LLM_FAILOVER_ENABLED=true` with `LLM_FIRST_TOKEN_TIMEOUT_S=5`, `LLM_INTER_TOKEN_TIMEOUT_S=8`, and Beijing `DASHSCOPE_COMPATIBLE_BASE_URL` only
