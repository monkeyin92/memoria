# 2026-10-15 LLM failover（软件门）

Feature flag `LLM_FAILOVER_ENABLED` defaults off. Primary DeepSeek; when on, first-token timeout cancels and retries the same turn on Beijing DashScope `qwen3.8-flash` with `enable_thinking=false`. Both down speak the fixed line and close with reason `llm_unavailable` into the offline story menu. No real-device dialogue in this run.

## Result

| Case | Outcome |
|---|---|
| Flag off returns the bare primary model | pass (`test_flag_off_build_returns_primary_only`) |
| Timeout → backup with byte-identical messages | pass (`test_first_token_timeout_retries_identical_messages_on_backup`) |
| Both down → `LlmUnavailableError` / fixed copy | pass |
| Backup forbidden-copy gate | pass |
| Beijing URL enforced; intl/us rejected | pass |
| Backup model locked to `qwen3.8-flash`; thinking off | pass |
| Three-tier fixed safety scripts model-independent | pass |
| Offline `OnLlmUnavailable(play_line=false)` enters menu while online | pass |
| Pending `llm_unavailable.ogg` placeholder with Appendix-E AIGC tags | pass (`pending_placeholder_clips=1`) |

Command:

```bash
uv run pytest --no-cov \
  services/agent/tests/unit/test_llm_failover.py \
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
- [ ] `LLM_FAILOVER_ENABLED=true` with `LLM_FIRST_TOKEN_TIMEOUT_S=5` and Beijing `DASHSCOPE_COMPATIBLE_BASE_URL` only
