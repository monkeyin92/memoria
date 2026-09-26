const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");
const { readSubjectLabel } = require("../utils/subject-label");

const storage = {};
const activeSubjectCalls = [];
let profilePayload = null;
let switchProfilePayload = null;
let resolutionPayload = null;
let nextRequestResult = null;
let deferProfileResponses = false;
const deferredResponses = [];
let settingsPayload = null;
let diagnosticsPayload = null;
let settingsPatchResult = null;
const settingsPatchCalls = [];
const personaCalls = [];
let personaFailure = false;
let personaPayload = () => ({
  assignments: [],
  binding_default: "starlight:v1",
});
let personasPayload = () => ({ custom_personas: [], builtin: [] });
let accountProfilePayload = null;
const unbindCalls = [];

global.wx = {
  getStorageSync: (key) => storage[key],
  setStorageSync: (key, value) => {
    storage[key] = value;
  },
  removeStorageSync: (key) => {
    delete storage[key];
  },
  showToast() {},
  showModal() {},
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
    if (pathname === "/v1/devices/dev_1/settings") {
      if (options.method === "PATCH") {
        settingsPatchCalls.push(options.data);
        options.success(
          settingsPatchResult === null
            ? { statusCode: 409, data: { detail: { code: "settings_version_conflict" } } }
            : { statusCode: 200, data: settingsPatchResult },
        );
      } else if (settingsPayload !== null) {
        options.success({ statusCode: 200, data: settingsPayload });
      } else {
        options.success(nextRequestResult);
      }
      return;
    }
    if (pathname === "/v1/devices/dev_1/diagnostics/latest") {
      options.success(
        diagnosticsPayload === null
          ? nextRequestResult
          : { statusCode: 200, data: diagnosticsPayload },
      );
      return;
    }
    if (pathname === "/v1/devices/wake-word-catalog") {
      options.success({
        statusCode: 200,
        data: {
          items: [
            {
              id: "mo_li",
              display: "茉莉",
              pinyin: "mo li",
              syllables: 2,
              device_ready: true,
              note: "当前板卡默认唤醒词。",
            },
            {
              id: "mei_mo_li_ya",
              display: "梅莫里亚",
              pinyin: "mei mo li ya",
              syllables: 4,
              device_ready: true,
              note: "白名单唤醒词。",
            },
          ],
        },
      });
      return;
    }
    if (pathname === "/v1/devices/wake-word/validate") {
      options.success({
        statusCode: 200,
        data: {
          wake_word_id: "custom",
          wake_word_display: options.data.wake_word_display,
          wake_word_pinyin: options.data.wake_word_pinyin,
          syllables: 2,
          source: "custom",
          warnings: ["两音节唤醒词误唤醒风险较高，建议在安静环境下单独验收。"],
        },
      });
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
    const personaMatch = pathname.match(
      /^\/v1\/devices\/([^/]+)\/persona-assignments(?:\/([^/]+))?$/,
    );
    if (personaMatch) {
      personaCalls.push({
        method: options.method || "GET",
        deviceId: decodeURIComponent(personaMatch[1]),
        personId: personaMatch[2] ? decodeURIComponent(personaMatch[2]) : null,
        data: options.data || null,
      });
      if (personaFailure) {
        options.fail({ errMsg: "request:fail" });
        return;
      }
      if ((options.method || "GET") === "GET") {
        options.success({ statusCode: 200, data: personaPayload() });
        return;
      }
      options.success({
        statusCode: 200,
        data:
          options.method === "PUT"
            ? { assignment_id: `${options.data.persona_selection}:v1`, persona_id: options.data.persona_selection }
            : { removed: true, effective: "starlight:v1" },
      });
      return;
    }
    if (pathname === "/v1/personas") {
      options.success({ statusCode: 200, data: personasPayload() });
      return;
    }
    if (pathname === "/v1/memory/profile/person_owner" && accountProfilePayload) {
      options.success({ statusCode: 200, data: accountProfilePayload });
      return;
    }
    if (pathname === "/v1/devices/dev_1/binding/unbind") {
      unbindCalls.push({ method: options.method, data: options.data });
      options.success({ statusCode: 200, data: { binding_id: "bd_1", status: "revoked" } });
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
api.syncDeviceBindings = async () => {
  const local = binding.readBindingManifest();
  return local
    ? { status: "ready", binding: local, bindings: [local] }
    : { status: "empty", binding: null, bindings: [] };
};

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

test("device page can set the bind-time subject remark used on home", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_alias",
    session_id: "ses_alias",
    session_epoch: 1,
  });
  resolutionPayload = familyResolution();

  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.subjectAliasLabel, "");
  page.onSubjectAliasInput({ detail: { value: "老爸" } });
  page.saveSubjectAlias();
  assert.equal(page.data.subjectAliasLabel, "老爸");
  assert.equal(readSubjectLabel(familyManifest()), "老爸");
  binding.clearBindingManifest();
  assert.equal(readSubjectLabel(familyManifest()), "");
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
  // 数字分身是动作时决策：确认后露出入口，是否允许由服务端动作接口决定。
  assert.ok(page.data.sensitiveEntries.some((entry) => entry.key === "digital_self"));
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
  await new Promise((resolve) => setImmediate(resolve));
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
  assert.ok(keys.includes("digital_self"), "动作时决策的入口在已确认 profile 下露出");
  assert.ok(!keys.includes("speaker_enrollment"));
  assert.ok(!keys.includes("guardian_summary"), "profile 未列出的能力仍然隐藏");
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
  assert.ok(page.data.error.includes("先在机器人上开始对话"));
  assert.equal(activeSubjectCalls.length, 0);
});

