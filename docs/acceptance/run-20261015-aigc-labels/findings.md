# AIGC labels — findings

## Inventory (measured)

| Surface | Explicit label today | Change in this PR |
|---|---|---|
| Live dialogue (screen on, tap-to-wake) | None on device chrome | Persistent 「AI」 corner badge |
| Offline story / bedtime (screen dimmed) | None | Required spoken intro (`aigc_story_intro` / `aigc_soothe_intro`) |
| Mini Program | 「回应由 AI 生成」 on auth/home/consent | Unchanged (firmware-only PR) |
| First-boot spoken disclosure | Not present as a device line in the offline pack | Not added (product dropped per-session speech) |

## Implicit tags

- Required Vorbis keys: `CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED`, `ARTIST`, `DATE`, `CONTENT_PRODUCER`.
- Pack gate: `build_offline_stories.py` fails when **any** shipped clip lacks them (no audit-exception list).
- The 25 #244 production oggs were retagged **metadata-only** (`scripts/retag_offline_aigc_metadata.py`); audio-packet and ffmpeg-PCM SHA-256 unchanged. Table: `metadata-retag-25.json`.
- `SERVICE_PROVIDER=Volcengine Doubao`, `ARTIST=zh_female_tianmeitaozi_uranus_bigtts`, `DATE=2026-10-14`, `CONTENT_PRODUCER=Memoria`.
- `llm_*` placeholders carry the full implicit set (still `TITLE=*_placeholder`; not Doubao speech).
- Spoken intros are required in the pack; CI fails if absent. No fabricated audio.

## Explicit labels

- Primary: LVGL 「AI」 badge, indigo `#4A52C9` at 100% opacity, ~20px Montserrat, layout `228,40,50×30` (~1 o'clock). Every corner stays within radius 172 (`static_assert` + host test).
- Visible while listening / thinking / speaking / connecting, and while offline menu is active (stuck → menu), with the panel kept lit.
- Fallback: end-of-turn tone behind `kAigcTurnToneEnabled` (default off), only when the badge cannot render.
- Mockups: `docs/acceptance/run-20261015-aigc-labels/mockups/` (includes `badge-stuck-offline-menu.png`).

## Verification

Host tests under `firmware/esp32/tests/test_memoria_aigc_label.py` and extended offline menu / pack checks.
