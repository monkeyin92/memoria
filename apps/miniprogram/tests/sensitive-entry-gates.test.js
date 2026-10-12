const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { CONTROL_API_BASE_URL } = require("../config");
const contracts = require("../utils/multi-subject-contracts");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
let pageDefinition = null;

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
    return await fn();
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

test("memory page loads the owner's own recap without consulting the Runtime Profile", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    let gateCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => {
        gateCalls += 1;
        return { allowed: false, reason: "capability_missing" };
      },
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
      getConversationReview: async () => ({
        actual_heard: [],
        memory_candidates: [],
        confirmed_memories: [],
      }),
      getConversationSessions: async () => ({ items: [] }),
    });
    try {
      await page.loadDays();
      // 一对一：登录账号就是使用者本人，回顾由服务端按账号鉴权。
      assert.equal(gateCalls, 0, "回顾不再走 Runtime Profile 门禁");
      assert.equal(memoryCalls, 1);
      assert.equal(page.data.error, "");
    } finally {
      restore();
    }
  });
});

test("memory page summarize action does not consult the Runtime Profile", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let summarizeCalls = 0;
    let gateCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      hasAuthenticatedSession: () => true,
      requireRuntimeCapability: async () => {
        gateCalls += 1;
        return { allowed: false, reason: "capability_missing" };
      },
      summarizeDay: async () => {
        summarizeCalls += 1;
      },
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => ({
        actual_heard: [],
        memory_candidates: [],
        confirmed_memories: [],
      }),
      getConversationSessions: async () => ({ items: [] }),
    });
    try {
      await page.summarizeSelectedDay();
      assert.equal(gateCalls, 0);
      assert.equal(summarizeCalls, 1);
    } finally {
      restore();
    }
  });
});

function policyProfile() {
  const now = Date.now();
  return {
    signature_schema: "runtime-profile-v1",
    runtime_profile_id: "rp_gate",
    device_id: "dev_1",
    session_id: "ses_gate",
    actor_id: "person_owner",
    binding_id: "bd_1",
    binding_version: 2,
    active_subject_id: "person_owner",
    subject_revision: 3,
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    speaker_confidence: 0.96,
    service_mode: "adult_companion",
    persona_assignment_id: "pa_1",
    persona: { persona_id: "starlight", version: 4, relationship_stage: "familiar" },
    policy_bundle_version: "policy-cn-adult-v2",
    capabilities: ["chat", "guardian_summary_view"],
    obligations: ["REQUIRE_SPEAKER_CONFIRMATION"],
    policy_receipt_ids: ["rc_1"],
    session_epoch: 1,
    issued_at: new Date(now - 60_000).toISOString(),
    expires_at: new Date(now + 60_000).toISOString(),
    signature: "b".repeat(64),
  };
}

function mockPolicyDecision(decision) {
  const requests = [];
  const profile = policyProfile();
  api.clearRuntimeProfileMemory();
  binding.saveBindingManifest(
    canonicalManifest({
      binding_id: "bd_1",
      device_id: "dev_1",
      binding_version: 2,
      declared_mode: "self_use",
    }),
  );
  global.wx.request = (options) => {
    requests.push(options);
    const url = options.url || "";
    if (url.includes("/runtime-profile")) {
      options.success({ statusCode: 200, data: profile });
      return;
    }
    if (url.endsWith("/v1/policy/decisions")) {
      options.success({ statusCode: 200, data: decision });
      return;
    }
    options.fail({ errMsg: `unexpected ${url}` });
  };
  return requests;
}

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
      delete storage["memoria:miniprogram:device:binding-manifest"];
      await page.loadConsent();
      await page.grant();
      await page.revoke();
      assert.equal(consentCalls, 0);
      assert.equal(grantCalls, 0);
      assert.equal(revokeCalls, 0);
      assert.match(page.data.error, /操作保持关闭/);
      assert.equal(page.data.error.includes("无法取得 Runtime Profile"), false);
    } finally {
      restore();
    }
  });
});