function parentChildManifest() {
  return canonicalManifest({
    binding_id: "bd_1",
    device_id: "dev_1",
    declared_mode: "parent_for_child",
    binding_version: 2,
    account_owner_id: "person_owner",
    device_admin_ids: ["person_owner"],
    primary_subject_ids: ["person_child", "person_owner"],
    guardian_ids: ["person_owner"],
    roles: [
      { person_id: "person_owner", role: "account_owner", permissions: [] },
      { person_id: "person_owner", role: "guardian", permissions: [] },
      { person_id: "person_child", role: "primary_subject", permissions: [] },
    ],
  });
}

test("ordinary parent_for_child can resolve and confirm with app_confirm", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_child",
    session_id: "ses_child",
    session_epoch: 3,
    active_subject_id: "person_owner",
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    service_mode: "adult_companion",
    capabilities: ["chat"],
    signature: "e".repeat(64),
  });
  resolutionPayload = {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.8 },
    ],
    temporary_service_mode: "student_minor",
    allowed_confirmation_methods: ["app_confirm"],
    runtime_profile_id: "rp_child",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.canConfirmWithApp, true);
  assert.equal(page.data.candidates.length, 1);
  assert.equal(page.data.ageRows.length, 1);
  assert.equal(page.data.ageRows[0].person_id, "person_child");
  assert.deepEqual(
    page.data.ageRows[0].options.map((option) => option.value),
    ["unknown", "under_14", "14_17"],
  );
  assert.equal(page.data.ageRows[0].evidenceLabel, "未核验");
  const template = fs.readFileSync(path.join(root, "pages/device/index.wxml"), "utf8");
  assert.match(template, /申报不是核验/);
  assert.doesNotMatch(template, /已核验|adult/);

  page.selectCandidate({ currentTarget: { dataset: { personId: "person_child" } } });
  switchProfilePayload = wireProfile({
    runtime_profile_id: "rp_child_next",
    session_id: "ses_child",
    session_epoch: 4,
    active_subject_id: "person_child",
    subject_category: "minor",
    age_band: "under_14",
    speaker_state: "confirmed",
    service_mode: "student_minor",
    capabilities: ["chat"],
    signature: "f".repeat(64),
  });
  await page.confirmSubject();
  assert.deepEqual(activeSubjectCalls[0], {
    sessionId: "ses_child",
    person_id: "person_child",
    confirmation_method: "app_confirm",
  });
  assert.equal(page.data.profile.session_epoch, 4);
  assert.equal(page.data.currentUserLabel, "小乐");
});

test("parent_for_child without app_confirm cannot switch", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_voice_only",
    session_id: "ses_voice",
    session_epoch: 1,
    service_mode: "adult_companion",
    speaker_state: "confirmed",
    capabilities: ["chat"],
  });
  resolutionPayload = {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.8 },
    ],
    temporary_service_mode: "student_minor",
    allowed_confirmation_methods: ["voice_question"],
    runtime_profile_id: "rp_voice_only",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.candidates.length, 0);
  assert.equal(page.data.canConfirmWithApp, false);
  page.setData({
    resolution: resolutionPayload,
    selectedCandidateId: "person_child",
  });
  await page.confirmSubject();
  assert.ok(page.data.error.includes("暂不支持在应用里切换使用人"));
  assert.equal(activeSubjectCalls.length, 0);
});

test("unconfirmed parent_for_child subject stays unlabeled", async () => {
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_unconfirmed",
    session_id: "ses_unconfirmed",
    session_epoch: 1,
    active_subject_id: null,
    subject_category: "unknown",
    age_band: "unknown",
    speaker_state: "unconfirmed",
    service_mode: "unknown_safe",
    capabilities: ["chat"],
    obligations: ["DO_NOT_PERSIST"],
  });
  resolutionPayload = {
    resolution: "unknown",
    candidate_subjects: [],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["app_confirm"],
    runtime_profile_id: "rp_unconfirmed",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.currentUserLabel, "未确认");
  assert.equal(page.data.currentUserLabelConfirmed, false);
  assert.equal(page.data.candidates.length, 0);
});

