# Bailian decision-model-preview vs turn classifiers (offline, 2026-10-15)

## Verdict

**Do not replace** the production crisis path with `decision-model-preview`. Keep `companion_safety_decision` / `route_crisis` in front; any model is additive evidence only.

**Supplement is plausible** for live-lookup and conversation-close only, and only after a gated shadow period. Prior production-server measurements (2026-10-03) already showed the decision model is much faster than DeepSeek/qwen-flash pair calls but slightly less accurate on close-intent. This run ships the offline harness and a flagged-off adapter. Live dual-run numbers for this date are **blocked** (no `DASHSCOPE_API_KEY` in the agent environment). No accuracy, latency, or cost figures were fabricated.

## Compliance (non-negotiable)

Crisis detection **must never rely solely** on the preview decision model.

- Deterministic safety (`companion_safety_decision`, fixed scripts including hotline **12356**) stays in front or alongside.
- The adapter in this PR is **additive only**, `enabled=False` by default, and is **not** wired into duplex / control_api production paths.
- Eval reports composite crisis recall (rules + optional evidence). Model-alone crisis metrics are diagnostic and must not gate a ship decision.

This matches the existing contract in `services/common/crisis_policy.py` (`route_crisis` refuses to let a model verdict execute alone) and the prior TODOLIST N-14 note that crisis classifiers stay untouched for cascade work.

## Where each judgment lives today

### 1. Intent classification (conversation close)

| Item | Location |
| --- | --- |
| Semantic classifier | `services/agent/src/providers/close_intent_semantic_classifier.py` (`CloseIntentSemanticClassifier`, default model `qwen-flash`, timeout 1.2 s) |
| Prompt | module `_SYSTEM_PROMPT` (enum `END_SESSION` / `CONTINUE` / `UNSURE`) |
| Wiring | `services/agent/src/conversation_close_wiring.py` → `DuplexRuntime.set_conversation_close_semantic_resolver` |
| Rule fast path | `services/agent/src/conversation_close_router.py` (`is_conversation_close_only`, lexical stop/ack beats semantic) |
| Config | `CONVERSATION_CLOSE_SEMANTIC_*` in `services/agent/src/config.py` |
| Latency path | Started at ASR final / endpoint pin; commit joins in-flight verdict via `classifier_inflight` (not a second call). Blocks commit only when the verdict is still pending after follow-up grace. |
| Endpoint | `services/agent/src/semantic_endpoint.py` (follows `LLM_PROVIDER`; DeepSeek uses `DEEPSEEK_FAST_MODEL`, else DashScope + configured model) |

Note: broader CHAT / INTERRUPT routing is rule-based in `utterance_router.py`, not this model call. The semantic “intent” stage in scope is **end-session** only. Production example env sets `LLM_PROVIDER=deepseek`, so the live close/live classifiers often hit `deepseek-flash`, not `qwen-flash`. The offline harness’s optional qwen arm is still useful as a DashScope baseline.

### 2. Danger-topic grading / crisis detection

| Item | Location |
| --- | --- |
| Deterministic front door | `services/common/companion_response_safety.py` → `companion_safety_decision` |
| Execution policy | `services/common/crisis_policy.py` → `route_crisis` (model cannot execute alone) |
| Fixed scripts (12356) | `services/common/fixed_safety_scripts.py` (`crisis-self-v1` speaks 心理援助热线 **12356**) |
| Bounded semantic evidence | `services/agent/src/providers/crisis_semantic_classifier.py` (`qwen-flash`, 1.2 s, enum only) |
| Call site | `services/control_api/app/routes/interaction.py` `_route_crisis_with_bounded_evidence` |
| Build | `services/control_api/app/text_models.py` `build_crisis_semantic_classifier` (flag `crisis_semantic_enabled`) |
| Candidate gate | `crisis_semantic_candidate` (first-person risk anchor + deterministic miss) |

