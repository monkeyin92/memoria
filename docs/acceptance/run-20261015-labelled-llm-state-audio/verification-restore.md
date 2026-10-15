# Verification: pre-#254 offline pack audit + restore

Date: 2026-10-15. Base commit before #254: `e8cff95a`. #254 landed as `161d5732` on main.

## Exact product copy (offline-lines v2 / pack table)

From `firmware/esp32/scripts/build_offline_stories.py` `OFFLINE_LINES` (offline-lines v2.1 table referenced by `docs/design/20261014-offline-story-menu.md`) and the online constants:

| id | text |
|---|---|
| `llm_thinking` | `嗯，我想一想。` |
| `llm_unavailable` | `我现在有点卡住了，等一会儿再来找我聊吧。` |

These match `LLM_THINKING_TEXT` / `LLM_UNAVAILABLE_TEXT` and the pre-#254 `offline_lines/index.json` entries. Unchanged.

## Method

For every `.ogg` under `boards/memoria/esp-vocat/assets` at `e8cff95a`:

1. OpusTags (`TITLE`, `CONTENT_ID`, `AI_GENERATED`, `ARTIST`, …)
2. Manifest `placeholder` flag when present
3. ffmpeg decode duration at 16 kHz
4. Spectral check on a mid-clip window: top-3 FFT energy fraction, spectral flatness, energy in fundamental+2nd+3rd harmonics (`harmonic_ratio`)

**Classification rules (measured):**

