const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
let pageDefinition = null;

function createDeferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function withWx(fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showToast() {},
    navigateTo() {},
    showModal(options) {
      options.success?.({ confirm: true });
    },
  };
  global.getApp = () => ({
    globalData: {},
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
    // 必须等 fn 完成再恢复 global.wx；同步 return 会在首个 await 后
    // 提前清掉 wx，让页面读取 storage 全部落空。
    await fn();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
}

function loadPage(relativePath) {
  const resolved = require.resolve(relativePath);
  delete require.cache[resolved];
  require(resolved);
  return pageDefinition;
}

function instantiate(definition) {
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
      let cursor = instance.data;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        if (typeof cursor[part] !== "object" || cursor[part] === null) {
          cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
        }
        cursor = cursor[part];
      }
      cursor[parts[parts.length - 1]] = value;
    }
  };
  return instance;
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

function baseStubs(overrides = {}) {
  const identity = { user_id: "person_owner" };
  const readyBinding = canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" });
  return {
    hasAuthenticatedSession: () => true,
    currentIdentity: () => identity,
    currentAuthEpoch: () => 0,
    isAuthEpochCurrent: () => true,
    syncDeviceBindings: async () => ({
      status: "ready",
      binding: readyBinding,
      bindings: [readyBinding],
    }),
    getProfile: async () => ({ display_name: "主人" }),
    getSpeakerEnrollmentStatus: async () => ({ enrollment: { state: "allowed" } }),
    listVoiceProfiles: async () => ({ items: [] }),
    getDeliveredCapabilities: async () => ({}),
    getRuntimeProfile: async () => ({
      valid: true,
      capabilities: [],
    }),
    requireRuntimeCapability: async () => ({ allowed: true, reason: "allowed" }),
    getMemoryDays: async () => ({ items: [] }),
    ...overrides,
  };
}

function newPage() {
  const definition = loadPage("../pages/profile/index");
  return instantiate(definition);
}

async function flushMicrotasks() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

async function flushAsync() {
  for (let i = 0; i < 10; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

test("空缓存只进入同步中，不能推断账号没有设备", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const deferred = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          syncCalls += 1;
          return deferred.promise;
        },
      }),
    });
    try {
      const loading = page._loadAuthenticatedData();
      await flushMicrotasks();
      assert.equal(syncCalls, 1);
      assert.equal(page.data.deviceBindingState, "syncing");
      assert.equal(page.data.deviceBindingStateLabel, "同步中");
      assert.equal(page.data.deviceBindingCountLabel, "未确认");
      assert.notEqual(page.data.deviceBindingStateLabel, "未绑定设备");
      deferred.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" }),
        bindings: [canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" })],
      });
      await loading;
      assert.equal(page.data.deviceBindingState, "ready");
      assert.equal(page.data.deviceBindingStateLabel, "已绑定设备");
      assert.equal(page.data.deviceBindingCountLabel, "1 台");
    } finally {
      restore();
    }
  });
});

test("服务端同步失败（405）显示同步失败，而不是没有设备", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    let gateCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          throw { status: 405, message: "Method Not Allowed" };
        },
        requireRuntimeCapability: async () => {
          gateCalls += 1;
          return { allowed: false, reason: "binding_sync_failed" };
        },
      }),
    });
    try {
      await page._loadAuthenticatedData();
      assert.equal(page.data.deviceBindingState, "error");
      assert.equal(page.data.deviceBindingStateLabel, "同步失败");
      assert.equal(page.data.deviceBindingCountLabel, "未确认");
      assert.ok(page.data.deviceBindingDetail.includes("这不代表未绑定"));
      assert.ok(page.data.deviceBindingDetail.includes("无需重新配网"));
      assert.ok(!page.data.deviceBindingDetail.includes("Method Not Allowed"));
      assert.equal(page.data.deviceBindingError, "Method Not Allowed");
      assert.ok(page.data.profileUnavailableReason.includes("同步失败"));
      assert.ok(!page.data.profileUnavailableReason.includes("还没有绑定设备"));
      assert.equal(gateCalls, 0, "同步失败时不得触发 Runtime 能力门禁请求");
      assert.deepEqual(page.data.stats, { totalDays: 0, moments: 0, streak: 0 });
    } finally {
      restore();
    }
  });
});

