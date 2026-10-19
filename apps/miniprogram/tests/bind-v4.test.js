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

const tapRole = (harness, index) => harness.page.onPersonaTap({ currentTarget: { dataset: { index } } });
const tapPreview = (harness) => harness.page.togglePreview({ currentTarget: { dataset: { index: harness.page.data.personaIndex } } });
const played = (harness) => harness.wxCalls.filter((call) => call.name === "InnerAudioContext.play").map((call) => call.args[0]);

test("the form step lays every AI role out as a grid card with exactly one selected", async () => {
  const harness = await bootForm();
  const { body, text } = view(harness);
  harness.restore();
  assert.equal((body.match(/class="role-card[ "]/g) || []).length, 5);
  assert.equal((body.match(/role-card-on/g) || []).length, 1);
  assert.match(text, /星澜 温和清晰，留一点想象 已选择 桃喜 明朗轻快，分享小乐趣 选这个/);
  assert.match(text, /声音 · 暖阳青年 清澈、自然 试听/);
  assert.doesNotMatch(body, /swiper/);
});

test("tapping a role selects it and the voice card follows the selection", async () => {
  const harness = await bootForm();
  tapRole(harness, 1);
  const { text } = view(harness);
  harness.restore();
  assert.match(text, /星澜 温和清晰，留一点想象 选这个 桃喜 明朗轻快，分享小乐趣 已选择/);
  assert.match(text, /声音 · 元气搭子 明亮、轻盈 试听/);
});

test("试听 plays the selected role's voice, and picking another role stops it", async () => {
  const harness = await bootForm();
  tapRole(harness, 2);
  tapPreview(harness);
  assert.deepEqual(played(harness), ["/assets/voices/soft_confidante.mp3"]);
  assert.match(view(harness).text, /声音 · 温柔知己 轻柔、舒缓 停止/);

  tapRole(harness, 3);
  const { text } = view(harness);
  const stopped = harness.wxCalls.filter((call) => call.name === "InnerAudioContext.stop").length;
  harness.restore();
  assert.equal(stopped, 1);
  assert.match(text, /声音 · 沉稳向导 平稳、利落 试听/);
});

test("the review step labels itself as steps 3 and 4 and keeps 上一步 inside the card", async () => {
  const harness = await bootForm();
  harness.page.setData({ "form.childNickname": "乐乐" });
  await harness.page.goToReview();
  await harness.settle();
  const { text } = view(harness);
  harness.restore();
  assert.match(text, /第 3、4 步 · 确认授权并提交 绑定设备 确认一下 核对下面的信息，勾选需要的授权后提交 设备用途 给孩子使用 使用者 乐乐 AI 角色 星澜/);
  assert.match(text, /上一步 开启安全提醒/);
});
