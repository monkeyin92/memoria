const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");
const { companions } = require("../utils/companions");

async function loadCompanion(api) {
  const harness = loadPage("pages/companion/index", { api: fx.baseApi(api), now: fx.NOW });
  const data = await harness.run();
  return { harness, data };
}

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

test("voices render as a vertical list with the account pick marked current", async () => {
  const { harness, data } = await loadCompanion();
  try {
    const html = renderPage("pages/companion/index", data).replace(/<style>[\s\S]*?<\/style>/, "");
    const text = visibleText(html);
    assert.equal((html.match(/class="voice-option /g) || []).length, companions.length);
    assert.equal((html.match(/voice-option-on/g) || []).length, 1);
    assert.match(text, /声音与表达 声音 选择一种音色与说话方式。/);
    assert.match(text, /Memoria 自然、清晰 统一默认声音 已选 试听/);
    assert.match(text, /自定义表达/);
    assert.equal((text.match(/试听(?= )/g) || []).length, companions.length);
    assert.doesNotMatch(html, /--role-|deck-/);
    assert.equal((html.match(/class="voice-portrait"/g) || []).length, companions.length);
  } finally {
    harness.restore();
  }
});

test("tapping the role already in use does not save again", async () => {
  const writes = [];
  const { harness } = await loadCompanion({
    updateProfile: async (userId, profile) => {
      writes.push(profile.companion_id);
      return { ...profile };
    },
  });
  try {
    await harness.page.chooseCompanion({ currentTarget: { dataset: { id: "starlight" } } });
    assert.deepEqual(writes, []);
    assert.equal(harness.page.data.profile.companion_id, "starlight");
    assert.equal(harness.page.data.currentName, "Memoria");
  } finally {
    harness.restore();
  }
});

test("自定义表达 opens the custom persona page", async () => {
  const { harness } = await loadCompanion();
  try {
    harness.page.openCustomPersona();
    const nav = harness.wxCalls.find((call) => call.name === "navigateTo");
    assert.equal(nav.args[0].url, "/pages/persona-custom/index");
  } finally {
    harness.restore();
  }
});

test("the voice row previews the role in use and stops on a second tap", async () => {
  const { harness } = await loadCompanion();
  const played = [];
  try {
    global.wx.createInnerAudioContext = () => ({
      play() { played.push(this.src); },
      stop() {},
      destroy() {},
      onEnded() {},
      onError() {},
    });
    harness.page.togglePreview({ currentTarget: { dataset: { id: "starlight" } } });
    assert.deepEqual(played, ["/assets/voices/warm_companion.mp3"]);
    assert.equal(harness.page.data.previewingId, "starlight");
    harness.page.togglePreview({ currentTarget: { dataset: { id: "starlight" } } });
    assert.equal(harness.page.data.previewingId, "");
  } finally {
    harness.restore();
  }
});
