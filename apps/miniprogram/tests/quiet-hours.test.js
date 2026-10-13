const test = require("node:test");
const assert = require("node:assert/strict");
const {
  DEFAULT_QUIET_HOURS,
  quietHoursDurationMinutes,
  quietHoursNeedsLengthConfirm,
  nightModeQuietHoursLabel,
  confirmUnusualQuietHours,
} = require("../utils/quiet-hours");

test("default quiet hours are 21:00–07:00 with the exact night-mode label", () => {
  assert.deepEqual(DEFAULT_QUIET_HOURS, { start: "21:00", end: "07:00" });
  assert.equal(
    nightModeQuietHoursLabel(DEFAULT_QUIET_HOURS),
    "夜间模式：21:00–07:00（这段时间点屏只会说晚安）",
  );
  assert.equal(
    nightModeQuietHoursLabel("21:00", "07:00"),
    "夜间模式：21:00–07:00（这段时间点屏只会说晚安）",
  );
});

test("duration is cross-midnight aware", () => {
  assert.equal(quietHoursDurationMinutes("21:00", "07:00"), 10 * 60);
  assert.equal(quietHoursDurationMinutes("22:00", "06:30"), 8 * 60 + 30);
  assert.equal(quietHoursDurationMinutes("23:30", "00:15"), 45);
  assert.equal(quietHoursDurationMinutes("12:00", "14:00"), 2 * 60);
  assert.equal(quietHoursDurationMinutes("09:00", "21:30"), 12 * 60 + 30);
  assert.equal(quietHoursDurationMinutes("21:00", "21:00"), 0);
});

test("windows shorter than 1 hour or longer than 12 hours need confirm", () => {
  assert.equal(quietHoursNeedsLengthConfirm("21:00", "07:00"), false);
  assert.equal(quietHoursNeedsLengthConfirm("22:00", "06:00"), false);
  assert.equal(quietHoursNeedsLengthConfirm("12:00", "13:00"), false);
  assert.equal(quietHoursNeedsLengthConfirm("12:00", "12:30"), true);
  assert.equal(quietHoursNeedsLengthConfirm("23:45", "00:15"), true);
  assert.equal(quietHoursNeedsLengthConfirm("09:00", "21:30"), true);
  assert.equal(quietHoursNeedsLengthConfirm("20:00", "08:30"), true);
  assert.equal(quietHoursNeedsLengthConfirm("21:00", "21:00"), false);
});

test("confirmUnusualQuietHours skips the modal for a usual window", async () => {
  let called = 0;
  const api = {
    showModal() {
      called += 1;
    },
  };
  assert.equal(
    await confirmUnusualQuietHours({ start: "21:00", end: "07:00", api }),
    true,
  );
  assert.equal(called, 0);
});

test("confirmUnusualQuietHours only proceeds when the guardian confirms", async () => {
  const api = {
    showModal(options) {
      assert.match(options.content, /23:45–00:15/);
      assert.match(options.content, /30 分钟/);
      options.success({ confirm: true });
    },
  };
  assert.equal(
    await confirmUnusualQuietHours({ start: "23:45", end: "00:15", api }),
    true,
  );

  const cancelApi = {
    showModal(options) {
      options.success({ confirm: false, cancel: true });
    },
  };
  assert.equal(
    await confirmUnusualQuietHours({ start: "09:00", end: "22:00", api: cancelApi }),
    false,
  );
});
