const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");

function createDeferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function flushMicrotasks() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

function makeAuth({ authenticated = true } = {}) {
  return {
    authEpoch: 7,
    authenticated,
    identity: {
      user_id: "person_owner",
      display_name: "主人",
      account_type: "registered",
    },
  };
}

function stubApi(overrides) {
  const originals = {};
  for (const [key, value] of Object.entries(overrides)) {
    originals[key] = api[key];
    api[key] = value;
  }
  return () => {
    for (const [key, value] of Object.entries(originals)) {
      if (value === undefined) delete api[key];
      else api[key] = value;
    }
  };
}

function authStubs(state, { restore = "succeed" } = {}) {
  return {
    hasAuthenticatedSession: () => state.authenticated,
    currentIdentity: () => (state.authenticated ? state.identity : null),
    currentAuthEpoch: () => state.authEpoch,
    isAuthEpochCurrent: (epoch) =>
      epoch === state.authEpoch && state.authenticated,
    restoreWechatIdentity: async () => {
      if (restore === "fail") throw new Error("微信登录已过期");
      state.authenticated = true;
      return state.identity;
    },
  };
}

function setPath(target, key, value) {
  const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
  let cursor = target;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index];
    if (typeof cursor[part] !== "object" || cursor[part] === null) {
      cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
    }
    cursor = cursor[part];
  }
  cursor[parts[parts.length - 1]] = value;
}

function instantiate(definition) {
  const page = { ...definition };
  page.data = JSON.parse(JSON.stringify(definition.data));
  page.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) setPath(page.data, key, value);
  };
  return page;
}

async function withPage(relativePath, callback) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  const loginNavigations = [];
  let pageDefinition;
  global.wx = {
    getStorageSync: () => "",
    setStorageSync: () => {},
    removeStorageSync: () => {},
    showToast() {},
    stopPullDownRefresh() {},
    navigateTo(options) {
      loginNavigations.push(options.url);
    },
  };
  global.getApp = () => ({
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  const resolved = require.resolve(relativePath);
  delete require.cache[resolved];
  require(resolved);
  try {
    await callback(instantiate(pageDefinition), loginNavigations);
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
}

function readyBinding() {
  return {
    binding_id: "bd_retry",
    device_id: "dev_retry",
    declared_mode: "owner_private",
    binding_version: 1,
  };
}

test("home retry 未授权时 fail closed，恢复失败后迟到响应不回填", async () => {
  const state = makeAuth();
  const deferred = createDeferred();
  let syncCalls = 0;
  const restore = stubApi({
    ...authStubs(state, { restore: "fail" }),
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return deferred.promise;
    },
  });
  await withPage("../pages/home/index", async (page, loginNavigations) => {
    page.setData({
      authenticated: true,
      hasBinding: true,
      bindingSyncError: "旧的同步失败状态",
      bindingChoices: [{ bindingId: "bd_old" }],
      needsBindingChoice: true,
    });
    const loading = page.loadHome();
    await flushMicrotasks();
    assert.equal(syncCalls, 1);
    state.authenticated = false;
    await page.retryBindingSync();
    assert.equal(syncCalls, 1);
    assert.equal(loginNavigations.length, 1);
    assert.match(loginNavigations[0], /\/pages\/auth\/index/);
    assert.equal(page.data.authenticated, false);
    assert.equal(page.data.hasBinding, false);
    assert.equal(page.data.bindingSyncError, "");
    assert.deepEqual(page.data.bindingChoices, []);
    deferred.resolve({
      status: "ready",
      binding: readyBinding(),
      bindings: [readyBinding()],
    });
    await loading;
    assert.equal(syncCalls, 1);
    assert.equal(page.data.hasBinding, false);
  });
  restore();
});

test("home retry 恢复登录后正常同步", async () => {
  const state = makeAuth({ authenticated: false });
  let syncCalls = 0;
  let restoreCalls = 0;
  const restore = stubApi({
    ...authStubs(state),
    restoreWechatIdentity: async () => {
      restoreCalls += 1;
      state.authenticated = true;
      return state.identity;
    },
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return {
        status: "ready",
        binding: readyBinding(),
        bindings: [readyBinding()],
      };
    },
    getActivationStatus: async () => null,
    getRuntimeProfile: async () => null,
    getDeviceSettings: async () => null,
    getProfile: async () => ({ display_name: "主人" }),
    requireRuntimeCapability: async () => ({ allowed: false }),
  });
  await withPage("../pages/home/index", async (page) => {
    await page.retryBindingSync();
    assert.equal(restoreCalls, 1);
    assert.equal(syncCalls, 1);
    assert.equal(page.data.authenticated, true);
    assert.equal(page.data.hasBinding, true);
    assert.equal(page.data.bindingSyncError, "");
  });
  restore();
});

