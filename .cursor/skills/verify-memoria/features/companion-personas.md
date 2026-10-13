# Companion personas

Companion personas lets a user browse the server's built-in companion catalogue and create, list, and delete custom personas with a controlled eleven-field structure. In the miniprogram this backs custom persona editing (`pages/persona-custom`) and persona pickers on `profile`, `home`, and `device`. The 「伙伴」 tab is not this feature: it uses a local catalogue and saves `companion_id` on the profile (see [memory-journal](./memory-journal.md)).

## Sub-features

- `persona-list` returns `builtin` companions plus the account's `custom_personas`.
- `persona-structure` optionally AI-structures free text (may be unavailable offline).
- `persona-create` confirms an immutable custom persona `v1` record.
- `persona-delete` removes a custom persona (`?confirm=true` when in use).

## How to get to it (user POV)

- Open the custom persona page (`pages/persona-custom`); it lists personas (`GET /v1/personas`), optionally structures free text (`POST /v1/personas/structuring`), then confirms (`POST /v1/personas`).
- Delete from the same page (`DELETE /v1/personas/{id}`).
- 「我的」, 「首页」, and 「设备」 also read `GET /v1/personas` for persona pickers.

## Driving it with bin/api

Preconditions:

- `bin/doctor` PASS.
- Registered session in `evidence/<run>/session.env`.

- **List catalogue.** Run `bin/api --token-file evidence/<run>/session.env GET /v1/personas --evidence personas/list-before.json`. HTTP 200; `builtin` ids are `axu`, `mianmian`, `starlight`, `taoxi`, `xuanmo`, `yanxi`, `zhiyao`; `custom_personas`=`[]`.
- **Structuring offline.** Run `bin/api --token-file evidence/<run>/session.env POST /v1/personas/structuring --evidence personas/structuring.json -H 'Content-Type: application/json' -d '{"free_text":"想要一个说话短一点、像朋友一样的伙伴"}'`. Exit 1; HTTP 503 with `detail.code`=`persona_structuring_unavailable`. This is the expected offline result; continue with hand-filled create.
- **Create custom persona.** Run `bin/api --token-file evidence/<run>/session.env POST /v1/personas --evidence personas/create.json -H 'Content-Type: application/json' -H 'Idempotency-Key: verify-persona-1' -d '{"display_name":"验收小北","fallback_designed_voice":"starlight","structured":{"style_description":"温和的陪伴者","warmth":"warm","directness":"gentle","response_length":"brief","question_frequency":"rare","interview_depth":"light","welcome_text":"嗨，我在。","conversation_instruction":"说话短一点，像朋友。","voice_instruction":"轻松自然","default_voice_emotion":"neutral","default_voice_rate":1.0}}'`. HTTP 201; `persona_id` matches `cu_` + 26 hex; `persona_version`=`1`; `owner_person_id`=`$USER_ID`.
- **Idempotent replay.** Repeat the create call unchanged. HTTP 201 with the same `persona_id`. The same key with `display_name`=`别名` returns 409 `idempotency_conflict`.
- **Reject out-of-domain.** Repeat create with `"warmth":"甜"`. HTTP 422 with `detail`=`{"code":"persona_structuring_invalid","field":"warmth"}`.
- **List after create.** Run `bin/api --token-file evidence/<run>/session.env GET /v1/personas --evidence personas/list-after.json`. HTTP 200; `custom_personas` contains exactly the new `persona_id`.
- **Delete.** Run `bin/api --token-file evidence/<run>/session.env DELETE "/v1/personas/$PERSONA_ID" --evidence personas/delete.json`. HTTP 200 with `{"deleted":true,"drifted_subjects":0}`. Deleting again returns 404 `persona_not_found`.
- **Proof.** Final list shows `custom_personas`=`[]`; retain create/replay/list/delete JSON.

## Gotchas

- Out-of-domain enum values (e.g. `warmth`=`甜`) return 422 `persona_structuring_invalid` — never clamped.
- Max five custom personas per account; 409 `custom_persona_limit_reached` beyond that.
- Idempotency-Key is optional (the miniprogram never sends it), user-scoped, ≤ 64 chars; an empty or over-long key is 409 `idempotency_conflict`.
- A referenced persona returns 409 `persona_in_use` on delete without `?confirm=true`; a fresh verify account has no device assignments, so this path is not reachable here.
- With `MEMORIA_IDENTITY_DATABASE_URL` empty the identity store is in-memory: custom personas vanish when the instance restarts.
- The server's 7 built-ins differ from the miniprogram's local 5-entry catalogue (`apps/miniprogram/utils/companions.js`); assert on the server list.
- Device persona assignment (`/v1/devices/{id}/persona-assignments`) needs a bound device — not covered by this feature file.
