# ASR note: `llm_thinking.ogg` / `llm_unavailable.ogg`

## Verdict (measured against `origin/main` @ `832bb8de`)

**`origin/main` does not carry real Doubao speech for these two ids.** Both files are the same non-decodable AIGC **placeholders** that #255 restored on purpose. This branch did **not** replace speech with placeholders; it only expanded OpusTags on those placeholder blobs.

| ref | file | bytes | SHA-256 | `TITLE` | ffmpeg decode | class |
|---|---|---:|---|---|---|---|
| `origin/main` | `offline_lines/llm_thinking.ogg` | 172 | `0f0e9f75ceddbd951be93523c9cbe3b1e24d2b8f94363947e96e3690b9cc976e` | `llm_thinking_placeholder` | CRC mismatch / EOF | **placeholder_tone** |
| `origin/main` | `offline_lines/llm_unavailable.ogg` | 179 | `14e61a1f8214f5fd16fb1f0accaba613928c0fc900b707681ab22c376981df93` | `llm_unavailable_placeholder` | CRC mismatch / EOF | **placeholder_tone** |
| this branch (`583c2061`) | `llm_thinking.ogg` | 267 | (tags expanded) | `llm_thinking_placeholder` | CRC mismatch / EOF | **placeholder_tone** + full implicit tags |
| this branch | `llm_unavailable.ogg` | 273 | (tags expanded) | `llm_unavailable_placeholder` | CRC mismatch / EOF | **placeholder_tone** + full implicit tags |
| `#254` `161d5732` (superseded by #255) | `llm_thinking.ogg` | 2853 | — | `llm_thinking` | ~1.1 s PCM | **ffmpeg scaffold tone** (`top3_frac≈0.90`, not speech) |
| `#254` | `llm_unavailable.ogg` | 5558 | — | `llm_unavailable` | ~2.4 s PCM | **ffmpeg scaffold tone** (`top3_frac≈0.90`) |

#255 commit message (on `main`): *“Pre-#254 offline llm_thinking/unavailable blobs were AIGC placeholders, not Doubao speech. Restore them byte-for-byte… fall back to streaming TTS until Doubao renders real labelled clips.”*

Main’s placeholder SHAs match the restore targets locked in `docs/acceptance/run-20261015-labelled-llm-state-audio/verification-restore.md`.

## What #259 changed on these two files

**Metadata-only** rewrite of the placeholder OpusTags to add `ARTIST`, `DATE`, `CONTENT_PRODUCER` (and keep `CONTENT_ID` / `SERVICE_PROVIDER` / `AI_GENERATED`). Not re-encoded from speech. Not re-synthesized. No Doubao credentials; fabricating speech is refused.

## faster-whisper ASR

Not run: there is no decodable PCM on `main` or on this branch. Running ASR on the #254 scaffold tones would only confirm non-speech tones; those files must not be restored (product/#255 explicitly rejected them as playable audio).

## Expected product copy (when Doubao speech is later produced)

Via `scripts/render_labelled_lines.py` with `DOUBAO_TTS_*` → `tool: doubao-seed-tts-2.0`:

| id | expected text | voice |
|---|---|---|
| `llm_thinking` | 嗯，我想一想。 | `zh_female_tianmeitaozi_uranus_bigtts` |
| `llm_unavailable` | 我现在有点卡住了，等一会儿再来找我聊吧。 | `zh_female_tianmeitaozi_uranus_bigtts` |

Until then, online playback uses **streaming TTS** (`services/common/assets/labelled_lines/provenance.json` status `no_playable_online_assets`).
