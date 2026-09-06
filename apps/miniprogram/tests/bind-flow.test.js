const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const api = require("../utils/api");
const { canonicalManifest } = require("./manifest-fixtures");
const { readSubjectLabel } = require("../utils/subject-label");

const storage = {};
let lastWxRequest = null;
let nextResponse = null;
const navigations = [];

global.wx = {
  getStorageSync: (key) => storage[key],
  setStorageSync: (key, value) => {
    storage[key] = value;
  },
  removeStorageSync: (key) => {
    delete storage[key];
  },
  scanCode(options) {
    options.success?.({ result: "scanned-device-token" });
    options.complete?.();
  },
  showToast() {},
  navigateTo(options) {
    navigations.push(options.url);
  },
  switchTab(options) {
    navigations.push(options.url);
  },
  request(options) {
    lastWxRequest = options;
    options.success(nextResponse);
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
api.getGuardianLinks = async () => [];
api.getDeviceClaim = async () => ({
  claim_id: "claim_test_1",
  device_id: "dev_test_1",
  onboarding_session_id: "onb_test_1",
  status: "reserved",
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  binding_id: null,
  device: null,
});

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

const pagePath = require.resolve("../pages/bind/index");
delete require.cache[pagePath];
require(pagePath);

function successResponse(payload) {
  return { statusCode: 200, data: payload };
}

function defaultManifestResponse(declaredMode) {
  return canonicalManifest({
    binding_id: "bd_test_1",
    device_id: "dev_test_1",
    declared_mode: declaredMode,
  });
}

async function bootToMode(mode) {
  const page = instantiate(pageDefinition);
  page.onLoad({ claim_id: "claim_test_1", onboarding_session_id: "onb_test_1" });
  await page.onShow();
  assert.equal(page.data.step, "mode");
  page.chooseMode({ currentTarget: { dataset: { mode } } });
  assert.equal(page.data.step, "form");
  return page;
}

async function bootSelfUseReady(remark = "阿宁") {
  const page = await bootToMode("self_use");
  page.setData({ "form.selfNickname": remark });
  return page;
}

test("app registers binding, device, and onboarding pages", () => {
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  assert.ok(appConfig.pages.includes("pages/bind/index"));
  assert.ok(appConfig.pages.includes("pages/device/index"));
  assert.ok(appConfig.pages.includes("pages/device-onboarding/index"));
  assert.deepEqual(
    appConfig.tabBar.list.map((item) => item.text),
    ["首页", "设备", "回顾", "我的"],
  );
});

test("binding page cannot enter from a device code or scanner", async () => {
  const page = instantiate(pageDefinition);
  page.onLoad({});
  await page.onShow();
  assert.equal(page.data.step, "error");
  assert.match(page.data.error, /claim_id/);
  assert.equal(typeof page.scanDeviceCode, "undefined");
});

test("binding page collects a required subject remark after choosing who it is for", async () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(template, /使用者备注/);
  assert.match(template, /亲爱的儿子/);
  assert.match(template, /首页的当前使用者会显示这条备注/);
  assert.doesNotMatch(template, /怎么称呼你（可选）/);

  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootToMode("self_use");
  assert.equal(page.data.form.selfNickname, "");
  page.goToReview();
  assert.ok(page.data.error.includes("备注"));
  assert.equal(page.data.step, "form");
  page.setData({ "form.selfNickname": "亲爱的儿子" });
  page.goToReview();
  assert.equal(page.data.step, "review");
  assert.equal(page.data.reviewSubjectLabel, "亲爱的儿子");
  await page.submitBinding();
  assert.equal(readSubjectLabel(page.data.manifest), "亲爱的儿子");
});

test("self_use flow keeps sensitive offers off by default and submits clean payload", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady("阿宁");
  const offers = page.data.offers;
  assert.equal(offers.find((offer) => offer.id === "offer_self_memory_retention_v1").checked, true);
  assert.equal(offers.find((offer) => offer.id === "offer_self_voice_profile_v1").checked, true);
  assert.equal(offers.find((offer) => offer.id === "offer_self_raw_audio_v1").checked, false);
  assert.equal(offers.find((offer) => offer.id === "offer_self_voice_clone_v1").checked, false);
  assert.equal(offers.find((offer) => offer.id === "offer_self_digital_self_v1").checked, false);
  assert.equal(offers.find((offer) => offer.id === "offer_self_legacy_v1").checked, false);

  page.goToReview();
  assert.equal(page.data.step, "review");
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  const payload = lastWxRequest.data;
  assert.equal(payload.declared_mode, "self_use");
  assert.equal(payload.claim_id, "claim_test_1");
  assert.equal(payload.onboarding_session_id, "onb_test_1");
  assert.equal(payload.device_claim_token, undefined);
  assert.equal(lastWxRequest.header["Idempotency-Key"], "bind-claim_test_1");
  assert.equal(payload.account_owner_person_id, "person_owner");
  assert.equal(payload.primary_subject.person_id, "person_owner");
  assert.equal(payload.primary_subject.relationship, "self");
  assert.equal(payload.primary_subject.subject_draft, undefined);
  assert.equal(payload.persona_selection, "starlight");
  assert.deepEqual(payload.service_preferences, {
    memory_level: "personal",
    interview_frequency: "low",
  });
  assert.deepEqual(payload.consent_offer_ids, [
    "offer_self_memory_retention_v1",
    "offer_self_voice_profile_v1",
  ]);
  assert.ok(!Object.prototype.hasOwnProperty.call(payload, "policy_version"));
  assert.equal(readSubjectLabel(page.data.manifest), "阿宁");
});

