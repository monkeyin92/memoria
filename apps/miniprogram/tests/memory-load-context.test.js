const assert = require("node:assert/strict");
const test = require("node:test");
const path = require("node:path");
const { loadPage, createWx, setPath } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");
const { canonicalManifest } = require("./manifest-fixtures");

process.env.TZ = "Asia/Shanghai";
const CHILD = fx.BINDING;
const SELF = canonicalManifest({ account_owner_id: "u_parent", primary_subject_ids: ["u_parent"] });
const ADULT_REVIEW = { actual_heard: [{ event_id: "adult", text: "成人私密原话" }] };

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function openContext(binding = CHILD, overrides = {}) {
  const state = { binding, userId: "u_parent", epoch: 1, loggedIn: true };
  const calls = [];
  let clearAuth;
  const record = (name, result) => async (...args) => { calls.push([name, ...args]); return result; };
  const api = {
    readBindingManifest: () => state.binding && JSON.parse(JSON.stringify(state.binding)),
    currentIdentity: () => state.loggedIn ? { user_id: state.userId } : null,
    hasAuthenticatedSession: () => state.loggedIn,
    currentAuthEpoch: () => state.epoch,
    isAuthEpochCurrent: (epoch) => epoch === state.epoch,
    syncDeviceBindings: record("sync", { status: "empty", binding: null }),
    getMemoryDays: record("adult-days", { items: [] }),
    getConversationReview: record("adult-review", ADULT_REVIEW),
    getConversationSessions: record("adult-sessions", { items: [] }),
    getGuardianChildDays: record("child-days", { items: [] }),
    getGuardianChildMemories: record("child-memories", { candidates: [], confirmed: [] }),
    getChildSessionSummaries: record("child-summaries", { items: [] }),
    getUnreadGuardianNotifications: record("safety", { newest: fx.SAFETY_ALERT, others_count: 0 }),
    getGuardianPushConfig: record("push", { enabled: false }),
    ...overrides,
  };
  const h = loadPage("pages/memory/index", {
    ...fx.authed(api),
    app: { subscribeAuthCleared: (fn) => { clearAuth = fn; return () => {}; } },
  });
  h.page.onLoad();
  const text = () => renderPage("pages/memory/index", h.page.data).replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  return { ...h, state, calls, text, clearAuth: () => clearAuth() };
}

for (const mode of ["child_for_parent", "family_shared"]) {
  test(`legacy ${mode} keeps the account's existing review without reading another subject`, async () => {
    const h = openContext({ ...SELF, declared_mode: mode, primary_subject_ids: ["p_elder"] });
    try {
      await h.page.onShow();
      assert.equal(h.page.data.error, "");
      assert.equal(h.page.data.guardianMode, false);
      assert.match(h.text(), /成人私密原话/);
      assert.deepEqual(h.calls.map(([name]) => name), ["adult-days", "adult-review", "adult-sessions"]);
      assert.deepEqual(h.calls[0], ["adult-days", "u_parent", 30]);
    } finally { h.restore(); }
  });
}

for (const [label, result] of Object.entries({
  "changed empty": { status: "empty", binding: null, contextChanged: true },
  unknown: { status: "unknown", binding: SELF },
  cached: { status: "cached", binding: CHILD },
  "ready without binding": { status: "ready", binding: null },
  "empty with binding": { status: "empty", binding: CHILD },
})) {
  test(`unconfirmed sync ${label} cannot open adult or child reads`, async () => {
    const h = openContext(null, { syncDeviceBindings: async () => result });
    try {
      await h.page.onShow();
      assert.deepEqual(h.calls, []);
      assert.equal(h.page.data.safetyReadStatus, "failed");
      assert.match(h.text(), /无法确认设备绑定/);
      assert.doesNotMatch(h.text(), /成人私密原话/);
    } finally { h.restore(); }
  });
}

test("same-epoch slow adult review cannot overwrite a newer child view", async () => {
  const slow = deferred();
  const started = deferred();
  const h = openContext(SELF, { getConversationReview: () => { started.resolve(); return slow.promise; } });
  try {
    const first = h.page.onShow();
    await started.promise;
    h.state.binding = CHILD;
    await h.page.onShow();
    assert.equal(h.page.data.guardianMode, true);
    slow.resolve(ADULT_REVIEW);
    await first;
    assert.equal(h.page.data.guardianMode, true);
    assert.deepEqual(h.page.data.heardTurns, []);
    assert.match(h.text(), /这里只有摘要/);
    assert.doesNotMatch(h.text(), /成人私密原话/);
    assert.equal(h.calls.filter(([name]) => name === "safety").length, 1);
  } finally { slow.resolve({}); h.restore(); }
});

test("overlapping cold syncs cannot share confirmation state", async () => {
  const firstSync = deferred();
  const secondSync = deferred();
  let reads = 0;
  const h = openContext(null, { syncDeviceBindings: () => ++reads === 1 ? firstSync.promise : secondSync.promise });
  try {
    const first = h.page.onShow();
    const second = h.page.onShow();
    h.state.binding = CHILD;
    firstSync.resolve({ status: "ready", binding: CHILD });
    await first;
    secondSync.resolve({ status: "error", binding: null });
    await second;
    assert.equal(h.page.data.safetyReadStatus, "failed");
    assert.equal(h.page.data.safetyBanner, null);
    assert.match(h.text(), /暂时没能读取，点击重试/);
    assert.deepEqual(h.calls, []);
  } finally { firstSync.resolve({}); secondSync.resolve({}); h.restore(); }
});

