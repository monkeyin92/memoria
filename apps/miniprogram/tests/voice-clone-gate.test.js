const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const fx = require("../scripts/mp-harness/fixtures");
const { isVoiceCloneOpen } = require("../utils/voice-clone-gate");

const PAGE = "pages/profile/index";

test("isVoiceCloneOpen is true only for a literal boolean true", () => {
  assert.equal(isVoiceCloneOpen({ voice_clone_enabled: false }), false);
  assert.equal(isVoiceCloneOpen({}), false);
  assert.equal(isVoiceCloneOpen(null), false);
  assert.equal(isVoiceCloneOpen({ voice_clone_enabled: "true" }), false);
  assert.equal(isVoiceCloneOpen({ voice_clone_enabled: 1 }), false);
  assert.equal(isVoiceCloneOpen({ voice_clone_enabled: true }), true);
});

test("profile stays closed when delivered capabilities say the switch is off", async () => {
  const harness = loadPage(PAGE, fx.authed({
    getDeliveredCapabilities: async () => ({ items: [], voice_clone_enabled: false }),
  }));
  try {
    await harness.run();
    assert.equal(harness.page.data.voiceCloneOpen, false);
  } finally {
    harness.restore();
  }
});

test("a failed capabilities load closes the gate", async () => {
  const holder = { load: async () => ({ items: [], voice_clone_enabled: true }) };
  const harness = loadPage(PAGE, fx.authed({
    getDeliveredCapabilities: () => holder.load(),
  }));
  try {
    await harness.run();
    assert.equal(harness.page.data.voiceCloneOpen, true);
    holder.load = async () => {
      throw new Error("capabilities unavailable");
    };
    await harness.page.loadDeliveredCapabilities();
    assert.equal(harness.page.data.voiceCloneOpen, false);
  } finally {
    harness.restore();
  }
});

test("leaving the signed-in profile closes the gate", async () => {
  const harness = loadPage(PAGE, fx.authed({
    getDeliveredCapabilities: async () => ({ items: [], voice_clone_enabled: true }),
  }));
  try {
    await harness.run();
    assert.equal(harness.page.data.voiceCloneOpen, true);
    harness.page._enterGuestState();
    assert.equal(harness.page.data.voiceCloneOpen, false);
  } finally {
    harness.restore();
  }
});

test("a guest profile does not open voice clone", async () => {
  const harness = loadPage(PAGE, fx.guest());
  try {
    await harness.run();
    assert.equal(harness.page.data.voiceCloneOpen, false);
  } finally {
    harness.restore();
  }
});

test("profile markup does not offer voice clone", () => {
  const wxml = fs.readFileSync(path.join(__dirname, "../pages/profile/index.wxml"), "utf8");
  assert.equal(wxml.includes("声音复刻"), false);
  assert.equal(wxml.includes("voiceCloneOpen"), false);
});