test("checking digital self includes it in the submitted consent offers", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady();
  page.toggleOffer({ currentTarget: { dataset: { id: "offer_self_digital_self_v1" } } });
  assert.equal(
    page.data.offers.find((offer) => offer.id === "offer_self_digital_self_v1").checked,
    true,
  );
  page.goToReview();
  await page.submitBinding();
  assert.ok(lastWxRequest.data.consent_offer_ids.includes("offer_self_digital_self_v1"));
});

test("parent_for_child flow validates minimal info and submits guardian relationship", async () => {
  nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
  const page = await bootToMode("parent_for_child");
  assert.equal(page.data.form.subjectSource, "new");
  assert.deepEqual(page.data.ageBands.map((band) => band.value), ["under_14", "14_17"]);

  page.goToReview();
  assert.ok(page.data.error.includes("备注"));
  assert.equal(page.data.step, "form");

  page.setData({ "form.childNickname": "小乐" });
  page.goToReview();
  assert.equal(page.data.step, "review");
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  const payload = lastWxRequest.data;
  assert.equal(payload.declared_mode, "parent_for_child");
  assert.equal(payload.primary_subject.relationship, "guardian_of");
  assert.deepEqual(payload.primary_subject.subject_draft, {
    display_name: "小乐",
    age_band: "under_14",
  });
  assert.equal(payload.service_preferences.memory_level, "growth_summary");
  assert.equal(payload.service_preferences.max_session_minutes, 30);
  assert.deepEqual(payload.service_preferences.quiet_hours, {
    start: "21:00",
    end: "07:00",
  });
  assert.ok(payload.consent_offer_ids.includes("offer_minor_voice_session_v1"));
  assert.ok(payload.consent_offer_ids.includes("offer_minor_memory_retention_v1"));
  assert.ok(payload.consent_offer_ids.includes("offer_guardian_weekly_summary_v1"));
  assert.ok(!payload.consent_offer_ids.includes("offer_emergency_contact_v1"));
  assert.equal(readSubjectLabel(page.data.manifest), "小乐");
});

test("parent_for_child can reuse an existing linked child profile", async () => {
  api.getGuardianLinks = async () => [
    { linkId: "link_1", minorUserId: "person_child", displayName: "小乐", status: "active" },
  ];
  nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
  const page = await bootToMode("parent_for_child");
  await Promise.resolve();
  assert.equal(page.data.existingSubjectOptions.length, 1);
  page.setData({ "form.subjectSource": "existing" });
  page.goToReview();
  assert.equal(page.data.step, "review");
  await page.submitBinding();
  const payload = lastWxRequest.data;
  assert.equal(payload.primary_subject.person_id, "person_child");
  assert.equal(payload.primary_subject.subject_draft, undefined);
  assert.equal(readSubjectLabel(page.data.manifest), "小乐");
});

test("late child-profile lookup is ignored after switching away from parent_for_child", async () => {
  let resolveLinks;
  const originalGetGuardianLinks = api.getGuardianLinks;
  api.getGuardianLinks = () =>
    new Promise((resolve) => {
      resolveLinks = resolve;
    });

  try {
    nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
    const page = await bootToMode("parent_for_child");
    assert.equal(page.data.existingSubjectOptions.length, 0);

    page.chooseMode({ currentTarget: { dataset: { mode: "self_use" } } });
    assert.equal(page.data.declaredMode, "self_use");

    resolveLinks([
      { linkId: "link_1", minorUserId: "person_child", displayName: "小乐", status: "active" },
    ]);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(page.data.declaredMode, "self_use");
    assert.equal(page.data.existingSubjectOptions.length, 0);
  } finally {
    api.getGuardianLinks = originalGetGuardianLinks;
  }
});