test("a late cold sync cannot replace a more recent self-use selection", async () => {
  const sync = deferred();
  const h = openContext(null, { syncDeviceBindings: () => sync.promise });
  try {
    const first = h.page.onShow();
    h.state.binding = SELF;
    await h.page.onShow();
    sync.resolve({ status: "ready", binding: SELF, contextChanged: true });
    await first;
    assert.equal(h.page.data.guardianMode, false);
    assert.equal(h.page.data.safetyBanner, null);
    assert.match(h.text(), /成人私密原话/);
    assert.equal(h.calls.some(([name]) => name.startsWith("child") || name === "safety"), false);
  } finally { sync.resolve({}); h.restore(); }
});

for (const result of ["resolve", "reject"]) {
  test(`late safety ${result} cannot add a child or failed banner to self-use`, async () => {
    const safety = deferred();
    const started = deferred();
    const h = openContext(CHILD, { getUnreadGuardianNotifications: () => { started.resolve(); return safety.promise; } });
    try {
      const first = h.page.onShow();
      await started.promise;
      h.state.binding = SELF;
      await h.page.onShow();
      if (result === "resolve") safety.resolve({ newest: fx.SAFETY_ALERT });
      else safety.reject(new Error("request:fail timeout"));
      await first;
      assert.equal(h.page.data.safetyBanner, null);
      assert.equal(h.page.data.safetyReadStatus, "not_found");
      assert.doesNotMatch(h.text(), /有一条需要你关注|安全提醒暂时没能读取/);
      assert.match(h.text(), /成人私密原话/);
    } finally { safety.resolve({}); h.restore(); }
  });
}

for (const entry of ["onShow", "onPullDownRefresh", "loginForReview", "retrySafetyRead"]) {
  for (const target of ["child", "adult"]) {
    test(`${entry} recovers failed cold binding and refreshes ${target} safety state`, async () => {
      let result = { status: "error", binding: null };
      const h = openContext(null, { syncDeviceBindings: async () => {
        h.state.binding = result.status === "ready" ? result.binding : null;
        return result;
      } });
      try {
        await h.page.onShow();
        assert.equal(h.page.data.safetyReadStatus, "failed");
        result = target === "child" ? { status: "ready", binding: CHILD } : { status: "empty", binding: null };
        await h.page[entry]();
        await h.settle();
        assert.equal(h.page.data.guardianMode, target === "child");
        assert.equal(h.page.data.safetyReadStatus, target === "child" ? "ok" : "not_found");
        assert.equal(Boolean(h.page.data.safetyBanner), target === "child");
        assert.doesNotMatch(h.text(), /安全提醒暂时没能读取/);
        assert.equal(h.calls.filter(([name]) => name === "safety").length, target === "child" ? 1 : 0);
        if (target === "adult") assert.match(h.text(), /成人私密原话/);
      } finally { h.restore(); }
    });
  }
}

for (const change of ["binding", "identity", "epoch", "logout", "authclear", "unload"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`superseded review ${outcome} makes zero writes after ${change}`, async () => {
      const review = deferred();
      const started = deferred();
      let reads = 0;
      const h = openContext(SELF, { getConversationReview: () => {
        if (++reads > 1) return { actual_heard: [{ text: "本轮回顾" }] };
        started.resolve(); return review.promise;
      } });
      try {
        const load = h.page.onShow();
        await started.promise;
        if (change === "binding") h.state.binding = CHILD;
        if (change === "identity") h.state.userId = "u_other";
        if (change === "epoch") h.state.epoch++;
        if (change === "logout") h.state.loggedIn = false;
        if (change === "authclear") h.clearAuth();
        if (change === "unload") h.page.onUnload();
        if (!["authclear", "unload"].includes(change)) await h.page.onShow();
        const before = JSON.parse(JSON.stringify(h.page.data));
        const writes = h.setDataLog.length;
        if (outcome === "resolve") review.resolve(ADULT_REVIEW);
        else review.reject(new Error("stale adult failure"));
        await load;
        assert.equal(h.setDataLog.length, writes);
        assert.deepEqual(h.page.data, before);
        assert.doesNotMatch(h.text(), /成人私密原话|stale adult failure/);
      } finally { review.resolve({}); h.restore(); }
    });
  }
}

test("an unrelated cached account binding is unconfirmed, while delegate and member bindings stay usable", async () => {
  for (const role of ["unrelated", "delegate", "member", "guardian"]) {
    const binding = { ...CHILD, account_owner_id: "u_other", device_admin_ids: ["u_other"], guardian_ids: [], roles: [], primary_subject_ids: ["p_child"] };
    if (role !== "unrelated") binding[`${role}_ids`] = ["u_parent"];
    const h = openContext(binding);
    try {
      await h.page.onShow();
      if (role === "unrelated") {
        assert.deepEqual(h.calls, []);
        assert.equal(h.page.data.safetyReadStatus, "failed");
        assert.match(h.text(), /无法确认设备绑定/);
      } else {
        assert.equal(h.page.data.error, "");
        assert.equal(h.page.data.guardianSubjectId, "p_child");
        assert.equal(h.page.data.safetyReadStatus, "ok");
        assert.equal(h.calls.some(([name]) => name.startsWith("adult")), false);
      }
    } finally { h.restore(); }
  }
});

