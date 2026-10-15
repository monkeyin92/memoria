const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { ROOT, loadPage } = require("../scripts/mp-harness/runtime");
const { shippedFiles } = require("../scripts/package-size");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const SECOND_ALERT = { ...fx.PINNED_ALERT, notification_id: "n2", occurred_at: "2026-10-12T08:05:00+08:00" };

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openAttention(query, overrides) {
  const harness = loadPage("pages/attention/index", fx.authed(overrides));
  await harness.run(query);
  return harness;
}

test("four conversation tips ending with the 12356 hotline, and 我知道了, never the words", async () => {
  const harness = await openAttention({ id: "n1" });
  try {
    assert.equal(
      visibleText(renderPage("pages/attention/index", harness.page.data)),
      "有一条需要你关注的对话 今天 19:42 孩子在聊天中提到了让 TA 不舒服的事。为了保护孩子，这里不展示对话内容。 " +
        "可以这样和孩子聊 1 找一个放松的时间 比如睡前或散步时，不要急着追问 2 先听，少评判 “愿意和我说说今天的事吗？” " +
        "3 告诉孩子你在 “不管发生什么，你都可以来找我。” 4 需要时寻求帮助 可拨打 12356 心理援助热线 我知道了",
    );
  } finally {
    harness.restore();
  }
});

test("an interrupted crisis script keeps the headline and shows the unfinished line", async () => {
  const selfHarm = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356";
  const victim = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线110";
  const harness = await openAttention({ id: "n1" }, {
    getGuardianNotifications: async () => ({
      items: [
        { ...fx.PINNED_ALERT, notification_id: "n1", detail: selfHarm },
        { ...fx.PINNED_ALERT, notification_id: "n-victim", detail: victim },
      ],
    }),
  });
  try {
    assert.equal(harness.page.data.alert.message, "有一条需要你关注的对话");
    assert.equal(harness.page.data.alert.detail, selfHarm);
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.ok(text.includes(selfHarm));
    assert.ok(text.startsWith("有一条需要你关注的对话"));
  } finally {
    harness.restore();
  }

  const victimPage = await openAttention({ id: "n-victim" }, {
    getGuardianNotifications: async () => ({
      items: [{ ...fx.PINNED_ALERT, notification_id: "n-victim", detail: victim }],
    }),
  });
  try {
    assert.equal(victimPage.page.data.alert.message, "有一条需要你关注的对话");
    assert.equal(victimPage.page.data.alert.detail, victim);
  } finally {
    victimPage.restore();
  }
});

test("the attention page opens the alert it was linked to", async () => {
  const harness = await openAttention({ id: "n2" }, {
    getGuardianNotifications: async () => ({ items: [fx.PINNED_ALERT, SECOND_ALERT] }),
  });
  try {
    assert.deepEqual(harness.page.data.alert, { notificationId: "n2", message: "有一条需要你关注的对话", timeLabel: "10月12日 08:05" });
  } finally {
    harness.restore();
  }
});

test("a stale attention link says nothing needs attention instead of showing an old alert", async () => {
  const harness = await openAttention({ id: "gone" }, { getGuardianNotifications: async () => ({ items: [] }) });
  try {
    assert.equal(harness.page.data.alert, null);
    assert.equal(
      visibleText(renderPage("pages/attention/index", harness.page.data)),
      "现在没有需要关注的对话 有新的提醒时，首页会置顶显示。 我知道了",
    );
  } finally {
    harness.restore();
  }
});

test("我知道了 goes back to where the parent came from", async () => {
  const harness = await openAttention({ id: "n1" });
  try {
    global.getCurrentPages = () => [{ route: "pages/home/index" }, { route: "pages/attention/index" }];
    harness.page.goBack();
    const navigation = harness.wxCalls.filter((call) => call.name === "navigateBack").map((call) => call.args[0]);
    assert.deepEqual(navigation, [{ delta: 1 }]);
  } finally {
    harness.restore();
  }
});

test("the home attention card and the review banner both open the attention page", async () => {
  const home = loadPage("pages/home/index", fx.authed());
  try {
    await home.run();
    home.page.openUnreadSafety();
    assert.deepEqual(
      home.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url),
      ["/pages/attention/index?id=n1"],
    );
  } finally {
    home.restore();
  }

  const memory = loadPage("pages/memory/index", fx.authed());
  try {
    await memory.run();
    const text = visibleText(renderPage("pages/memory/index", memory.page.data, { tabBar: false }));
    assert.match(text, /^按天查看 对话回顾 这里只有摘要，不显示原话，也没有录音。 有一条需要你关注的对话 今天 19:42 三 14 /);
    memory.page.openAttention({ currentTarget: { dataset: { id: "n1" } } });
    assert.deepEqual(
      memory.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url),
      ["/pages/attention/index?id=n1"],
    );
  } finally {
    memory.restore();
  }
});

test("12356 ships only in the attention page's fourth tip", () => {
  const hits = shippedFiles()
    .filter((file) => /\.(js|wxml|json|wxss)$/.test(file))
    .filter((file) => fs.readFileSync(path.join(ROOT, file), "utf8").includes("12356"));
  assert.deepEqual(hits, ["pages/attention/index.wxml"]);
});
