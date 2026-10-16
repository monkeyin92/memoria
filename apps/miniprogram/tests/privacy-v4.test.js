process.env.TZ = "Asia/Shanghai";

const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

async function loadPrivacy(overrides = {}) {
  const harness = loadPage("pages/privacy/index", fx.authed(overrides));
  await harness.run();
  const confirmModal = (options) => options.success && options.success({ confirm: true });
  global.wx.showModal = confirmModal;
  global.wx.showToast = () => {};
  return harness;
}

const text = (harness) => visibleText(renderPage("pages/privacy/index", harness.page.data));

test("long-term memory row opens the child memory page instead of a local switch", async () => {
  const harness = await loadPrivacy();
  try {
    assert.equal("onMemorySwitch" in harness.page, false);
    harness.page.openChildMemory();
    const nav = harness.wxCalls.find((call) => call.name === "navigateTo");
    assert.equal(nav.args[0].url, "/pages/child-memory/index");
  } finally {
    harness.restore();
  }
});
