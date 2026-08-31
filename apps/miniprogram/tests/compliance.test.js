const assert = require("node:assert/strict");
const test = require("node:test");

const compliance = require("../utils/compliance");

test("continuous use reminder fires after two hours in foreground", () => {
  const storage = {};
  const previousWx = global.wx;
  let modalShown = false;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showModal(options) {
      modalShown = true;
      assert.match(options.content, /家庭桌面档案终端/);
      assert.match(options.content, /人工智能生成/);
      options.success?.({ confirm: true });
    },
  };
  try {
    compliance.startForegroundSession();
    storage[compliance.SESSION_START_KEY] =
      Date.now() - compliance.FOREGROUND_REMINDER_MS - 1000;
    compliance.checkContinuousUseReminder();
    assert.equal(modalShown, true);
    assert.equal(storage[compliance.REMINDER_SHOWN_KEY], true);
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
  }
});

test("foreground session resets when app hides", () => {
  const storage = {};
  const previousWx = global.wx;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showModal() {
      assert.fail("reminder should not fire after hide");
    },
  };
  try {
    compliance.startForegroundSession();
    assert.ok(storage[compliance.SESSION_START_KEY]);
    compliance.endForegroundSession();
    assert.equal(storage[compliance.SESSION_START_KEY], undefined);
    compliance.checkContinuousUseReminder();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
  }
});
