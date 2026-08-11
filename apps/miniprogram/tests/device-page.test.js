const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
const activeSubjectCalls = [];
let profilePayload = null;
let switchProfilePayload = null;
let resolutionPayload = null;
let nextRequestResult = null;
let deferProfileResponses = false;
const deferredResponses = [];

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
  stopPullDownRefresh() {},
  request(options) {
    const pathname = options.url.replace("https://aigcnice.com:8443/memoria-api", "");
    if (pathname.startsWith("/v1/devices/dev_1/runtime-profile")) {
      if (deferProfileResponses) {
        deferredResponses.push({ options, kind: "refresh" });
        return;
      }
      options.success(
        profilePayload === null
          ? nextRequestResult
          : { statusCode: 200, data: profilePayload },
      );
      return;
    }
    if (pathname === "/v1/sessions/resolve-subject") {
      options.success({ statusCode: 200, data: resolutionPayload });
      return;
    }
    const activeMatch = pathname.match(/^\/v1\/sessions\/([^/]+)\/active-subject$/);
    if (activeMatch) {
      activeSubjectCalls.push({
        sessionId: decodeURIComponent(activeMatch[1]),
        ...options.data,
      });
      if (deferProfileResponses) {
        deferredResponses.push({ options, kind: "switch" });
        return;
      }
      options.success({ statusCode: 200, data: switchProfilePayload || profilePayload });
      return;
    }
    options.success(nextRequestResult);
  },
};

global.getApp = () => ({
  globalData: {
    identity: { user_id: "person_owner", display_name: "主人", account_type: "registered" },
    accessToken: "test-token",
    accessTokenExpiresAt: Date.now() + 3600_000,
    authEpoch: 0,
  },
  subscribeAuthCleared: () => () => {},
});

api.hasAuthenticatedSession = () => true;
api.currentIdentity = () => ({ user_id: "person_owner", display_name: "主人" });
api.currentAccessToken = () => "test-token";
api.currentAuthEpoch = () => 0;
api.isAuthEpochCurrent = () => true;

function setPath(data, key, value) {
  const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
  let cursor = data;
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
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      setPath(instance.data, key, value);
    }
  };
  return instance;
}

let pageDefinition;
global.Page = (definition) => {
  pageDefinition = definition;
};
const pagePath = require.resolve("../pages/device/index");
delete require.cache[pagePath];
require(pagePath);

function familyManifest() {
  return canonicalManifest({
    binding_id: "bd_1",
    device_id: "dev_1",
    declared_mode: "family_shared",
    binding_version: 2,
    device_admin_ids: ["person_owner"],
    roles: [{ person_id: "person_owner", role: "device_admin", permissions: [] }],
  });
}

function familyResolution() {
  return {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.71 },
      { person_id: "person_parent", display_name: "妈妈", confidence: 0.23 },
    ],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["voice_question", "app_confirm"],
    runtime_profile_id: "rp_tmp_1",
  };
}

function wireProfile(overrides = {}) {
  const now = Date.now();
  return {
    signature_schema: "runtime-profile-v1",
    runtime_profile_id: "rp_1",
    device_id: "dev_1",
    session_id: "ses_1",
    actor_id: "person_owner",
    binding_id: "bd_1",
    binding_version: 2,
    active_subject_id: "person_owner",
    subject_revision: 1,
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    speaker_confidence: 0.9,
    service_mode: "adult_companion",
    persona_assignment_id: "pa_1",
    persona: { persona_id: "starlight", version: 4, relationship_stage: "familiar" },
    policy_bundle_version: "policy-cn-adult-v2",
    capabilities: ["chat"],
    obligations: [],
    policy_receipt_ids: [],
    session_epoch: 1,
    issued_at: new Date(now - 60_000).toISOString(),
    expires_at: new Date(now + 60_000).toISOString(),
    signature: "a".repeat(64),
    ...overrides,
  };
}

function wireUnknownSafeProfile(overrides = {}) {
  return wireProfile({
    session_id: "ses_safe",
    active_subject_id: null,
    subject_category: "unknown",
    age_band: "unknown",
    speaker_state: "unconfirmed",
    speaker_confidence: null,
    service_mode: "unknown_safe",
    capabilities: ["chat"],
    obligations: ["DO_NOT_PERSIST"],
    ...overrides,
  });
}