test("stale confirm response does not refill after a newer load", async () => {
  activeSubjectCalls.length = 0;
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_stale_base",
    session_id: "ses_stale",
    session_epoch: 2,
    service_mode: "adult_companion",
    speaker_state: "confirmed",
    capabilities: ["chat"],
    signature: "a".repeat(64),
  });
  resolutionPayload = {
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.8 },
    ],
    temporary_service_mode: "student_minor",
    allowed_confirmation_methods: ["app_confirm"],
    runtime_profile_id: "rp_stale_base",
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  page.selectCandidate({ currentTarget: { dataset: { personId: "person_child" } } });
  deferProfileResponses = true;
  const confirmFlow = page.confirmSubject();
  await Promise.resolve();
  assert.equal(deferredResponses.length, 1);
  profilePayload = wireProfile({
    runtime_profile_id: "rp_fresh",
    session_id: "ses_stale",
    session_epoch: 5,
    active_subject_id: "person_owner",
    service_mode: "adult_companion",
    speaker_state: "confirmed",
    capabilities: ["chat"],
    signature: "b".repeat(64),
  });
  deferProfileResponses = false;
  await page.loadDevice();
  assert.equal(page.data.profile.runtime_profile_id, "rp_fresh");
  deferredResponses[0].options.success({
    statusCode: 200,
    data: wireProfile({
      runtime_profile_id: "rp_late",
      session_id: "ses_stale",
      session_epoch: 3,
      active_subject_id: "person_child",
      subject_category: "minor",
      age_band: "under_14",
      service_mode: "student_minor",
      speaker_state: "confirmed",
      capabilities: ["chat"],
      signature: "c".repeat(64),
    }),
  });
  await confirmFlow;
  assert.equal(page.data.profile.runtime_profile_id, "rp_fresh");
  assert.equal(page.data.profile.session_epoch, 5);
  assert.equal(page.data.error, "");
  deferredResponses.length = 0;
});

test("age declaration offers only three bands and never claims verification", async () => {
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_age",
    session_id: "ses_age",
    session_epoch: 1,
    service_mode: "adult_companion",
    speaker_state: "confirmed",
    capabilities: ["chat"],
  });
  resolutionPayload = {
    resolution: "confirmed",
    candidate_subjects: [],
    temporary_service_mode: "adult_companion",
    allowed_confirmation_methods: [],
    runtime_profile_id: "rp_age",
  };
  const calls = [];
  const originalRequest = global.wx.request;
  global.wx.request = (options) => {
    const pathname = options.url.replace("https://aigcnice.com:8443/memoria-api", "");
    if (pathname === "/v1/persons/person_child/age-evidence") {
      // GET reads the recorded band (P0-04 D4); PATCH declares a new one.
      const isRead = (options.method || "GET") === "GET";
      if (!isRead) calls.push(options.data);
      options.success({
        statusCode: 200,
        data: {
          person_id: "person_child",
          age_band: isRead ? "under_14" : options.data.age_band,
          age_evidence_status: "unverified",
          subject_category: "minor",
        },
      });
      return;
    }
    return originalRequest(options);
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(
    page.data.ageRows[0].options.map((option) => option.label),
    ["年龄未知", "申报 14 岁以下", "申报 14 至 17 岁"],
  );
  // The row shows the band the server has on record, not a fixed placeholder.
  assert.equal(page.data.ageRows[0].selected, "under_14");
  assert.equal(page.data.ageRows[0].declaredLabel, "申报 14 岁以下");
  page.selectAgeBand({
    currentTarget: { dataset: { personId: "person_child", ageBand: "adult" } },
  });
  assert.equal(page.data.ageRows[0].selected, "under_14");
  page.selectAgeBand({
    currentTarget: { dataset: { personId: "person_child", ageBand: "14_17" } },
  });
  await page.declareAge({ currentTarget: { dataset: { personId: "person_child" } } });
  assert.deepEqual(calls, [{ age_band: "14_17" }]);
  assert.equal(page.data.ageRows[0].declaredLabel, "申报 14 至 17 岁");
  assert.equal(page.data.ageRows[0].evidenceLabel, "未核验");
  assert.equal(page.data.profile.runtime_profile_id, "rp_age");
  global.wx.request = originalRequest;
});