test("服务端确认 empty 才显示未绑定设备，且保持 fail-closed", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    let gateCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => ({
          status: "empty",
          binding: null,
          bindings: [],
        }),
        requireRuntimeCapability: async () => {
          gateCalls += 1;
          return { allowed: false, reason: "no_binding" };
        },
      }),
    });
    try {
      await page._loadAuthenticatedData();
      assert.equal(page.data.deviceBindingState, "empty");
      assert.equal(page.data.deviceBindingStateLabel, "未绑定设备");
      assert.equal(page.data.deviceBindingCountLabel, "0 台");
      assert.ok(page.data.deviceBindingDetail.includes("服务端确认"));
      assert.ok(page.data.profileUnavailableReason.includes("服务端确认还没有绑定设备"));
      assert.equal(gateCalls, 0, "empty 时 loadStats 不应重复走 requireRuntimeCapability");
    } finally {
      restore();
    }
  });
});

test("cached 状态保留本机绑定并提示列表未同步", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_keep", device_id: "dev_keep77" }),
    );
    const page = newPage();
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => ({
          status: "cached",
          binding: binding.readBindingManifest(),
          bindings: [],
          error: { status: 405, message: "设备列表同步失败" },
        }),
      }),
    });
    try {
      await page._loadAuthenticatedData();
      assert.equal(page.data.deviceBindingState, "cached");
      assert.equal(page.data.deviceBindingStateLabel, "已绑定设备");
      assert.ok(page.data.deviceBindingCountLabel.includes("列表未同步"));
      assert.ok(page.data.deviceBindingDetail.includes("设备列表暂时无法同步"));
      assert.ok(page.data.deviceBindingError);
      assert.equal(
        page.data.hasRuntimeProfile,
        true,
        "reason=" + page.data.profileUnavailableReason + " error=" + page.data.error,
      );
      assert.equal(page.data.profileUnavailableReason, "");
    } finally {
      restore();
    }
  });
});

test("多台设备进入待选择，选择后按所选设备刷新", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const bindingA = canonicalManifest({ binding_id: "bd_a", device_id: "dev_aa11" });
    const bindingB = canonicalManifest({ binding_id: "bd_b", device_id: "dev_bb22" });
    let syncRound = 0;
    const selections = [];
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          syncRound += 1;
          if (syncRound === 1) {
            return { status: "choose", binding: null, bindings: [bindingA, bindingB] };
          }
          return { status: "ready", binding: bindingB, bindings: [bindingA, bindingB] };
        },
        selectDeviceBinding: (selected) => {
          selections.push(selected);
          binding.saveBindingManifest(selected);
          return selected;
        },
      }),
    });
    try {
      await page._loadAuthenticatedData();
      assert.equal(page.data.deviceBindingState, "choose");
      assert.equal(page.data.deviceBindingStateLabel, "待选择设备");
      assert.equal(page.data.deviceBindingCountLabel, "2 台待选择");
      assert.equal(page.data.deviceBindingChoices.length, 2);
      assert.ok(page.data.profileUnavailableReason.includes("请先选择当前设备"));

      await page.chooseDeviceBinding({
        currentTarget: { dataset: { bindingId: "bd_b" } },
      });
      assert.equal(selections.length, 1);
      assert.equal(selections[0].binding_id, "bd_b");
      assert.equal(page.data.deviceBindingState, "ready");
      assert.equal(page.data.deviceBinding.device_id, "dev_bb22");
      assert.ok(page.data.deviceBindingLabel.includes("bb22"));
      assert.equal(page.data.deviceBindingChoices.length, 2);
    } finally {
      restore();
    }
  });
});

test("重复调用共享同一在途同步，不产生重复请求", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const deferred = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          syncCalls += 1;
          return deferred.promise;
        },
      }),
    });
    try {
      const first = page.loadDeviceBindingSync();
      const second = page.loadDeviceBindingSync();
      assert.equal(syncCalls, 1, "同账号在途请求必须去重");
      assert.ok(second, "去重路径必须返回同一个在途请求结果");
      deferred.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" }),
        bindings: [canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" })],
      });
      const [stateOne, stateTwo] = await Promise.all([first, second]);
      assert.equal(stateOne.status, "ready");
      assert.equal(stateTwo.status, "ready");
      assert.equal(stateTwo, stateOne, "两次调用必须共享同一次同步结果");
    } finally {
      restore();
    }
  });
});