test("device page registers in app.json", () => {
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  assert.ok(appConfig.pages.includes("pages/device/index"));
});

test("no binding manifest shows the empty state", async () => {
  binding.clearBindingManifest();
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.loading, false);
  assert.equal(page.data.hasBinding, false);
});

test("family mode loads candidates and switches with an epoch-bumped profile", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_family",
    session_id: "ses_family",
    session_epoch: 1,
    capabilities: ["chat", "english_practice"],
  });
  resolutionPayload = familyResolution();

  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.hasBinding, true);
  assert.equal(page.data.bindingModeLabel, "家庭共同使用");
  assert.deepEqual(page.data.bindingRoles, ["设备管理员"]);
  assert.equal(page.data.candidates.length, 2);
  assert.equal(page.data.canConfirmWithApp, true);
  assert.ok(page.data.degradation, "unconfirmed 说话人必须降级");
  assert.ok(page.data.degradation.reasons.some((reason) => reason.includes("尚未确认")));
  assert.deepEqual(page.data.sensitiveEntries, [], "unconfirmed 不允许任何敏感入口");

  page.selectCandidate({ currentTarget: { dataset: { personId: "person_parent" } } });
  assert.equal(page.data.selectedCandidateId, "person_parent");

  switchProfilePayload = wireProfile({
    runtime_profile_id: "rp_switched",
    session_id: "ses_family",
    session_epoch: 2,
    active_subject_id: "person_parent",
    capabilities: ["chat", "memory_recall_private"],
    signature: "b".repeat(64),
  });
  resolutionPayload = {
    resolution: "confirmed",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.71 },
      { person_id: "person_parent", display_name: "妈妈", confidence: 0.23 },
    ],
    temporary_service_mode: "adult_companion",
    allowed_confirmation_methods: ["voice_question", "app_confirm"],
    runtime_profile_id: "rp_switched",
  };

  await page.confirmSubject();
  assert.deepEqual(activeSubjectCalls[0], {
    sessionId: "ses_family",
    person_id: "person_parent",
    confirmation_method: "app_confirm",
  });
  assert.equal(page.data.profile.service_mode, "adult_companion");
  assert.equal(page.data.profile.session_epoch, 2);
  assert.equal(page.data.currentUserLabel, "妈妈");
  assert.equal(page.data.degradation, null);
  assert.ok(page.data.sensitiveEntries.some((entry) => entry.key === "memory_recall"));
  assert.ok(!page.data.sensitiveEntries.some((entry) => entry.key === "digital_self"));
});

test("switch result without an epoch bump is rejected and not applied", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_epoch1",
    session_id: "ses_epoch",
    session_epoch: 1,
    capabilities: ["chat", "english_practice"],
  });
  resolutionPayload = familyResolution();
  const page = instantiate(pageDefinition);
  await page.onShow();

  page.selectCandidate({ currentTarget: { dataset: { personId: "person_child" } } });
  switchProfilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_epoch1_stale",
    session_id: "ses_epoch",
    session_epoch: 1,
    capabilities: ["chat", "english_practice"],
    signature: "c".repeat(64),
  });
  await page.confirmSubject();
  assert.ok(
    page.data.error.includes("已过期") ||
      page.data.error.includes("未提升") ||
      page.data.error.includes("校验失败"),
  );
  assert.equal(page.data.profile.session_epoch, 1, "同 epoch 的旧 profile 不得覆盖当前状态");
  assert.equal(page.data.profile.runtime_profile_id, "rp_epoch1");
});

