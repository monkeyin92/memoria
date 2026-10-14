const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

async function loadDigitalSelf(overrides) {
  const harness = loadPage("pages/digital-self/index", fx.authed(overrides));
  await harness.run();
  return harness;
}

const text = (harness) => visibleText(renderPage("pages/digital-self/index", harness.page.data));

test("digital-self counts confirmed sources and labels every growth dimension", async () => {
  const harness = await loadDigitalSelf(fx.digitalSelfApi);
  try {
    const shown = text(harness);
    assert.match(shown, /8 条已确认的来源 3 \/ 7 个方面已有依据 已启用/);
    assert.match(shown, /人生阶段 已有依据 · 已在当前版本里 4 条/);
    assert.match(shown, /说话方式 有内容待你确认 · 当前版本待更新 1 条/);
    assert.match(shown, /做决定的方式 还没有内容 0 条/);
    assert.match(shown, /声音 暂未开放 0 条/);
    assert.doesNotMatch(shown, /life_chapters|supported|not_built|dependency_pending|加密|已开启|%/);
  } finally {
    harness.restore();
  }
});

test("version records stay folded until tapped, then list labelled versions", async () => {
  const harness = await loadDigitalSelf(fx.digitalSelfApi);
  try {
    assert.match(text(harness), /版本记录 已生成 1 个版本/);
    assert.doesNotMatch(text(harness), /版本 1 /);
    harness.page.toggleEngineering();
    assert.match(text(harness), /版本记录 收起版本记录 版本 1 已通过 · 已绑定资料摘要/);
  } finally {
    harness.restore();
  }
});

test("digital-self with no growth data shows zero and the empty state", async () => {
  const harness = await loadDigitalSelf();
  try {
    assert.match(text(harness), /0 条已确认的来源 0 \/ 7 个方面已有依据 尚未启用 各个方面 还没有可展示的内容/);
  } finally {
    harness.restore();
  }
});