test("账号切换后旧账号迟到同步不得覆盖新账号状态", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    let identity = { user_id: "person_a" };
    let authEpoch = 0;
    const deferredOld = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        currentIdentity: () => identity,
        currentAuthEpoch: () => authEpoch,
        syncDeviceBindings: async () => {
          syncCalls += 1;
          if (syncCalls === 1) return deferredOld.promise;
          return {
            status: "ready",
            binding: canonicalManifest({ binding_id: "bd_new", device_id: "dev_9999" }),
            bindings: [canonicalManifest({ binding_id: "bd_new", device_id: "dev_9999" })],
          };
        },
      }),
    });
    try {
      page.setData({ deviceBindingAccountId: "person_a" });
      const oldRequest = page.loadDeviceBindingSync();
      await flushMicrotasks();

      identity = { user_id: "person_b" };
      authEpoch = 1;
      const newRequest = page.loadDeviceBindingSync();
      await flushMicrotasks();
      assert.equal(syncCalls, 2, "账号切换必须允许发起新账号同步");
      assert.equal(page.data.deviceBindingAccountId, "person_b");

      deferredOld.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_old", device_id: "dev_old00" }),
        bindings: [canonicalManifest({ binding_id: "bd_old", device_id: "dev_old00" })],
      });
      const oldState = await oldRequest;
      const newState = await newRequest;
      assert.equal(oldState, null, "旧账号迟到结果必须被丢弃");
      assert.equal(newState.status, "ready");
      assert.equal(page.data.deviceBindingAccountId, "person_b");
      assert.equal(page.data.deviceBinding.device_id, "dev_9999");
      assert.ok(!page.data.deviceBindingDetail.includes("ld00"), "不得残留旧账号设备标签");
    } finally {
      restore();
    }
  });
});

test("onShow 读取到新账号时重置旧绑定展示", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => ({
          status: "ready",
          binding: canonicalManifest({ binding_id: "bd_new", device_id: "dev_9999" }),
          bindings: [canonicalManifest({ binding_id: "bd_new", device_id: "dev_9999" })],
        }),
      }),
    });
    try {
      page.setData({
        deviceBindingAccountId: "person_old",
        deviceBindingState: "ready",
        deviceBindingStateLabel: "已绑定设备",
        deviceBindingDetail: "旧账号的设备",
      });
      page.onShow();
      assert.equal(page.data.deviceBindingAccountId, "person_owner");
      assert.equal(page.data.deviceBindingState, "syncing");
      assert.ok(!page.data.deviceBindingDetail.includes("旧账号"));
      await page._deviceBindingRequest;
      await page.loadProfile();
      assert.equal(page.data.deviceBindingState, "ready");
      assert.equal(page.data.deviceBinding.device_id, "dev_9999");
      assert.ok(page.data.deviceBindingLabel.includes("9999"));
    } finally {
      restore();
    }
  });
});

test("游客状态不持有绑定信息，也不显示未绑定设备", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    let authenticated = true;
    const restore = stubApi({
      ...baseStubs({
        hasAuthenticatedSession: () => authenticated,
        currentIdentity: () => (authenticated ? { user_id: "person_owner" } : null),
      }),
    });
    try {
      await page._loadAuthenticatedData();
      assert.equal(page.data.deviceBindingState, "ready");
      authenticated = false;
      page.onShow();
      assert.equal(page.data.authenticated, false);
      assert.equal(page.data.deviceBindingState, "idle");
      assert.equal(page.data.deviceBindingStateLabel, "待同步");
      assert.equal(page.data.deviceBindingDetail, "登录后从服务端同步账号可见设备。");
      assert.equal(page.data.hasRuntimeProfile, false);
      assert.equal(page.data.profileUnavailableReason, "");
    } finally {
      restore();
    }
  });
});