test("late refresh response cannot overwrite the switched profile", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_base",
    session_id: "ses_race",
    session_epoch: 1,
    capabilities: ["chat", "english_practice"],
  });
  resolutionPayload = familyResolution();
  const page = instantiate(pageDefinition);
  await page.onShow();
  page.selectCandidate({ currentTarget: { dataset: { personId: "person_parent" } } });

  // 拉刷新（refresh）与切换（switch）并发：先发 refresh，后发 switch。
  deferProfileResponses = true;
  const refreshFlow = page.loadDevice();
  const switchFlow = page.confirmSubject();
  await Promise.resolve();
  assert.equal(deferredResponses.length, 2);
  assert.equal(deferredResponses[0].kind, "refresh");
  assert.equal(deferredResponses[1].kind, "switch");

  // 切换先返回（epoch 2）。
  deferredResponses[1].options.success({
    statusCode: 200,
    data: wireProfile({
      runtime_profile_id: "rp_race_new",
      session_id: "ses_race",
      session_epoch: 2,
      active_subject_id: "person_parent",
      capabilities: ["chat", "memory_recall_private"],
      signature: "d".repeat(64),
    }),
  });
  await switchFlow;
  assert.equal(page.data.profile.session_epoch, 2);
  assert.equal(page.data.profile.runtime_profile_id, "rp_race_new");

  // refresh 后到（epoch 1 旧内容）：页面必须丢弃，不能回退。
  deferredResponses[0].options.success({
    statusCode: 200,
    data: wireUnknownSafeProfile({
      runtime_profile_id: "rp_base",
      session_id: "ses_race",
      session_epoch: 1,
      capabilities: ["chat", "english_practice"],
    }),
  });
  await refreshFlow;
  assert.equal(page.data.profile.session_epoch, 2, "晚到的 refresh 响应不得覆盖切换结果");
  assert.equal(page.data.profile.runtime_profile_id, "rp_race_new");
  assert.equal(page.data.currentUserLabel, "妈妈");
  deferProfileResponses = false;
  deferredResponses.length = 0;
});

test("unknown_safe shows explainable degradation and hides sensitive entries", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_safe",
    session_id: "ses_safe",
    session_epoch: 1,
  });
  resolutionPayload = {
    resolution: "unknown",
    candidate_subjects: [],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["voice_question"],
    runtime_profile_id: "rp_safe",
  };

  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.ok(page.data.degradation);
  assert.ok(page.data.degradation.reasons.some((reason) => reason.includes("安全模式")));
  assert.deepEqual(page.data.sensitiveEntries, []);
  assert.equal(page.data.canConfirmWithApp, false);
});

test("capability hiding removes un-granted sensitive entries", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_3",
    session_id: "ses_3",
    session_epoch: 1,
    capabilities: ["chat", "raw_audio_retention"],
  });
  const page = instantiate(pageDefinition);
  await page.onShow();
  const keys = page.data.sensitiveEntries.map((entry) => entry.key);
  assert.ok(keys.includes("raw_voice_consent"));
  assert.ok(!keys.includes("digital_self"));
  assert.ok(!keys.includes("speaker_enrollment"));
  assert.ok(!keys.includes("guardian_summary"));
  assert.equal(page.data.degradation, null);
});

test("confirming without a usable session is rejected with an explainable message", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_expired",
    session_id: "ses_expired",
    session_epoch: 1,
    expires_at: new Date(Date.now() - 60_000).toISOString(),
  });
  resolutionPayload = familyResolution();
  const page = instantiate(pageDefinition);
  await page.onShow();
  page.selectCandidate({ currentTarget: { dataset: { personId: "person_child" } } });
  await page.confirmSubject();
  assert.ok(page.data.error.includes("先开始一次语音对话"));
  assert.equal(activeSubjectCalls.length, 0);
});

test("confirming is blocked when the server does not allow app confirmation", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_5",
    session_id: "ses_5",
    session_epoch: 1,
  });
  resolutionPayload = {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.71 },
    ],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["voice_question"],
    runtime_profile_id: "rp_5",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.canConfirmWithApp, false);
  page.selectCandidate({ currentTarget: { dataset: { personId: "person_child" } } });
  await page.confirmSubject();
  assert.ok(page.data.error.includes("语音确认"));
  assert.equal(activeSubjectCalls.length, 0);
});

test("low confidence candidates are rendered with the low-confidence hint", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_6",
    session_id: "ses_6",
    session_epoch: 1,
  });
  resolutionPayload = {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_guest", display_name: "新朋友", confidence: 0.31 },
    ],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["app_confirm"],
    runtime_profile_id: "rp_6",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.candidates[0].confidence, 0.31);
  assert.ok(page.data.degradation.reasons.some((reason) => reason.includes("尚未确认")));
});

test("device load failures surface an error without breaking the page", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = null;
  nextRequestResult = { statusCode: 500, data: { detail: "boom" } };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.hasBinding, true);
  assert.ok(page.data.error);
});
