# Memory decision and save-receipt contract

Reference for A01. Code source of truth:
`services/common/memory_decision_contract.py`.

## Why this exists

Spoken promise language, catalog writes, child-layer extract, parent display,
and companion recall used different words for the same outcomes. Callers could
treat async enqueue or `will_save=True` as "saved", and auto-extract could be
described as confirmed with confidence 1.0. This contract names one decision
shape and one receipt shape those seams must share.

## Lifecycles

| Lifecycle | Meaning |
| --- | --- |
| `not_allowed` | Policy forbids durable write. |
| `eligible_unwritten` | Allowed, nothing durable yet (includes async accepted). |
| `pending_confirm` | Durable candidate awaiting parent or policy confirm. |
| `confirmed` | Durable and confirmed. Ordinary companion recall may use it. |
| `save_failed` | Write attempted and failed. |
| `withdrawn` | Previously held fact withdrawn. |
| `deleted` | Erased. |

Catalog `MemoryStatus` (`candidate` / `confirmed` / `disputed` / `retracted`)
still owns storage rows. Map `pending_confirm` ↔ `candidate` and keep
`confirmed` aligned. Do not invent a second catalog enum.

## Provenance

`explicit_remember`, `auto_extract`, `parent_entry`, `parent_confirm`.

Illegal:

- `auto_extract` + lifecycle `confirmed`
- `auto_extract` + `confidence >= 1.0` (uncalibrated extract must not claim
  accuracy 1.0)

Use `decide_auto_extract(...)` for extract births.

## Save receipts

| `write_mode` | `may_claim_saved` | `may_claim_remembered_aloud` |
| --- | --- | --- |
| `sync_persisted` | Yes, when lifecycle is `pending_confirm` or `confirmed` and `record_id` is set | Only when lifecycle is `confirmed` and provenance is not `auto_extract` |
| `async_accepted` | No | No |
| `not_written` | No | No |

Async ≠ saved. A turn may keep processing after the reply, but only a
`sync_persisted` receipt may support "saved" or "I remembered".

## Promise matrix

`PRODUCT_PROMISE_MATRIX` encodes product-visible cases. Key rows:

| example_id | Utterance | will_save (minor) | Promise |
| --- | --- | --- | --- |
| `blue-closed-vocab` | 帮我记住我最喜欢蓝色。 | True | May claim remembered after sync |
| `dinosaur-not-allowlisted` | 帮我记住我喜欢恐龙。 | False | Must not claim remembered |
| `auto-extract-preference` | 我喜欢画画。 | False | Must not claim remembered; pending_confirm only |
| `async-not-saved` | Same as blue, async path | True | Acknowledge only until sync |

## Display and recall

- `pending_confirm` → parent review; recall only after confirm
- `confirmed` → parent visible; companion recall when provenance is not
  auto-extract at birth (parent confirm promotes provenance)
- other lifecycles → hidden / no recall

Child-layer archive facts and catalog claims both consume this vocabulary in
follow-up cards (A02/A03/A04/A10). This document does not change extractors.

## Contract version

`CONTRACT_VERSION = memory-decision-v1`
