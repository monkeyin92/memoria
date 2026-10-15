# AIGC labels on device audio and live dialogue

Status. Architect Phase B synthesis for 《人工智能生成合成内容标识办法》 art.4 / GB 45438-2025 on the ESP-VoCat pack and live dialogue chrome.

## Problem

Shipped Ogg Opus clips must carry implicit AIGC metadata. Live dialogue needs an explicit 「AI」 signal while the face stays readable. Offline story and bedtime play with the screen dimmed, so a spoken intro must precede those clips and must not be skippable.

## Usage (caller's view)

```bash
# Pack gate (check-overlay / CI) — fails if any ogg lacks tags or intros are absent
python firmware/esp32/scripts/build_offline_stories.py --check-only

# Metadata-only retag of the 25 #244 production clips (audio packets unchanged)
python scripts/retag_offline_aigc_metadata.py

# Generate spoken intros when Doubao credentials exist
DOUBAO_TTS_APP_ID=... DOUBAO_TTS_ACCESS_TOKEN=... \
  uv run python scripts/render_aigc_notices.py
```

```cpp
// Live dialogue: badge follows conversation phases on MemoriaMascotDisplay.
// Offline story long-press prepends aigc_story_intro; night soothe requires
// aigc_soothe_intro. Missing intro refuses the story/soothe path.
```

## Shape

Domain model is an **AIGC clip policy registry** (required Vorbis keys + required intro ids) plus an **explicit label mode** enum on the device (`kBadge` / `kTurnTone` / `kNone`).

| Concern | Owner |
|---|---|
| Implicit tags + pack fail | `services/common/aigc_opus.py` + `build_offline_stories.py` |
| Metadata-only retag | `scripts/retag_offline_aigc_metadata.py` |
| Intro TTS recipe | `scripts/render_aigc_notices.py` (no fabricated audio) |
| Story/bedtime prepend + refuse | `memoria_offline_menu.h` + board path |
| Live badge | LVGL chrome on `MemoriaMascotDisplay` |
| Turn-tone fallback | `kAigcTurnToneEnabled` default false in `memoria_aigc_label.h` |

Required Vorbis comments: `CONTENT_ID`, `SERVICE_PROVIDER` (`Volcengine Doubao` on Doubao clips), `AI_GENERATED`, `ARTIST` (voice id), `DATE`, `CONTENT_PRODUCER`. Provider-embedded marks are never stripped.

## Synthesis decision

Candidate A (LVGL corner badge + pack policy registry + notice prepend in the existing clip pipeline) is the base.

From Candidate B (scene-raster badge + OfflineMenu-only notices): keep the insight that story notice belongs in `OfflineMenuMachine` clip order so the board shell stays dumb. Reject raster-composited 「AI」 text (no CJK glyph path in the software renderer; LVGL already owns chrome text).

## Tradeoffs accepted

- We accept a constexpr turn-tone flag instead of a new Kconfig option, because check-overlay freezes Memoria Kconfig to bench/keep-awake only.
- Spoken intros block pack CI and runtime playback until Doubao-rendered oggs are copied in (no skip-if-missing, no fabricated audio).

## Alternatives considered

1. Scene-raster badge drawn into the framebuffer. Lost: no font atlas for 「AI」 in the mascot compositor; would obscure face pixels or need a new glyph pack.
2. Per-session spoken disclosure at conversation start. Dropped by product: primary signal is the persistent corner badge; session speech is not required.
3. Leave the 25 production oggs unlabelled under an audit-exception list. Rejected by compliance: retag metadata-only with verified bit-identical audio packets.

## Open questions and risks

- Operators drop Doubao-rendered `aigc_story_intro.ogg` / `aigc_soothe_intro.ogg` into the pack (or `/cursor/stores/bot/aigc-intros/` for the agent to commit).

## Next implementation step

Land retagged pack + required intros; keep badge geometry as product-approved.