Latency path: awaited inside Control API `POST /response-plan` only when deterministic is `none` and `crisis_semantic_candidate` is true. It is **not** on the voice `prepare_turn` gather with close/live. Timeout / error → `UNSURE` → no crisis action. On device sessions the spoken line can still come from the regex script while the background response-plan POST may enqueue a guardian alert from semantic evidence.

Parent notify today is **not** tied to classifier confidence. The three production classifiers return enums, not scores. `UNSURE` / timeout never notifies. Guardian notify runs only from `CrisisRoute.notify_guardian` inside response-plan (`interaction.py`). A future “low confidence → parent notify” idea would be new product behavior, not a drop-in for this adapter.

### 3. Pre-web-search (live lookup) decision

| Item | Location |
| --- | --- |
| Semantic classifier | `services/agent/src/providers/live_lookup_semantic_classifier.py` (`qwen-flash`, 1.2 s) |
| Prompt | `_SYSTEM_PROMPT` (`NEEDS_LIVE_LOOKUP` / `NO_LIVE_LOOKUP` / `UNSURE`) |
| Wiring | `services/agent/src/live_lookup_wiring.py` |
| Keyword fast path | `services/agent/src/live_lookup_router.py` + `services/common/realtime_information.py` |
| Safety short-circuit | Any non-`none` companion safety decision forces live-lookup false |
| Config | `LIVE_LOOKUP_SEMANTIC_*` in `services/agent/src/config.py` |

Latency path: same inflight-cache pattern as close-intent; keyword hits skip the model.

## What this PR adds (default off)

| Artifact | Role |
| --- | --- |
| `services/agent/src/providers/decision_model.py` | System One client + three-question turn bundle. `DecisionModelConfig.enabled` defaults **False**. |
| `services/agent/evaluation/decision_model_offline_cases.json` | 267 synthetic cases: prior live/close sets + safety fixtures (no real child data). 9 labeled `must_route_12356`. |
| `services/agent/evaluation/decision_model_scoring.py` | Pure metrics, including composite crisis 12356 recall. |
| `scripts/run_decision_model_offline_eval.py` | Dry-run by default; live dual-run only with `DECISION_MODEL_EVAL_LIVE=1` plus keys. |
| `services/agent/tests/unit/test_decision_model.py` | Adapter guard, parse helpers, rule-wins-over-model invariant. |

No duplex or control_api installer calls the new client.

## Offline eval set

Built from:

- `docs/acceptance/run-20261003-decision-model/dataset-dev.json` (157) and `dataset-heldout.json` (79)
- `services/common/tests/test_product_acceptance_safety_tiers.py`
- `services/common/tests/test_companion_response_safety.py`
- `services/common/tests/test_crisis_policy.py` (semantic-candidate ambiguous line)

Crisis cases that **must** route to the fixed `crisis-self-v1` script (hotline 12356) are tagged `must_route_12356: true`.

## Live metrics (this environment)

**Blocked.** `DASHSCOPE_API_KEY` was empty in the cloud agent env. The harness dry-run confirms fixture load and rule-front-door behavior only.

To run live later (Beijing workspace endpoint; keys stay on the operator host / bridge container):

```bash
export DECISION_MODEL_EVAL_LIVE=1
export DASHSCOPE_API_KEY=...
export DECISION_MODEL_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone
python scripts/run_decision_model_offline_eval.py --out /tmp/dm-eval.json
```

Gates to report from that run (do not invent them here):

- live-lookup / close-intent accuracy vs qwen-flash (and DeepSeek if configured)
- crisis **composite** 12356 recall (must be **100%** of the rule baseline on explicit `crisis-self-*` fixtures; no regression)
- false-positive rate on close-intent and on crisis negatives
- wall-clock p50 / p95 for the one-call turn bundle vs parallel qwen pair
- mean `input_tokens` for cost estimates

### Prior measured baseline (cite only; not re-run here)

From `docs/acceptance/run-20261003-decision-model/findings.md` (production bridge container, synthetic sentences, 2026-10-03):

