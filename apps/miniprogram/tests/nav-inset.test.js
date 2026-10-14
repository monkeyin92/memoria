const assert = require("node:assert/strict");
const test = require("node:test");

function styleWith(wx) {
  const previous = global.wx;
  global.wx = wx;
  try {
    delete require.cache[require.resolve("../utils/nav-inset")];
    return require("../utils/nav-inset").statusBarStyle();
  } finally {
    if (previous === undefined) delete global.wx;
    else global.wx = previous;
  }
}

test("the style carries the status bar and the real capsule bottom, which sits lower on Android", () => {
  const style = styleWith({
    getWindowInfo: () => ({ statusBarHeight: 28 }),
    getMenuButtonBoundingClientRect: () => ({ top: 36, bottom: 68 }),
  });
  assert.equal(style, "--mp-status-bar: 28px; --mp-capsule-bottom: 68px;");
});

test("without a capsule rect the style assumes a capsule 40px below the status bar", () => {
  const style = styleWith({
    getWindowInfo: () => ({ statusBarHeight: 20 }),
    getMenuButtonBoundingClientRect: () => {
      throw new Error("not ready");
    },
  });
  assert.equal(style, "--mp-status-bar: 20px; --mp-capsule-bottom: 60px;");
});
