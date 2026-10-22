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
`AI_GENERATED`, `ARTIST`, `VOICE_ID`, `DATE`, `CONTENT_PRODUCER=Memoria`).

## Live dialogue (screen on)

Primary explicit label: persistent 「AI」 corner badge on the device while a
conversation is active. Fallback end-of-turn tone is behind
`kAigcTurnToneEnabled` (default off) and only applies when the badge cannot
render. No per-session spoken notice.

### Dialogue system lines (no spoken AI intro)

Hidden tags only. Voice: `zh_female_tianmeitaozi_uranus_bigtts`.

| id | Text |
|---|---|
| `llm_thinking` | 嗯，我想一想。 |
| `llm_unavailable` | 我现在有点卡住了，等一会儿再来找我聊吧。 |

定稿 v2，产品部 2026-10-15 确认

## Pack gate

`firmware/esp32/scripts/build_offline_stories.py` fails CI when any shipped
clip lacks implicit tags, including the 25 production oggs from #244 (metadata-only
retag; audio packets unchanged). `check_device_ogg_aigc.py` also requires every
board ``*.ogg`` to decode and to carry `VOICE_ID`. Do not strip provider-embedded
marks.

## Addendum, 2026-10-22

Monkey removed the persistent 「AI」 badge and the two spoken intros from the
shipped device. `aigc_story_intro.ogg` and `aigc_soothe_intro.ogg` are not in
the board pack. Firmware plays each offline story and `night_soothe.ogg` with
no intro ahead of the clip. A missing intro does not fail the pack check, and
it does not refuse playback. The sections above stay as the 2026-10-15
description.

Implicit OpusTags stay required on every shipped clip. The live-dialogue AIGC
voice prompt stays. The parent miniprogram stays.