test("age declaration explains owner-only and unavailable authority failures", async () => {
  binding.saveBindingManifest(parentChildManifest());
  profilePayload = wireProfile({
    runtime_profile_id: "rp_age_fail",
    session_id: "ses_age_fail",
    session_epoch: 1,
    service_mode: "adult_companion",
    speaker_state: "confirmed",
    capabilities: ["chat"],
  });
  resolutionPayload = {
    resolution: "confirmed",
    candidate_subjects: [],
    temporary_service_mode: "adult_companion",
    allowed_confirmation_methods: [],
    runtime_profile_id: "rp_age_fail",
  };
  const originalRequest = global.wx.request;
  let statusCode = 403;
  global.wx.request = (options) => {
    const pathname = options.url.replace("https://aigcnice.com:8443/memoria-api", "");
    if (pathname === "/v1/persons/person_child/age-evidence") {
      options.success({
        statusCode,
        data: {
          detail: {
            code:
              statusCode === 403
                ? "guardian_binding_owner_required"
                : "identity_authority_unavailable",
          },
        },
      });
      return;
    }
    return originalRequest(options);
  };
  const page = instantiate(pageDefinition);
  await page.onShow();
  await page.declareAge({ currentTarget: { dataset: { personId: "person_child" } } });
  assert.match(page.data.ageError, /只有监护绑定发起人/);
  assert.doesNotMatch(page.data.ageError, /聊天|记忆|声纹/);
  statusCode = 503;
  await page.declareAge({ currentTarget: { dataset: { personId: "person_child" } } });
  assert.match(page.data.ageError, /暂时无法确认/);
  assert.match(page.data.ageError, /受限模式/);
  assert.doesNotMatch(page.data.ageError, /能力/);
  global.wx.request = originalRequest;
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
  assert.ok(page.data.error.includes("暂不支持在应用里切换使用人"));
  assert.equal(activeSubjectCalls.length, 0);
});

test("candidate voice-match confidence is never decorated or shown", async () => {
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
  assert.equal(page.data.candidates[0].person_id, "person_guest");
  assert.equal(page.data.candidates[0].confidencePercent, undefined);
  assert.equal(page.data.candidates[0].confidenceLow, undefined);
  assert.ok(page.data.degradation.reasons.some((reason) => reason.includes("尚未确认")));
  const template = fs.readFileSync(path.join(__dirname, "../pages/device/index.wxml"), "utf8");
  assert.doesNotMatch(template, /置信度|confidencePercent|说话人候选|语音确认身份/);
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

function wireSettings(overrides = {}) {
  return {
    device_id: "dev_1",
    settings_version: 3,
    volume_limit: 60,
    screen_brightness: 70,
    night_mode: false,
    do_not_disturb: false,
    learning_mode: "off",
    audio_mode: "half_duplex_safe",
    wake_mode: "button",
    wake_word_id: "mo_li",
    wake_word_display: "茉莉",
    allowed_barge_in: ["button", "keyword"],
    updated_by: "person_owner",
    updated_at: new Date().toISOString(),
    update_reason: "defaults",
    ...overrides,
  };
}

function wireDiagnostics(overrides = {}) {
  return {
    device_id: "dev_1",
    generated_at: "2026-08-13T00:00:00Z",
    binding: {
      binding_id: "bd_1",
      binding_version: 2,
      activation_status: "ready_for_conversation",
      config_hash: "abc",
    },
    settings: wireSettings(),
    acoustic_capability: {
      device_id: "dev_1",
      board_profile: "memoria-atk-dnesp32s3-v1",
      firmware_version_range: "0.2.0-0.3.0",
      acoustic_profile_version: 1,
      simultaneous_capture_playback: false,
      aec_reference_type: "none",
      aec_verified: false,
      max_barge_in_level: "level_1",
      tested_volume_range: "40-70",
      tested_distance_m: null,
      test_report_uri: "",
      approved_at: "2026-08-01T00:00:00Z",
      approved_by: "qa",
      revoked_at: null,
    },
    allowed_audio_modes: ["half_duplex_safe"],
    runtime_profile_version: 2,
    live_runtime: {
      device_id: "dev_1",
      connected: false,
    },
    ...overrides,
  };
}

test("device page loads authoritative settings and diagnostics (half-duplex fail-closed)", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_diag", session_id: "ses_diag", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = wireDiagnostics();
  nextRequestResult = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.settings.settings_version, 3);
  assert.equal(page.data.settings.volume_limit, 60);
  assert.equal(page.data.diagnosticsUnavailable, false);
  assert.equal(page.data.diagnostics.runtime_profile_version, 2);
  assert.equal(page.data.diagnostics.binding.activation_status, "ready_for_conversation");
  assert.equal(page.data.acousticCapability.aec_verified, false);
  assert.equal(page.data.acousticVerified, false);
  assert.deepEqual(page.data.audioModeOptions, [
    { value: "half_duplex_safe", label: "半双工安全模式" },
  ]);
  assert.equal(page.data.allowedAudioModesLabel, "半双工安全模式");
  assert.equal(page.data.currentAudioModeLabel, "半双工安全模式");
  assert.equal(page.data.effectiveAudioModeLabel, "未连接，暂无实际模式");
  assert.equal(page.data.liveRuntimeStatusLabel, "当前未连接");
  assert.equal(page.data.online, true);
  assert.equal(page.data.onlineLabel, "设备在线");
  assert.equal(page.data.wakeModeLabel, "按键唤醒");
  assert.deepEqual(
    page.data.bargeInOptions.map((option) => option.value),
    ["none", "button", "keyword"],
    "AEC 未验收时不得提供语音打断选项",
  );
  assert.deepEqual(page.data.bargeInChecked, { button: true, keyword: true });
});

