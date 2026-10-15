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

test("roles render as a grid card per companion with the account's pick selected", async () => {
  const { harness, data } = await loadCompanion();
  try {
    const html = renderPage("pages/companion/index", data).replace(/<style>[\s\S]*?<\/style>/, "");
    const text = visibleText(html);
    assert.equal((html.match(/class="role-card[ "]/g) || []).length, companions.length);
    assert.equal((html.match(/role-card-on/g) || []).length, 1);
    assert.match(text, /说话风格 角色 换个说话风格。它始终是陪孩子聊天、讲故事的 AI 助手。/);
    assert.match(text, /星澜 温和清晰，留一点想象 正在使用/);
    assert.equal((text.match(/切换/g) || []).length, companions.length - 1);
    assert.match(text, /新建角色/);
    assert.match(text, /角色默认声音 星澜 · 清澈、自然 试听/);
    assert.doesNotMatch(html, /--role-|deck-/);
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
    await harness.page.chooseCompanion({ currentTarget: { dataset: { id: "mianmian" } } });
    assert.deepEqual(writes, ["mianmian"]);
    assert.equal(harness.page.data.profile.companion_id, "mianmian");
    assert.equal(harness.page.data.currentName, "绵绵");
  } finally {
    harness.restore();
  }
});

test("新建角色 opens the custom persona page", async () => {
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
