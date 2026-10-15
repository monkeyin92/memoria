# Provenance audit: 25 `real_speech` offline pack oggs

Date: 2026-10-15. Branch tip at write time: `cursor/restore-labelled-speech-47eb`. Read-only survey of git, manifests, scripts, docs, OpusTags, plus ASR of the three story clips. No guesses filled in for missing fields.

throughput checkpoint: n/a, read-only investigation

## PR #244 (clip content commit)

| field | value |
|---|---|
| PR | https://github.com/monkeyin92/memoria/pull/244 |
| Title | feat(firmware): ship Doubao offline clips and fix awake-offline tap |
| GitHub author | `monkeyin92` (MonkeyIn) |
| Merged by | `monkeyin92` |
| Merge time (UTC) | 2026-10-14 23:01:56 UTC |
| Merge time (UTC+8) | **2026-10-15 07:01:56 UTC+8** |
| Merge commit | `c262de1a821aee0d5abe11356fbb2bcbc922e981` |
| Head branch | `cursor/production-offline-audio-8766` (deleted on remote after merge) |
| Cloud agent | `bc-ca20e122-1389-5689-be95-19e28c838766` — name “Swap offline placeholder audio for 甜美桃子 production clips” — https://cursor.com/agents/bc-ca20e122-1389-5689-be95-19e28c838766 |
| Agent source | `sand` (Cursor cloud agent). PR footer links the same `bcId`. Commits on the PR list author **Cursor Agent** with co-author MonkeyIn. |

Evidence: `gh pr view 244`, merge commit author date `+0800`, PR HTML agent footer, `cursor-cloud` agent index for that `bcId`.

## Plain findings

1. **Current audio bytes for all 25 files land in #244** (`c262de1a`, 2026-10-15, author MonkeyIn). Commit message and PR body say they were **hand-copied** from a “recording drop” / “production drop” of **Doubao trial** clips (甜美桃子 2.0). The in-repo pack script does **not** synthesize them (`build_offline_stories.py`: “copied in by hand. This script never writes audio”).
2. **Before #244** the same paths held **ffmpeg sine placeholders** (`TITLE=*_placeholder`) from #215 / #241. Those are not the files audited here.
3. **OpusTags on all 25 current files:** only `encoder=Lavc61.19.101 libopus` (vendor `Lavf61.7.103`). **No** `CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED`, `ARTIST`, or `TITLE`. Appendix-E AIGC labels are **absent** from the audio bytes.
4. **Voice id is claim-only in manifests**, not in the ogg tags: both `stories/index.json` and `offline_lines/index.json` set `voice.id=zh_female_tianmeitaozi_uranus_bigtts`, `generated=2026-10-14`, `terms=trial/demo only pending Volcengine terms`. Board clips `night_soothe.ogg` / `wo_zai_ne.ogg` are not listed in those arrays; their Doubao attribution is only the PR #244 / pack-script `VOICE` constant shared with the manifests.
5. **Story source texts are missing from the repo.** Product states the three story bodies were never committed or sent for recording. Repo search finds **titles only** (manifests, `OFFLINE_LINES` menu items, control API, miniprogram). No `.txt` / script body for the spoken stories. ASR transcripts below are the only full text recovered from the shipped audio.
6. **Audible AIGC notice:** not ear-checked. ASR of stories starts mid-narrative with no spoken disclosure preamble in the transcript (ASR imperfect; see caveats).

## Evidence index

| path | role |
|---|---|
| `firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/stories/index.json` | story titles + voice/terms block |
| `firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/offline_lines/index.json` | line texts + voice/terms block |
| `firmware/esp32/scripts/build_offline_stories.py` | pack checker; `VOICE`, `STORIES`, `OFFLINE_LINES`; “never writes audio” |
| `infra/voices/doubao_voice_ids.json` | maps 甜美桃子 2.0 → `zh_female_tianmeitaozi_uranus_bigtts`, model `seed-tts-2.0` |
| `HANDOFF.md` | release-blocker: trial/demo Doubao voice pending Volcengine terms |
| git `c262de1a` / PR #244 | content swap for all 25; “Copied 25 production Ogg Opus clips” |
| git `823f4939` / PR #241 | prior sine placeholders for offline_lines (+ story rename) |
| git `43edf22b` / PR #215 | first add of board clips + story placeholder oggs |
| `docs/acceptance/run-20261015-labelled-llm-state-audio/story-asr-transcripts.json` | faster-whisper ASR machine dump |

## Provenance table (25 files)

Shared for every row unless noted:

- **voice/tool (claimed):** Doubao / Volcengine `seed-tts-2.0`, speaker `zh_female_tianmeitaozi_uranus_bigtts` (甜美桃子 2.0). Source of claim: manifests + PR #244. **Not present in OpusTags.**
- **content commit:** `c262de1a` · 2026-10-15 · MonkeyIn · `feat(firmware): ship Doubao offline clips and fix awake-offline tap (#244)`
- **AIGC label:** **no** in file tags. Manifest has no per-clip AIGC field (only `placeholder: false`). No in-repo generation log tying each file to a TTS job id.
- **license/source note:** manifest `terms`: “trial/demo only pending Volcengine terms”; `HANDOFF.md` release-blocker. No separate copyright line for story text (text absent from repo).

| file | origin (measured) | voice/tool | commit+date | AIGC label | license/source note | evidence path |
|---|---|---|---|---|---|---|
| `night_soothe.ogg` | Hand-copied production drop in #244 replacing #215/#241 sine placeholder (`title=wo_zai_ne`-style `*_placeholder` era). Not in story/line manifest arrays. | Claimed Doubao peach; not in tags | `c262de1a` 2026-10-15 | **no** (tags: encoder only) | trial/demo pending Volcengine terms (HANDOFF + pack `VOICE`) | PR #244 body; `build_offline_stories.py` `BOARD_CLIPS` / `VOICE`; OpusTags scan |
| `wo_zai_ne.ogg` | Same as `night_soothe` | Claimed Doubao peach; not in tags | `c262de1a` 2026-10-15 | **no** | same | same |
| `stories/story_01.ogg` | Same #244 drop; prior `story_01_placeholder` sine (~17 KiB). Spoken body text **not in repo** | Claimed Doubao peach; not in tags | `c262de1a` 2026-10-15 | **no** | trial/demo terms; **story text origin unknown / missing** | `stories/index.json` title 小星星的枕头; PR #244; ASR below |
| `stories/story_02.ogg` | Same | same | `c262de1a` 2026-10-15 | **no** | same; title 月亮不着急 | same |
| `stories/story_03.ogg` | Same; title renamed in #241 to 大熊和森林里的悄悄话 | same | `c262de1a` 2026-10-15 | **no** | same | same + #241 rename commit |
| `offline_lines/offline-wake-no-network-v1.ogg` | #244 drop over #241 placeholder; manifest text 「我现在连不上网。」 | Claimed Doubao peach; not in tags | `c262de1a` 2026-10-15 | **no** | trial/demo terms | `offline_lines/index.json`; PR #244 |
| `offline_lines/offline-wake-ask-parent-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | manifest 「请爸爸妈妈看看家里的网络。」 |
| `offline_lines/offline-wake-story-hint-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「不联网也能听故事。」 |
| `offline_lines/offline-mid-drop-v2.ogg` | same (path first appeared #215 as placeholder, rewritten #241/#244) | same | `c262de1a` 2026-10-15 | **no** | same | 「网络断开了，刚才的话先停一下。」 |
| `offline_lines/offline-mid-ask-parent-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「请爸爸妈妈帮忙看看网络。」 |
| `offline_lines/offline-mid-retry-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「我在试着重新连网，等一等。」 |
| `offline_lines/offline-reconnecting-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「还在连网，再等一小会儿。」 |
| `offline_lines/online-restored-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「网络连好了。」 |
| `offline_lines/online-restored-chat-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「现在可以和我聊天了。」 |
| `offline_lines/online-restored-in-story-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「网络连好了，故事讲完再聊。」 |
| `offline_lines/offline-menu-intro-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「这里有三个故事。」 |
| `offline_lines/offline-menu-how-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「点一下屏幕，换下一个。」 |
| `offline_lines/offline-menu-confirm-how-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「按住屏幕不放，就开始播放。」 |
| `offline_lines/offline-menu-item-01-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「第一个故事：小星星的枕头。」 |
| `offline_lines/offline-menu-item-02-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「第二个故事：月亮不着急。」 |
| `offline_lines/offline-menu-item-03-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「第三个故事：大熊和森林里的悄悄话。」 |
| `offline_lines/offline-menu-choose-v2.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「选好了，就按住屏幕。」 |
| `offline_lines/offline-menu-start-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「好的，现在开始。」 |
| `offline_lines/offline-menu-end-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「讲完啦。点一下屏幕，再选一个。」 |
| `offline_lines/offline-menu-idle-v1.ogg` | same | same | `c262de1a` 2026-10-15 | **no** | same | 「我先休息一下，点屏幕就能叫醒我。」 |

## Story titles (`stories/index.json`) and full ASR

Titles from `firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/stories/index.json` (also mirrored in `build_offline_stories.py` `STORIES`):

| id | title in `stories/index.json` | ogg |
|---|---|---|
| `story_01` | 小星星的枕头 | `stories/story_01.ogg` |
| `story_02` | 月亮不着急 | `stories/story_02.ogg` |
| `story_03` | 大熊和森林里的悄悄话 | `stories/story_03.ogg` |

Stated origin of the story **bodies** (public-domain / rewrite / author): **missing**. Product: never committed or sent for recording. Design doc `docs/design/20261014-offline-story-menu.md` covers menu UX only, not story authorship. App design mock titles elsewhere (小兔的纸船月亮 / 小狐狸收集星星) do **not** match shipped firmware titles (`docs/design/app-v4/TEST-REPORT.md`).

### Full ASR transcripts (Chinese)

Tool: `faster-whisper` model `small`, `language=zh`, `vad_filter=True`, CPU int8. Audio decoded with ffmpeg to 16 kHz mono WAV then passed as float32 PCM. Machine dump: `story-asr-transcripts.json`.

**Caveat (measured):** Whisper `small` mishears names and some phrases (examples: 「小青蛙」→「小青香/小青发」, 「芦苇」→「卢伟」, 「照路」→「照录」, 「噗通」→「普通」, English fragment `the` in story_02). Treat as a recovery aid, not a gold script.

#### story_01 · title 《小星星的枕头》 · ~117.9 s

```
天黑了,小星星要睡觉了,可是小星星的枕头不见了,小星星找呀找,云朵下面没有,月亮旁边也没有。小星星有点着急,他的光一闪一闪的。月亮说,别着急,我们问问风吧。一阵风吹过来,风说,我看见一个软软的东西飘到小河边去了。小星星跟着风,慢慢飞到小河边。河边有一只小青蛙,小青蛙正抱着一团白白的东西,睡得很香。那正是小星星的枕头。小星星轻轻的说,小青蛙,这是我的枕头。小星发柔柔眼睛,说,对不起,我以为是一朵掉下来的云。它太软了,我抱着就睡着了。小星星想了想,说,没关系,你也没有枕头吗?小青香点点头,我的荷叶被风吹走了。小星星说,我们一起找吧。他们沿着小河找。小星星一闪一闪,把河边照得亮亮的。他们看见了小�小鱼在土泡泡,看见了卢伟在轻轻摇。找呀找,找到了一片圆圆的荷叶。荷叶上还有一颗小鹿猪。小青发高兴地跳上去,说,这是我的床,也是我的枕头。小星星抱着自己的枕头,飞回了天上。他往下看,小青蛙已经在荷叶上躺好了。小星星说,晚安,小青蛙。小青蛙说,晚安,小星星,谢谢你帮我照录。小星星把枕头拍一拍,放好,闭上了眼睛,天上很安静,河边也很安静。小星星的光变得小小的,暖暖的。他睡着了。
```

#### story_02 · title 《月亮不着急》 · ~121.8 s

```
小兔子要去奶奶家,她跑得很快,一边跑一边喊。快点,快点。月亮挂在天上,慢慢地走。小兔子抬头说,月亮,你走得太慢了。月亮笑着说,我不着急。小兔子跑过草地,跑得太快,没看见路边的小石头。普通,小兔子摔了一跤,他的蓝子翻了,胡萝卜滚了一地。小兔子坐在地上,眼睛红红的。月亮停下来,把光撒在草地上,草地亮了。小兔子看清楚了,胡萝卜一根一根躺在草里。小兔子捡起一根,又捡起一根。月亮说,慢慢来,一根一根捡。小兔子数着,一、二、三、四、五。五根胡萝卜都回到了篮子里。小兔子站起来,拍拍身上的土。他动动小手,动动小脚,一点也不疼。小兔子问,月亮,你为什么不着急呢?月亮说,慢慢走,路就在脚下,跑 the  跑不掉的。这一次,他不跑了,他慢慢地走。他听见了小虫子在唱歌,他闻到了野花香香的味道。他还看见一只萤火虫,提着小灯笼,从身边飞过去。小兔子说,原来慢慢走,能看见这么多东西。月亮陪着小兔子,一直照着前面的小路。小兔子到了奶奶家,奶奶打开门,说,路上辛苦了。奶奶给小兔子端来一杯热热的牛奶。小兔子把篮子递给奶奶,说,我和月亮一起走来的,小兔子往窗外看,月亮还挂在天上,慢慢地走。小兔子挥挥手,说,月亮晚安。
```

#### story_03 · title 《大熊和森林里的悄悄话》 · ~118.5 s

```
!森林里住着一只大熊,大熊不太爱说话,可是他很会听,有一天小松鼠跑来了,小松.熊说,我的松果掉进树洞里了,我拿不出来,大熊没有马上说话,他歪着头,认真的听,听完了,大熊说,我们一起去看看,大熊伸出长长的手臂,把松果拿了出来,小.熊说,谢谢,看你,大熊,小松鼠抱着松果,蹦蹦跳跳地回家了,第二天,小鸭子来了,小鸭子低着头,说,我学游泳,总是游不远,大家都游到前面去了,大熊坐下来,认真的听,听完了,大熊说,你想先游到那块石头吗?小鸭子游压游,游到了石头边,大熊在岸边看着他,轻轻趴趴手,小鸭子歇了一会儿,又游到了大树旁边,小鸭子高兴地说,我游得比昨天远了,晚上小刺猬来了,小刺猬什么也没说,只是坐在大熊旁边,大熊也不说话,陪他一起看星星,过了一会儿,小刺猬小声说,今天我和好朋友吵架了,大熊点点头,听小刺猬慢慢说完,小刺猬说完了,心里轻松多了,他说,明天我想去跟他说对不起,大熊说,这个办法很好,小刺猬站起来,说,大熊,谢谢你听我说话,他慢慢走回家了,森林里静悄悄的,风吹过树叶,沙沙,沙沙,大熊也在听,他听小鸭的声音,大熊慢慢闭上眼睛,睡着了。
```

## Explicit gaps (do not invent)

| gap | status |
|---|---|
| In-repo generation script / TTS job receipts for the 25 clips | **missing** (hand copy only) |
| OpusTags AIGC / voice id on production oggs | **missing** (encoder only) |
| Story body source files and authorship (public domain / rewrite / author) | **missing**; product confirms never committed |
| Human recording credit (if any clip were human) | **no evidence**; all claims point to Doubao TTS |
| Audible disclosure spoken in the clips | **not ear-verified**; ASR shows no clear preamble |
| Exact Volcengine license grant for shipping trial voice | **unresolved**; HANDOFF release-blocker still open |
| Who produced the external “recording drop” and from which prompt texts | **missing** outside PR prose |

## Doubao preset vs Alibaba designed voice (per file)

Catalogs checked:

- Doubao presets: `infra/voices/doubao_voice_ids.json` (`speaker_id`, model `seed-tts-2.0`)
- Alibaba designed: `infra/voices/designed_voice_ids.json` (`cosyvoice-v3.5-flash-vd-warmboy|softgirl|calmguide|brightpeer|lowmag-…`)

**No file among the 25 names any Alibaba designed `voice_id`.** No generation script in-repo synthesized these bytes. Records are pack manifests, `build_offline_stories.py` `VOICE`, PR #244 / commit `c262de1a`, and `HANDOFF.md`. OpusTags carry no voice id.

| file | recorded voice source | id | evidence |
|---|---|---|---|
| `night_soothe.ogg` | Doubao preset (pack-level; **no per-file manifest row**) | `zh_female_tianmeitaozi_uranus_bigtts` | PR #244 (“25 … Doubao trial … 甜美桃子 2.0” / same speaker id); `VOICE` in `build_offline_stories.py`; HANDOFF trial-Doubao blocker. Alibaba designed: **no record** |
| `wo_zai_ne.ogg` | Doubao preset (pack-level; **no per-file manifest row**) | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `stories/story_01.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | `stories/index.json` `voice.id`; PR #244; pack `VOICE`. Alibaba designed: **no record** |
| `stories/story_02.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `stories/story_03.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-wake-no-network-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | `offline_lines/index.json` `voice.id`; PR #244; pack `VOICE`. Alibaba designed: **no record** |
| `offline_lines/offline-wake-ask-parent-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-wake-story-hint-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-mid-drop-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-mid-ask-parent-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-mid-retry-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-reconnecting-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/online-restored-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/online-restored-chat-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/online-restored-in-story-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-intro-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-how-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-confirm-how-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-item-01-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-item-02-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-item-03-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-choose-v2.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-start-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-end-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |
| `offline_lines/offline-menu-idle-v1.ogg` | Doubao preset | `zh_female_tianmeitaozi_uranus_bigtts` | same |

Notes:

- Doubao catalog field name is `speaker_id`, not `voice_type`. Manifest field is `voice.id`. Same string: `zh_female_tianmeitaozi_uranus_bigtts` (display 甜美桃子 2.0 in `doubao_voice_ids.json`).
- No record of `cosyvoice-v3.5-flash-vd-warmboy|softgirl|calmguide|brightpeer|lowmag-*` for any of these 25 paths.
- No in-file tag proves the claimed speaker. Attribution is metadata + PR prose only.
