const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { ROOT, loadPage } = require("../scripts/mp-harness/runtime");
const { shippedFiles } = require("../scripts/package-size");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const VICTIM_ALERT = {
  ...fx.SAFETY_ALERT,
  notification_id: "n2",
  occurred_at: "2026-10-12T08:05:00+08:00",
  occurred_label: "10月12日 08:05",
  category_label: "人身安全",
  summary: "孩子说到了自己可能受到伤害",
  parent_action: "尽快当面陪孩子聊聊，先让孩子安心，不反复追问细节。如有危险请拨打110",
};

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openAttention(query, overrides) {
  const harness = loadPage("pages/attention/index", fx.authed(overrides));
  await harness.run(query);
  global.getCurrentPages = () => [{ route: "pages/home/index" }, { route: "pages/attention/index" }];
  return harness;
}

function navigations(harness) {
  return harness.wxCalls.filter((call) => call.name === "navigateBack").map((call) => call.args[0]);
}

function failingSave(error) {
  const calls = [];
  return {
    calls,
    markGuardianNotificationRead: async (id) => {
      calls.push(id);
      throw error;
    },
  };
}

const SELF_HARM_UNFINISHED = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356";
const VICTIM_UNFINISHED = "如有危险请拨打110";

test("the detail shows category, summary, what the robot said and what to do, never the words", async () => {
  const harness = await openAttention({ id: "n1" });
  try {
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.equal(
      text,
      "有一条需要你关注的对话 10月14日 19:42 情绪和安全 孩子说到了和自己情绪、安全有关的内容 " +
        "机器人已提醒孩子去找身边的大人 " +
        "找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356 我知道了",
    );
    assert.equal(text.includes("这段总结由 AI 生成，可能不完全准确"), false);
    assert.equal(text.includes("我不想活了"), false);
  } finally {
    harness.restore();
  }
});

test("an interrupted crisis script keeps the headline and shows the server sentence verbatim", async () => {
  const harness = await openAttention(
    { id: "n1" },
    fx.safetyAlertApi([{ ...fx.SAFETY_ALERT, notification_id: "n1", detail: SELF_HARM_UNFINISHED }]),
  );
  try {
    assert.equal(harness.page.data.alert.message, "有一条需要你关注的对话");
    assert.equal(harness.page.data.alert.detail, SELF_HARM_UNFINISHED);
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.ok(text.startsWith("有一条需要你关注的对话"));
    assert.ok(text.includes(SELF_HARM_UNFINISHED));
    assert.equal(text.includes(VICTIM_UNFINISHED), false);
    // 「放在建议下面」: the unfinished-script line follows the advice, not the summary.
    const advice = "找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356";
    assert.ok(text.indexOf(advice) < text.indexOf(SELF_HARM_UNFINISHED));
    assert.equal(text.includes("这段总结由 AI 生成"), false);
  } finally {
    harness.restore();
  }

  const victimPage = await openAttention(
    { id: "n-victim" },
    fx.safetyAlertApi([{ ...VICTIM_ALERT, notification_id: "n-victim", detail: VICTIM_UNFINISHED }]),
  );
  try {
    assert.equal(victimPage.page.data.alert.message, "有一条需要你关注的对话");
    assert.equal(victimPage.page.data.alert.detail, VICTIM_UNFINISHED);
    const text = visibleText(renderPage("pages/attention/index", victimPage.page.data));
    assert.ok(text.includes(VICTIM_UNFINISHED));
    assert.equal(text.includes(SELF_HARM_UNFINISHED), false);
  } finally {
    victimPage.restore();
  }
});

test("a guessed retell note never becomes the attention title or the detail", async () => {
  const harness = await openAttention(
    { id: "n1" },
    fx.safetyAlertApi([{ ...fx.SAFETY_ALERT, message: "有一条需要你关注的对话（可能是在复述故事）" }]),
  );
  try {
    assert.equal(harness.page.data.alert.message, "有一条需要你关注的对话");
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.match(text, /^有一条需要你关注的对话 10月14日 19:42 /);
    assert.doesNotMatch(text, /复述|可能是/);
  } finally {
    harness.restore();
  }
});

test("the attention page opens the alert it was linked to", async () => {
  const harness = await openAttention({ id: "n2" }, fx.safetyAlertApi([fx.SAFETY_ALERT, VICTIM_ALERT]));
  try {
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.match(text, /^有一条需要你关注的对话 10月12日 08:05 人身安全 孩子说到了自己可能受到伤害 /);
    assert.ok(text.includes("尽快当面陪孩子聊聊，先让孩子安心，不反复追问细节。如有危险请拨打110"));
    assert.equal(text.includes("发生了什么"), false);
  } finally {
    harness.restore();
  }
});

test("an unknown script shows the title and neither a category nor a hotline", async () => {
  const harness = await openAttention(
    { id: "n-unknown" },
    fx.safetyAlertApi([
      {
        ...fx.SAFETY_ALERT,
        notification_id: "n-unknown",
        category_label: "",
        summary: "",
        robot_said: "",
        parent_action: "",
      },
    ]),
  );
  try {
    const text = visibleText(renderPage("pages/attention/index", harness.page.data));
    assert.equal(text, "有一条需要你关注的对话 10月14日 19:42 我知道了");
    assert.equal(text.includes("12356"), false);
    assert.equal(text.includes("110"), false);
    assert.equal(text.includes("情绪和安全"), false);
  } finally {
    harness.restore();
  }
});