- `placeholder_tone`: `TITLE` ends with `_placeholder`, or undecodable / tiny blob, or sine-like (`harmonic_ratio >= 0.75` and `top3_frac >= 0.80` with short duration)
- `real_speech`: otherwise decodable durable audio (Doubao production clips from #244)

Machine-readable rows: `pack-audit-e8cff95a.json`.

## Full pack list at `e8cff95a` (27 files)

| path | bytes | dur_s | TITLE | manifest_ph | harm | top3 | class |
|---|---:|---:|---|---|---:|---:|---|
| `night_soothe.ogg` | 8023 | 4.365 | — | — | 0.652 | 0.081 | **real_speech** |
| `wo_zai_ne.ogg` | 2196 | 1.173 | — | — | 0.234 | 0.035 | **real_speech** |
| `stories/story_01.ogg` | 224322 | 117.909 | — | false | 0.447 | 0.071 | **real_speech** |
| `stories/story_02.ogg` | 228661 | 121.795 | — | false | 0.213 | 0.034 | **real_speech** |
| `stories/story_03.ogg` | 225382 | 118.512 | — | false | 0.029 | 0.055 | **real_speech** |
| `offline_lines/offline-wake-no-network-v1.ogg` | 2781 | 1.406 | — | false | 0.249 | 0.042 | **real_speech** |
| `offline_lines/offline-wake-ask-parent-v1.ogg` | 5566 | 2.792 | — | false | 0.261 | 0.100 | **real_speech** |
| `offline_lines/offline-wake-story-hint-v2.ogg` | 4425 | 2.205 | — | false | 0.191 | 0.066 | **real_speech** |
| `offline_lines/offline-mid-drop-v2.ogg` | 6268 | 3.183 | — | false | 0.290 | 0.031 | **real_speech** |
| `offline_lines/offline-mid-ask-parent-v1.ogg` | 4312 | 2.198 | — | false | 0.249 | 0.053 | **real_speech** |
| `offline_lines/offline-mid-retry-v1.ogg` | 5165 | 2.599 | — | false | 0.148 | 0.021 | **real_speech** |
| `offline_lines/offline-reconnecting-v1.ogg` | 5781 | 3.006 | — | false | 0.060 | 0.125 | **real_speech** |
| `offline_lines/online-restored-v1.ogg` | 2691 | 1.406 | — | false | 0.138 | 0.023 | **real_speech** |
| `offline_lines/online-restored-chat-v1.ogg` | 3521 | 1.801 | — | false | 0.282 | 0.034 | **real_speech** |
| `offline_lines/online-restored-in-story-v2.ogg` | 5289 | 2.784 | — | false | 0.481 | 0.099 | **real_speech** |
| `offline_lines/offline-menu-intro-v2.ogg` | 3620 | 1.802 | — | false | 0.261 | 0.028 | **real_speech** |
| `offline_lines/offline-menu-how-v1.ogg` | 4314 | 2.202 | — | false | 0.338 | 0.036 | **real_speech** |
| `offline_lines/offline-menu-confirm-how-v2.ogg` | 5222 | 2.602 | — | false | 0.487 | 0.069 | **real_speech** |
| `offline_lines/offline-menu-item-01-v2.ogg` | 6758 | 3.404 | — | false | 0.065 | 0.016 | **real_speech** |
| `offline_lines/offline-menu-item-02-v2.ogg` | 5454 | 2.781 | — | false | 0.448 | 0.067 | **real_speech** |
| `offline_lines/offline-menu-item-03-v2.ogg` | 8275 | 4.204 | — | false | 0.146 | 0.016 | **real_speech** |
| `offline_lines/offline-menu-choose-v2.ogg` | 5380 | 2.775 | — | false | 0.414 | 0.057 | **real_speech** |
| `offline_lines/offline-menu-start-v1.ogg` | 3838 | 1.986 | — | false | 0.450 | 0.069 | **real_speech** |
| `offline_lines/offline-menu-end-v1.ogg` | 6389 | 3.413 | — | false | 0.326 | 0.044 | **real_speech** |
| `offline_lines/offline-menu-idle-v1.ogg` | 6022 | 3.011 | — | false | 0.134 | 0.040 | **real_speech** |
| `offline_lines/llm_thinking.ogg` | 172 | — | `llm_thinking_placeholder` | true | — | — | **placeholder_tone** |
| `offline_lines/llm_unavailable.ogg` | 179 | — | `llm_unavailable_placeholder` | true | — | — | **placeholder_tone** |

**Counts:** 25 `real_speech`, 2 `placeholder_tone`.

### Note on the three stories

Product said the three offline stories were “already only placeholder tones.” Measured against `e8cff95a`, that is **false**. `story_01` / `story_02` / `story_03` decode to ~118–122 s of speech-like audio, `manifest_placeholder=false`, and fail the sine-tone spectral test. They shipped as Doubao production clips in #244 (`voice.id=zh_female_tianmeitaozi_uranus_bigtts` in the stories manifest).

### Note on `llm_thinking` / `llm_unavailable`

These two alone are placeholders (TITLE `*_placeholder`, 172/179 bytes, undecodable). #254 overwrote them with ffmpeg scaffold tones (~1.1 s / ~2.4 s). Neither state is playable labelled speech for online use.

SHA-256 of the pre-#254 placeholder blobs (restore target):

- `llm_thinking.ogg` `0f0e9f75ceddbd951be93523c9cbe3b1e24d2b8f94363947e96e3690b9cc976e`
- `llm_unavailable.ogg` `14e61a1f8214f5fd16fb1f0accaba613928c0fc900b707681ab22c376981df93`

## Online play eligibility

For online catalogue playback a clip must be **real_speech**, carry Appendix-E AIGC tags with the matching `CONTENT_ID`, and come from provenance `tool: doubao-seed-tts-2.0`.

No such asset exists today for `llm_thinking` / `llm_unavailable` (the only pack files with those ids are placeholders; the 25 real-speech clips lack those CONTENT_IDs). Online path therefore uses **streaming TTS** for both exact texts until Doubao regeneration.

## Restore actions (this PR)

1. Offline `llm_thinking.ogg` / `llm_unavailable.ogg` restored byte-for-byte from `e8cff95a`.
2. Pending-placeholder pack rules restored (25 production / 2 placeholders).
3. Online scaffold audio removed; catalogue refuses synthetic provenance tools.
4. Thinking filler / unavailable fall back to streaming TTS when the catalogue has no real labelled speech.
