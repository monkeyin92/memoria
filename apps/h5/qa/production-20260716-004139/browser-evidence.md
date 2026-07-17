# Production browser evidence — 20260716-004139

- Date: 2026-07-16 CST
- H5: `https://110.42.235.198/memoria-h5/`
- API readiness: release `20260716-004139`, provider `qwen`
- Browser: Codex in-app browser
- Live viewport: 390 × 844
- Anonymous account: fresh production identity; no profile or preference mutation was submitted

## Home and explicit ready gate

1. The page first showed the anonymous-identity bootstrap state, then rendered the normal home screen.
2. Tapping the mascot immediately produced `正在靠近你…` and a disabled `正在连接` button. Conversation controls were not shown at this point, so transport connection was not presented as Agent readiness.
3. The UI entered `我在认真听` only after the Agent's explicit ready event. The controls `关闭麦克风`, `停止回答`, and `结束对话` then appeared.
4. The remote audio element was active (`paused=false`, `muted=false`) after readiness. No browser warning or error was recorded.

## Live controls and cleanup

- Mute changed the control from `关闭麦克风` to `打开麦克风`.
- Unmute changed it back to `关闭麦克风`.
- `停止回答` completed without a warning/error and returned to the authoritative listening state.
- `结束对话` changed the status to `今天先聊到这里` and removed all conversation controls.
- Three seconds after ending, no `<audio>` element remained and the conversation controls did not reappear. This verifies that an old connection did not reopen the microphone after cleanup.

## Navigation and responsive checks

- `回顾` opened the correct first-use empty state with `生成今天的回顾` and `去聊聊`.
- `我的` exposed the profile card, three preference switches, edit form, and privacy entry. The edit form was opened and cancelled without submitting a profile change.
- At 390 × 844, both review and profile checks returned `documentClientWidth=390` and `documentScrollWidth=390`.
- The profile switch states were exposed to assistive technology as `true / true / false`; the third switch was disabled as designed.
- Console verification after navigation, live controls, stop, and end: 0 warnings, 0 errors.

## Visual evidence mapping

The current release's screenshot transport timed out, so this pass reused the immediately preceding public-production visual captures below and paired them with a fresh live DOM/interaction pass. This is valid visual evidence because server-side comparison confirmed that releases `20260715-224555` and `20260716-004139` have the identical main CSS file and identical hashes for all eight neutral/happy/curious/upset PNG/WebP mascot assets.

- Home: `../production-home-final-390x720.png`
- Connected conversation: `../production-live-connected-390x720.png`
- Review empty state: `../production-memory-empty-390x720.jpg`
- Profile: `../production-profile-390x720.jpg`
- Full comparison: `../source-vs-production-home-final.png`
- Focused mascot comparison: `../source-vs-production-mascot-focus.png`

The session contained no user speech, so it correctly did not create a daily summary. Qwen summary generation remains covered by the production provider smoke and backend automated tests rather than this empty-session browser pass.
