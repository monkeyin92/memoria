process.env.TZ = "Asia/Shanghai";

const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

async function loadGuardian(overrides = {}) {
  const harness = loadPage("pages/guardian/index", fx.authed({ ...fx.guardianApi, ...overrides }));
  const data = await harness.run();
  return { harness, text: visibleText(renderPage("pages/guardian/index", data)) };
}

test("guardian opens on a large title and the bound child's weekly bars", async () => {
  const { harness, text } = await loadGuardian();
  try {
    assert.match(text, /家庭监护 · 只看趋势 家庭与监护 只看聚合趋势，不展示孩子与角色的对话原文。/);
    assert.match(text, /本周成长小结 不含对话原文 10月8日 至 10月14日 学习陪伴 1 小时 35 分钟/);
    assert.match(text, /情绪趋势 愉快 6 平静 4 有些担心 1 话题分布 兴趣爱好 · 5 校园生活 · 3 家庭日常 · 2/);
    assert.equal(harness.page.data.summary.emotionItems[0].width, "55%");
    assert.doesNotMatch(text, /家人/);
  } finally {
    harness.restore();
  }
});

test("guardian alerts show the child, a readable time and no delivery internals", async () => {
  const { harness, text } = await loadGuardian();
  try {
    assert.match(text, /需要及时关注 乐乐 有一条需要你关注的对话 今天 19:42/);
    assert.doesNotMatch(text, /no_subscription|2026-10-14T/);
  } finally {
    harness.restore();
  }
});

test("guardian alerts without a name or valid time fall back to TA and drop the time line", async () => {
  const { harness, text } = await loadGuardian({
    getGuardianNotifications: async () => ({ items: [{ notification_id: "n2", occurred_at: "not a time" }, { message: "missing id" }] }),
  });
  try {
    assert.deepEqual(
      harness.page.data.notifications.map(({ notificationId, minorDisplayName, timeLabel }) => [notificationId, minorDisplayName, timeLabel]),
      [["n2", "TA", ""]],
    );
    assert.match(text, /需要及时关注 TA 有一条需要你关注的对话 /);
  } finally {
    harness.restore();
  }
});
