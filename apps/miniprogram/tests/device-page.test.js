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
let settingsPayload = null;
let diagnosticsPayload = null;
let settingsPatchResult = null;
const settingsPatchCalls = [];

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
  assert.ok(page.data.error.includes("先在机器人上开始对话"));
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