for (const outcome of ["resolve", "reject"]) {
  test(`older same-binding review ${outcome} cannot replace the latest successful request`, async () => {
    const old = deferred();
    const started = deferred();
    let reads = 0;
    const h = openContext(SELF, {
      getConversationReview: async () => {
        if (++reads === 1) { started.resolve(); return old.promise; }
        return { actual_heard: [{ text: "本轮回顾" }] };
      },
    });
    try {
      const first = h.page.onShow();
      await started.promise;
      await h.page.onShow();
      const writes = h.setDataLog.length;
      if (outcome === "resolve") old.resolve(ADULT_REVIEW);
      else old.reject(new Error("旧请求失败"));
      await first;
      assert.equal(h.setDataLog.length, writes);
      assert.match(h.text(), /本轮回顾/);
      assert.doesNotMatch(h.text(), /成人私密原话|旧请求失败/);
    } finally { old.resolve({}); h.restore(); }
  });
}

for (const change of ["authclear", "unload", "logout", "binding"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`superseded safety ${outcome} makes zero writes after ${change}`, async () => {
      const safety = deferred();
      const started = deferred();
      let reads = 0;
      const h = openContext(CHILD, {
        getUnreadGuardianNotifications: () => {
          if (++reads > 1) return { newest: null, others_count: 0 };
          started.resolve(); return safety.promise;
        },
      });
      try {
        const load = h.page.onShow();
        await started.promise;
        if (change === "authclear") h.clearAuth();
        if (change === "unload") h.page.onUnload();
        if (change === "logout") h.state.loggedIn = false;
        if (change === "binding") h.state.binding = { ...CHILD, binding_version: 2, primary_subject_ids: ["u_parent", "p_other"] };
        if (["logout", "binding"].includes(change)) await h.page.onShow();
        const before = JSON.parse(JSON.stringify(h.page.data));
        const writes = h.setDataLog.length;
        if (outcome === "resolve") safety.resolve({ newest: fx.SAFETY_ALERT });
        else safety.reject(new Error("request:fail timeout"));
        await load;
        assert.deepEqual(h.page.data, before);
        assert.equal(h.setDataLog.length, writes);
        assert.doesNotMatch(h.text(), /有一条需要你关注|安全提醒暂时没能读取/);
      } finally { safety.resolve({}); h.restore(); }
    });
  }
}

test("cold sync can populate storage, but a conflicting returned binding remains unconfirmed", async () => {
  for (const conflict of [false, true]) {
    const h = openContext(null, {
      syncDeviceBindings: async () => {
        h.state.binding = conflict ? SELF : CHILD;
        return { status: "ready", binding: CHILD };
      },
    });
    try {
      await h.page.onShow();
      if (conflict) {
        assert.deepEqual(h.calls, []);
        assert.equal(h.page.data.safetyReadStatus, "failed");
        assert.match(h.text(), /无法确认设备绑定/);
      } else {
        assert.equal(h.page.data.guardianSubjectId, "p_child");
        assert.equal(h.page.data.safetyReadStatus, "ok");
        assert.match(h.text(), /有一条需要你关注/);
      }
    } finally { h.restore(); }
  }
});

for (const change of ["child", "unload", "authclear"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`old conversation detail ${outcome} cannot write after ${change}`, async () => {
      const history = deferred();
      const started = deferred();
      const h = openContext(SELF, {
        getConversationHistory: () => { started.resolve(); return history.promise; },
      });
      try {
        await h.page.onShow();
        const detail = h.page.openConversation({ currentTarget: { dataset: { sessionId: "adult-session" } } });
        await started.promise;
        if (change === "child") { h.state.binding = CHILD; await h.page.onShow(); }
        if (change === "unload") h.page.onUnload();
        if (change === "authclear") h.clearAuth();
        const before = JSON.parse(JSON.stringify(h.page.data));
        const writes = h.setDataLog.length;
        if (outcome === "resolve") history.resolve({ turns: [{ owner_text: "旧账号原话" }] });
        else history.reject(new Error("旧对话详情失败"));
        await detail;
        assert.deepEqual(h.page.data, before);
        assert.equal(h.setDataLog.length, writes);
        assert.doesNotMatch(h.text(), /旧账号原话|旧对话详情失败/);
      } finally { history.resolve({}); h.restore(); }
    });
  }
}

async function flush() {
  for (let tick = 0; tick < 20; tick++) await Promise.resolve();
}

