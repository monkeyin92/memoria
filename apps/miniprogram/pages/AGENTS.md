# apps/miniprogram/pages/ — View Layer

## OVERVIEW
18 page dirs, each a fixed index.{js,wxml,wxss,json} quartet; route strings and rendered copy are the public contract. Earned file: score 11 (74 files, 18 subdirs, distinct view domain).

## WHERE TO LOOK
| Page | Focus | Notes |
|------|-------|-------|
| device/ | status/binding/sheets (1992 L) | 5 sheets via _syncSheetOpen; mp-slider |
| memory/ | summaries + crisis banner | day/guardian summary merge |
| home/ | binding cards + guest gate | 7 cards; tab page |
| profile/ | binding sync + deletion flow | staged deletion copy |
| bind/ | multi-mode claim form | unbind paths never become guardian links |
| device-onboarding/ | BLE provisioning | reprovision URL: mode=reprovision&fresh=1 |
| guardian/ | consent toggles + export | share file from tap handler only |
| child-memory/ | archive facts | query minorId=&name=&section=recent |
| persona-custom/ | structured persona | server-backed only; no pretend structure |
| attention/ | crisis alerts | loadSafetyAlert/acknowledge |
| memory-lifecycle/ | long-term memory | only page with default nav bar |
| user-agreement/ | legal doc wrapper | 18-line JS |

## CONVENTIONS
- Custom nav on 17/18 pages: each JS sets statusBarStyle() in Page.data; pull-down pages pair onPullDownRefresh with wx.stopPullDownRefresh (enablePullDownRefresh explicit, never silent).
- Device data derived once from readBindingManifest()/MODE_META/entryAllowed; never re-fetched per section; stale-epoch responses dropped.
- usingComponents are absolute paths /components/<name>/index; every declared component must be rendered in the WXML.
- Copy: hard-coded Chinese literals + frozen label maps (ROLE_LABELS, NETWORK_LABELS, ACTIVATION_LABELS); parent-visible wording constrained by check-forbidden-ui-copy.
- Navigation: switchTab for tab pages, redirectTo/navigateTo elsewhere; guest pages enter via _enterGuestState().

## ANTI-PATTERNS
- Subscription dialogs must hang off a user tap (guardianPush.subscribeCrisisAlerts); never auto-invoked.
- Age bands must read 「申报」, never 「已核验」; unbind copy never claims data was kept (「已保留」) or that deletion is absolute.
- Sheet open: lock page scroll and hide tabBar via getTabBar().setData({hidden}) — the bar overlays bottom sheets.
- Unbind clears nickname/age/recent summaries/long-term archive (irreversible); network change or reprovision must NOT unbind.
- Profile in-flight key is authEpoch:user_id; different account/epoch must start a fresh request.
- Reprovision must start from a new QR (mode=reprovision&fresh=1); without fresh=1, onShow reattaches the old session.