test("child_for_parent never submits parent self-acceptance and keeps admin scope", async () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(template, /需要父母本人确认/);
  assert.match(template, /不会代替父母开启/);

  nextResponse = successResponse(defaultManifestResponse("child_for_parent"));
  const page = await bootToMode("child_for_parent");
  const parentOffers = page.data.offers.filter((offer) => offer.requiresParentSelfAcceptance);
  assert.equal(parentOffers.length, 2);
  for (const offer of parentOffers) {
    assert.equal(offer.checked, false);
    page.toggleOffer({ currentTarget: { dataset: { id: offer.id } } });
    assert.ok(!page.data.acceptedOfferIds.includes(offer.id));
  }

  page.setData({ "form.parentNickname": "妈妈" });
  page.goToReview();
  assert.equal(page.data.step, "review");
  await page.submitBinding();
  const payload = lastWxRequest.data;
  assert.ok(!payload.consent_offer_ids.includes("offer_senior_service_acceptance_v1"));
  assert.ok(!payload.consent_offer_ids.includes("offer_senior_memory_retention_v1"));
  assert.ok(payload.consent_offer_ids.includes("offer_admin_device_management_v1"));
  assert.ok(payload.consent_offer_ids.includes("offer_senior_anti_fraud_v1"));
  assert.equal(payload.service_preferences.admin_visibility, "device_status");
  assert.equal(payload.service_preferences.speech_speed, "slow");
  assert.equal(payload.service_preferences.memory_level, "none");
  assert.deepEqual(payload.primary_subject.subject_draft, {
    display_name: "妈妈",
    age_band: "adult",
  });
  assert.equal(readSubjectLabel(page.data.manifest), "妈妈");
});

test("family_shared flow keeps extra members off the binding request", async () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(template, /家庭名称与其他成员可以稍后添加/);
  assert.doesNotMatch(template, /家庭空间 \*/);

  nextResponse = successResponse(defaultManifestResponse("family_shared"));
  const page = await bootToMode("family_shared");
  page.goToReview();
  assert.ok(page.data.error.includes("备注"));
  page.setData({ "form.subjectAlias": "老爸" });
  page.goToReview();
  assert.equal(page.data.step, "review");
  await page.submitBinding();
  const payload = lastWxRequest.data;
  assert.equal(payload.primary_subject.person_id, "person_owner");
  assert.equal(payload.primary_subject.relationship, "family_member_of");
  assert.deepEqual(payload.service_preferences, {
    memory_level: "family_shared",
    shared_persona_enabled: true,
  });
  assert.ok(!Object.prototype.hasOwnProperty.call(payload, "familyName"));
  assert.ok(!JSON.stringify(payload).includes("familyDrafts"));
  assert.equal(readSubjectLabel(page.data.manifest), "老爸");
});

test("binding failures surface explainable errors and stay on review", async () => {
  nextResponse = {
    statusCode: 400,
    data: { detail: { code: "device_claim_token_invalid", message: "设备码无效" } },
  };
  const page = await bootSelfUseReady();
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "review");
  assert.ok(page.data.error);
  assert.equal(page.data.manifest, null);
});

test("done step shows the binding manifest and offers device management", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady();
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  assert.equal(page.data.manifest.binding_id, "bd_test_1");
  page.openDevicePage();
  assert.ok(navigations.includes("/pages/device/index"));
});

test("subject remark helpers normalize, scope, and extract bind-form labels", () => {
  const {
    normalizeSubjectLabel,
    subjectLabelFromBindForm,
    saveSubjectLabel,
    readSubjectLabel,
    clearSubjectLabel,
  } = require("../utils/subject-label");

  assert.equal(normalizeSubjectLabel("  老爸  "), "老爸");
  assert.equal(subjectLabelFromBindForm("self_use", { selfNickname: "我自己" }), "我自己");
  assert.equal(
    subjectLabelFromBindForm("parent_for_child", { childNickname: "亲爱的儿子" }),
    "亲爱的儿子",
  );
  assert.equal(
    subjectLabelFromBindForm(
      "parent_for_child",
      { subjectSource: "existing", subjectAlias: "" },
      { existingLabel: "小乐" },
    ),
    "小乐",
  );
  assert.equal(subjectLabelFromBindForm("child_for_parent", { parentNickname: "老爸" }), "老爸");
  assert.equal(subjectLabelFromBindForm("family_shared", { subjectAlias: "老爸" }), "老爸");

  const binding = { binding_id: "bd_scope", device_id: "dev_scope" };
  assert.equal(
    saveSubjectLabel({ bindingId: binding.binding_id, deviceId: binding.device_id, label: "老爸" }),
    true,
  );
  assert.equal(readSubjectLabel(binding), "老爸");
  assert.equal(readSubjectLabel({ binding_id: "bd_other", device_id: "dev_scope" }), "");
  clearSubjectLabel();
  assert.equal(readSubjectLabel(binding), "");
});