function realRuntime(binding = SELF) {
  const root = path.resolve(__dirname, "..");
  const clearModules = () => {
    for (const key of Object.keys(require.cache)) {
      if (key.startsWith(root + path.sep) && !key.includes("/scripts/")) delete require.cache[key];
    }
  };
  clearModules();
  const globals = new Map(["wx", "App", "Page", "Component", "getApp", "getCurrentPages"].map((key) => [key, global[key]]));
  const { wx, calls } = createWx();
  const requests = [];
  wx.request = (options) => requests.push(options);
  global.wx = wx;
  let definition;
  global.App = (value) => { definition = value; };
  require(path.join(root, "app.js"));
  const app = { ...definition, globalData: structuredClone(definition.globalData), _authClearedListeners: new Set() };
  global.getApp = () => app;
  global.Component = () => {};
  global.getCurrentPages = () => [];
  const api = require(path.join(root, "utils/api.js"));
  app.setAuthenticatedIdentity({ user_id: "u_parent", account_type: "registered", access_token: "token", expires_in: 3600 });
  if (binding) api.selectDeviceBinding(binding);
  const page = (name) => {
    let value;
    global.Page = (def) => { value = def; };
    require(path.join(root, `pages/${name}/index.js`));
    const instance = { ...value, data: structuredClone(value.data), writes: [] };
    instance.setData = (patch) => {
      instance.writes.push(structuredClone(patch));
      for (const [key, item] of Object.entries(patch)) setPath(instance.data, key, structuredClone(item));
    };
    instance.getTabBar = () => ({ setData() {} });
    instance.onLoad?.();
    return instance;
  };
  const memory = page("memory");
  const reply = (request, data, statusCode = 200) => {
    assert.ok(request && !request.answered, "a pending real wx.request must exist");
    request.answered = true;
    request.success({ statusCode, data });
  };
  const pending = (part) => requests.find((request) => !request.answered && request.url.includes(part));
  const respond = (request) => {
    if (request.url.includes("conversation-review")) return { ...ADULT_REVIEW, memory_candidates: [{ claim_id: "same-claim", value: "成人候选" }] };
    if (request.url.includes("conversation-history")) return { turns: [{ owner_text: "本轮详情" }] };
    if (request.url.includes("conversation-sessions")) return { items: [{ session_id: "session", turn_count: 1 }] };
    if (request.url.includes("/memories?")) return { candidates: [{ claim_id: "same-claim", value: "孩子候选" }], confirmed: [] };
    if (request.url.includes("/notifications/unread")) return { newest: fx.SAFETY_ALERT, others_count: 0 };
    if (request.url.includes("/push-config")) return { enabled: false };
    return { items: [] };
  };
  const finish = async (promise, answer = respond) => {
    let settled = false;
    let result;
    let failure;
    promise.then((value) => { settled = true; result = value; }, (error) => { settled = true; failure = error; });
    for (let round = 0; round < 24 && !settled; round++) {
      await flush();
      for (const request of requests.filter((item) => !item.answered && !item.held)) reply(request, answer(request));
    }
    await flush();
    assert.equal(settled, true, "the page operation must settle within the bounded request drain");
    if (failure) throw failure;
    return result;
  };
  const text = () => renderPage("pages/memory/index", memory.data).replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  return {
    app, api, wx, calls, requests, memory, page, pending, reply, finish, text, response: respond,
    restore() {
      memory.onUnload();
      clearModules();
      for (const [key, value] of globals) {
        if (value === undefined) delete global[key];
        else global[key] = value;
      }
    },
  };
}

for (const binding of [{ ...SELF, binding_version: 2 }, CHILD]) {
  test(`real runtime warm home sync recovers the active ${binding.declared_mode} review`, async (t) => {
    const h = realRuntime();
    try {
      const home = h.page("home");
      home.onShow();
      await flush();
      const sync = h.pending("/v1/device-bindings");
      const load = h.memory.onShow();
      await flush();
      const oldReview = h.pending("conversation-review");
      h.reply(sync, { bindings: [binding] });
      await flush();
      assert.equal(h.api.readBindingManifest().binding_version, binding.binding_version);
      h.reply(oldReview, ADULT_REVIEW);
      assert.equal(await h.finish(load, (request) => request.url.includes("conversation-review")
        ? { actual_heard: [{ text: "本轮成人回顾" }] } : h.response(request)), "ok");
      assert.equal(h.memory.data.loading, false);
      assert.equal(h.memory.data.guardianMode, binding.declared_mode === "parent_for_child");
      if (binding.declared_mode === "parent_for_child") {
        assert.equal(h.memory.data.guardianSubjectId, "p_child");
        assert.deepEqual(h.memory.data.heardTurns, []);
        assert.equal(h.memory.data.safetyReadStatus, "ok");
        assert.doesNotMatch(h.text(), /成人私密原话|正在读取回顾/);
      } else {
        assert.match(h.text(), /本轮成人回顾/);
        assert.doesNotMatch(h.text(), /成人私密原话/);
        assert.equal(h.requests.filter((request) => request.url.includes("/memory/days?") && request.url.endsWith("limit=30")).length, 2);
      }
      t.diagnostic(JSON.stringify({ loading: h.memory.data.loading, guardianSubjectId: h.memory.data.guardianSubjectId, visibleText: h.text() }));
    } finally { h.restore(); }
  });
}

test("real runtime token expiry during the active review enters a settled guest page", async (t) => {
  const h = realRuntime();
  try {
    const load = h.memory.onShow();
    await flush();
    h.app.globalData.accessTokenExpiresAt = Date.now() + 1000;
    await h.finish(load);
    assert.equal(h.memory.data.loading, false);
    assert.equal(h.memory.data.authenticated, false);
    assert.match(h.text(), /微信登录后打开/);
    assert.doesNotMatch(h.text(), /成人私密原话|正在读取回顾/);
    t.diagnostic(JSON.stringify({ authenticated: h.memory.data.authenticated, loading: h.memory.data.loading, visibleText: h.text() }));
  } finally { h.restore(); }
});

