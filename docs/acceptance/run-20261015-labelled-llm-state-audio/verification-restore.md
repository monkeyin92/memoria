# Verification: offline oggs before #254 vs scaffold overwrite

Date: 2026-10-15. Compared `e8cff95a` (main tip before #254) to post-#254 blobs.

## Offline `llm_thinking.ogg` / `llm_unavailable.ogg`

| | Before #254 (`e8cff95a`) | After #254 scaffold |
|---|---|---|
| `llm_thinking` size | 172 bytes | 2853 bytes |
| `llm_unavailable` size | 179 bytes | 5558 bytes |
| Decode as speech | No (truncated / CRC fail) | Yes, ~1.1 s / ~2.4 s sine tones |
| TITLE | `*_placeholder` | line id |
| AIGC tags | CONTENT_ID / SERVICE_PROVIDER / AI_GENERATED | same + ARTIST peach-voice id |
| `index.json` placeholder | true | false (incorrectly promoted) |
| Provenance tool | n/a (pack placeholders) | `ffmpeg-libopus-scaffold` |

SHA-256 before restore (pre-#254 blobs):

- `llm_thinking.ogg` `0f0e9f75ceddbd951be93523c9cbe3b1e24d2b8f94363947e96e3690b9cc976e`
- `llm_unavailable.ogg` `14e61a1f8214f5fd16fb1f0accaba613928c0fc900b707681ab22c376981df93`

**Finding.** The pre-#254 offline files were AIGC-tagged **placeholders**, not Doubao peach-voice recordings. Git history has no larger Doubao speech blobs for these two line ids. #254 overwrote those placeholders with ffmpeg cadence tones that also are not speech.

## Online play path

No Doubao-rendered online assets exist in-repo. Catalogue must stay empty until `scripts/render_labelled_lines.py` runs with `DOUBAO_TTS_*` and writes `tool: doubao-seed-tts-2.0`. Until then, online playback uses streaming TTS for both lines (thinking filler via `generate_output`, unavailable via normal reply TTS).

## Restore actions

1. Offline oggs restored byte-for-byte from `e8cff95a` (cmp clean).
2. Offline pack pending-placeholder rules restored (25 production / 2 placeholders).
3. Online scaffold ogg/s16le removed from the play asset dir; provenance marks `playable: false`.
4. Catalogue refuses `ffmpeg-libopus-scaffold` and TITLE placeholders.
