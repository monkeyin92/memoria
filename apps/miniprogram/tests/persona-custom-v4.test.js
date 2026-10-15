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

test("my personas list the version and voice name, never the persona or voice id", async () => {
  const { text } = await render(fx.personaApi);
  assert.match(text, /角色 自定义人格 写下名字和说话方式。/);
  assert.match(text, /我的人格 小北 v1 · 音色 星澜 删除/);
  assert.doesNotMatch(text, /cu_0123|回落|starlight|不落库/);
});

test("a persona whose fallback voice is unknown shows no voice instead of the first companion", async () => {
  const { text } = await render({
    listPersonas: async () => ({
      custom_personas: [{ persona_id: "cu_x", display_name: "阿木", persona_version: 2, fallback_designed_voice: "retired_voice" }],
    }),
  });
  assert.match(text, /阿木 v2 删除/);
  assert.doesNotMatch(text, /阿木 v2 · 音色/);
});

test("pickers and the rate slider use the shared v4 field chrome", async () => {
  const { html } = await render(fx.personaApi);
  const body = html.replace(/<style>[\s\S]*?<\/style>/g, "");
  assert.equal((body.match(/class="picker-row"/g) || []).length, 7);
  assert.equal((body.match(/chev-faint\.svg/g) || []).length, 7);
  assert.doesNotMatch(body, /row-chevron|page-head/);
});