test("real runtime cold ready cannot authorize a child after concurrent unbind clears its cache", async (t) => {
  const h = realRuntime(null);
  try {
    const unbind = h.api.unbindDevice(CHILD.device_id);
    const load = h.memory.onShow();
    await flush();
    h.reply(h.pending("/v1/device-bindings"), { bindings: [CHILD] });
    await Promise.resolve();
    h.reply(h.pending("/binding/unbind"), { unbound: true });
    await unbind;
    assert.equal(h.api.readBindingManifest(), null);
    assert.equal(await h.finish(load), "error");
    assert.equal(h.memory.data.loading, false);
    assert.equal(h.memory.data.guardianSubjectId, "");
    assert.deepEqual(h.memory.data.memoryCandidates, []);
    assert.equal(h.requests.some((request) => request.url.includes("/v1/guardian/minors/")), false);
    assert.equal(h.requests.some((request) => request.url.includes("/v1/archive/") || request.url.includes("/v1/memory/days")), false);
    assert.match(h.text(), /无法确认设备绑定|点击重试/);
    t.diagnostic(JSON.stringify({ manifest: h.api.readBindingManifest(), loading: h.memory.data.loading, guardianSubjectId: h.memory.data.guardianSubjectId, visibleText: h.text() }));
  } finally { h.restore(); }
});

for (const outcome of ["resolve", "reject"]) {
  test(`real runtime active detail ${outcome} after same-epoch refresh unlocks the next detail`, async (t) => {
    const h = realRuntime();
    try {
      await h.finish(h.memory.onShow());
      const detail = h.memory.openConversation({ currentTarget: { dataset: { sessionId: "session" } } });
      await flush();
      const old = h.pending("conversation-history");
      h.api.selectDeviceBinding({ ...SELF, binding_version: 2 });
      h.reply(old, outcome === "resolve" ? { turns: [{ owner_text: "旧详情" }] } : { detail: "旧详情失败" }, outcome === "resolve" ? 200 : 500);
      await h.finish(detail);
      assert.equal(h.memory.data.conversationLoading, false);
      assert.equal(h.memory.data.selectedConversationSessionId, "");
      assert.deepEqual(h.memory.data.conversationTurns, []);
      assert.doesNotMatch(h.text(), /旧详情/);
      t.diagnostic(JSON.stringify({ afterOldDetail: { loading: h.memory.data.conversationLoading, selected: h.memory.data.selectedConversationSessionId, turns: h.memory.data.conversationTurns } }));
      await h.finish(h.memory.openConversation({ currentTarget: { dataset: { sessionId: "session" } } }));
      assert.equal(h.memory.data.conversationLoading, false);
      assert.match(h.text(), /本轮详情/);
      t.diagnostic(JSON.stringify({ afterNextDetail: { loading: h.memory.data.conversationLoading, selected: h.memory.data.selectedConversationSessionId, turns: h.memory.data.conversationTurns } }));
    } finally { h.restore(); }
  });
}

test("real runtime active owner retries once then fails closed and remains manually retryable", async (t) => {
  const h = realRuntime();
  try {
    const load = h.memory.onShow();
    let reviews = 0;
    await h.finish(load, (request) => {
      if (request.url.includes("conversation-review")) {
        reviews++;
        h.api.selectDeviceBinding({ ...SELF, binding_version: reviews + 1 });
      }
      return { ...ADULT_REVIEW, items: [] };
    });
    assert.equal(reviews, 2, "only one automatic retry is permitted");
    assert.equal(h.memory.data.loading, false);
    assert.equal(h.memory.data.guardianMode, true);
    assert.equal(h.memory.data.guardianSubjectId, "");
    assert.equal(h.memory.data.safetyBanner, null);
    assert.match(h.text(), /无法确认设备绑定.*点击重试|点击重试.*无法确认设备绑定/);
    assert.doesNotMatch(h.text(), /成人私密原话|这里还空空的|正在读取回顾/);
    t.diagnostic(JSON.stringify({ reviews, loading: h.memory.data.loading, visibleText: h.text() }));
    assert.equal(await h.finish(h.memory.retrySafetyRead()), "ok");
    assert.match(h.text(), /成人私密原话/);
    assert.equal(h.memory.data.loading, false);
  } finally { h.restore(); }
});

for (const outcome of ["resolve", "reject"]) {
  test(`real runtime old adult confirmation ${outcome} cannot affect a new child's same-claim lock`, async (t) => {
    const h = realRuntime();
    try {
      await h.finish(h.memory.onShow());
      const event = { currentTarget: { dataset: { claimId: "same-claim" } } };
      const oldConfirm = h.memory.confirmCandidate(event);
      await flush();
      const old = h.pending("/archive/memories/same-claim/review");
      old.held = true;
      h.api.selectDeviceBinding(CHILD);
      await h.finish(h.memory.onShow());
      h.memory.showMemories();
      const newConfirm = h.memory.confirmCandidate(event);
      await flush();
      const current = h.pending("/minors/p_child/memories/same-claim/review");
      assert.ok(current);
      current.held = true;
      const writes = h.memory.writes.length;
      const before = structuredClone(h.memory.data);
      const toasts = h.calls.filter((call) => call.name === "showToast").length;
      h.reply(old, outcome === "resolve" ? {} : { detail: "旧成人审核失败" }, outcome === "resolve" ? 200 : 500);
      await h.finish(oldConfirm);
      assert.deepEqual(h.memory.data, before);
      assert.equal(h.memory.writes.length, writes);
      assert.equal(h.calls.filter((call) => call.name === "showToast").length, toasts);
      assert.equal(h.memory.data.reviewingClaimId, "same-claim");
      t.diagnostic(JSON.stringify({ writesFromOldConfirmation: h.memory.writes.length - writes, activeChildClaim: h.memory.data.reviewingClaimId, error: h.memory.data.error }));
      current.held = false;
      await h.finish(newConfirm);
      assert.equal(h.memory.data.reviewingClaimId, "");
      assert.equal(h.memory.data.error, "");
      assert.ok(h.calls.some((call) => call.name === "showToast" && call.args[0].title === "已确认，会记住"));
    } finally { h.restore(); }
  });
}

