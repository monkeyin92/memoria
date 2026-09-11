const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const script = fs.readFileSync(path.join(root, "pages/profile/index.js"), "utf8");
const template = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
const styles = fs.readFileSync(path.join(root, "pages/profile/index.wxss"), "utf8");

const api = require("../utils/api");

const POLL_INTERVAL_MS = 2000;

let pageDefinition = null;
let storage = {};

// ---- wx / Page harness ----------------------------------------------------

function installPageTimeoutHarness() {
  const timers = [];
  const realSetInterval = global.setInterval;
  const realClearInterval = global.clearInterval;
  global.setInterval = (callback, delay) => {
    const handle = { callback, delay, cleared: false };
    timers.push(handle);
    return handle;
  };
  global.clearInterval = (handle) => {
    if (handle) handle.cleared = true;
  };
  return {
    timers,
    restore() {
      global.setInterval = realSetInterval;
      global.clearInterval = realClearInterval;
    },
    finished() {
      return timers.filter((timer) => !timer.cleared);
    },
    // 让所有 setInterval 回调各自走一拍，并等页面异步请求落定。
    async tick() {
      for (const timer of timers) {
        if (timer.cleared) continue;
        timer.callback();
      }
      await flushAsync();
    },
  };
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
    switchTab() {},
    showModal(options) {
      options.success?.({ confirm: true });
    },
    chooseMessageFile(options) {
      options.success({
        tempFiles: [{ path: "sample.mp3", name: "sample.mp3", size: 240000 }],
      });
    },
    getFileSystemManager: () => ({
      readFile(options) {
        options.success({ data: "ZmFrZS1hdWRpbw==" });
      },
    }),
  };
  global.getApp = () => ({
    globalData: {},
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
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

function loadPage() {
  const resolved = require.resolve("../pages/profile/index");
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

function newPage() {
  return instantiate(loadPage());
}

async function flushAsync() {
  for (let i = 0; i < 10; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function apiStubs(overrides = {}) {
  return {
    hasAuthenticatedSession: () => true,
    currentIdentity: () => ({ user_id: "person_owner" }),
    currentAuthEpoch: () => 0,
    isAuthEpochCurrent: () => true,
    syncDeviceBindings: async () => ({ status: "empty", binding: null, bindings: [] }),
    getProfile: async () => ({ display_name: "主人" }),
    getSpeakerEnrollmentStatus: async () => ({ enrollment: { state: "allowed" } }),
    listVoiceProfiles: async () => ({ items: [] }),
    getDeliveredCapabilities: async () => ({}),
    getRuntimeProfile: async () => null,
    requireRuntimeCapability: async () => ({ allowed: false, reason: "no_binding" }),
    getMemoryDays: async () => ({ items: [] }),
    ...overrides,
  };
}

// 服务端冻结契约里的一段 enrolling 快照。
// 额外字段用 override 覆盖，避免默认参数把显式 undefined 顶回默认值。
function enrollingItem(override = {}) {
  return {
    profile_id: "vp_1",
    status: "enrolling",
    evaluation_status: "pending",
    quality_status: "pending",
    sample_validation_status: "pending",
    enrollment: {
      stage: "cloning",
      stage_index: 2,
      stage_count: 3,
      stage_label: "正在生成你的声音",
      progress: 0.35,
      elapsed_ms: 12000,
      expected_total_ms: 60000,
      over_budget: false,
      retryable: false,
      error_code: null,
      validation: { passed: true, duration_ms: 23150, reasons: [] },
      ...override,
    },
  };
}

// ---- source-text assertions ----------------------------------------------

test("轮询使用 2000ms 间隔，且 onUnload 必须清掉计时器", () => {
  // 定时器标识必须独立于录音计时器存在。
  assert.match(script, /VOICE_CLONE_POLL_INTERVAL_MS\s*=\s*2000/);
  assert.match(script, /this\._voiceClonePollTimer\s*=\s*setInterval\(/);
  assert.match(script, /VOICE_CLONE_POLL_INTERVAL_MS\)/);
  // 必须存在独立的清理函数，并在 onUnload 里调用。
  assert.match(script, /_stopVoiceClonePoll\(\)\s*\{[\s\S]*?clearInterval\(this\._voiceClonePollTimer\)/);
  assert.match(script, /onUnload\(\)\s*\{[\s\S]*?this\._stopVoiceClonePoll\(\)/);
  // 录音计时器与复刻轮询计时器不得共用一个字段。
  assert.match(script, /_stopRecordingTimer\(\)\s*\{[\s\S]*?clearInterval\(this\._recordTimer\)/);
  assert.doesNotMatch(script, /_recordTimer\s*=\s*setInterval\([\s\S]{0,200}?VOICE_CLONE_POLL_INTERVAL_MS/);
});

test("WXML 渲染 enrollment.stage_label 与绑定 enrollment.progress 的进度条", () => {
  assert.match(template, /\{\{voiceCloneEnrollment\.label\}\}/);
  assert.match(template, /class="enrollment-bar"/);
  assert.match(template, /class="enrollment-bar-fill"/);
  assert.match(template, /style="width:\{\{voiceCloneEnrollment\.progressPercent\}\}%"/);
  assert.match(template, /\{\{voiceCloneEnrollment\.progressLabel\}\}/);
  // progress 缺失时整条进度条和百分比都不渲染，不编造进度。
  assert.match(template, /wx:if="\{\{voiceCloneEnrollment\.hasProgress\}\}"[\s\S]{0,200}?enrollment-bar/);
  assert.match(template, /wx:if="\{\{voiceCloneEnrollment\.hasProgress\}\}"[^>]*>\{\{voiceCloneEnrollment\.progressLabel\}\}/);
  assert.match(template, /wx:if="\{\{voiceCloneEnrollment\}\}"[\s\S]*?enrollment-progress/);
  // 进度条样式必须沿用页面既有视觉语言。
  assert.match(styles, /\.enrollment-bar\s*\{[^}]*border-radius:\s*999rpx/s);
  assert.match(styles, /\.enrollment-bar-fill\s*\{[^}]*background:\s*#326e70/s);
});

test("over_budget 之后不再承诺「一分钟」，而是提示可以离开", () => {
  assert.match(script, /over_budget\s*===\s*true/);
  assert.match(script, /已经比预计久了一点。可以先去忙别的，回来这一页再看就行。/);
  // "一分钟" 只出现在服务端已确认预算（expected_total_ms）的分支里，
  // 且 over_budget 分支必须排在它前面，才能提前短路掉这句承诺。
  const promiseIndex = script.indexOf("预计 1 分钟左右");
  const overBudgetIndex = script.indexOf("over_budget === true");
  assert.ok(promiseIndex > 0, "缺少 elapsed/expected 提示");
  assert.ok(
    overBudgetIndex > 0 && overBudgetIndex < promiseIndex,
    "over_budget 必须在承诺时长之前判断，超时后不得再承诺一分钟",
  );
  // 模板自身不得硬编码任何时长承诺。
  assert.doesNotMatch(template, /一分钟|大约一分/);
});

test("422 voice_sample_rejected 直接展示服务端 detail", () => {
  assert.match(script, /error\?\.message \|\| "这次没生成成功，请再试一次。"/);
  // 失败文案必须写回复刻状态，而不是只塞进通用错误行。
  assert.match(script, /catch\s*\(error\)\s*\{[\s\S]{0,400}?voiceCloneStatusData\(message\)/);
});

// ---- behaviour -----------------------------------------------------------

test("页面进入时开始轮询，进度写回视图，active 后停表", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    // 服务端状态可切换：先 enrolling，随后变成 active。
    let profiles = { items: [enrollingItem()] };
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => profiles,
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();

      // 只应存在一个 2000ms 的复刻轮询计时器。
      assert.deepEqual(
        harness.timers.map((timer) => timer.delay),
        [POLL_INTERVAL_MS],
        "复刻进度必须按 2000ms 轮询",
      );
      assert.equal(page.data.voiceCloneEnrollment.label, "正在生成你的声音");
      assert.equal(page.data.voiceCloneEnrollment.progressPercent, 35);

      // 服务端报告完成了：下一拍必须停表，并回到既有「已就绪」文案。
      profiles = {
        items: [{ profile_id: "vp_1", status: "active", enrollment: { stage: "done" } }],
      };
      await harness.tick();

      assert.equal(page.data.voiceCloneStatusLabel.includes("自定义声音已就绪"), true);
      assert.equal(page.data.voiceCloneEnrollment, null);
      assert.deepEqual(harness.finished(), [], "没有任何 enrolling 档案时必须停止轮询");
      assert.equal(page._voiceClonePollTimer, null, "停表后计时器标识必须清空");
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("onUnload 清掉复刻轮询计时器，迟到的结果不再改写页面", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => ({ items: [enrollingItem()] }),
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();
      assert.equal(harness.finished().length, 1, "进入页面后应有一个在跑轮询");

      page.onUnload();
      assert.deepEqual(harness.finished(), [], "onUnload 必须清掉轮询计时器");
      assert.equal(page._voiceClonePollTimer, null);
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("离开再回来仍能接上进度（进度来自服务端，不依赖上一个页面实例）", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => ({ items: [enrollingItem({ progress: 0.6 })] }),
      }),
    );
    try {
      const first = newPage();
      first.onShow();
      await flushAsync();
      first.onUnload();

      // 全新页面实例：没有任何内存状态可继承，仍必须从服务端恢复进度。
      const second = newPage();
      second.onShow();
      await flushAsync();
      assert.equal(second.data.voiceCloneEnrollment.progressPercent, 60);
      assert.equal(second.data.voiceCloneEnrollment.label, "正在生成你的声音");
      assert.equal(harness.finished().length, 1, "重新进入后应恢复轮询");
      second.onUnload();
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("onUnload 后在途轮询迟到，不得把计时器重新起回来", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    let release;
    const inFlight = new Promise((resolve) => {
      release = resolve;
    });
    // 先让页面正常进入轮询，之后再把请求挂住，模拟"卸载时请求还在路上"。
    let profiles = { items: [enrollingItem()] };
    let holdNext = false;
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => {
          if (holdNext) {
            await inFlight;
            return { items: [enrollingItem()] };
          }
          return profiles;
        },
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();
      assert.equal(harness.finished().length, 1, "进入页面后应有一个在跑轮询");

      // 下一拍开始请求，但让它挂在路上；此时卸载页面。
      holdNext = true;
      harness.tick();
      await flushAsync();
      page.onUnload();
      assert.deepEqual(harness.finished(), [], "卸载后不应还有计时器");
      assert.equal(page._voiceClonePollTimer, null);

      // 迟到的响应回来了：不能重新起表，也不能改写已卸载的页面。
      profiles = { items: [enrollingItem()] };
      release();
      await flushAsync();
      assert.deepEqual(
        harness.finished(),
        [],
        "迟到的轮询结果不得重新起表（否则会在已卸载页面上泄漏 setInterval）",
      );
      assert.equal(page._voiceClonePollTimer, null);
      assert.equal(page._voiceClonePollBusy, false, "忙位必须放掉");
    } finally {
      release();
      stubs();
      harness.restore();
    }
  });
});

test("over_budget 时提示不再承诺一分钟", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => ({ items: [enrollingItem({ over_budget: true })] }),
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();

      assert.equal(page.data.voiceCloneEnrollmentOverBudget, true);
      const hint = page.data.voiceCloneEnrollment.hint;
      // 超时后不得再承诺「一分钟」，也不能暗示"大约"。
      assert.doesNotMatch(hint, /一分钟|大约|通常/);
      assert.match(hint, /可以先去忙别的/);
      assert.match(hint, /比预计久了一点/);
      // progress 仍然由服务端给出时照样显示，不做隐瞒也不夸大。
      assert.equal(page.data.voiceCloneEnrollment.progressPercent, 35);
      page.onUnload();
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("enrollment 缺失时回退既有 voiceCloneStatusLabel，且不显示进度条", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        // 旧服务端：只有 status，没有 enrollment 对象。
        listVoiceProfiles: async () => ({ items: [{ profile_id: "vp_1", status: "enrolling" }] }),
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();

      assert.equal(page.data.voiceCloneEnrollment, null, "没有 enrollment 就不该渲染进度块");
      assert.match(page.data.voiceCloneStatusLabel, /正在生成自定义声音/);
      // 没有 enrollment 也仍要按 status 继续轮询。
      assert.equal(harness.finished().length, 1);
      page.onUnload();
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("progress 缺失时不显示百分比", async () => {
  await withWx(async () => {
    storage = {};
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        listVoiceProfiles: async () => ({ items: [enrollingItem({ progress: undefined })] }),
      }),
    );
    try {
      const page = newPage();
      page.onShow();
      await flushAsync();

      assert.equal(page.data.voiceCloneEnrollment.hasProgress, false);
      assert.equal(page.data.voiceCloneEnrollment.progressLabel, "");
      // 标签仍要显示，只是没有进度条。
      assert.equal(page.data.voiceCloneEnrollment.label, "正在生成你的声音");
      page.onUnload();
    } finally {
      stubs();
      harness.restore();
    }
  });
});

test("422 拒绝时把服务端 detail 显示给用户", async () => {
  await withWx(async () => {
    storage = {};
    const detail = "这段声音太安静了，请在安静的环境里重录";
    const harness = installPageTimeoutHarness();
    const stubs = stubApi(
      apiStubs({
        grantVoiceCloneConsent: async () => ({}),
        enrollVoiceClone: async () => {
          const error = new Error(detail);
          error.status = 422;
          error.code = "voice_sample_rejected";
          throw error;
        },
      }),
    );
    try {
      const page = newPage();
      await page.uploadVoiceSample();

      assert.equal(page.data.voiceCloneStatusLabel, detail);
      assert.equal(page.data.error, detail);
      assert.equal(page.data.voiceSampleBusy, false);
    } finally {
      stubs();
      harness.restore();
    }
  });
});
