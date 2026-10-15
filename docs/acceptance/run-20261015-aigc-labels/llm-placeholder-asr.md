# ASR note: `llm_thinking.ogg` / `llm_unavailable.ogg`

## Current pack (Doubao speech)

These offline lines are now real Doubao `seed-tts-2.0` clips (`zh_female_tianmeitaozi_uranus_bigtts`), not placeholders. Whisper **large-v3** transcripts (see also `asr-transcripts.json`):

| file | sha256 | whisper large-v3 |
|---|---|---|
| `offline_lines/llm_thinking.ogg` | (see `asr-transcripts.json`) | 嗯我想一想 |
| `offline_lines/llm_unavailable.ogg` | (see `asr-transcripts.json`) | 我现在有点卡住了等一会儿再来找我聊吧 |

Expected product copy (offline-lines v2 / filler constants):

| id | expected text |
|---|---|
| `llm_thinking` | 嗯，我想一想。 |
| `llm_unavailable` | 我现在有点卡住了，等一会儿再来找我聊吧。 |

## Historical note (pre-Doubao drop)

On `origin/main` before the Doubao llm pack land, these files were undecodable AIGC placeholders (172/179 B) restored by #255. #254’s larger files were ffmpeg scaffold tones, not speech. That history is why earlier #259 revisions reported ASR N/A.
