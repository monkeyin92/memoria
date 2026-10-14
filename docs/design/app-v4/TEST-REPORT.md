# Mini program v4 test report

This report records how the v4 restyle of the Memoria WeChat mini program was tested on branch `cursor/miniprogram-design-v4-14a0`. It covers every page under `apps/miniprogram/pages`. It lists the commands, the measured results, side-by-side images against the design PNGs in this folder, and each place where the mini program still differs from the design.

Every number comes from runs on 2026-10-14 on a Linux cloud VM with Node v22.14.0 and headless Chrome driven by puppeteer-core. Nothing ran in WeChat DevTools, through miniprogram-automator or on a phone, because none of them is available on the VM. The harness approximates the WeChat renderer. Read the images as close evidence of layout and copy, not as proof of on-device pixels.

## Results

| Check | Result |
|---|---|
| Unit, function, perf and layout tests (`npm test`) | 472 of 472 pass, 0 skipped |
| Forbidden UI copy (`npm run check:forbidden-copy`) | Pass on 57 files |
| `12356` in shipped `.js`, `.wxml`, `.json` and `.wxss` files | Absent |
| `node --check` on every tracked JS file in the app | 0 failures |
| Page screenshots at five views (`scripts/screenshot-pages.js`) | 130 shots, 0 layout issues |
| Parity side-by-sides (`scripts/v4-parity/render-and-compare.js`) | 9 mapped screens, rendered from live pages |
| Main package before minification (`scripts/package-size.js`) | 1837.1 KiB of the 2048 KiB limit |

## How the harness works

The harness lives in `apps/miniprogram/scripts/mp-harness/`. It runs the real page code, so each screenshot shows what the page produced from fixture API responses, not exported design HTML.

- `runtime.js` loads a page module with stubbed `wx` APIs, runs its `onLoad` and `onShow`, and records every `setData` call with its JSON size.
- `render.js` turns the page WXML, the resulting data, the page WXSS and `app.wxss` into HTML. It converts rpx at 750 per screen width and adds `custom-tab-bar` on tab pages.
- `shoot.js` lays the HTML out in Chrome, measures it and can save a PNG.
- `fixtures.js` pins the clock to 2026-10-14 19:42 in Asia/Shanghai and returns the API shapes the pages call in production.
- `pages.js` lists 26 page states and the five views below.

| View | Width | Height | Status bar | Home indicator | Font scale |
|---|---:|---:|---:|---:|---:|
| 390 | 390 | 844 | 47 | 34 | 1 |
| 320 | 320 | 568 | 20 | 0 | 1 |
| 375 | 375 | 667 | 20 | 0 | 1 |
| 430 | 430 | 932 | 59 | 34 | 1 |
| 390-large-font | 390 | 844 | 47 | 34 | 1.3 |

Sizes are CSS px. The 390 view captures the full page height and the others capture one screen. The large-font view multiplies every font size and type token by 1.3 to stand in for the system large-text setting, which the harness cannot drive.

## Reproduce

Run from `apps/miniprogram`. The tests and scripts set `TZ=Asia/Shanghai` themselves.

```bash
cd apps/miniprogram
npm test
npm run check:forbidden-copy
node scripts/screenshot-pages.js
node scripts/v4-parity/render-and-compare.js
node scripts/perf-report.js
node scripts/package-size.js
```

The Chrome steps need `puppeteer-core` from `package.json` and Chrome at `CHROME_PATH`, `/usr/local/bin/google-chrome` or `/usr/bin/google-chrome`. Without them `v4-layout.test.js` skips and reports why.

The recipe gives the parity command as `npm --prefix apps/miniprogram exec -- node scripts/v4-parity/render-and-compare.js`. Run from the repo root, it exits with `MODULE_NOT_FOUND`, because `npm exec` keeps the caller's working directory and resolves the script path against the repo root. Run from `apps/miniprogram`, `npm exec -- node scripts/v4-parity/render-and-compare.js` works and gives the same nine diff ratios as plain `node`. In both places `npm exec` runs Node v26.11.1 from the npm `node` package instead of the system Node.