test("aec-verified diagnostics expose server-approved modes and voice barge-in", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_aec", session_id: "ses_aec", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = wireDiagnostics({
    acoustic_capability: {
      ...wireDiagnostics().acoustic_capability,
      simultaneous_capture_playback: true,
      aec_reference_type: "software_post_gain_pre_i2s",
      aec_verified: true,
    },
    allowed_audio_modes: ["full_duplex_verified", "interrupt_assist", "half_duplex_safe"],
  });
  nextRequestResult = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.acousticVerified, true);
  assert.deepEqual(
    page.data.audioModeOptions.map((option) => option.value),
    ["full_duplex_verified", "interrupt_assist", "half_duplex_safe"],
  );
  assert.ok(
    page.data.audioModeOptions.some(
      (option) => option.value === "full_duplex_verified" && option.label.includes("声学验收"),
    ),
  );
  assert.deepEqual(
    page.data.bargeInOptions.map((option) => option.value),
    ["none", "button", "keyword", "voice"],
    "AEC 验收通过后才开放语音打断选项",
  );
});

test("device page shows the Edge-negotiated effective audio mode, not the requested echo", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_live", session_id: "ses_live", session_epoch: 1 });
  settingsPayload = wireSettings({ audio_mode: "interrupt_assist" });
  diagnosticsPayload = wireDiagnostics({
    settings: settingsPayload,
    allowed_audio_modes: ["half_duplex_safe", "interrupt_assist"],
    live_runtime: {
      device_id: "dev_1",
      connected: true,
      session_id: "ses_live",
      stream_epoch: 18,
      audio_mode_requested: "interrupt_assist",
      audio_mode_effective: "half_duplex_safe",
    },
  });
  nextRequestResult = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.currentAudioModeLabel, "打断辅助");
  assert.equal(page.data.effectiveAudioModeLabel, "半双工安全模式");
  assert.equal(page.data.liveRuntimeStatusLabel, "已连接 · epoch 18");
  assert.equal(page.data.online, true);
  assert.equal(page.data.onlineLabel, "设备在线");
});

test("diagnostics unavailable fails closed for acoustics controls", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_nodiag", session_id: "ses_nodiag", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = null;
  nextRequestResult = { statusCode: 500, data: { detail: "diagnostics unavailable" } };
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.diagnostics, null);
  assert.equal(page.data.diagnosticsUnavailable, true);
  assert.equal(page.data.online, false);
  assert.equal(page.data.onlineLabel, "绑定已确认，设备状态待同步");
  assert.deepEqual(page.data.audioModeOptions, [], "诊断不可用时音频模式不可选");
  assert.deepEqual(
    page.data.bargeInOptions.map((option) => option.value),
    ["none", "button", "keyword"],
    "诊断不可用时不得提供语音打断选项",
  );
  assert.equal(page.data.acousticCapability, null);
});

test("settings saves go through versioned updateDeviceSettings and revert on conflict", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_set", session_id: "ses_set", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = wireDiagnostics();
  nextRequestResult = null;
  settingsPatchCalls.length = 0;

  settingsPatchResult = wireSettings({ settings_version: 4, volume_limit: 80 });
  const page = instantiate(pageDefinition);
  await page.onShow();
  await page.changeVolume({ detail: { value: 80 } });
  assert.equal(settingsPatchCalls.length, 1);
  assert.equal(settingsPatchCalls[0].expected_settings_version, 3);
  assert.deepEqual(settingsPatchCalls[0].changes, { volume_limit: 80 });
  assert.equal(page.data.settings.settings_version, 4);
  assert.equal(page.data.settings.volume_limit, 80);

  settingsPatchCalls.length = 0;
  settingsPatchResult = null; // 409 settings_version_conflict
  await page.changeBrightness({ detail: { value: 55 } });
  assert.equal(settingsPatchCalls.length, 1);
  assert.equal(settingsPatchCalls[0].expected_settings_version, 4);
  assert.equal(page.data.settings.settings_version, 4, "冲突后不得保留本地假状态");
  assert.equal(page.data.settings.screen_brightness, 70);
  assert.ok(page.data.settingsError.length > 0);
});

test("audio mode and wake mode pickers only submit server-approved values", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_pick", session_id: "ses_pick", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = wireDiagnostics();
  nextRequestResult = null;
  settingsPatchCalls.length = 0;
  settingsPatchResult = wireSettings({ settings_version: 4, wake_mode: "keyword" });
  const page = instantiate(pageDefinition);
  await page.onShow();

  // 越界/非法选择被忽略（未验证 full duplex 不可选：选项来自 allowed_audio_modes）。
  await page.selectAudioMode({ detail: { value: "9" } });
  assert.equal(settingsPatchCalls.length, 0, "越界音频模式不得提交");
  await page.selectWakeMode({ detail: { value: "1" } }); // keyword
  assert.equal(settingsPatchCalls.length, 1);
  assert.deepEqual(settingsPatchCalls[0].changes, { wake_mode: "keyword" });
  assert.equal(page.data.wakeModeLabel, "唤醒词唤醒");
  assert.equal(page.data.settings.wake_mode, "keyword");
});

