const assert = require("node:assert/strict");
const test = require("node:test");
const { loadPage } = require("../scripts/mp-harness/runtime");
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
    sync.resolve({ status: "ready", binding: CHILD });
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
      const h = openContext(null, { syncDeviceBindings: async () => result });
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
    test(`pending review ${outcome} is discarded after ${change}`, async () => {
      const review = deferred();
      const started = deferred();
      const h = openContext(SELF, { getConversationReview: () => { started.resolve(); return review.promise; } });
      try {
        const load = h.page.onShow();
        await started.promise;
        if (change === "binding") h.state.binding = CHILD;
        if (change === "identity") h.state.userId = "u_other";
        if (change === "epoch") h.state.epoch++;
        if (change === "logout") h.state.loggedIn = false;
        if (change === "authclear") h.clearAuth();
        if (change === "unload") h.page.onUnload();
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
    test(`pending safety ${outcome} makes no page writes after ${change}`, async () => {
      const safety = deferred();
      const started = deferred();
      const h = openContext(CHILD, {
        getUnreadGuardianNotifications: () => { started.resolve(); return safety.promise; },
      });
      try {
        const load = h.page.onShow();
        await started.promise;
        if (change === "authclear") h.clearAuth();
        if (change === "unload") h.page.onUnload();
        if (change === "logout") h.state.loggedIn = false;
        if (change === "binding") h.state.binding = { ...CHILD, binding_version: 2, primary_subject_ids: ["u_parent", "p_other"] };
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
