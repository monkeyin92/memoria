# ASR note: `llm_thinking.ogg` / `llm_unavailable.ogg`

These two offline pack files remain **non-speech placeholders** (`TITLE` ends in `_placeholder`).

| file | bytes (this branch) | ffmpeg decode | faster-whisper ASR |
|---|---:|---|---|
| `offline_lines/llm_thinking.ogg` | 267 | fails (`CRC mismatch` / EOF) | not run — no decodable PCM |
| `offline_lines/llm_unavailable.ogg` | 273 | fails (`CRC mismatch` / EOF) | not run — no decodable PCM |

What changed in #259: **metadata-only** rewrite of the placeholder OpusTags to add the full implicit AIGC set (`ARTIST`, `DATE`, `CONTENT_PRODUCER`, …). Not re-encoded from speech. Not re-synthesized (no Doubao credentials; fabricating speech is refused).

Expected product copy (when Doubao speech is later produced via `scripts/render_labelled_lines.py`):

| id | expected text | voice |
|---|---|---|
| `llm_thinking` | 嗯，我想一想。 | `zh_female_tianmeitaozi_uranus_bigtts` |
| `llm_unavailable` | 我现在有点卡住了，等一会儿再来找我聊吧。 | `zh_female_tianmeitaozi_uranus_bigtts` |

Online playback continues to use streaming TTS until real Doubao assets exist (`services/common/assets/labelled_lines/provenance.json` status `no_playable_online_assets`).
