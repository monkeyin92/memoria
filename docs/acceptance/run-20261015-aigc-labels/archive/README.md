# AIGC intro clips (aigc_story_intro / aigc_soothe_intro)

Generated 2026-10-15 ~17:42 (Asia/Shanghai) on memoria test server (VM-0-8-ubuntu),
release `/opt/memoria/current -> releases/20261015-restore-audio-v1`.

| id | text | ogg sha256 | duration |
|---|---|---|---|
| aigc_story_intro | 下面是 AI 合成的声音给你讲故事。 | 3b4761aded3784daed29e4e2a7c35bfdc894a10102bbbf6ef45a6078e090ee72 | 3.419 s |
| aigc_soothe_intro | AI 合成的声音陪你睡觉啦。 | 3d8f52d61bd1bfe09974a16dfb62fddfc376628b4046ed90a90449da4750b12a | 2.611 s |

Source WAVs (24 kHz mono s16, raw Doubao PCM, sha256 verified identical on server and here):
- aigc_story_intro-24k.wav 574cfa2b7a8a806dfe6ab0645af96947726f6243f7d5aadc4bf8ad9484ad3f78 (Doubao task aa694ce8-c34a-4b02-8554-fca48260ab2a)
- aigc_soothe_intro-24k.wav 87dc786f1df439a14bd0c62ef9a34210eff1df0fccd18cc490d66eab74b0c5b5 (task 88ed574a-5b10-4673-91fe-d57afadf2267)

Params: voice zh_female_tianmeitaozi_uranus_bigtts, resource seed-tts-2.0, speech_rate 0,
loudness_rate 0, pitch 0, style control off, no instruction (normal speed/volume).

## Why not the script as-is
`scripts/render_aigc_notices.py` (branch cursor/aigc-audio-labels-6f7c, sha256 a3599af3...b944)
does not run against the current release provider: it requests 16 kHz (provider raises
"Memoria requires Doubao PCM at 24000 Hz") and calls `DoubaoTTS.start()` which no longer exists.
`synthesize_stream_text` now takes `[texts], fence=...` and returns SynthesizeResult. Fix the script
the same way as `run_wav24.py` before committing it. Server has no ffmpeg (host or agent image).

## Exact steps
1. TTS on server (one-off container, no service touched):
   `sudo docker run --rm --env-file /etc/memoria-agent.env -e PYTHONPATH=/app -v /tmp/aigc-work:/work:ro -v /tmp/aigc-intros:/out --entrypoint /app/.venv/bin/python memoria-agent:20261015-restore-audio-v1 /work/run_wav24.py`
2. Encode on the box with the script's own `_ffmpeg_from_wav` (ffmpeg libopus, -ar 16000 -ac 1 -b:a 16000, Lavc61.19.101)
   with generated=2026-10-15.

## OpusTags (both files)
TITLE / CONTENT_ID = line id, SERVICE_PROVIDER=Volcengine Doubao, AI_GENERATED=true,
ARTIST=zh_female_tianmeitaozi_uranus_bigtts, DATE=2026-10-15, CONTENT_PRODUCER=Memoria.

Loudness: story mean -18.4 dB / max -4.6 dB; soothe mean -20.7 dB / max -5.1 dB. No ASR transcript yet.
Server /tmp/aigc-work and /tmp/aigc-intros removed afterwards.