test("device retry 未授权时清空敏感设备态，迟到响应不回填", async () => {
  const state = makeAuth();
  const deferred = createDeferred();
  let syncCalls = 0;
  const restore = stubApi({
    ...authStubs(state, { restore: "fail" }),
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return deferred.promise;
    },
  });
  await withPage("../pages/device/index", async (page, loginNavigations) => {
    page.setData({
      hasBinding: true,
      binding: readyBinding(),
      bindingRoles: ["账号持有人"],
      bindingSyncError: "旧的同步失败状态",
      sensitiveEntries: [{ label: "旧敏感条目" }],
    });
    const loading = page.loadDevice();
    await flushMicrotasks();
    assert.equal(syncCalls, 1);
    state.authenticated = false;
    await page.retryBindingSync();
    assert.equal(syncCalls, 1);
    assert.equal(loginNavigations.length, 1);
    assert.match(loginNavigations[0], /\/pages\/auth\/index/);
    assert.equal(page.data.hasBinding, false);
    assert.equal(page.data.binding, null);
    assert.deepEqual(page.data.bindingRoles, []);
    assert.deepEqual(page.data.sensitiveEntries, []);
    assert.equal(page.data.bindingSyncError, "");
    deferred.resolve({
      status: "ready",
      binding: readyBinding(),
      bindings: [readyBinding()],
    });
    await loading;
    assert.equal(syncCalls, 1);
    assert.equal(page.data.hasBinding, false);
    assert.equal(page.data.binding, null);
  });
  restore();
});

test("device retry 恢复登录后正常同步", async () => {
  const state = makeAuth({ authenticated: false });
  let syncCalls = 0;
  const restore = stubApi({
    ...authStubs(state),
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return {
        status: "ready",
        binding: readyBinding(),
        bindings: [readyBinding()],
      };
    },
    getRuntimeProfile: async () => null,
    getActivationStatus: async () => null,
    getDeviceSettings: async () => null,
    getDeviceDiagnostics: async () => null,
    getWakeWordCatalog: async () => ({ items: [] }),
  });
  await withPage("../pages/device/index", async (page) => {
    await page.retryBindingSync();
    assert.equal(syncCalls, 1);
    assert.equal(page.data.hasBinding, true);
    assert.equal(page.data.binding.device_id, "dev_retry");
    assert.equal(page.data.bindingSyncError, "");
  });
  restore();
});

test("profile retry 未授权时进入游客态，旧同步迟到不得回填", async () => {
  const state = makeAuth();
  const deferred = createDeferred();
  let syncCalls = 0;
  const restore = stubApi({
    ...authStubs(state, { restore: "fail" }),
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return deferred.promise;
    },
  });
  await withPage("../pages/profile/index", async (page, loginNavigations) => {
    page.setData({
      deviceBindingAccountId: "person_owner",
      deviceBindingState: "error",
      deviceBinding: readyBinding(),
      deviceBindingChoices: [{ bindingId: "bd_old" }],
      speakerEntryAllowed: true,
    });
    page._lastCapabilityState = { speakerEntryAllowed: true };
    const loading = page._loadAuthenticatedData();
    await flushMicrotasks();
    assert.equal(syncCalls, 1);
    assert.equal(page.data.deviceBindingState, "syncing");
    const profileSeqDuring = page._profileDataSeq;
    const bindingSeqDuring = page._deviceBindingSeq;
    state.authenticated = false;
    await page.retryDeviceBindingSync();
    assert.equal(syncCalls, 1);
    assert.equal(loginNavigations.length, 1);
    assert.match(loginNavigations[0], /\/pages\/auth\/index/);
    assert.equal(page.data.authenticated, false);
    assert.equal(page.data.deviceBindingState, "idle");
    assert.equal(page.data.deviceBinding, null);
    assert.deepEqual(page.data.deviceBindingChoices, []);
    assert.equal(page.data.speakerEntryAllowed, false);
    assert.equal(page._profileDataSeq, profileSeqDuring + 1);
    assert.equal(page._deviceBindingSeq, bindingSeqDuring + 1);
    assert.equal(page._profileDataBusy, false);
    assert.equal(page._profileDataBusyKey, "");
    assert.equal(page._profileDataRequest, null);
    assert.equal(page._deviceBindingBusy, false);
    assert.equal(page._deviceBindingBusyKey, "");
    assert.equal(page._deviceBindingRequest, null);
    assert.equal(page._lastCapabilityState, null);
    deferred.resolve({
      status: "ready",
      binding: readyBinding(),
      bindings: [readyBinding()],
    });
    await loading;
    assert.equal(syncCalls, 1);
    assert.equal(page.data.deviceBindingState, "idle");
    assert.equal(page.data.deviceBinding, null);
    assert.equal(page._profileDataSeq, profileSeqDuring + 1);
    assert.equal(page._deviceBindingSeq, bindingSeqDuring + 1);
    assert.equal(page._profileDataBusy, false);
    assert.equal(page._deviceBindingBusy, false);
  });
  restore();
});

test("profile retry 恢复登录后正常同步", async () => {
  const state = makeAuth({ authenticated: false });
  let syncCalls = 0;
  const restore = stubApi({
    ...authStubs(state),
    syncDeviceBindings: async () => {
      syncCalls += 1;
      return {
        status: "ready",
        binding: readyBinding(),
        bindings: [readyBinding()],
      };
    },
    getProfile: async () => ({ display_name: "主人" }),
    getSpeakerEnrollmentStatus: async () => ({
      enrollment: { state: "blocked" },
    }),
    listVoiceProfiles: async () => ({ items: [] }),
    getDeliveredCapabilities: async () => ({}),
    requireRuntimeCapability: async () => ({ allowed: false }),
  });
  await withPage("../pages/profile/index", async (page) => {
    await page.retryDeviceBindingSync();
    assert.equal(syncCalls, 1);
    assert.equal(page.data.authenticated, true);
    assert.equal(page.data.deviceBindingState, "ready");
    assert.equal(page.data.deviceBinding.device_id, "dev_retry");
    assert.equal(page.data.deviceBindingError, "");
  });
  restore();
});
