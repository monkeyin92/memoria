const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const ALERT_COPY = /有一条需要你关注的对话/g;

function alert(notificationId, minutesAgo, categoryLabel) {
  return { ...fx.SAFETY_ALERT, notification_id: notificationId, occurred_at: fx.isoMinutesAgo(minutesAgo), category_label: categoryLabel };
}

function threeAlerts() {
  return [alert("n3", 0, "情绪和安全"), alert("n2", 30, "人身安全"), alert("n1", 90, "孩子提到的安全话题")];
}

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function openedUrls(harness) {
  return harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url);
}

async function homeScreen(store) {
  const harness = loadPage("pages/home/index", fx.authed(store));
  try {
    await harness.run();
    const text = visibleText(renderPage("pages/home/index", harness.page.data));
    if (harness.page.data.unreadSafetyAlert) harness.page.openUnreadSafety();
    return { pins: (text.match(ALERT_COPY) || []).length, opens: openedUrls(harness) };
  } finally {
    harness.restore();
  }
}

function reviewBanner(harness) {
  const html = renderPage("pages/memory/index", harness.page.data, { tabBar: false });
  const id = html.match(/class="alert-banner"[^>]*?data-id="([^"]*)"/)?.[1];
  if (id !== undefined) harness.page.openAttention({ currentTarget: { dataset: { id } } });
  return { text: visibleText(html), opens: openedUrls(harness) };
}

async function reviewScreen(store) {
  const harness = loadPage("pages/memory/index", fx.authed(store));
  try {
    await harness.run();
    return reviewBanner(harness);
  } finally {
    harness.restore();
  }
}

async function acknowledgeFromAttention(store, url) {
  const harness = loadPage("pages/attention/index", fx.authed(store));
  try {
    await harness.run({ id: decodeURIComponent(url.split("id=")[1]) });
    global.getCurrentPages = () => [{ route: "pages/home/index" }, { route: "pages/attention/index" }];
    await harness.page.acknowledge();
    return harness.wxCalls.filter((call) => call.name === "navigateBack").length;
  } finally {
    harness.restore();
  }
}

test("no unread alert puts no safety banner on home or 回顾", async () => {
  const store = fx.safetyAlertApi([]);
  assert.deepEqual(await homeScreen(store), { pins: 0, opens: [] });
  const review = await reviewScreen(store);
  assert.doesNotMatch(review.text, /需要你关注|还有/);
  assert.deepEqual(review.opens, []);
});

test("one unread alert shows on home and 回顾 without a 还有 line", async () => {
  const store = fx.safetyAlertApi([fx.SAFETY_ALERT]);
  assert.deepEqual(await homeScreen(store), { pins: 1, opens: ["/pages/attention/index?id=n1"] });
  const review = await reviewScreen(store);
  assert.match(review.text, / 有一条需要你关注的对话 今天 19:42 今天 10月14日 /);
  assert.doesNotMatch(review.text, /还有/);
  assert.deepEqual(review.opens, ["/pages/attention/index?id=n1"]);
});

test("three unread alerts show the newest once, and 回顾 says 还有 2 条", async () => {
  const store = fx.safetyAlertApi(threeAlerts());
  assert.deepEqual(await homeScreen(store), { pins: 1, opens: ["/pages/attention/index?id=n3"] });
  const review = await reviewScreen(store);
  assert.equal((review.text.match(ALERT_COPY) || []).length, 1);
  assert.match(review.text, / 有一条需要你关注的对话 今天 19:42 还有 2 条 今天 10月14日 /);
  assert.deepEqual(review.opens, ["/pages/attention/index?id=n3"]);
});

test("after 我知道了 a later login shows the next unread alert, never the acknowledged one", async () => {
  const store = fx.safetyAlertApi(threeAlerts());
  const [firstUrl] = (await homeScreen(store)).opens;
  assert.equal(await acknowledgeFromAttention(store, firstUrl), 1);

  assert.deepEqual(await homeScreen(store), { pins: 1, opens: ["/pages/attention/index?id=n2"] });
  const review = await reviewScreen(store);
  assert.match(review.text, / 有一条需要你关注的对话 今天 19:12 还有 1 条 今天 10月14日 /);
  assert.deepEqual(review.opens, ["/pages/attention/index?id=n2"]);

  for (const { opens } of [await homeScreen(store), await reviewScreen(store)]) {
    assert.ok(!opens.includes(firstUrl));
  }
});

test("coming back to 回顾 after 我知道了 drops the acknowledged alert", async () => {
  const store = fx.safetyAlertApi(threeAlerts().slice(0, 2));
  const harness = loadPage("pages/memory/index", fx.authed(store));
  try {
    await harness.run();
    assert.match(reviewBanner(harness).text, / 今天 19:42 还有 1 条 /);
    await store.markGuardianNotificationRead("n3");
    harness.page.onShow();
    await harness.settle();
    const { text } = reviewBanner(harness);
    assert.match(text, / 有一条需要你关注的对话 今天 19:12 今天 10月14日 /);
    assert.doesNotMatch(text, /还有/);
  } finally {
    harness.restore();
  }
});
