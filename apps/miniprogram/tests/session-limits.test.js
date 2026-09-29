const test = require("node:test");
const assert = require("node:assert/strict");
const { sessionLimitsFromProfile, inQuietHours } = require("../utils/session-limits");

function profile(overrides = {}) {
  return {
    valid: true,
    subject_category: "minor",
    obligations: [
      { code: "MAX_SESSION_SECONDS", params: { max_session_seconds: 1800, retention_ttl_seconds: null, quiet_hours: null, extras: [] } },
      { code: "QUIET_HOURS", params: { max_session_seconds: null, retention_ttl_seconds: null, quiet_hours: ["21:00", "07:00"], extras: [] } },
    ],
    ...overrides,
  };
}

// 2026-09-28 15:44 UTC = 23:44 北京时间
const NIGHT = new Date(Date.UTC(2026, 8, 28, 15, 44));
// 2026-09-28 02:00 UTC = 10:00 北京时间
const DAY = new Date(Date.UTC(2026, 8, 28, 2, 0));

test("a minor's signed obligations become the displayed session limits", () => {
  assert.deepEqual(sessionLimitsFromProfile(profile(), NIGHT), {
    maxSessionMinutes: 30,
    quietStart: "21:00",
    quietEnd: "07:00",
    quietHoursLabel: "21:00 – 次日 07:00",
    inQuietHours: true,
    maxSessionLabel: "30 分钟",
  });
  assert.equal(sessionLimitsFromProfile(profile(), DAY).inQuietHours, false);
});

test("nothing is shown for adults, invalid profiles or missing obligations", () => {
  assert.equal(sessionLimitsFromProfile(profile({ subject_category: "adult" }), NIGHT), null);
  assert.equal(sessionLimitsFromProfile(profile({ valid: false }), NIGHT), null);
  assert.equal(sessionLimitsFromProfile(profile({ obligations: [] }), NIGHT), null);
  assert.equal(sessionLimitsFromProfile(null, NIGHT), null);
  // v1 profiles carry bare codes without params.
  assert.equal(sessionLimitsFromProfile(profile({ obligations: ["QUIET_HOURS"] }), NIGHT), null);
});

test("quiet hours follow Beijing time and the agent's [start, end) window", () => {
  assert.equal(inQuietHours(["21:00", "07:00"], new Date(Date.UTC(2026, 8, 28, 13, 0))), true); // 21:00
  assert.equal(inQuietHours(["21:00", "07:00"], new Date(Date.UTC(2026, 8, 28, 23, 0))), false); // 07:00
  assert.equal(inQuietHours(["12:00", "14:00"], new Date(Date.UTC(2026, 8, 28, 5, 0))), true); // 13:00
  assert.equal(inQuietHours(["21:00", "21:00"], NIGHT), false);
});

test("a same-day window has no 次日 marker", () => {
  const limits = sessionLimitsFromProfile(
    profile({ obligations: [{ code: "QUIET_HOURS", params: { max_session_seconds: null, retention_ttl_seconds: null, quiet_hours: ["12:00", "14:00"], extras: [] } }] }),
    DAY,
  );
  assert.equal(limits.quietHoursLabel, "12:00 – 14:00");
  assert.equal(limits.maxSessionLabel, "");
});