test("privacy grant and revoke run only when the policy decision allows", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/privacy/index");
    const page = instantiate(definition);
    let grantCalls = 0;
    let revokeCalls = 0;
    const restore = stubApi({
      hasAuthenticatedSession: () => true,
      isAuthEpochCurrent: () => true,
      grantRawVoiceConsent: async () => {
        grantCalls += 1;
        return { policy_version: "x" };
      },
      revokeRawVoiceConsent: async () => {
        revokeCalls += 1;
      },
    });
    try {
      mockPolicyDecision({
        effect: "deny",
        reason_code: "capability_not_in_runtime_profile",
        obligations: [],
      });
      await page.grant();
      await page.revoke();
      assert.equal(grantCalls, 0);
      assert.equal(revokeCalls, 0);
      assert.equal(page.data.configOpen, false);
      assert.match(page.data.error, /未允许/);

      const requests = mockPolicyDecision({
        effect: "allow",
        reason_code: "subject_authorized",
        obligations: [],
      });
      await page.grant();
      await page.revoke();
      assert.equal(grantCalls, 1);
      assert.equal(revokeCalls, 1);
      assert.equal(page.data.configOpen, true);
      const decisions = requests.filter((item) => item.url.endsWith("/v1/policy/decisions"));
      assert.equal(decisions.length, 2);
      assert.equal(decisions[0].url, `${CONTROL_API_BASE_URL}/v1/policy/decisions`);
      assert.equal(decisions[0].data.capability, "raw_audio_retention");
      assert.equal(decisions[0].data.data_classification, "biometric");
      assert.equal(decisions[0].data.safety_state, "normal");
      assert.equal(decisions[0].data.runtime_profile_id, "rp_gate");
    } finally {
      restore();
      api.clearRuntimeProfileMemory();
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
      delete storage["memoria:miniprogram:device:binding-manifest"];
      page.setData({ minorUserId: "person_child" });
      await page.createLink();
      assert.equal(createCalls, 0, "配置动作不得依赖使用类能力门禁");
      assert.match(page.data.error, /操作保持关闭/);
      assert.equal(page.data.error.includes("无法取得 Runtime Profile"), false);

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
      assert.match(page.data.error, /操作保持关闭/);
    } finally {
      restore();
    }
  });
});