test("onShow 换账号时旧请求忙位不得拦截新账号同步", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    let identity = { user_id: "person_a" };
    let authEpoch = 0;
    const deferredA = createDeferred();
    const deferredB = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        currentIdentity: () => identity,
        currentAuthEpoch: () => authEpoch,
        isAuthEpochCurrent: (epoch) => epoch === authEpoch,
        syncDeviceBindings: async () => {
          syncCalls += 1;
          if (syncCalls === 1) return deferredA.promise;
          if (syncCalls === 2) return deferredB.promise;
          return {
            status: "ready",
            binding: canonicalManifest({ binding_id: "bd_b", device_id: "dev_9999" }),
            bindings: [canonicalManifest({ binding_id: "bd_b", device_id: "dev_9999" })],
          };
        },
      }),
    });
    try {
      page.setData({ deviceBindingAccountId: "person_a" });
      page.onShow();
      await flushAsync();
      assert.equal(syncCalls, 1, "首次 onShow 必须发起一次同步");
      assert.equal(page.data.deviceBindingState, "syncing");

      // 旧账号请求在途时切换账号并再次 onShow：必须允许新账号发起新请求。
      identity = { user_id: "person_b" };
      authEpoch = 1;
      page.onShow();
      await flushAsync();
      assert.equal(syncCalls, 2, "换账号 onShow 必须发起新账号同步，不能被旧忙位拦截");
      assert.equal(page.data.deviceBindingAccountId, "person_b");
      assert.equal(page.data.deviceBindingState, "syncing");

      // 旧账号迟到结果必须被丢弃，不得污染新账号展示。
      deferredA.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_old", device_id: "dev_old00" }),
        bindings: [canonicalManifest({ binding_id: "bd_old", device_id: "dev_old00" })],
      });
      await flushAsync();
      assert.equal(page.data.deviceBindingState, "syncing", "旧账号迟到结果不得改写新账号状态");
      assert.ok(!page.data.deviceBindingDetail.includes("ld00"));

      // 同账号 B 的第三次 onShow 必须去重，不能清掉在途新请求的忙位。
      page.onShow();
      await flushAsync();
      assert.equal(syncCalls, 2, "同账号同 epoch 在途加载必须复用请求");
      assert.equal(page._profileDataBusy, true, "在途新请求的忙位不得被旧请求 finally 清掉");

      deferredB.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_b", device_id: "dev_9999" }),
        bindings: [canonicalManifest({ binding_id: "bd_b", device_id: "dev_9999" })],
      });
      await flushAsync();
      assert.equal(page.data.deviceBindingState, "ready");
      assert.equal(page.data.deviceBinding.device_id, "dev_9999");
      assert.ok(page.data.deviceBindingLabel.includes("9999"));
      assert.ok(!page.data.deviceBindingDetail.includes("ld00"));
      assert.equal(page._profileDataBusy, false, "加载完成后忙位必须释放");
      assert.equal(page._deviceBindingBusy, false);
    } finally {
      restore();
    }
  });
});

test("同账号重复 onShow 去重且完成后释放忙位", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const deferred = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          syncCalls += 1;
          return deferred.promise;
        },
      }),
    });
    try {
      page.onShow();
      await flushAsync();
      page.onShow();
      await flushAsync();
      assert.equal(syncCalls, 1, "同账号同 epoch 重复 onShow 必须去重");
      deferred.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" }),
        bindings: [canonicalManifest({ binding_id: "bd_a", device_id: "dev_ab12" })],
      });
      await flushAsync();
      assert.equal(page.data.deviceBindingState, "ready");
      assert.equal(page.data.deviceBinding.device_id, "dev_ab12");
      assert.equal(page._profileDataBusy, false);
      assert.equal(page._deviceBindingBusy, false);
      assert.equal(page._profileDataRequest, null);
      assert.equal(page._deviceBindingRequest, null);
    } finally {
      restore();
    }
  });
});

test("onUnload 后迟到加载不得改写页面且必须释放忙位", async () => {
  await withWx(async () => {
    for (const key of Object.keys(storage)) delete storage[key];
    const page = newPage();
    const deferred = createDeferred();
    let syncCalls = 0;
    const restore = stubApi({
      ...baseStubs({
        syncDeviceBindings: async () => {
          syncCalls += 1;
          return deferred.promise;
        },
      }),
    });
    try {
      page.onShow();
      await flushAsync();
      assert.equal(syncCalls, 1);
      page.onUnload();
      assert.equal(page._profileDataBusy, false, "onUnload 必须释放外层忙位");
      assert.equal(page._deviceBindingBusy, false, "onUnload 必须释放同步忙位");
      assert.equal(page._profileDataRequest, null);
      assert.equal(page._deviceBindingRequest, null);
      deferred.resolve({
        status: "ready",
        binding: canonicalManifest({ binding_id: "bd_late", device_id: "dev_late88" }),
        bindings: [canonicalManifest({ binding_id: "bd_late", device_id: "dev_late88" })],
      });
      await flushAsync();
      assert.equal(page.data.deviceBindingState, "syncing", "卸载后的迟到结果不得改写页面状态");
      assert.ok(!page.data.deviceBindingDetail.includes("te88"));
    } finally {
      restore();
    }
  });
});
