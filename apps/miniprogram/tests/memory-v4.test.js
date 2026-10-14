const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const NO_ALERTS = { getGuardianNotifications: async () => ({ items: [] }) };

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openMemory(overrides = NO_ALERTS) {
  const harness = loadPage("pages/memory/index", fx.authed(overrides));
  await harness.run();
  return harness;
}

test("summary days carry the date only and an unsummarised day says how many chats it had", async () => {
  const harness = await openMemory();
  try {
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /^按天查看 对话回顾 这里只有摘要，不显示原话，也没有录音。 三 14 二 13 一 12 日 11 六 10 五 9 /);
    assert.match(text, / 今天 10月14日 孩子聊了 3 次，还没整理 昨天 10月13日 孩子聊了 2 次，还没整理 /);
    assert.doesNotMatch(text, /日常回顾|· 孩子聊了/);
  } finally {
    harness.restore();
  }
});

test("整理选中这一天 turns today's card into the recap highlights", async () => {
  const harness = await openMemory();
  try {
    await harness.page.summarizeSelectedDay();
    await harness.settle();
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, / 今天 10月14日 讲了《大熊和森林里的悄悄话》 聊到了幼儿园的新朋友 一起数了天上的星星 昨天 10月13日 /);
  } finally {
    harness.restore();
  }
});

test("记住的事 opens from a row under the days and 日常回顾 goes back", async () => {
  const harness = await openMemory();
  try {
    const reviews = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(reviews, /分享这一天 记住的事 确认过的事，机器人才会在以后的聊天里提起 AI 整理可能有偏差/);

    harness.page.showMemories();
    assert.equal(harness.page.data.memoryTab, "memories");
    const memories = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(memories, /对话回顾 这里只有摘要，不显示原话，也没有录音。 日常回顾 记住的事 这里是孩子和机器人聊天时可能被记住的事。/);
    assert.doesNotMatch(memories, /整理选中这一天/);

    harness.page.showReviews();
    assert.equal(harness.page.data.memoryTab, "reviews");
  } finally {
    harness.restore();
  }
});
