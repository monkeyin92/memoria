# AIGC labels on device audio and live dialogue

Status. Architect Phase B synthesis for 《人工智能生成合成内容标识办法》 art.4 / GB 45438-2025 on the ESP-VoCat pack and live dialogue chrome.

## Problem

Shipped Ogg Opus clips must carry implicit AIGC metadata. Live dialogue needs an explicit 「AI」 signal while the face stays readable. Offline story and bedtime play with the screen dimmed, so a spoken notice must precede those clips. Twenty-five existing production oggs stay untouched pending provenance audit.

## Usage (caller's view)

```bash
# Pack gate (check-overlay / CI)
python firmware/esp32/scripts/build_offline_stories.py --check-only

# Generate spoken notices when Doubao credentials exist
uv run python scripts/render_aigc_notices.py
```

```cpp
// Live dialogue: badge follows conversation phases on MemoriaMascotDisplay.
// Offline story long-press already emits start_story; the machine prepends
// aigc-notice-story before the story file. Night soothe plays
// aigc-notice-bedtime then night_soothe.
```

## Shape

Domain model is an **AIGC clip policy registry** (one table of required Vorbis keys, pending-audit exceptions, pending-TTS line ids) plus an **explicit label mode** enum on the device (`kBadge` / `kTurnTone` / `kNone`).

| Concern | Owner |
|---|---|
| Implicit tags + pack fail | `services/common/aigc_opus.py` + `build_offline_stories.py` |
| Notice TTS recipe | `scripts/render_aigc_notices.py` (no fabricated audio) |
| Story/bedtime prepend | `memoria_offline_menu.h` + night-soothe board path |
| Live badge | LVGL chrome on `MemoriaMascotDisplay` |
| Turn-tone fallback | `kAigcTurnToneEnabled` default false in `memoria_aigc_label.h` |

Required Vorbis comments: `CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED`, `ARTIST` (voice id), `DATE`, `CONTENT_PRODUCER`. Provider-embedded marks are never stripped.

## Synthesis decision

Candidate A (LVGL corner badge + pack policy registry + notice prepend in the existing clip pipeline) is the base.

From Candidate B (scene-raster badge + OfflineMenu-only notices): keep the insight that story notice belongs in `OfflineMenuMachine` clip order so the board shell stays dumb. Reject raster-composited 「AI」 text (no CJK glyph path in the software renderer; LVGL already owns chrome text).

## Tradeoffs accepted

- We accept a pending-audit exception list for the 25 unlabelled production clips so the gate can land without rewriting audited audio.
- We accept missing notice oggs until Doubao credentials run the render script; playback skips a missing clip and still plays the story or soothe.
- We accept a constexpr turn-tone flag instead of a new Kconfig option, because check-overlay freezes Memoria Kconfig to bench/keep-awake only.

## Alternatives considered

1. Scene-raster badge drawn into the framebuffer. Lost: no font atlas for 「AI」 in the mascot compositor; would obscure face pixels or need a new glyph pack.
2. Per-session spoken disclosure at conversation start. Dropped by product: primary signal is the persistent corner badge; session speech is not required.
3. Relabel all 27 oggs in this PR. Rejected: provenance of 25 clips is under audit.

## Open questions and risks

- When Doubao credentials land, who runs `render_aigc_notices.py` and copies the two notice files into the pack?
- After the audit, remove paths from `PENDING_AIGC_AUDIT_EXCEPTIONS` as each clip is labelled.

## Next implementation step

Extend `aigc_opus.py` required keys, wire the pack exception list, then the badge and notice prepend.
