const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { ROOT, loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");

const OWNING_TAB = {
  guardian: "/pages/profile/index",
  bind: "/pages/profile/index",
  "persona-custom": "/pages/profile/index",
  auth: "/pages/home/index",
  privacy: "/pages/profile/index",
  "digital-self": "/pages/profile/index",
  "child-memory": "/pages/home/index",
  "device-onboarding": "/pages/device/index",
};

function readPage(page, ext) {
  return fs.readFileSync(path.join(ROOT, "pages", page, `index.${ext}`), "utf8");
}

for (const [page, tab] of Object.entries(OWNING_TAB)) {
  test(`${page} draws its own round back button under the status bar instead of the native bar`, () => {
    assert.equal(JSON.parse(readPage(page, "json")).navigationStyle, "custom");
    const harness = loadPage(`pages/${page}/index`);
    try {
      assert.equal(harness.page.data.statusBarStyle, "--mp-status-bar: 47px;");
      const html = renderPage(`pages/${page}/index`, harness.page.data);
      assert.doesNotMatch(html, /class="wx-navbar"/);
      assert.match(html, /class="[^"]*\bsub-page\b[^"]*" style="--mp-status-bar: 47px;"/);
      assert.match(html, /class="nav-back"[^>]*><wx-image class="nav-back-icon"><img src="[^"]*\/assets\/ui\/v4\/back-strong\.svg"/);
      assert.doesNotMatch(readPage(page, "wxml"), /back-row|‹/);
    } finally {
      harness.restore();
    }
  });

  test(`${page} back pops the stack, or opens ${tab} when it is the first page`, () => {
    const harness = loadPage(`pages/${page}/index`);
    try {
      global.getCurrentPages = () => [{ route: "pages/home/index" }, { route: `pages/${page}/index` }];
      harness.page.goBack();
      global.getCurrentPages = () => [{ route: `pages/${page}/index` }];
      harness.page.goBack();
      const navigation = harness.wxCalls
        .filter((call) => ["navigateBack", "switchTab", "redirectTo", "reLaunch"].includes(call.name))
        .map((call) => [call.name, call.args[0]]);
      assert.deepEqual(navigation, [
        ["navigateBack", { delta: 1 }],
        ["switchTab", { url: tab }],
      ]);
    } finally {
      harness.restore();
    }
  });
}