## Visual parity

`render-and-compare.js` renders each mapped page state at the design frame of 390 by 844 CSS px at 2x, with a 47 px status bar and a 34 px home indicator. It writes three files per screen:

- `mp-screenshots/actual/<slug>.png` is the live page.
- `mp-screenshots/side-by-side/<slug>.png` puts the live page on the left and the design PNG on the right, each pane 780 by 1688.
- `mp-screenshots/metrics/<slug>.json` holds the diff numbers and the layout checks.

`diffRatio` is the share of pixels whose squared RGBA distance from the design exceeds 25. A card that sits 4 px lower counts all of its pixels as different. So the ratio mostly measures where content lands and how much of it there is, and real data changes both on every screen. Treat it as a regression signal between reruns, not a parity score. Two runs on this tree gave identical ratios on all nine screens. Each pair was also read by eye, and what differs is listed under its image.

| Screen | Live page and state | diffRatio |
|---|---|---:|
| 01-login | `pages/home`, guest | 0.647 |
| 02-consent | `pages/auth`, guest | 0.391 |
| 03-home | `pages/home`, signed in | 0.316 |
| 04-summary | `pages/memory`, daily review with today summarized | 0.339 |
| 05-attention | `pages/memory`, attention item pinned | 0.499 |
| 06-usage | `pages/device`, child usage settings | 0.253 |
| 08-roles | `pages/companion` | 0.354 |
| 09-memory | `pages/child-memory`, long-term memory | 0.371 |
| 10-profile | `pages/profile` | 0.245 |

07-content has no live page. It is a story catalogue with play buttons, and the app has no story list or story data, so a mapped page would need invented stories. Its earlier actual, metrics and side-by-side files were removed.

### 01-login

![01-login, live page left, design right](mp-screenshots/side-by-side/01-login.png)

The guest home keeps the tab bar because 首页 is a tab page. There is no 手机号登录 button. Login is one WeChat flow that asks for the phone number through WeChat's own `getPhoneNumber` authorization on the auth page, so a second button would open the same flow. The greeting reads 「晚上好，朋友」, since a guest may be visiting for the first time and 「欢迎回来」 would be wrong.

### 02-consent

![02-consent, live page left, design right](mp-screenshots/side-by-side/02-consent.png)

Consent lives on the auth page, a secondary page, so it has a back button. The login fields sit under the facts, which pushes the checkbox and the submit button below the fold. The fourth fact is 「回应由 AI 生成」 in place of 「设备没有摄像头」. `auth-layout.test.js` pins the four facts and rejects a camera claim.

### 03-home

![03-home, live page left, design right](mp-screenshots/side-by-side/03-home.png)

The avatar sits about 6 px lower than the design so it clears the WeChat capsule. The ring shows the limit on an empty track with 「用量暂未统计」, because no API field reports minutes used today. It fills only when a real tally exists, which `home-v4-dashboard.test.js` covers. 「今天对话 3 次」 replaces 「今天讲了 3 个故事」, and there is no 今晚推荐 card, because no story field exists. The 孩子的记忆 card and the AI disclosure line come from the existing page.

### 04-summary

![04-summary, live page left, design right](mp-screenshots/side-by-side/04-summary.png)

The review tab holds both the daily review and long-term memory, so a 日常回顾 and 记住的事 switch sits above the day strip. Day cards add the real conversation count. A day without an AI summary shows the prompt to make one.

### 05-attention

![05-attention, live page left, design right](mp-screenshots/side-by-side/05-attention.png)

The design shows a separate detail page. The app shows the attention item inline at the top of the review tab, so there is no back button and the tab bar stays. The 12356 hotline step is gone under the copy rules. There is no 我知道了 button because `utils/api.js` has no call to acknowledge an alert, so the button would do nothing. In the harness, content below the fold shows through the translucent tab bar at scroll position zero. Each tab page pads its bottom with `--mp-tab-clearance`, so the last card scrolls clear of the bar.

### 06-usage

