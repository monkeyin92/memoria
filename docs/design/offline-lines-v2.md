# Offline lines v2

Host-side inventory for the ESP-VoCat offline pack. Clip ids are flat basenames
(`CONFIG_MMAP_FILE_NAME_LENGTH=32`). Audio lives under
`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/`.

## AIGC spoken notices (screen dimmed)

Offline stories and bedtime (`night_soothe`) play with the screen dimmed, so the
persistent 「AI」 dialogue badge is not the signal. Firmware prepends a short
spoken notice that contains 「AI」 and 「合成」.

| id | Text | When |
|---|---|---|
| `aigc-notice-story` | 下面是 AI 合成的声音给你讲故事。 | Before each offline story file |
| `aigc-notice-bedtime` | AI 合成的声音陪你睡觉啦。 | Before `night_soothe.ogg` |

Voice: `zh_female_tianmeitaozi_uranus_bigtts`, normal speed, normal volume.
Render: `scripts/render_aigc_notices.py` (requires `DOUBAO_TTS_*`). Each file
must carry implicit AIGC OpusTags (`CONTENT_ID`, `SERVICE_PROVIDER`,
`AI_GENERATED`, `ARTIST`, `DATE`, `CONTENT_PRODUCER`).

TODO: notice oggs are not in the pack until Doubao credentials run the render
script. Playback skips a missing notice and still plays the story or soothe.

## Live dialogue (screen on)

Primary explicit label: persistent 「AI」 corner badge on the device while a
conversation is active. Fallback end-of-turn tone is behind
`kAigcTurnToneEnabled` (default off) and only applies when the badge cannot
render. No per-session spoken notice.

## Pack gate

`firmware/esp32/scripts/build_offline_stories.py` fails CI when any shipped
clip lacks implicit tags, except `PENDING_AIGC_AUDIT_EXCEPTIONS` (25 production
oggs under provenance audit). Do not strip provider-embedded marks.