test("wake word picker only submits server-approved catalog ids", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_wake", session_id: "ses_wake", session_epoch: 1 });
  settingsPayload = wireSettings({ settings_version: 3, wake_word_id: "mo_li", wake_word_display: "茉莉" });
  diagnosticsPayload = wireDiagnostics();
  settingsPatchResult = wireSettings({
    settings_version: 4,
    wake_word_id: "mo_li",
    wake_word_display: "茉莉",
  });
  settingsPatchCalls.length = 0;
  nextRequestResult = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.wakeWordLabel, "茉莉");
  await page.selectWakeWord({ detail: { value: "0" } });
  assert.equal(settingsPatchCalls.length, 1);
  assert.deepEqual(settingsPatchCalls[0].changes, { wake_word_id: "mo_li" });
  assert.equal(page.data.wakeWordLabel, "茉莉");
  assert.equal(page.data.settings.wake_word_id, "mo_li");
});

test("custom wake word validates and saves through the settings API", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_custom", session_id: "ses_custom", session_epoch: 1 });
  settingsPayload = wireSettings({ settings_version: 3, wake_word_id: "mo_li" });
  diagnosticsPayload = wireDiagnostics();
  settingsPatchResult = wireSettings({
    settings_version: 4,
    wake_word_id: "custom",
    wake_word_display: "小黑",
    wake_word_pinyin: "xiao hei",
  });
  settingsPatchCalls.length = 0;
  nextRequestResult = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  page.setData({ customWakeWordDisplay: "小黑", customWakeWordPinyin: "xiao hei" });
  await page.saveCustomWakeWord();
  assert.equal(settingsPatchCalls.length, 1);
  assert.deepEqual(settingsPatchCalls[0].changes, {
    wake_word_id: "custom",
    wake_word_display: "小黑",
    wake_word_pinyin: "xiao hei",
  });
  assert.equal(page.data.settings.wake_word_id, "custom");
  assert.equal(page.data.customWakeWordWarnings.length, 1);
});

test("empty barge-in selection is blocked and voice kind follows AEC evidence", async () => {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireProfile({ runtime_profile_id: "rp_bg", session_id: "ses_bg", session_epoch: 1 });
  settingsPayload = wireSettings();
  diagnosticsPayload = wireDiagnostics();
  nextRequestResult = null;
  settingsPatchCalls.length = 0;
  const page = instantiate(pageDefinition);
  await page.onShow();

  await page.toggleBargeIn({ detail: { value: [] } });
  assert.equal(settingsPatchCalls.length, 0, "空打断列表不得提交（后端要求非空）");
  assert.equal(page.data.settings.allowed_barge_in.length, 2);

  settingsPatchResult = wireSettings({
    settings_version: 4,
    allowed_barge_in: ["button"],
  });
  await page.toggleBargeIn({ detail: { value: ["button"] } });
  assert.equal(settingsPatchCalls.length, 1);
  assert.deepEqual(settingsPatchCalls[0].changes, { allowed_barge_in: ["button"] });
  assert.deepEqual(page.data.bargeInChecked, { button: true });
});

test("persona assignment follows the subject override and the binding default", async () => {
  personaCalls.length = 0;
  personaFailure = false;
  personasPayload = () => ({
    custom_personas: [
      {
        persona_id: "cu_0123456789abcdef0123456789abcd",
        display_name: "奶奶的伙伴",
        persona_version: 1,
      },
    ],
    builtin: [],
  });
  personaPayload = () => ({
    assignments: [
      {
        binding_id: "bd_1",
        subject_id: "person_child",
        assignment_id: "taoxi:v1",
        persona_id: "taoxi",
        persona_version: 1,
      },
      {
        binding_id: "bd_1",
        subject_id: "person_parent",
        assignment_id: "cu_0123456789abcdef0123456789abcd:v1",
        persona_id: "cu_0123456789abcdef0123456789abcd",
        persona_version: 1,
      },
    ],
    binding_default: "starlight:v1",
  });
  const manifest = familyManifest();
  binding.saveBindingManifest({
    ...manifest,
    member_ids: ["person_child", "person_parent"],
    roles: [
      { person_id: "person_owner", role: "device_admin", permissions: [] },
      { person_id: "person_child", role: "primary_subject", permissions: [] },
      { person_id: "person_parent", role: "member", permissions: [] },
    ],
  });
  profilePayload = wireProfile({ runtime_profile_id: "rp_pa", session_id: "ses_pa", session_epoch: 1 });
  nextRequestResult = null;

  const page = instantiate(pageDefinition);
  await page.onShow();

  assert.deepEqual(
    page.data.personaRows.map((row) => [row.person_id, row.persona_id, row.is_override]),
    [
      ["person_child", "taoxi", true],
      ["person_parent", "cu_0123456789abcdef0123456789abcd", true],
    ],
  );
  assert.equal(
    page.data.personaRows[1].persona_name,
    "奶奶的伙伴",
    "自建人格显示名必须来自目录，不能裸 id",
  );
  assert.deepEqual(
    page.data.personaOptions.map((item) => item.id).slice(-1),
    ["cu_0123456789abcdef0123456789abcd"],
  );

  page.openPersonaSheet({ currentTarget: { dataset: { personId: "person_child" } } });
  assert.equal(page.data.personaSheetVisible, true);
  assert.equal(page.data.personaSheetSelection, "taoxi");
  page.pickPersonaOption({ currentTarget: { dataset: { personaId: "xuanmo" } } });
  await page.confirmPersonaAssignment();
  assert.deepEqual(personaCalls.at(-2), {
    method: "PUT",
    deviceId: "dev_1",
    personId: "person_child",
    data: { persona_selection: "xuanmo" },
  });
  assert.equal(personaCalls.at(-1).method, "GET", "写入后必须回读服务端分配");
  assert.equal(page.data.personaSheetVisible, false);

  await page.clearPersonaAssignment({ currentTarget: { dataset: { personId: "person_child" } } });
  assert.deepEqual(personaCalls.at(-2), {
    method: "DELETE",
    deviceId: "dev_1",
    personId: "person_child",
    data: null,
  });
});