![06-usage, live page left, design right](mp-screenshots/side-by-side/06-usage.png)

One slider, labelled 单次上限, edits the signed `MAX_SESSION_SECONDS` limit. The agent enforces it per device session in `services/agent/src/voice_core/minor_session_limits.py`. No daily total exists, so the design's 每天上限 slider has nothing to drive. The ring shows the limit with 「用量暂未统计」 for the same reason as home. 安静时段 shows the real window from the signed profile in place of 即将上线. The slider track stays thin because WeChat's `slider` has no track height attribute.

### 08-roles

![08-roles, live page left, design right](mp-screenshots/side-by-side/08-roles.png)

Names, art and taglines come from the companion catalogue, so five real roles replace the design's four samples. 新建角色 sits below the fold after the fifth card.

### 09-memory

![09-memory, live page left, design right](mp-screenshots/side-by-side/09-memory.png)

Long-term memory is the child-memory page, a secondary page with a back button and no tab bar. The title names the child and the caption counts the facts. The categories are 称呼, 兴趣 and 小名, the three fields the server keeps. Each fact shows its date and its own delete button, and a category can hold several facts.

### 10-profile

![10-profile, live page left, design right](mp-screenshots/side-by-side/10-profile.png)

There are no 帮助与反馈 or 关于 rows because neither page exists. The device card shows binding status, not online status, because the profile page does not fetch presence. The role, custom persona and preference rows come from the existing page.

## Differences from the design

### Platform differences

These come from WeChat itself, and no page change removes them.

- With `navigationStyle: "custom"`, WeChat still draws its menu capsule at the top right. The home avatar moves down to clear the capsule's real rect from `wx.getMenuButtonBoundingClientRect`, which sits lower on Android than on iOS.
- Custom navigation removes the native back button, so secondary pages draw their own round back button under the status bar.
- rpx scales with screen width. The tokens are authored on a 375 px base, so at the 390 design frame every rpx size renders about 4% larger than the design's px.
- The `slider` component has no track height attribute, so its track stays thin.

### Differences the data and copy rules require

- No used minutes, story counts, recommendation card or battery appear, because no API field backs them.
- No 12356 appears, and none of 危机, 伙伴, 好朋友, 家人, 推送 or 通知 appears in the UI.
- Subscription and push UI stays behind `subscriptionPushEnabled`, which defaults to false.
- Fixture names, roles, dates and counts replace the design's samples and its 「数字为示例」 captions.
- 07-content has no page.

### Differences that follow the existing app

- The guest home keeps the tab bar.
- Login has one WeChat button.
- Attention renders inline on the review tab, with no acknowledge button.
- Device usage has one per-session slider and a real quiet-hours row.
- Profile has no help, feedback or about rows, and no online status.
- Auth keeps the login fields and states the AI disclosure in place of the camera claim.

## Layout checks across widths

`v4-layout.test.js` renders all 26 page states at all five views, 130 layouts in one test. A layout fails when any of these holds:

- The document is wider than the view.
- An element extends past the left or right edge without a clipping ancestor.
- A tappable element overlaps the capsule on a custom-navigation page.
- Content on a tab page ends closer to the page bottom than the tab bar's height.

All 130 pass. `screenshot-pages.js` runs the same checks while it writes the PNGs and records them in `mp-screenshots/pages/results.json`.

## Page screenshots

`mp-screenshots/pages/` holds one PNG per page state and view, named `<slug>-<view>.png`. The 390 full-page and 320 shots are committed, 52 files and 3.9 MB. The 375, 430 and large-font shots add 5.6 MB, so `pages/.gitignore` excludes them and `node scripts/screenshot-pages.js` regenerates them.

The 26 page states are these:

