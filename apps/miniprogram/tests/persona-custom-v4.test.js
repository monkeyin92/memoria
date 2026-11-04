const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

async function render(overrides) {
  const harness = loadPage("pages/persona-custom/index", fx.authed(overrides));
  const data = await harness.run();
  const html = renderPage("pages/persona-custom/index", data);
  harness.restore();
  return { html, text: visibleText(html) };
}

test("custom expressions show the default voice without a voice selector", async () => {
  const { text } = await render(fx.personaApi);
  assert.match(text, /说话方式 自定义表达 写下名字和说话方式。/);
  assert.match(text, /我的人格 小北 v1 · 默认声音 删除/);
  assert.doesNotMatch(text, /克隆|用谁的音色/);
  assert.doesNotMatch(text, /cu_0123|回落|starlight|不落库/);
});

test("legacy fallback voice keys never appear as a selectable sound", async () => {
  const { text } = await render({
    listPersonas: async () => ({
      custom_personas: [{ persona_id: "cu_x", display_name: "阿木", persona_version: 2, fallback_designed_voice: "retired_voice" }],
    }),
  });
  assert.match(text, /阿木 v2 · 默认声音 删除/);
  assert.doesNotMatch(text, /retired_voice|音色 阿木/);
});

test("pickers and the rate slider use the shared v4 field chrome", async () => {
  const { html } = await render(fx.personaApi);
  const body = html.replace(/<style>[\s\S]*?<\/style>/g, "");
  assert.equal((body.match(/class="picker-row"/g) || []).length, 6);
  assert.equal((body.match(/chev-faint\.svg/g) || []).length, 6);
  assert.doesNotMatch(body, /row-chevron|page-head/);
});