for (const change of ["binding", "account", "newload", "unload"]) {
  test(`real runtime retract modal cannot submit the old claim after ${change}`, async () => {
    const h = realRuntime(CHILD);
    try {
      await h.finish(h.memory.onShow());
      let modal;
      h.wx.showModal = (options) => { modal = options; };
      const retract = h.memory.retractMemory({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } });
      await flush();
      assert.ok(modal);
      if (change === "binding") h.api.selectDeviceBinding({ ...CHILD, binding_version: 2, primary_subject_ids: ["p_other"] });
      if (change === "account") h.app.setAuthenticatedIdentity({ user_id: "u_other", account_type: "registered", access_token: "other-token", expires_in: 3600 });
      if (change === "newload") await h.finish(h.memory.onShow());
      if (change === "unload") h.memory.onUnload();
      const writes = h.memory.writes.length;
      modal.success({ confirm: true });
      await h.finish(retract);
      if (change === "binding") {
        assert.ok(h.memory.writes.length > writes);
        assert.equal(h.memory.data.guardianSubjectId, "p_other");
        assert.equal(h.memory.data.loading, false);
      } else {
        assert.equal(h.memory.writes.length, writes);
      }
      assert.equal(h.requests.filter((request) => request.method === "POST" && request.url.includes("/memories/")).length, 0);
      assert.equal(h.memory.data.reviewingClaimId, "");
    } finally { h.restore(); }
  });
}

for (const stage of ["child-days", "child-memories", "child-summaries", "adult-review", "adult-sessions", "guardian-links", "fallback-summaries", "safety", "push"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`active owner recovers binding drift during ${stage} ${outcome}`, async () => {
      const blocked = deferred();
      const started = deferred();
      const child = ["child-days", "child-memories", "child-summaries", "safety", "push"].includes(stage);
      const methods = {
        "child-days": "getGuardianChildDays", "child-memories": "getGuardianChildMemories",
        "child-summaries": "getChildSessionSummaries", "adult-review": "getConversationReview",
        "adult-sessions": "getConversationSessions", "guardian-links": "getGuardianLinks",
        "fallback-summaries": "getChildSessionSummaries", safety: "getUnreadGuardianNotifications", push: "getGuardianPushConfig",
      };
      let reads = 0;
      const overrides = {};
      if (["guardian-links", "fallback-summaries"].includes(stage)) {
        overrides.getMemoryDays = async () => { throw new Error("days unavailable"); };
        overrides.getGuardianLinks = async () => [{ minorUserId: "p_child", status: "active" }];
      }
      overrides[methods[stage]] = async () => {
        if (++reads > 1) return { items: [], newest: null };
        started.resolve();
        return blocked.promise;
      };
      const h = openContext(child ? CHILD : SELF, overrides);
      try {
        const load = h.page.onShow();
        await started.promise;
        h.state.binding = child ? SELF : CHILD;
        if (outcome === "resolve") blocked.resolve({ ...ADULT_REVIEW, newest: fx.SAFETY_ALERT, items: [{ text: "过期小结", day: "2026-10-14" }] });
        else blocked.reject(new Error("过期响应失败"));
        assert.equal(await load, "ok");
        assert.equal(h.page.data.loading, false);
        assert.equal(h.page.data.guardianMode, !child);
        assert.equal(h.page.data.guardianSubjectId, child ? "" : "p_child");
        assert.doesNotMatch(h.text(), /过期小结|过期响应失败|正在读取回顾/);
        if (child) {
          assert.equal(h.page.data.safetyBanner, null);
          assert.match(h.text(), /成人私密原话/);
        } else {
          assert.deepEqual(h.page.data.heardTurns, []);
          assert.match(h.text(), /这里只有摘要/);
        }
      } finally { blocked.resolve({}); h.restore(); }
    });
  }
}

for (const outcome of ["resolve", "reject"]) {
  test(`active owner safety ${outcome} after session expiry clears all private partitions`, async () => {
    const safety = deferred();
    const started = deferred();
    const h = openContext(CHILD, { getUnreadGuardianNotifications: () => { started.resolve(); return safety.promise; } });
    try {
      const load = h.page.onShow();
      await started.promise;
      h.state.loggedIn = false;
      if (outcome === "resolve") safety.resolve({ newest: fx.SAFETY_ALERT });
      else safety.reject(new Error("request:fail timeout"));
      assert.equal(await load, "guest");
      assert.equal(h.page.data.loading, false);
      assert.equal(h.page.data.authenticated, false);
      assert.deepEqual(h.page.data.days, []);
      assert.equal(h.page.data.safetyBanner, null);
      assert.match(h.text(), /微信登录后打开/);
      assert.doesNotMatch(h.text(), /有一条需要你关注|正在读取回顾/);
    } finally { safety.resolve({}); h.restore(); }
  });
}