- Home: `home`, `home-guest`.
- Review tab: `memory`, `memory-attention`, `memory-empty`, `memory-empty-memories`.
- Roles tab: `companion`.
- Device tab: `device`.
- Profile tab: `profile`.
- Guardian: `guardian`.
- Bind: `bind`, `bind-mode`, `bind-form`, `bind-review`.
- Custom persona: `persona-custom`.
- Auth: `auth`.
- Privacy: `privacy`, `privacy-granted`.
- Digital self: `digital-self`, `digital-self-empty`.
- Child memory: `child-memory`, `child-memory-archive`.
- Device onboarding: `device-onboarding`, `device-onboarding-wifi`, `device-onboarding-progress`, `device-onboarding-complete`.

For example, `mp-screenshots/pages/bind-form-390.png` is the full bind form at 390 and `mp-screenshots/pages/device-320.png` is the first screen of the device tab at 320.

## Performance

`scripts/perf-report.js` writes every number in this section to `mp-screenshots/perf.json`. `v4-perf.test.js` turns the budgets into tests.

### Per page

These numbers come from one run at the 390 view.

- Load is the time from requiring the page module to `onLoad` and `onShow` settling, with stubbed APIs that resolve at once. It measures page JS, not network.
- Render is the harness step that turns WXML into HTML. WeChat does this natively, so the number only bounds template work.
- Layout is Chrome loading the rendered HTML, its local images and fonts, until `document.fonts.ready`.
- The setData columns count every call during load and the actions the state needs, sized as JSON bytes.

| Page state | Load (ms) | Render (ms) | Layout (ms) | setData calls | Largest call (B) | Total (B) |
|---|---:|---:|---:|---:|---:|---:|
| home | 10.4 | 5.2 | 86 | 3 | 2260 | 2500 |
| home-guest | 1.6 | 1.6 | 59 | 2 | 1633 | 1719 |
| memory | 1.7 | 1.9 | 65 | 9 | 1876 | 3689 |
| memory-attention | 0.8 | 2 | 60 | 5 | 1876 | 2268 |
| memory-empty | 0.9 | 1.9 | 50 | 5 | 331 | 486 |
| memory-empty-memories | 1.7 | 1.4 | 54 | 6 | 331 | 531 |
| companion | 1.3 | 2.1 | 69 | 1 | 166 | 166 |
| device | 5.6 | 3.4 | 78 | 7 | 4323 | 5122 |
| profile | 3.5 | 2.2 | 64 | 10 | 1177 | 2726 |
| guardian | 2.9 | 2.1 | 56 | 11 | 1037 | 2283 |
| bind | 2.2 | 1.8 | 43 | 2 | 132 | 187 |
| bind-mode | 1.5 | 1.1 | 40 | 3 | 193 | 309 |
| bind-form | 1.3 | 2.3 | 58 | 5 | 1760 | 2084 |
| bind-review | 1.4 | 1.2 | 51 | 7 | 1760 | 2362 |
| persona-custom | 1 | 1 | 47 | 3 | 168 | 219 |
| auth | 0.7 | 1.2 | 48 | 1 | 111 | 111 |
| privacy | 1.4 | 0.9 | 53 | 4 | 35 | 113 |
| privacy-granted | 1.1 | 0.8 | 49 | 4 | 192 | 271 |
| digital-self | 1.8 | 0.9 | 44 | 3 | 1207 | 1258 |
| digital-self-empty | 0.9 | 0.6 | 53 | 2 | 143 | 170 |
| child-memory | 2 | 1 | 61 | 5 | 1323 | 1629 |
| child-memory-archive | 1.9 | 1.7 | 62 | 6 | 2942 | 3357 |
| device-onboarding | 7.8 | 2.6 | 66 | 2 | 1371 | 1405 |
| device-onboarding-wifi | 1.4 | 1.4 | 63 | 4 | 1371 | 2450 |
| device-onboarding-progress | 1.2 | 1.4 | 58 | 3 | 1371 | 2347 |
| device-onboarding-complete | 1.4 | 1.8 | 56 | 3 | 1371 | 2316 |

The test budget per state is at most 16 setData calls, no call over 16 KiB and at most 32 KiB in total. The largest call is 4323 B on device and the most calls are 11 on guardian. home, device and device-onboarding load slowest because each is the first state of its page in the run and pays for the first require of its modules. The later states of home and device-onboarding load in 1.2 to 1.6 ms. That cause is inferred from the pattern, not profiled.

