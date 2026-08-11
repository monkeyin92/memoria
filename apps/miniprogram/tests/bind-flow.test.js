const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const api = require("../utils/api");
const { canonicalManifest } = require("./manifest-fixtures");

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
  await page.onShow();
  page.setData({ deviceClaimToken: "dev-token-1" });
  page.goToModeStep();
  assert.equal(page.data.step, "mode");
  page.chooseMode({ currentTarget: { dataset: { mode } } });
  assert.equal(page.data.step, "form");
  return page;
}

test("app registers the bind and device pages", () => {
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  assert.ok(appConfig.pages.includes("pages/bind/index"));
  assert.ok(appConfig.pages.includes("pages/device/index"));
});

test("claim step accepts manual token and QR scan", () => {
  const page = instantiate(pageDefinition);
  page.setData({ deviceClaimToken: "dev-manual-1" });
  page.goToModeStep();
  assert.equal(page.data.step, "mode");

  const page2 = instantiate(pageDefinition);
  page2.scanDeviceCode();
  assert.equal(page2.data.deviceClaimToken, "scanned-device-token");
  assert.equal(page2.data.scanning, false);
});

test("self_use flow keeps sensitive offers off by default and submits clean payload", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootToMode("self_use");
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
});

test("checking digital self includes it in the submitted consent offers", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootToMode("self_use");
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
  assert.ok(page.data.error.includes("昵称"));
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
});

test("family_shared flow keeps member drafts client-side", async () => {
  nextResponse = successResponse(defaultManifestResponse("family_shared"));
  const page = await bootToMode("family_shared");
  page.goToReview();
  assert.ok(page.data.error.includes("家庭空间"));
  page.setData({ "form.familyName": "我们的小家" });
  page.setData({ "form.familyDrafts[0].nickname": "小乐" });
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
  assert.ok(!JSON.stringify(payload).includes("小乐"), "成员档案不应随绑定请求提交");
});

test("binding failures surface explainable errors and stay on review", async () => {
  nextResponse = {
    statusCode: 400,
    data: { detail: { code: "device_claim_token_invalid", message: "设备码无效" } },
  };
  const page = await bootToMode("self_use");
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "review");
  assert.ok(page.data.error);
  assert.equal(page.data.manifest, null);
});

test("done step shows the binding manifest and offers device management", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootToMode("self_use");
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  assert.equal(page.data.manifest.binding_id, "bd_test_1");
  page.openDevicePage();
  assert.ok(navigations.includes("/pages/device/index"));
});
