const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const stripStyle = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "");
const visibleText = (html) => stripStyle(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

async function bootForm() {
  const harness = loadPage("pages/bind/index", fx.authed(fx.bindApi));
  await harness.run(fx.BIND_QUERY);
  await harness.page.chooseMode({ currentTarget: { dataset: { mode: "parent_for_child" } } });
  await harness.settle();
  return harness;
}

function view(harness) {
  const html = renderPage("pages/bind/index", harness.page.data);
  return { body: stripStyle(html), text: visibleText(html) };
}

test("the form step shows the shared default voice and no role picker", async () => {
  const harness = await bootForm();
  try {
    const { body, text } = view(harness);
    assert.match(text, /声音与表达 默认声音 所有机器人共用/);
    assert.doesNotMatch(text, /试听|已选择|选这个|星澜|桃喜/);
    assert.doesNotMatch(body, /bindtap="onPersonaTap"|catchtap="togglePreview"|personaOptions/);
    assert.equal(harness.page.onPersonaTap, undefined);
    assert.equal(harness.page.togglePreview, undefined);
  } finally {
    harness.restore();
  }
});

test("the review step labels itself as steps 3 and 4 and keeps 上一步 inside the card", async () => {
  const harness = await bootForm();
  try {
    harness.page.setData({ "form.childNickname": "乐乐" });
    await harness.page.goToReview();
    await harness.settle();
    const { text } = view(harness);
    assert.match(text, /第 3、4 步 · 确认授权并提交 绑定设备 确认一下 核对下面的信息，勾选需要的授权后提交 设备用途 给孩子使用 使用者 乐乐 声音 默认声音/);
    assert.match(text, /上一步 提交绑定/);
    assert.doesNotMatch(text, /试听|星澜|桃喜/);
  } finally {
    harness.restore();
  }
});