### Long lists

`LONG_LISTS` in `pages.js` loads 200 days of summaries into the review tab and 200 facts into child-memory. The script scrolls each list from top to bottom at 120 px per animation frame, three times. It reports the median of the runs and the worst frame of any run.

| List | setData calls | Largest call (B) | Scroll height (px) | Render (ms) | Layout (ms) | Frames | p50 frame (ms) | p95 frame (ms) | Worst frame (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| memory, 200 days | 5 | 51618 | 27860 | 4.7 | 72 | 226 | 16.7 | 16.7 | 16.8 |
| child-memory, 200 facts | 6 | 79144 | 12993 | 6.9 | 73 | 102 | 16.7 | 16.8 | 16.8 |

Frame intervals sit at 16.7 ms, the 60 Hz frame clock of headless Chrome on the VM. No frame took longer than one vsync, so the page is not what limits these numbers. They do not predict a phone's WebView. An earlier run had one 49.9 ms frame, which did not recur in six later runs.

`v4-perf.test.js` asserts that both lists render every item, keep each setData call under WeChat's 256 KiB guidance and render in under 500 ms. child-memory keeps both `archiveFlat` and `archiveGroups` in its data, which stores each fact twice. That is why its largest call, 79.1 KB, is bigger than the review tab's 51.6 KB for 200 days. Changing it is page logic, outside a restyle.

### Package size

The shipped files total 1881240 B, 1837.1 KiB of WeChat's 2048 KiB main package limit before minification. `package-size.js` follows `packOptions.ignore` in `project.config.example.json` plus the folders the runtime never loads. It skips `tests`, `design-preview`, `scripts`, `node_modules`, `assets/companions`, `package.json`, `package-lock.json`, `project.config*.json` and Markdown files.

| Folder | Size (KiB) |
|---|---:|
| assets/mascots | 602.3 |
| utils/device-onboarding | 166.1 |
| assets/voices | 145.8 |
| assets/ui | 141.0 |
| assets/bg | 138.7 |
| pages/device | 100.2 |
| utils/device-binding.js | 60.4 |
| utils/api.js | 51.6 |

## Function tests

Each new test file loads the live page through the harness, drives its handlers the way a tap would and asserts on the resulting data or rendered text.

`home-v4-dashboard.test.js` has 10 tests.

- The home ring shows the usage limit without inventing used minutes.
- The home ring fills from a real used-minutes tally when one exists.
- Home counts only today's summaries and today's conversations.
- Home pins the attention card with its time and the open-to-pin meta.
- Home hides the safety subscribe card unless subscription push is enabled.
- The home memory card previews the latest summary and opens child memory.
- Home never renders battery, story counts or a recommendation card.
- The guest home shows the v4 login hero.
- The alert time label reads today or the calendar day.
- Home load stays within the setData budget.

`companion-v4-grid.test.js` has 4 tests.

- Roles render as a grid card per companion with the account's pick selected.
- Tapping the role already in use does not save again.
- 新建角色 opens the custom persona page.
- The voice row previews the role in use and stops on a second tap.

`device-v4.test.js` has 1 test.

- The usage slider names the per-session limit the device enforces, never a daily cap.

`bind-v4.test.js` has 4 tests.

- The form step lays every AI role out as a grid card with exactly one selected.
- Tapping a role selects it and the voice card follows the selection.
- 试听 plays the selected role's voice, and picking another role stops it.
- The review step labels itself as steps 3 and 4 and keeps 上一步 inside the card.

`device-onboarding-v4.test.js` has 3 tests.

- The Wi-Fi step lists networks by signal, never dBm, and marks the tapped one.
- The stepper marks finished steps done and the Wi-Fi step current while the robot joins.
- The finish card replaces the cancel link with the way into device management.

`guardian-v4.test.js` has 3 tests.

- Guardian opens on a large title and the bound child's weekly bars.
- Guardian alerts show the child, a readable time and no delivery internals.
- Guardian alerts without a name or valid time fall back to TA and drop the time line.

`privacy-v4.test.js` has 3 tests.

- Privacy states raw audio retention as kept until revoked, never a 7-day timer.
- Granting then revoking raw audio updates the card from the server responses.
- The long-term memory row opens the child memory page instead of a local switch.

`digital-self-v4.test.js` has 3 tests.

- Digital self counts confirmed sources and labels every growth dimension.
- Version records stay folded until tapped, then list labelled versions.
- Digital self with no growth data shows zero and the empty state.

`persona-custom-v4.test.js` has 3 tests.

- My personas list the version and voice name, never the persona or voice id.
- A persona whose fallback voice is unknown shows no voice instead of the first companion.
- Pickers and the rate slider use the shared v4 field chrome.

`sub-page-nav.test.js` runs 2 tests per secondary page.

- The page draws its own round back button under the status bar instead of the native bar.
- Back pops the stack, or opens the right tab when it is the first page.

`nav-inset.test.js` has 2 tests.

- The style carries the status bar and the real capsule bottom, which sits lower on Android.
- Without a capsule rect the style assumes a capsule 40 px below the status bar.

These files guard structure, styles and budgets.

- `v4-tokens.test.js` requires every colour in page and shared stylesheets to come from the `app.wxss` tokens, every type size to come from the `--mp-type` scale, and raw colours in `app.wxss` to appear only in the token block.
- `v4-style-lint.test.js` requires v4 line icons, checks that every icon name and asset path resolves to a file, and checks that each page registers exactly the components its template renders.
- `v4-perf.test.js` runs one setData budget test per page state, the two long-list tests and the package size test.
- `v4-layout.test.js` runs the 130 layout checks.

Older tests that asserted v3 strings, colours or structure now assert the v4 equivalents.

- `auth-layout.test.js` pins the four consent facts and rejects a camera or view-and-delete promise.
- `bind-flow.test.js` completes a parent-for-child bind without a subscribe prompt while the push flag is off.
- `child-memory.test.js` asserts the v4 caption, the field order and icons, and no retention-period copy.
- `demo03-family-review.test.js` asserts 分享这一天 in place of 分享选中日期的回顾.
- `device-page.test.js` asserts the 单次上限 label in the usage card.
- `dynamic-companion-assets.test.js` asserts that the v4 home renders no mascot and opens the device page from the usage card.
- `guest-login-flow.test.js` asserts the 微信一键登录 hero on home and the guest prompt on the review tab.

## Gaps

- Nothing ran in WeChat DevTools, through miniprogram-automator or on a phone. Images and timings come from Chrome on the VM, and native components such as `slider`, `switch` and `picker` are approximations.
- CI runs only on pull requests, so it has not run `v4-layout.test.js` yet. A clean `npm ci --ignore-scripts` of this tree on the VM passes 472 of 472, with the 130 layouts checked. If the CI runner lacks Chrome the layout test skips, and if it lacks CJK fonts the text metrics differ from these results.
- No screen is pixel-identical to the design. diffRatio stays between 0.245 and 0.647, mostly because real data changes how much content each screen holds and where it lands.
- The design and the task brief assume a daily usage cap, but the device enforces only a per-session limit. The device slider now says 单次上限. The home ring still reads 「今天聊了」 above 「30 分钟上限」, which a parent may take as a daily figure. Relabelling the ring or adding a daily cap to the backend is a product decision.
- Internal names still say daily. These are `onTrialDailyCapSlider` and `_saveDailyCapMinutes` in `pages/device/index.js`, and the unrendered `usageLabel` 「每日上限 N 分钟」 in `utils/trial-device-card.js`.
- `components/device-screen` and `components/face-badge` keep their own illustration palettes, outside the token test.
- The persona-custom placeholder 「例如：说话短一点，像朋友。」 still uses 朋友. The forbidden-copy check does not ban that word.
- child-memory stores each archive fact twice in its data.
- The large-font view is a CSS scale, not WeChat's own font-size setting.
