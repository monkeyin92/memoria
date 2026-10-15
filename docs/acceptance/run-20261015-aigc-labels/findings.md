# AIGC labels — findings

## Inventory (measured)

| Surface | Explicit label today | Change in this PR |
|---|---|---|
| Live dialogue (screen on, tap-to-wake) | None on device chrome | Persistent 「AI」 corner badge |
| Offline story / bedtime (screen dimmed) | None | Spoken notice prepend (pending TTS) |
| Mini Program | 「回应由 AI 生成」 on auth/home/consent | Unchanged (firmware-only PR) |
| First-boot spoken disclosure | Not present as a device line in the offline pack | Not added (product dropped per-session speech) |

## Implicit tags

- Required Vorbis keys: `CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED`, `ARTIST`, `DATE`, `CONTENT_PRODUCER`.
- Pack gate: `build_offline_stories.py` fails when a non-excepted clip lacks them.
- `PENDING_AIGC_AUDIT_EXCEPTIONS`: 25 production oggs left untouched.
- `llm_*` placeholders carry the full implicit set.
- Notice clips: `PENDING_TTS_GENERATION` until `scripts/render_aigc_notices.py` runs with Doubao credentials. No fabricated audio.

## Explicit labels

- Primary: LVGL 「AI」 badge, indigo `#4A52C9`, top-right, while listening / thinking / speaking / connecting.
- Fallback: end-of-turn tone behind `kAigcTurnToneEnabled` (default off), only when the badge cannot render.
- Mockups: `docs/acceptance/run-20261015-aigc-labels/mockups/`.

## Verification

Host tests under `firmware/esp32/tests/test_memoria_aigc_label.py` and extended offline menu / pack checks.
