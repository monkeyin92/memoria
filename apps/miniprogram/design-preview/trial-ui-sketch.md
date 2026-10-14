# Trial UI sketch (architect)

## Usage (call sites)

```js
// home bound state
setData({
  online: true,
  usageUsedMinutes: 12,       // real when known; else 0
  usageLimitMinutes: 30,      // from Runtime Profile MAX_SESSION_SECONDS
  quietHoursLine: "夜间 21:00–07:00",
  unreadSafetyAlert: true,    // from undelivered/pinned guardian notifications
});

// device settings group (parent_for_child with sessionLimits)
setData({
  trialSettingsVisible: true,
  quietHoursLabel: "21:00 – 次日 07:00",
  dailyCapLabel: "30 分钟",
  safetySubscribeOn: true,
});
```

## Candidate A: page-local skins

Each page owns palette hexes and card chrome. No shared module.
Rejected: four copies of the same mint/apricot tokens drift on the next tweak.

## Candidate B: shared tokens + thin view helpers (chosen)

- CSS vars on `.trial-page` / page roots: `--trial-bg #FAF7F2`, `--trial-apricot #F5EBDD`, `--trial-mint #3CB8A0`.
- `utils/trial-device-card.js`: build home card fields from binding + profile + notifications.
- Settings stay on `pages/device` as an inset group when `sessionLimits` exists.
- Memory is `pages/child-memory` (privacy line + tabs already live there).
- Bind keeps #229 subscribe gate; this PR only skins copy/layout and does not re-add `crisis-alerts.js`.

## Synthesis

B wins on one palette source and one home view helper. No new page. No fifth route for alerts.