for (const outcome of ["resolve", "reject"]) {
  test(`real runtime superseded detail ${outcome} cannot clear the next detail's lock`, async () => {
    const h = realRuntime();
    try {
      await h.finish(h.memory.onShow());
      const event = { currentTarget: { dataset: { sessionId: "session" } } };
      const oldDetail = h.memory.openConversation(event);
      await flush();
      const old = h.pending("conversation-history");
      old.held = true;
      await h.finish(h.memory.onShow());
      const newDetail = h.memory.openConversation(event);
      await flush();
      const current = h.requests.find((request) => !request.answered && request !== old && request.url.includes("conversation-history"));
      assert.ok(current);
      const writes = h.memory.writes.length;
      const before = structuredClone(h.memory.data);
      h.reply(old, outcome === "resolve" ? { turns: [{ owner_text: "旧详情" }] } : { detail: "旧详情失败" }, outcome === "resolve" ? 200 : 500);
      await oldDetail;
      assert.deepEqual(h.memory.data, before);
      assert.equal(h.memory.writes.length, writes);
      assert.equal(h.memory.data.conversationLoading, true);
      await h.finish(newDetail);
      assert.equal(h.memory.data.conversationLoading, false);
      assert.match(h.text(), /本轮详情/);
    } finally { h.restore(); }
  });
}

for (const method of ["openConversation", "confirmCandidate", "retractMemory"]) {
  test(`real runtime ${method} captures its binding before awaiting the login gate`, async () => {
    const h = realRuntime(method === "openConversation" ? SELF : CHILD);
    try {
      await h.finish(h.memory.onShow());
      const operation = h.memory[method]({ currentTarget: { dataset: { sessionId: "session", claimId: "same-claim" } } });
      h.api.selectDeviceBinding({ ...CHILD, binding_version: 2, primary_subject_ids: ["p_other"] });
      const writes = h.memory.writes.length;
      const requests = h.requests.length;
      await h.finish(operation);
      assert.ok(h.memory.writes.length > writes);
      assert.ok(h.requests.length > requests);
      assert.equal(h.memory.data.guardianSubjectId, "p_other");
      assert.equal(h.memory.data.loading, false);
      assert.equal(h.requests.slice(requests).some((request) => request.url.includes("conversation-history") || request.method === "POST"), false);
    } finally { h.restore(); }
  });
}

for (const change of ["newload", "unload", "authclear"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`real runtime old retract ${outcome} makes zero writes after ${change}`, async () => {
      const h = realRuntime(CHILD);
      try {
        await h.finish(h.memory.onShow());
        h.wx.showModal = (options) => options.success({ confirm: true });
        const event = { currentTarget: { dataset: { claimId: "same-claim", pending: true } } };
        const retract = h.memory.retractMemory(event);
        await flush();
        const old = h.pending("/minors/p_child/memories/same-claim/review");
        assert.ok(old);
        old.held = true;
        let confirm;
        if (change === "newload") {
          await h.finish(h.memory.onShow());
          confirm = h.memory.confirmCandidate(event);
          await flush();
        }
        if (change === "unload") h.memory.onUnload();
        if (change === "authclear") h.app.clearAuthenticatedIdentity();
        const writes = h.memory.writes.length;
        const before = structuredClone(h.memory.data);
        const toasts = h.calls.filter((call) => call.name === "showToast").length;
        h.reply(old, outcome === "resolve" ? {} : { detail: "旧撤回失败" }, outcome === "resolve" ? 200 : 500);
        await retract;
        assert.deepEqual(h.memory.data, before);
        assert.equal(h.memory.writes.length, writes);
        assert.equal(h.calls.filter((call) => call.name === "showToast").length, toasts);
        if (confirm) {
          assert.equal(h.memory.data.reviewingClaimId, "same-claim");
          await h.finish(confirm);
          assert.equal(h.memory.data.reviewingClaimId, "");
        }
      } finally { h.restore(); }
    });
  }
}

for (const change of ["newload", "unload", "authclear"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`real runtime superseded review ${outcome} makes zero writes after ${change}`, async () => {
      const h = realRuntime();
      try {
        const load = h.memory.onShow();
        await flush();
        const oldRequests = [...h.requests];
        for (const request of oldRequests) request.held = true;
        if (change === "newload") await h.finish(h.memory.onShow());
        if (change === "unload") h.memory.onUnload();
        if (change === "authclear") h.app.clearAuthenticatedIdentity();
        const before = structuredClone(h.memory.data);
        const writes = h.memory.writes.length;
        for (const request of oldRequests) h.reply(request, outcome === "resolve" ? ADULT_REVIEW : { detail: "旧回顾失败" }, outcome === "resolve" ? 200 : 500);
        await load;
        assert.deepEqual(h.memory.data, before);
        assert.equal(h.memory.writes.length, writes);
        assert.equal(h.requests.length, change === "newload" ? oldRequests.length + 3 : oldRequests.length);
      } finally { h.restore(); }
    });
  }
}

