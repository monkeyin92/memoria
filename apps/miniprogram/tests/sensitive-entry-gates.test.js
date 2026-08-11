const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");
const binding = require("../utils/device-binding");
const contracts = require("../utils/multi-subject-contracts");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
let pageDefinition = null;

function withWx(fn) {
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
    globalData: {
      identity: { user_id: "person_owner", display_name: "主人" },
      accessToken: "t",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
    return fn();
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

test("memory page denies private recall when memory_recall_private is not granted", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => ({
        allowed: false,
        reason: "capability_missing",
      }),
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
    });
    try {
      await page.loadDays();
      assert.equal(memoryCalls, 0, "未授权时不得请求私人回顾数据");
      assert.equal(page.data.days.length, 0);
      assert.ok(page.data.error.includes("尚未开放"));
    } finally {
      restore();
    }
  });
});

test("memory page summarize action is refused without the capability", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let summarizeCalls = 0;
    const restore = stubApi({
      requireLogin: async () => true,
      requireRuntimeCapability: async () => ({ allowed: false, reason: "no_binding" }),
      summarizeDay: async () => {
        summarizeCalls += 1;
      },
    });
    try {
      await page.summarizeSelectedDay();
      assert.equal(summarizeCalls, 0);
      assert.ok(page.data.error.includes("绑定设备"));
    } finally {
      restore();
    }
  });
});

test("privacy load/grant/revoke are each gated by raw_audio_retention", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/privacy/index");
    const page = instantiate(definition);
    let consentCalls = 0;
    let grantCalls = 0;
    let revokeCalls = 0;
    const restore = stubApi({
      hasAuthenticatedSession: () => true,
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => ({ allowed: false, reason: "invalid_profile" }),
      getRawVoiceConsent: async () => {
        consentCalls += 1;
        return { consent: null };
      },
      grantRawVoiceConsent: async () => {
        grantCalls += 1;
        return { policy_version: "x" };
      },
      revokeRawVoiceConsent: async () => {
        revokeCalls += 1;
      },
    });
    try {
      await page.loadConsent();
      await page.grant();
      await page.revoke();
      assert.equal(consentCalls, 0);
      assert.equal(grantCalls, 0);
      assert.equal(revokeCalls, 0);
      assert.ok(page.data.error.includes("尚未接入"), "配置动作必须走 config seam fail-closed");
    } finally {
      restore();
    }
  });
});

test("guardian config actions use the config seam, not the usage capability", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/guardian/index");
    const page = instantiate(definition);
    let createCalls = 0;
    let grantCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      requireRuntimeCapability: async () => ({ allowed: false, reason: "no_binding" }),
      createGuardianLink: async () => {
        createCalls += 1;
        return { binding_code: "12345678" };
      },
      grantGuardianConsent: async () => {
        grantCalls += 1;
      },
    });
    try {
      page.setData({ minorUserId: "person_child" });
      await page.createLink();
      assert.equal(createCalls, 0, "配置动作不得依赖使用类能力门禁");
      assert.ok(page.data.error.includes("尚未接入"));

      await page.toggleConsent({
        currentTarget: {
          dataset: {
            linkId: "link_1",
            kind: "weekly_report",
            policyVersion: "guardian-weekly-v1",
            consentId: "",
            active: false,
          },
        },
      });
      assert.equal(grantCalls, 0);
      assert.ok(page.data.error.includes("尚未接入"));
    } finally {
      restore();
    }
  });
});

test("digital-self loadOverview is gated by digital_self_preview", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/digital-self/index");
    const page = instantiate(definition);
    let overviewCalls = 0;
    const restore = stubApi({
      hasAuthenticatedSession: () => true,
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => ({ allowed: false, reason: "unavailable" }),
      getGrowthOverview: async () => {
        overviewCalls += 1;
        return { dimensions: [] };
      },
      getPersonaStatus: async () => ({}),
      getDigitalSelfVersions: async () => ({ items: [] }),
    });
    try {
      await page.loadOverview();
      assert.equal(overviewCalls, 0);
      assert.ok(page.data.error.includes("暂时无法提供"));
    } finally {
      restore();
    }
  });
});

test("guardian refresh is gated by guardian_summary_view", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/guardian/index");
    const page = instantiate(definition);
    let linksCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      requireRuntimeCapability: async () => ({ allowed: false, reason: "no_binding" }),
      getGuardianLinks: async () => {
        linksCalls += 1;
        return [];
      },
      getProfile: async () => ({}),
    });
    try {
      await page.refresh();
      assert.equal(linksCalls, 0, "未授权时不得读取监护数据");
      assert.ok(page.data.error.includes("绑定设备"));
    } finally {
      restore();
    }
  });
});

test("profile stats stay zero and no private recall request without the capability", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/profile/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      requireRuntimeCapability: async () => ({ allowed: false, reason: "no_binding" }),
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
    });
    try {
      await page.loadStats();
      assert.equal(memoryCalls, 0);
      assert.deepEqual(page.data.stats, { totalDays: 0, moments: 0, streak: 0 });
    } finally {
      restore();
    }
  });
});

test("profile sensitive entry flags come only from the runtime profile capabilities", async () => {
  await withWx(async () => {
    binding.saveBindingManifest(
      canonicalManifest({
        binding_id: "bd_gate",
        device_id: "dev_gate",
        declared_mode: "self_use",
        binding_version: 1,
      }),
    );
    const definition = loadPage("../pages/profile/index");
    const page = instantiate(definition);
    const now = Date.now();
    const restore = stubApi({
      getRuntimeProfile: async () => ({
        valid: true,
        degraded: false,
        capabilities: [
          contracts.Capability.VoiceProfileCreate,
          contracts.Capability.GuardianSummaryView,
        ],
        session_id: "ses_gate",
        session_epoch: 1,
        runtime_profile_id: "rp_gate",
      }),
      currentIdentity: () => ({ user_id: "person_owner" }),
      getProfile: async () => ({ subject_category: "adult" }),
      isAuthEpochCurrent: () => true,
      getMemoryDays: async () => ({ items: [] }),
    });
    try {
      const state = await page.loadRuntimeCapabilities();
      assert.equal(state.speakerEntryAllowed, true);
      assert.equal(state.guardianEntryAllowed, true);
      assert.equal(state.digitalSelfEntryAllowed, false);
      assert.equal(state.rawVoiceEntryAllowed, false);
      assert.equal(state.hasRuntimeProfile, true);

      // 非法 profile：全部入口关闭且给出可解释原因。
      api.getRuntimeProfile = async () => ({ valid: false, capabilities: [] });
      const denied = await page.loadRuntimeCapabilities();
      assert.equal(denied.speakerEntryAllowed, false);
      assert.equal(denied.digitalSelfEntryAllowed, false);
      assert.equal(denied.guardianEntryAllowed, false);
      assert.equal(denied.rawVoiceEntryAllowed, false);
      assert.ok(denied.profileUnavailableReason.length > 0);
      assert.equal(now > 0, true);
    } finally {
      restore();
    }
  });
});