test("guardian config actions proceed only when the policy decision allows", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/guardian/index");
    const page = instantiate(definition);
    let createCalls = 0;
    let grantCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      createGuardianLink: async () => {
        createCalls += 1;
        return { binding_code: "12345678" };
      },
      grantGuardianConsent: async () => {
        grantCalls += 1;
      },
      getGuardianLinks: async () => [],
      getProfile: async () => ({ subject_category: "adult" }),
    });
    try {
      mockPolicyDecision({
        effect: "deny",
        reason_code: "guardian_summary_forbidden",
        obligations: [],
      });
      page.setData({ minorUserId: "person_child" });
      await page.createLink();
      assert.equal(createCalls, 0);
      assert.equal(page.data.configOpen, false);

      const requests = mockPolicyDecision({
        effect: "allow_with_obligations",
        reason_code: "guardian_summary_authorized",
        obligations: ["WRITE_POLICY_RECEIPT"],
      });
      await page.createLink();
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
      assert.equal(createCalls, 1);
      assert.equal(grantCalls, 1);
      assert.equal(page.data.configOpen, true);
      const decision = requests.find((item) => item.url.endsWith("/v1/policy/decisions"));
      assert.equal(decision.data.capability, "guardian_summary_view");
      assert.equal(decision.data.data_classification, "aggregate");
      assert.equal(decision.data.safety_state, "normal");
      assert.equal(decision.data.runtime_profile_id, "rp_gate");
    } finally {
      restore();
      api.clearRuntimeProfileMemory();
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

test("profile stats stay zero and no private recall request without a device binding", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/profile/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
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

test("profile entry flags: profile-listed from capabilities, action-time from a confirmed profile", async () => {
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
      // 动作时决策：已确认的有效 profile 即露出入口，由服务端动作接口决定。
      assert.equal(state.digitalSelfEntryAllowed, true);
      assert.equal(state.rawVoiceEntryAllowed, true);
      assert.equal(state.hasRuntimeProfile, true);
      // 内置人格不得被当成自定义人格 id 交给克隆接口。
      assert.equal(state.customPersonaId, "");

      api.getRuntimeProfile = async () => ({
        valid: true,
        degraded: false,
        capabilities: [contracts.Capability.VoiceCloneUse],
        persona: { persona_id: "cu_0123456789abcdef0123456789abcd", version: 1 },
        session_id: "ses_gate",
        session_epoch: 1,
        runtime_profile_id: "rp_gate_custom",
      });
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

test("profile derives a custom persona id only from a cu_ runtime persona", async () => {
  await withWx(async () => {
    binding.saveBindingManifest(
      canonicalManifest({
        binding_id: "bd_cu",
        device_id: "dev_cu",
        declared_mode: "self_use",
        binding_version: 1,
      }),
    );
    const definition = loadPage("../pages/profile/index");
    const page = instantiate(definition);
    const restore = stubApi({
      getRuntimeProfile: async () => ({
        valid: true,
        degraded: false,
        capabilities: [contracts.Capability.VoiceCloneUse],
        persona: { persona_id: "cu_0123456789abcdef0123456789abcd", version: 1 },
        session_id: "ses_cu",
        session_epoch: 1,
        runtime_profile_id: "rp_cu",
      }),
      currentIdentity: () => ({ user_id: "person_owner" }),
      getProfile: async () => ({ subject_category: "adult" }),
      isAuthEpochCurrent: () => true,
    });
    try {
      const state = await page.loadRuntimeCapabilities();
      assert.equal(state.customPersonaId, "cu_0123456789abcdef0123456789abcd");
      assert.equal(state.voiceCloneAllowed, true);

      // 内置人格不是自定义人格：不得把它的 id 当成 custom_persona_id。
      api.getRuntimeProfile = async () => ({
        valid: true,
        degraded: false,
        capabilities: [contracts.Capability.VoiceCloneUse],
        persona: { persona_id: "starlight", version: 1 },
        session_id: "ses_cu",
        session_epoch: 1,
        runtime_profile_id: "rp_cu_builtin",
      });
      const builtin = await page.loadRuntimeCapabilities();
      assert.equal(builtin.customPersonaId, "");
    } finally {
      restore();
    }
  });
});

test("profile entry clicks follow the entry flags, so action-time entries open", async () => {
  await withWx(async () => {
    const navigations = [];
    const toasts = [];
    global.wx.navigateTo = ({ url }) => navigations.push(url);
    global.wx.showToast = ({ title }) => toasts.push(title);
    const definition = loadPage("../pages/profile/index");
    const page = instantiate(definition);
    page.setData({
      runtimeCapabilities: [contracts.Capability.GuardianSummaryView],
      digitalSelfEntryAllowed: true,
      guardianEntryAllowed: true,
      rawVoiceEntryAllowed: true,
    });

    assert.equal(page._allowSensitiveEntry("数字分身", contracts.Capability.DigitalSelfPreview), true);
    page.openPrivacy();
    assert.deepEqual(navigations, ["/pages/privacy/index"]);
    assert.deepEqual(toasts, []);

    page.setData({ rawVoiceEntryAllowed: false, profileUnavailableReason: "profile 已过期" });
    page.openPrivacy();
    assert.deepEqual(navigations, ["/pages/privacy/index"]);
    assert.equal(toasts.length, 1);
    assert.match(toasts[0], /原始语音授权暂未开放：profile 已过期/);
  });
});

test("privacy voice revoke fails closed when logged out, the gate is closed, or delete fails", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/privacy/index");
    const page = instantiate(definition);
    const toasts = [];
    global.wx.showToast = ({ title }) => toasts.push(title);
    let listed = 0;
    let deleted = 0;
    let consentRevoked = 0;
    const restore = stubApi({
      hasAuthenticatedSession: () => false,
      listVoiceProfiles: async () => {
        listed += 1;
        return { items: [], consent: null };
      },
      revokeVoiceProfile: async () => {
        deleted += 1;
      },
      revokeVoiceCloneConsent: async () => {
        consentRevoked += 1;
      },
    });
    try {
      await page.revokeVoiceTip();
      assert.equal(listed, 0);
      assert.equal(deleted, 0);
      assert.equal(consentRevoked, 0);
      assert.match(page.data.error, /请先登录/);
      assert.equal(page.data.voiceOn, true);

      restore();
      const signedIn = stubApi({
        hasAuthenticatedSession: () => true,
        isAuthEpochCurrent: () => true,
        currentAuthEpoch: () => 0,
        listVoiceProfiles: async () => {
          listed += 1;
          return { items: [], consent: null };
        },
        revokeVoiceProfile: async () => {
          deleted += 1;
        },
        revokeVoiceCloneConsent: async () => {
          consentRevoked += 1;
        },
      });
      mockPolicyDecision({
        effect: "deny",
        reason_code: "capability_not_in_runtime_profile",
        obligations: [],
      });
      await page.revokeVoiceTip();
      assert.equal(listed, 0);
      assert.equal(page.data.voiceConfigOpen, false);
      assert.match(page.data.error, /未允许/);

      mockPolicyDecision({
        effect: "allow",
        reason_code: "subject_authorized",
        obligations: [],
      });
      const failing = stubApi({
        hasAuthenticatedSession: () => true,
        isAuthEpochCurrent: () => true,
        currentAuthEpoch: () => 0,
        listVoiceProfiles: async () => {
          listed += 1;
          return {
            consent: { policy_version: "voice-clone-v1", revoked_at: null },
            items: [{ profile_id: "vp_1", status: "active", enrollment: { stage: "done", state: "ready" } }],
          };
        },
        revokeVoiceProfile: async () => {
          deleted += 1;
          const error = new Error("声音资产删除未完成，请稍后重试");
          error.status = 503;
          throw error;
        },
        revokeVoiceCloneConsent: async () => {
          consentRevoked += 1;
        },
      });
      await page.revokeVoiceTip();
      assert.equal(listed, 1);
      assert.equal(deleted, 1);
      assert.equal(consentRevoked, 0);
      assert.match(page.data.error, /删除未完成/);
      assert.equal(page.data.voiceOn, true);
      assert.deepEqual(toasts, []);
      failing();
      signedIn();
    } finally {
      restore();
    }
  });
});

test("privacy voice revoke deletes retained profiles and withdraws voice consent", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/privacy/index");
    const page = instantiate(definition);
    const toasts = [];
    const modals = [];
    global.wx.showToast = ({ title }) => toasts.push(title);
    global.wx.showModal = (options) => {
      modals.push(options.title);
      options.success?.({ confirm: true });
    };
    const deleted = [];
    let consentRevoked = 0;
    const restore = stubApi({
      hasAuthenticatedSession: () => true,
      isAuthEpochCurrent: () => true,
      currentAuthEpoch: () => 0,
      listVoiceProfiles: async () => ({
        consent: { policy_version: "voice-clone-v1", revoked_at: null },
        items: [
          { profile_id: "vp_live", status: "enrolling", enrollment: { stage: "queued", state: "reconciliation_required", operation_state: "reconciliation_required" } },
          { profile_id: "vp_done", status: "revoked", deletion_status: "completed", enrollment: { stage: "failed", state: "revoked" } },
        ],
      }),
      revokeVoiceProfile: async (profileId) => {
        deleted.push(profileId);
        return { profile_id: profileId, deletion_status: "completed" };
      },
      revokeVoiceCloneConsent: async () => {
        consentRevoked += 1;
        return { revoked_at: "2026-10-12T00:00:00Z" };
      },
    });
    try {
      const requests = mockPolicyDecision({
        effect: "allow",
        reason_code: "subject_authorized",
        obligations: [],
      });
      await page.revokeVoiceTip();
      assert.deepEqual(deleted, ["vp_live"]);
      assert.equal(consentRevoked, 1);
      assert.equal(page.data.voiceOn, false);
      assert.equal(page.data.error, "");
      assert.deepEqual(toasts, ["声音样本已撤回"]);
      assert.deepEqual(modals, ["撤回声音样本授权"]);
      const decisions = requests.filter((item) => String(item.url).endsWith("/v1/policy/decisions"));
      assert.ok(decisions.length >= 1);
      assert.equal(decisions[0].data.capability, "voice_clone_use");
      assert.equal(decisions[0].data.data_classification, "biometric");

      toasts.length = 0;
      modals.length = 0;
      await page.revokeMemoryTip();
      assert.deepEqual(modals, ["撤回记忆档案授权"]);
      assert.equal(consentRevoked, 1);
      assert.deepEqual(toasts, []);
    } finally {
      restore();
    }
  });
});
