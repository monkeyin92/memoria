const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

function visibleText(html) {
  return html
    .replace(/<style>[\s\S]*?<\/style>/g, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function loginNavigation(harness) {
  return harness.wxCalls
    .filter((call) => call.name === "navigateTo")
    .map((call) => call.args[0]?.url)
    .find((url) => url?.startsWith("/pages/auth/index?"));
}

async function openGuest(pagePath, query = {}) {
  const harness = loadPage(pagePath, fx.guest());
  await harness.run(query);
  return harness;
}

test("guest states on all affected pages show a visible login CTA", async () => {
  const cases = [
    ["pages/guardian/index", {}, /请先登录[\s\S]*登录后才能查看和管理家庭监护信息[\s\S]*重新登录/],
    ["pages/bind/index", fx.BIND_QUERY, /需要登录才能继续[\s\S]*去登录[\s\S]*返回设备管理/],
    ["pages/child-memory/index", { minorId: "p_child", name: "孩子" }, /请先登录[\s\S]*登录后即可打开孩子的记忆档案[\s\S]*重新登录/],
    ["pages/memory-lifecycle/index", {}, /请先登录[\s\S]*登录后才能查看和管理孩子的记忆[\s\S]*重新登录/],
    ["pages/privacy/index", {}, /请先登录[\s\S]*登录后才能查看和管理隐私授权[\s\S]*重新登录/],
  ];

  for (const [pagePath, query, expected] of cases) {
    const harness = await openGuest(pagePath, query);
    try {
      const text = visibleText(renderPage(pagePath, harness.page.data, { tabBar: false }));
      assert.match(text, expected, pagePath);
      assert.doesNotMatch(text, /正在读取机器人|请求未完成（401）|unauthorized|claim_id|device_id/, pagePath);
    } finally {
      harness.restore();
    }
  }
});

test("each affected page routes its CTA through the existing auth page and keeps its return route", async () => {
  const cases = [
    ["pages/guardian/index", {}, "view_guardian_summary", "/pages/guardian/index"],
    ["pages/bind/index", fx.BIND_QUERY, "bind_device", "/pages/bind/index?claim_id=claim_1&onboarding_session_id=onb_1"],
    ["pages/child-memory/index", { minorId: "p_child", name: "孩子" }, "view_child_memory", "/pages/child-memory/index?minorId=p_child&name=%E5%AD%A9%E5%AD%90"],
    ["pages/memory-lifecycle/index", {}, "view_memory_lifecycle", "/pages/memory-lifecycle/index"],
    ["pages/privacy/index", {}, "view_privacy", "/pages/privacy/index"],
  ];

  for (const [pagePath, query, reason, route] of cases) {
    const harness = await openGuest(pagePath, query);
    try {
      global.getCurrentPages = () => [{ route: pagePath, options: query }];
      const method = {
        "pages/guardian/index": "openGuardianLogin",
        "pages/bind/index": "openBindLogin",
        "pages/child-memory/index": "openChildMemoryLogin",
        "pages/memory-lifecycle/index": "openLifecycleLogin",
        "pages/privacy/index": "openPrivacyLogin",
      }[pagePath];
      harness.page[method]();
      assert.equal(
        loginNavigation(harness),
        `/pages/auth/index?redirect=${encodeURIComponent(route)}&reason=${encodeURIComponent(reason)}`,
        pagePath,
      );
      assert.equal(harness.wxCalls.some((call) => call.name === "login"), false, pagePath);
    } finally {
      harness.restore();
    }
  }
});

test("401 recovery gives guardian, bind, child memory, and lifecycle a safe visible state", async () => {
  const expired = Object.assign(new Error("请求未完成（401）"), { status: 401 });

  const guardian = loadPage("pages/guardian/index", fx.authed({
    getProfile: async () => {
      throw expired;
    },
  }));
  await guardian.run();
  assert.equal(guardian.page.data.authRequired, true);
  assert.match(visibleText(renderPage("pages/guardian/index", guardian.page.data)), /重新登录/);
  guardian.restore();

  const bind = loadPage("pages/bind/index", {
    ...fx.authed({
      getDeviceClaim: async () => {
        throw expired;
      },
    }),
  });
  await bind.run(fx.BIND_QUERY);
  assert.equal(bind.page.data.authRequired, true);
  assert.equal(bind.page.data.claimId, fx.BIND_QUERY.claim_id);
  assert.equal(bind.page.data.onboardingSessionId, fx.BIND_QUERY.onboarding_session_id);
  assert.equal(bind.page._claimLoaded, false);
  assert.doesNotMatch(visibleText(renderPage("pages/bind/index", bind.page.data)), /正在读取机器人/);
  bind.restore();

  const child = loadPage("pages/child-memory/index", fx.authed({
    getChildSessionSummaries: async () => {
      throw expired;
    },
  }));
  await child.run({ minorId: "p_child", name: "孩子" });
  assert.equal(child.page.data.authenticated, false);
  assert.match(visibleText(renderPage("pages/child-memory/index", child.page.data)), /重新登录/);
  child.restore();

  const lifecycle = loadPage("pages/memory-lifecycle/index", fx.authed({
    getGuardianChildMemories: async () => {
      throw expired;
    },
  }));
  await lifecycle.run();
  assert.equal(lifecycle.page.data.authRequired, true);
  assert.equal(lifecycle.page.data.minorUserId, "p_child");
  assert.deepEqual(lifecycle.page.data.pendingItems, []);
  assert.match(visibleText(renderPage("pages/memory-lifecycle/index", lifecycle.page.data)), /重新登录/);
  lifecycle.restore();
});

test("bind keeps the claim context visible after login is refused and reloads it after login", async () => {
  let authenticated = false;
  let claimReads = 0;
  const harness = loadPage("pages/bind/index", {
    ...fx.authed({
      hasAuthenticatedSession: () => authenticated,
      getDeviceClaim: async () => {
        claimReads += 1;
        return fx.CLAIM;
      },
    }),
  });
  try {
    await harness.run(fx.BIND_QUERY);
    assert.equal(harness.page.data.authRequired, true);
    assert.equal(harness.page.data.claimId, fx.BIND_QUERY.claim_id);
    assert.equal(claimReads, 0);

    authenticated = true;
    await harness.page.onShow();
    assert.equal(harness.page.data.authRequired, false);
    assert.equal(harness.page.data.step, "mode");
    assert.equal(claimReads, 1);
  } finally {
    harness.restore();
  }
});

test("child-memory operation errors remain inside the open sheet", async () => {
  const harness = loadPage("pages/child-memory/index", fx.authed({
    getPersonConsents: fx.archiveApi.getPersonConsents,
    getChildArchive: async () => ({
      items: [{ record_id: "fact-1", field: "preference", text: "喜欢蓝色", display_lifecycle: "parent_confirmed" }],
    }),
    reviewChildArchiveFact: async () => {
      throw new Error("保存失败，请稍后重试。");
    },
  }));
  try {
    await harness.run({ minorId: "p_child", name: "孩子" });
    harness.page.askHistoryChange({ currentTarget: { dataset: { recordId: "fact-1" } } });
    await harness.page.runConfirm();
    assert.equal(harness.page.data.error, "");
    assert.equal(harness.page.data.confirm.error, "保存失败，请稍后重试。");
    assert.match(visibleText(renderPage("pages/child-memory/index", harness.page.data)), /保存失败，请稍后重试。/);
  } finally {
    harness.restore();
  }
});