- Decision model one-request two-question pair cold p50 ≈ **169 ms** (qwen-flash pair ≈ 378 ms, DeepSeek ≈ 651 ms)
- Close-intent held-out accuracy with choice wording (v2) at thr 0.5 ≈ **96.2%** (DeepSeek 100%)
- Crisis classifiers were **not** measured in that run on purpose

## Estimated cost per call (official pages)

| Model | Published price | Source |
| --- | --- | --- |
| `decision-model-preview` | Listed as **限时免费** (time-limited free) for 华北2（北京） and 新加坡. No post-preview unit price published on the model card. | [decision-model-preview](https://help.aliyun.com/zh/model-studio/decision-model-preview), [Token Plan overview](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview) (“调用不消耗 Credits”) |
| `qwen-flash` (≤128K) | Input **0.15 元 / 百万 tokens**, output **1.5 元 / 百万 tokens** (北京) | [Model pricing](https://help.aliyun.com/zh/model-studio/model-pricing), [qwen-flash](https://help.aliyun.com/zh/model-studio/qwen-flash) |

API shape: [Decision model API](https://docs.bailian.console.aliyun.com/zh/model-studio/decision-model-api) / [help.aliyun.com mirror](https://help.aliyun.com/zh/model-studio/decision-model-api). Beijing endpoint form: `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone`. SDK hint: `pip install typesafe-sdk`.

Until a non-preview price is published, treat decision-model cost as **unknown after the free window**; do not budget production traffic on “free forever.”

### Auth and gateway notes (from official pages)

- API reference uses workspace host `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone` and `Authorization: Bearer $DASHSCOPE_API_KEY` ([decision-model API](https://docs.bailian.console.aliyun.com/zh/model-studio/decision-model-api)).
- The 2026-10-03 acceptance scripts and a tuning-guide FAQ also mention a raw-key header and, in places, the DashScope host. This harness follows the API reference (`Bearer`). If a live run returns 401, try the account’s known-good header from that prior bridge setup before changing code.
- Token Plan personal edition says the key must not drive automated scripts; an offline eval is a script. Prefer the pay-as-you-go / model-card free window path for harness runs ([Token Plan overview](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)).

## Recommendation by stage

| Stage | Fit | Change size | Main risks |
| --- | --- | --- | --- |
| Live-lookup | **Best supplement candidate.** Keyword path already covers many hits; model only for ambiguous turns. One System One call can carry this with close-intent. | Small if shadow-only; medium for cascade (new wiring + thresholds + metrics). | Chinese short-utterance bias; false live lookups waste search latency. |
| Close-intent | **Supplement only, high precision required.** False END_SESSION puts the device on standby. Prior eval needed thr ≈ 0.9 and still trailed DeepSeek. | Same as above. | Mis-standby UX; preview model + data-processing terms for child speech. |
| Crisis / danger | **Do not replace. Additive evidence only.** Keep rules + fixed 12356 script. Optional: use decision-model choice as `semantic_evidence` under the same `crisis_semantic_candidate` gate that qwen uses today, never alone. | Small for offline eval (done). Production swap of the crisis classifier: **out of scope / refuse** without a separate safety review. | Adversarial / fiction bleed; any sole-model miss is a child-safety incident. Quoted self-harm text can still match the first-person risk anchor, so a model `SELF_CRISIS` without the deterministic fiction filter would fire the fixed script. That is why rules stay in front. |

### Suggested sequence (unchanged in spirit from TODOLIST N-14)

1. Keep this harness; run live when a key is available; freeze thresholds before looking at held-out.
2. Shadow mode for live+close only (operator must accept data egress of utterance text to the preview endpoint).
3. Cascade only if commit-path `close_ms` data shows the classifier is actually on the critical path.
4. Crisis stays rule-first forever in this design; preview model may only duplicate today’s bounded evidence role.

## Change size of this PR

Scaffold only: adapter + eval set + scoring + dry harness + unit tests + this report. **Zero default behavior change** in production runtimes.