for (const binding of [CHILD, null]) {
  test(`settled adult detail rejects a late ${binding ? "child" : "empty"} binding and refreshes the page`, async () => {
    const h = realRuntime();
    try {
      await h.finish(h.memory.onShow());
      if (binding) h.api.selectDeviceBinding(binding);
      else h.api.clearDeviceBindingContext();
      const start = h.requests.length;
      await h.finish(h.memory.openConversation({ currentTarget: { dataset: { sessionId: "session" } } }));
      assert.equal(h.requests.slice(start).some((request) => request.url.includes("conversation-history")), false);
      assert.equal(h.memory.data.loading, false);
      assert.deepEqual(h.memory.data.conversationTurns, []);
      assert.equal(h.memory.data.guardianSubjectId, binding ? "p_child" : "");
      assert.doesNotMatch(h.text(), /成人私密原话详情/);
    } finally { h.restore(); }
  });
}
for (const method of ["confirmCandidate", "retractMemory"]) {
  test(`settled ${method} refreshes a changed binding instead of silently ignoring the action`, async () => {
    const h = realRuntime(CHILD);
    try {
      await h.finish(h.memory.onShow());
      h.api.selectDeviceBinding({ ...CHILD, binding_version: 2, primary_subject_ids: ["p_other"] });
      const start = h.requests.length;
      await h.finish(h.memory[method]({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } }));
      assert.equal(h.memory.data.guardianSubjectId, "p_other");
      assert.equal(h.memory.data.loading, false);
      assert.equal(h.memory.data.reviewingClaimId, "");
      assert.ok(h.requests.length > start);
      assert.equal(h.requests.slice(start).some((request) => request.method === "POST"), false);
    } finally { h.restore(); }
  });
  test(`in-flight ${method} refreshes after the server reply when only the binding version changes`, async () => {
    const h = realRuntime(CHILD);
    try {
      await h.finish(h.memory.onShow());
      h.wx.showModal = (options) => options.success({ confirm: true });
      const operation = h.memory[method]({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } });
      await flush();
      const old = h.pending("/memories/same-claim/review");
      assert.ok(old);
      h.api.selectDeviceBinding({ ...CHILD, binding_version: 2 });
      const start = h.requests.length;
      h.reply(old, {});
      await h.finish(operation);
      assert.ok(h.requests.length > start);
      assert.equal(h.memory.data.loading, false);
      assert.equal(h.memory.data.reviewingClaimId, "");
      const next = h.memory[method]({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } });
      await flush();
      assert.ok(h.pending("/minors/p_child/memories/same-claim/review"));
      await h.finish(next);
    } finally { h.restore(); }
  });
}

for (const binding of [CHILD, { ...CHILD, primary_subject_ids: ["p_other"] }, { ...SELF, binding_version: 2 }]) {
  for (const stage of ["review", "safety"]) {
    if (stage === "safety" && binding.declared_mode === "self_use") continue;
    test(`recovery double click waits for ${binding.declared_mode} ${binding.primary_subject_ids[0]} ${stage} to settle`, async () => {
      const h = realRuntime(binding.declared_mode === "self_use" ? SELF : CHILD);
      try {
        await h.finish(h.memory.onShow());
        h.api.selectDeviceBinding({ ...binding, binding_version: 3 });
        const method = binding.declared_mode === "self_use" ? "openConversation" : "confirmCandidate";
        const event = { currentTarget: { dataset: { sessionId: "session", claimId: "same-claim" } } };
        const recovery = h.memory[method](event);
        await flush();
        if (stage === "safety") {
          for (let round = 0; round < 8; round++) {
            for (const request of h.requests.filter((item) => !item.answered && !item.url.includes("/notifications/unread"))) {
              h.reply(request, h.response(request));
            }
            await flush();
          }
        }
        assert.equal(h.memory.data.loading, true);
        const start = h.requests.length;
        const second = h.memory[method](event);
        await flush();
        assert.equal(h.requests.slice(start).some((request) => request.method === "POST" || request.url.includes("conversation-history")), false);
        await h.finish(second);
        await h.finish(recovery);
        assert.equal(h.memory.data.loading, false);
        assert.equal(h.memory.data.reviewingClaimId, "");
      } finally { h.restore(); }
    });
  }
}
for (const method of ["confirmCandidate", "retractMemory"]) {
  test(`failed ${method} after version drift recovers without leaking the old error`, async () => {
    const h = realRuntime(CHILD);
    try {
      await h.finish(h.memory.onShow());
      h.wx.showModal = (options) => options.success({ confirm: true });
      const operation = h.memory[method]({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } });
      await flush();
      const post = h.pending("/memories/same-claim/review");
      h.api.selectDeviceBinding({ ...CHILD, binding_version: 2 });
      h.reply(post, { detail: "旧操作失败" }, 500);
      await h.finish(operation);
      assert.equal(h.memory.data.loading, false);
      assert.equal(h.memory.data.error, "");
      assert.equal(h.memory.data.reviewingClaimId, "");
      assert.doesNotMatch(h.text(), /旧操作失败/);
      const next = h.memory[method]({ currentTarget: { dataset: { claimId: "same-claim", pending: true } } });
      await flush();
      assert.ok(h.pending("/minors/p_child/memories/same-claim/review"));
      await h.finish(next);
    } finally { h.restore(); }
  });
}
for (const method of ["openConversation", "confirmCandidate", "retractMemory"]) {
  test(`expired login at ${method} enters the guest page without private requests`, async () => {
    const h = realRuntime(method === "openConversation" ? SELF : CHILD);
    const now = Date.now;
    try {
      await h.finish(h.memory.onShow());
      const current = now();
      Date.now = () => current + 4_000_000;
      const start = h.requests.length;
      await h.memory[method]({ currentTarget: { dataset: { sessionId: "session", claimId: "same-claim" } } });
      assert.equal(h.memory.data.authenticated, false);
      assert.equal(h.memory.data.loading, false);
      assert.deepEqual(h.memory.data.conversationTurns, []);
      assert.equal(h.requests.length, start);
      assert.match(h.text(), /微信登录后打开/);
    } finally { Date.now = now; h.restore(); }
  });
}