test("an override written by the companion picker reads as following the pick", async () => {
  personaCalls.length = 0;
  personaFailure = false;
  personasPayload = () => ({ custom_personas: [], builtin: [] });
  personaPayload = () => ({
    assignments: [
      {
        binding_id: "bd_1",
        subject_id: "person_child",
        assignment_id: "taoxi:v1",
        persona_id: "taoxi",
        persona_version: 1,
      },
      {
        binding_id: "bd_1",
        subject_id: "person_parent",
        assignment_id: "xuanmo:v1",
        persona_id: "xuanmo",
        persona_version: 1,
      },
    ],
    binding_default: "starlight:v1",
  });
  accountProfilePayload = { user_id: "person_owner", companion_id: "taoxi" };
  binding.saveBindingManifest({
    ...familyManifest(),
    member_ids: ["person_child", "person_parent"],
    roles: [
      { person_id: "person_owner", role: "device_admin", permissions: [] },
      { person_id: "person_child", role: "primary_subject", permissions: [] },
      { person_id: "person_parent", role: "member", permissions: [] },
    ],
  });
  profilePayload = wireProfile({ runtime_profile_id: "rp_pick", session_id: "ses_pick", session_epoch: 1 });
  nextRequestResult = null;

  try {
    const page = instantiate(pageDefinition);
    await page.onShow();
    assert.deepEqual(
      page.data.personaRows.map((row) => [row.person_id, row.persona_name, row.source_label]),
      [
        ["person_child", "桃喜", "跟随陪伴选择"],
        ["person_parent", "玄墨", "已单独分配"],
      ],
    );

    // 回读分配后仍按账号伙伴解释来源。
    await page.reloadPersonaAssignments("dev_1");
    assert.equal(page.data.personaRows[0].source_label, "跟随陪伴选择");
  } finally {
    accountProfilePayload = null;
  }
});

test("persona assignment failure keeps the sheet open and reports the error", async () => {
  personaCalls.length = 0;
  personaFailure = true;
  personaPayload = () => ({ assignments: [], binding_default: "starlight:v1" });
  binding.saveBindingManifest({
    ...familyManifest(),
    roles: [
      { person_id: "person_owner", role: "device_admin", permissions: [] },
      { person_id: "person_child", role: "primary_subject", permissions: [] },
    ],
  });
  profilePayload = wireProfile({ runtime_profile_id: "rp_pf", session_id: "ses_pf", session_epoch: 1 });
  nextRequestResult = null;

  const page = instantiate(pageDefinition);
  await page.onShow();
  page.openPersonaSheet({ currentTarget: { dataset: { personId: "person_child" } } });
  page.pickPersonaOption({ currentTarget: { dataset: { personaId: "axu" } } });
  await page.confirmPersonaAssignment();

  assert.equal(page.data.personaSheetVisible, true, "失败时不得假装已分配");
  assert.ok(page.data.personaAssignmentError);
  assert.equal(
    personaCalls.filter((call) => call.method === "PUT").length,
    1,
  );
  personaFailure = false;
});

async function bootBoundDevicePage() {
  binding.saveBindingManifest(familyManifest());
  profilePayload = wireUnknownSafeProfile({
    runtime_profile_id: "rp_unbind",
    session_id: "ses_unbind",
    session_epoch: 1,
  });
  resolutionPayload = null;
  const page = instantiate(pageDefinition);
  await page.onShow();
  assert.equal(page.data.hasBinding, true);
  return page;
}

