# apps/miniprogram/ — WeChat Mini Program Client

## OVERVIEW
Native WeChat Mini Program (CommonJS, no bundler, zero runtime npm deps): the parent-side control/view surface; realtime voice deliberately absent. Earned file: score 15 (498 files, 11 subdirs, own config set, api.js required by 36 files).

## STRUCTURE
- pages/ (18 dirs) + utils/ (44 js) + components/ (4) + custom-tab-bar/ + tests/ (70 node:test files) + scripts/ (mp-harness, codemods, CI gates) + generated/ + assets/ + legal/ + styles/ + design-preview/
- Child AGENTS.md: pages/, utils/.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| App shell | app.js, app.json, app.wxss | 18 routes, custom tabBar, --mp-* design tokens |
| Page layer | pages/ | see pages/AGENTS.md |
| Domain logic + API | utils/ | see utils/AGENTS.md |
| Test harness | scripts/mp-harness/ | render.js WXML subset; pages.js must list every app.json page |
| UI copy gate | scripts/check-forbidden-ui-copy.js | banned word list; scans pages/components/custom-tab-bar |
| Release gate | scripts/check-release-gate.js | fails when INTERNAL_TEST_CONSENT_UNLOCK is true |
| Contracts | generated/multi-subject-contracts.js | generated from packages/contracts schema; never hand-edit |
| Legal text | legal/user-agreement.md | force-included in package; asserted verbatim by tests |
| Tab bar | custom-tab-bar/ | 4 hard-coded tabs; selected index set per page |

## CONVENTIONS
- CommonJS only, 2-space indent, double quotes, semicolons; no TS; Node >=20 <25 for tests/scripts (runtime is WeChat JS).
- Page = dir/index.{js,wxml,wxss,json}; 17/18 pages custom nav; every route listed in app.json.pages.
- Design tokens only: --mp-* colors, --mp-type* sizes; lengths are design px at a 390pt frame (rpx banned); SVG only via `<image>` src — no inline `<svg>`; tone variants are separate files under assets/ui/v4/.
- UI copy is Chinese and centralized in utils/*.js constants; WXML stays thin.
- Tests: node:test + assert/strict; wx/Page/Component stubbed per test; V4 tests run real pages through scripts/mp-harness and assert rendered text (copy is contractual).
- config.js hardcodes prod API https://aginice.cn:8443/memoria-api; SUBSCRIPTION_PUSH_ENABLED=true (DECISIONS #029; bind only offers the prompt, #021/#030).
- project.config.json packOptions: ignore tests/, design-preview/; include legal md + assets/mascots.

## ANTI-PATTERNS
- No realtime voice surface: no RecorderManager, no realtime TTS/media WSS, no voiceprint/voice-clone page or copy.
- Never commit INTERNAL_TEST_CONSENT_UNLOCK=true; inject via upload:test isolated build only.
- Tab pages: never navigateTo/redirectTo; switchTab takes no query. No `new Date` inside `<wxs>`.
- Retired UI must not return: no glass-icon/page-backdrop components, no assets/ui/icons|bg references.
- No TODO/FIXME/eslint-disable markers; CI syntax-checks every .js under apps/miniprogram.

## COMMANDS
```bash
cd apps/miniprogram
npm test                       # node --test tests/*.test.js
npm run check:forbidden-copy   # node scripts/check-forbidden-ui-copy.js
npm run check:release-gate
npm run upload:test            # isolated build + internal-test consent unlock
```