test("a stale or missing link says nothing needs attention instead of showing another alert", async () => {
  for (const query of [{ id: "gone" }, {}]) {
    const harness = await openAttention(query, fx.safetyAlertApi([fx.SAFETY_ALERT]));
    try {
      assert.equal(harness.page.data.alert, null);
      assert.equal(
        visibleText(renderPage("pages/attention/index", harness.page.data)),
        "现在没有需要关注的对话 有新的提醒时，首页会置顶显示。 我知道了",
      );
    } finally {
      harness.restore();
    }
  }
});

test("我知道了 records the receipt once and leaves only after it is saved", async () => {
  const store = fx.safetyAlertApi([fx.SAFETY_ALERT, VICTIM_ALERT]);
  const saves = [];
  let finishSave;
  const harness = await openAttention({ id: "n1" }, {
    ...store,
    markGuardianNotificationRead: (id) => {
      saves.push(id);
      return new Promise((resolve) => {
        finishSave = () => resolve(store.markGuardianNotificationRead(id));
      });
    },
  });
  try {
    const tap = harness.page.acknowledge();
    await harness.page.acknowledge();
    assert.equal(harness.page.data.acknowledging, true);
    assert.match(renderPage("pages/attention/index", harness.page.data), /<button[^>]*disabled[^>]*>\s*我知道了/);
    assert.deepEqual(navigations(harness), []);
    finishSave();
    await tap;
    assert.deepEqual(saves, ["n1"]);
    assert.deepEqual(navigations(harness), [{ delta: 1 }]);
    assert.equal((await store.getUnreadGuardianNotifications()).newest.notification_id, "n2");
  } finally {
    harness.restore();
  }
});

test("the back chevron leaves without recording anything", async () => {
  const store = fx.safetyAlertApi([fx.SAFETY_ALERT]);
  const save = failingSave(new Error("must not be called"));
  const harness = await openAttention({ id: "n1" }, { ...store, markGuardianNotificationRead: save.markGuardianNotificationRead });
  try {
    harness.page.goBack();
    assert.deepEqual(navigations(harness), [{ delta: 1 }]);
    assert.deepEqual(save.calls, []);
    assert.equal((await store.getUnreadGuardianNotifications()).newest.notification_id, "n1");
  } finally {
    harness.restore();
  }
});

test("a save that fails stays on the alert and asks for another tap", async () => {
  for (const error of [Object.assign(new Error("server"), { status: 503 }), new Error("request:fail timeout")]) {
    const save = failingSave(error);
    const harness = await openAttention({ id: "n1" }, { markGuardianNotificationRead: save.markGuardianNotificationRead });
    try {
      await harness.page.acknowledge();
      assert.deepEqual(navigations(harness), []);
      assert.equal(harness.page.data.acknowledging, false);
      assert.match(
        visibleText(renderPage("pages/attention/index", harness.page.data)),
        /如需帮助可拨打心理援助热线12356 还没记下，请再点一次。 我知道了$/,
      );
      await harness.page.acknowledge();
      assert.deepEqual(save.calls, ["n1", "n1"]);
    } finally {
      harness.restore();
    }
  }
});

test("an alert that is already gone when 我知道了 is tapped still leaves", async () => {
  const save = failingSave(Object.assign(new Error("guardian_notification_not_found"), { status: 404 }));
  const harness = await openAttention({ id: "n1" }, { markGuardianNotificationRead: save.markGuardianNotificationRead });
  try {
    await harness.page.acknowledge();
    assert.deepEqual(navigations(harness), [{ delta: 1 }]);
    assert.equal(harness.page.data.ackError, "");
  } finally {
    harness.restore();
  }
});

test("我知道了 on the empty card just leaves", async () => {
  const save = failingSave(new Error("must not be called"));
  const harness = await openAttention({ id: "gone" }, { ...fx.safetyAlertApi([]), markGuardianNotificationRead: save.markGuardianNotificationRead });
  try {
    await harness.page.acknowledge();
    assert.deepEqual(navigations(harness), [{ delta: 1 }]);
    assert.deepEqual(save.calls, []);
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
    assert.match(text, /^最近几天 对话回顾 这里只有摘要，不显示原话，也没有录音。 有一条需要你关注的对话 今天 19:42 今天 10月14日 /);
    memory.page.openAttention({ currentTarget: { dataset: { id: "n1" } } });
    assert.deepEqual(
      memory.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url),
      ["/pages/attention/index?id=n1"],
    );
  } finally {
    memory.restore();
  }
});

const HOTLINE_IN_COPY = /^(?=.*[\u4e00-\u9fff])(?=.*(?<!\d)(?:12356|110)(?!\d))/;

test("no shipped copy carries a hotline number; the server's next-step line owns them", () => {
  assert.match("可拨打 12356 心理援助热线", HOTLINE_IN_COPY);
  assert.match("如有危险请拨打110", HOTLINE_IN_COPY);
  assert.doesNotMatch("网信算备330110507206401230035号", HOTLINE_IN_COPY);
  const hits = shippedFiles()
    .filter((file) => /\.(js|wxml|wxs|json|wxss)$/.test(file))
    .flatMap((file) =>
      fs
        .readFileSync(path.join(ROOT, file), "utf8")
        .split("\n")
        .filter((line) => HOTLINE_IN_COPY.test(line))
        .map((line) => `${file}: ${line.trim()}`),
    );
  assert.deepEqual(hits, []);
});