test("unbind keeps the subject's data when the owner chooses to keep it", async () => {
  unbindCalls.length = 0;
  const modals = [];
  const previousShowModal = global.wx.showModal;
  global.wx.showModal = (options) => {
    modals.push(options);
    options.success?.({ confirm: true });
  };
  try {
    const page = await bootBoundDevicePage();
    page.openUnbindSheet();
    assert.equal(page.data.unbindSheetVisible, true);
    // 没有二选一之前不能提交。
    await page.confirmUnbind();
    assert.equal(unbindCalls.length, 0);
    assert.match(page.data.unbindError, /请先选择/);

    page.pickUnbindChoice({ currentTarget: { dataset: { choice: "keep" } } });
    await page.confirmUnbind();
    assert.equal(unbindCalls.length, 1);
    assert.equal(unbindCalls[0].method, "POST");
    assert.deepEqual(unbindCalls[0].data, { reason: "unbind", purge_subject_data: false });
    // 保留数据不需要再弹删除确认。
    assert.equal(modals.length, 0);
    assert.equal(binding.readBindingManifest(), null);
    assert.equal(page.data.unbindSheetVisible, false);
    assert.equal(page.data.hasBinding, false);
  } finally {
    global.wx.showModal = previousShowModal;
  }
});

test("unbind with purge asks a second time and sends purge_subject_data true", async () => {
  unbindCalls.length = 0;
  const modals = [];
  let confirmPurge = false;
  const previousShowModal = global.wx.showModal;
  global.wx.showModal = (options) => {
    modals.push(options);
    options.success?.({ confirm: confirmPurge });
  };
  try {
    const page = await bootBoundDevicePage();
    page.openUnbindSheet();
    page.pickUnbindChoice({ currentTarget: { dataset: { choice: "purge" } } });
    // 第二次确认点了「再想想」：不发请求，绑定仍在。
    await page.confirmUnbind();
    assert.equal(modals.length, 1);
    assert.match(modals[0].content, /永久删除/);
    assert.equal(unbindCalls.length, 0);
    assert.notEqual(binding.readBindingManifest(), null);

    confirmPurge = true;
    await page.confirmUnbind();
    assert.equal(modals.length, 2);
    assert.equal(unbindCalls.length, 1);
    assert.deepEqual(unbindCalls[0].data, { reason: "unbind", purge_subject_data: true });
    assert.equal(binding.readBindingManifest(), null);
  } finally {
    global.wx.showModal = previousShowModal;
  }
});

test("device page offers both unbind data choices", () => {
  const template = fs.readFileSync(path.join(__dirname, "../pages/device/index.wxml"), "utf8");
  assert.match(template, /同时删除 TA 的记忆和对话数据/);
  assert.match(template, /保留数据（重新绑定后可恢复）/);
  assert.match(template, /停止记忆/);
});

test("device page shows a bound elder's style labels and resets only after confirmation", async () => {
  const api = require("../utils/api");
  const originals = {
    getSubjectPersonaStyle: api.getSubjectPersonaStyle,
    resetSubjectPersona: api.resetSubjectPersona,
    currentAuthEpoch: api.currentAuthEpoch,
    isAuthEpochCurrent: api.isAuthEpochCurrent,
  };
  const styleCalls = [];
  const resetCalls = [];
  api.getSubjectPersonaStyle = async (personId) => {
    styleCalls.push(personId);
    return { subjectId: personId, versionNumber: 3, styleLabels: ["说话节奏偏从容，适合保留自然停顿"] };
  };
  api.resetSubjectPersona = async (personId) => {
    resetCalls.push(personId);
    return { subject_id: personId, deleted_rows: 4 };
  };
  api.isAuthEpochCurrent = () => true;
  const previousWx = global.wx;
  const modalAnswers = [false, true];
  global.wx = {
    ...(previousWx || {}),
    showModal: ({ success }) => success({ confirm: modalAnswers.shift() }),
    showToast: () => {},
  };
  const page = instantiate(pageDefinition);
  page._flowSeq = 1;
  try {
    await page._loadSubjectPersonas(
      {
        declared_mode: "child_for_parent",
        status: "active",
        account_owner_id: "person_owner",
        primary_subject_ids: ["person_elder"],
      },
      1,
      0,
    );
    // A self-use binding has nobody else's persona to show.
    await page._loadSubjectPersonas(
      { declared_mode: "self_use", status: "active", account_owner_id: "person_owner",
        primary_subject_ids: ["person_owner"] },
      1,
      0,
    );
    assert.deepEqual(styleCalls, ["person_elder"]);
    assert.deepEqual(page.data.subjectPersonas, [
      { personId: "person_elder", styleLabels: ["说话节奏偏从容，适合保留自然停顿"], versionNumber: 3, loadError: "" },
    ]);

    const tap = { currentTarget: { dataset: { personId: "person_elder" } } };
    await page.resetSubjectPersona(tap); // declined
    assert.deepEqual(resetCalls, []);
    await page.resetSubjectPersona(tap); // confirmed
    assert.deepEqual(resetCalls, ["person_elder"]);
    assert.deepEqual(page.data.subjectPersonas[0].styleLabels, []);
    assert.equal(page.data.personaResetting, false);
  } finally {
    Object.assign(api, originals);
    global.wx = previousWx;
  }
});
