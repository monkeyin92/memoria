# Offline lines v2

Host-side inventory for the ESP-VoCat offline pack. Clip ids are flat basenames
(`CONFIG_MMAP_FILE_NAME_LENGTH=32`). Audio lives under
`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/`.

## AIGC spoken intros (screen dimmed)

Offline stories and bedtime (`night_soothe`) play with the screen dimmed, so the
persistent 「AI」 dialogue badge is not the signal. Firmware **must** play a short
spoken intro that contains 「AI」 and 「合成」 before the story or soothe clip.
Missing intro → build/CI fail; at runtime the story/soothe path is refused.

| id | Text | When |
|---|---|---|
| `aigc_story_intro` | 下面是 AI 合成的声音给你讲故事。 | Before each offline story file |
| `aigc_soothe_intro` | AI 合成的声音陪你睡觉啦。 | Before `night_soothe.ogg` |

Voice: `zh_female_tianmeitaozi_uranus_bigtts`, normal speed, normal volume.
Render: `scripts/render_aigc_notices.py` (requires `DOUBAO_TTS_*`). Each file
must carry implicit AIGC OpusTags (`CONTENT_ID`, `SERVICE_PROVIDER=Volcengine Doubao`,
`AI_GENERATED`, `ARTIST`, `DATE`, `CONTENT_PRODUCER=Memoria`).

## Live dialogue (screen on)

Primary explicit label: persistent 「AI」 corner badge on the device while a
conversation is active. Fallback end-of-turn tone is behind
`kAigcTurnToneEnabled` (default off) and only applies when the badge cannot
render. No per-session spoken notice.

## Pack gate

`firmware/esp32/scripts/build_offline_stories.py` fails CI when any shipped
clip lacks implicit tags, including the 25 production oggs from #244 (metadata-only
retag; audio packets unchanged). Do not strip provider-embedded marks.
