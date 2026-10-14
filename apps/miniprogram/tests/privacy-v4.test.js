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

test("privacy states raw audio retention as kept until revoked, never a 7-day timer", async () => {
  const harness = await loadPrivacy();
  try {
    const shown = text(harness);
    assert.match(shown, /原始语音归档 默认关闭 开启后，机器人对话的原始语音会保存在你的账号下，直到你撤回授权。/);
    assert.match(shown, /明确授权原始语音归档/);
    assert.doesNotMatch(shown, /7 天|书面回执|可证明|&#10;|Cannot read/);
  } finally {
    harness.restore();
  }
});

test("granting then revoking raw audio updates the card from the server responses", async () => {
  const calls = [];
  const harness = await loadPrivacy({
    grantRawVoiceConsent: async () => {
      calls.push("grant");
      return fx.RAW_VOICE_CONSENT;
    },
    revokeRawVoiceConsent: async () => {
      calls.push("revoke");
      return {};
    },
  });
  try {
    await harness.page.grant();
    assert.match(text(harness), /原始语音归档 2026年10月4日授权 .* 撤回并删除录音/);
    await harness.page.revoke();
    assert.deepEqual(calls, ["grant", "revoke"]);
    assert.equal(harness.page.data.consent, null);
    assert.match(text(harness), /原始语音归档 默认关闭/);
  } finally {
    harness.restore();
  }
});

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
