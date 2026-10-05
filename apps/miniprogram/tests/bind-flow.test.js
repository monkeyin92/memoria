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
const profileWrites = [];

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
    if (options.url.includes("/v1/memory/profile/")) {
      profileWrites.push({ method: options.method, data: options.data });
      options.success({ statusCode: 200, data: { user_id: "person_owner", ...options.data } });
      return;
    }
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
const realGetBindingSubjectCandidates = api.getBindingSubjectCandidates;
api.getGuardianLinks = async () => [];
api.getBindingSubjectCandidates = async () => [];
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
    ["首页", "回顾", "伙伴", "设备", "我的"],
  );
});

test("binding page cannot enter from a device code or scanner", async () => {
  const page = instantiate(pageDefinition);
  page.onLoad({});
  await page.onShow();
  assert.equal(page.data.step, "error");
  // Plain words for the person, no protocol names, and a way out.
  assert.match(page.data.error, /重新扫码/);
  assert.doesNotMatch(page.data.error, /claim_id|服务端|认领/);
  assert.equal(typeof page.scanDeviceCode, "undefined");
});

test("binding page names the robot by the tail passed from the onboarding page, never its raw id", async () => {
  const page = instantiate(pageDefinition);
  page.onLoad({ claim_id: "claim_test_1", onboarding_session_id: "onb_test_1", device_tail: "AB12" });
  assert.equal(page.data.deviceTail, "AB12");
  // Display-only and strictly shaped: anything else is dropped.
  for (const bad of ["", "<b>x</b>", "AB 12", "A".repeat(17)]) {
    const other = instantiate(pageDefinition);
    other.onLoad({ claim_id: "claim_test_1", onboarding_session_id: "onb_test_1", device_tail: bad });
    assert.equal(other.data.deviceTail, "", bad);
  }
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(template, /尾号 \{\{deviceTail\}\}/);
  for (const raw of ["claim.device_id", "claimId", "manifest.binding_id", "manifest.device_id"]) {
    assert.ok(!template.includes(raw), `the bind page still shows ${raw}`);
  }
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

test("the companion picked while binding becomes the account's companion", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  profileWrites.length = 0;
  const page = await bootSelfUseReady("阿宁");
  const index = page.data.personaOptions.findIndex((option) => option.id === "mianmian");
  page.onPersonaSwipe({ detail: { current: index } });
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  assert.equal(lastWxRequest.data.persona_selection, "mianmian");
  // 首页、设备、伙伴页读的是账号伙伴；不写它，这些页面会停在旧伙伴上。
  assert.deepEqual(profileWrites, [{ method: "PUT", data: { companion_id: "mianmian" } }]);
});

test("self_use flow keeps sensitive offers off by default and submits clean payload", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady("阿宁");
  const offers = page.data.offers;
  assert.equal(offers.find((offer) => offer.id === "offer_self_memory_retention_v1").checked, true);
  assert.equal(offers.some((offer) => offer.id === "offer_self_voice_profile_v1"), false);
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
  assert.deepEqual(payload.consent_offer_ids, ["offer_self_memory_retention_v1"]);
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
  // 长期记忆默认不勾选，也不是完成绑定的前提。
  assert.equal(payload.service_preferences.memory_level, "none");
  assert.equal(payload.service_preferences.max_session_minutes, 30);
  assert.deepEqual(payload.service_preferences.quiet_hours, {
    start: "21:00",
    end: "07:00",
  });
  assert.ok(payload.consent_offer_ids.includes("offer_minor_voice_session_v1"));
  assert.ok(!payload.consent_offer_ids.includes("offer_minor_memory_retention_v1"));
  // The weekly summary rides on long-term memory (P0-04 D6); no separate offer.
  assert.ok(!payload.consent_offer_ids.includes("offer_guardian_weekly_summary_v1"));
  assert.ok(!page.data.offers.some((offer) => offer.id === "offer_guardian_weekly_summary_v1"));
  assert.ok(!payload.consent_offer_ids.includes("offer_emergency_contact_v1"));
  assert.equal(readSubjectLabel(page.data.manifest), "小乐");
});

test("minor long-term memory defaults unticked and sends memory_level none", async () => {
  nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
  const page = await bootToMode("parent_for_child");
  const memoryOffer = page.data.offers.find(
    (offer) => offer.id === "offer_minor_memory_retention_v1",
  );
  assert.equal(memoryOffer.checked, false);
  assert.equal(memoryOffer.requiresParentSelfAcceptance, false);
  assert.ok(!page.data.acceptedOfferIds.includes("offer_minor_memory_retention_v1"));

  page.setData({ "form.childNickname": "小乐" });
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  const payload = lastWxRequest.data;
  assert.equal(payload.service_preferences.memory_level, "none");
  assert.ok(!payload.consent_offer_ids.includes("offer_minor_memory_retention_v1"));
});

test("ticking minor long-term memory sends memory_level growth_summary", async () => {
  nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
  const page = await bootToMode("parent_for_child");
  page.setData({ "form.childNickname": "小乐" });
  page.goToReview();
  page.toggleOffer({ currentTarget: { dataset: { id: "offer_minor_memory_retention_v1" } } });
  assert.ok(page.data.acceptedOfferIds.includes("offer_minor_memory_retention_v1"));
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  const payload = lastWxRequest.data;
  assert.equal(payload.service_preferences.memory_level, "growth_summary");
  assert.ok(payload.consent_offer_ids.includes("offer_minor_memory_retention_v1"));

  // 再取消勾选，memory_level 跟着回到 none。
  page.toggleOffer({ currentTarget: { dataset: { id: "offer_minor_memory_retention_v1" } } });
  nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
  await page.submitBinding();
  assert.equal(lastWxRequest.data.service_preferences.memory_level, "none");
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

// The account's guardian links stay empty when a child was only ever declared
// by a binding, so those children come from the binding's own list.
async function withExistingSubjects({ links, candidates }, body) {
  const originalLinks = api.getGuardianLinks;
  const originalCandidates = api.getBindingSubjectCandidates;
  api.getGuardianLinks = links;
  api.getBindingSubjectCandidates = candidates;
  try {
    await body();
  } finally {
    api.getGuardianLinks = originalLinks;
    api.getBindingSubjectCandidates = originalCandidates;
  }
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a child declared by an earlier binding can be reused when no guardian link exists", async () => {
  await withExistingSubjects(
    {
      links: async () => [],
      candidates: async () => [{ personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" }],
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      await settle();
      assert.deepEqual(page.data.existingSubjectOptions, [
        { minorUserId: "person_wangzai", label: "旺仔" },
      ]);

      page.setData({ "form.subjectSource": "existing" });
      page.goToReview();
      assert.equal(page.data.step, "review");
      await page.submitBinding();
      const payload = lastWxRequest.data;
      assert.equal(payload.primary_subject.person_id, "person_wangzai");
      assert.equal(payload.primary_subject.subject_draft, undefined);
      assert.equal(readSubjectLabel(page.data.manifest), "旺仔");
    },
  );
});

test("guardian-linked and binding-declared children are merged without repeats", async () => {
  await withExistingSubjects(
    {
      links: async () => [
        { linkId: "link_1", minorUserId: "person_child", displayName: "小乐", status: "active" },
        { linkId: "link_2", minorUserId: "person_old", displayName: "已撤销", status: "revoked" },
      ],
      candidates: async () => [
        { personId: "person_child", displayName: "小乐", ageBand: "under_14" },
        { personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" },
        { personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" },
      ],
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      await settle();
      assert.deepEqual(page.data.existingSubjectOptions.map((option) => option.minorUserId).sort(), [
        "person_child",
        "person_wangzai",
      ]);
      assert.deepEqual(page.data.existingSubjectOptions.map((option) => option.label).sort(), ["小乐", "旺仔"]);
    },
  );
});

test("a child already chosen keeps its place when the slower lookup adds more", async () => {
  let resolveLinks;
  await withExistingSubjects(
    {
      links: () =>
        new Promise((resolve) => {
          resolveLinks = resolve;
        }),
      candidates: async () => [{ personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" }],
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      await settle();
      assert.deepEqual(page.data.existingSubjectOptions.map((option) => option.minorUserId), ["person_wangzai"]);

      page.setData({ "form.subjectSource": "existing" });
      page.onExistingSubjectChange({ detail: { value: "0" } });
      resolveLinks([
        { linkId: "link_1", minorUserId: "person_child", displayName: "小乐", status: "active" },
      ]);
      await settle();

      assert.deepEqual(page.data.existingSubjectOptions.map((option) => option.minorUserId), [
        "person_wangzai",
        "person_child",
      ]);
      assert.equal(page.data.form.existingSubjectIndex, 0);
      page.goToReview();
      await page.submitBinding();
      assert.equal(lastWxRequest.data.primary_subject.person_id, "person_wangzai");
    },
  );
});

test("a failing lookup leaves the children the other one found", async () => {
  const failure = Object.assign(new Error("HTTP 500"), { status: 500 });
  await withExistingSubjects(
    {
      links: async () => [
        { linkId: "link_1", minorUserId: "person_child", displayName: "小乐", status: "active" },
      ],
      candidates: async () => {
        throw failure;
      },
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      await settle();
      assert.deepEqual(page.data.existingSubjectOptions, [{ minorUserId: "person_child", label: "小乐" }]);
    },
  );
  await withExistingSubjects(
    {
      links: async () => {
        throw failure;
      },
      candidates: async () => [{ personId: "person_wangzai", displayName: "", ageBand: "" }],
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      await settle();
      // A child without a remark still gets a readable entry.
      assert.deepEqual(page.data.existingSubjectOptions, [{ minorUserId: "person_wangzai", label: "孩子 1" }]);
    },
  );
});

test("a late binding-children lookup is ignored after switching away from parent_for_child", async () => {
  let resolveCandidates;
  await withExistingSubjects(
    {
      links: async () => [],
      candidates: () =>
        new Promise((resolve) => {
          resolveCandidates = resolve;
        }),
    },
    async () => {
      nextResponse = successResponse(defaultManifestResponse("parent_for_child"));
      const page = await bootToMode("parent_for_child");
      page.chooseMode({ currentTarget: { dataset: { mode: "self_use" } } });
      assert.equal(page.data.declaredMode, "self_use");

      resolveCandidates([{ personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" }]);
      await settle();

      assert.equal(page.data.declaredMode, "self_use");
      assert.equal(page.data.existingSubjectOptions.length, 0);
    },
  );
});

test("the binding children list is read from the server and tolerates an older backend", async () => {
  nextResponse = successResponse({
    subjects: [
      { person_id: " person_wangzai ", display_name: " 旺仔 ", age_band: "under_14" },
      { person_id: "person_wangzai", display_name: "旺仔", age_band: "under_14" },
      { person_id: "", display_name: "无编号" },
      null,
      { person_id: "person_nameless" },
    ],
  });
  assert.deepEqual(await realGetBindingSubjectCandidates(), [
    { personId: "person_wangzai", displayName: "旺仔", ageBand: "under_14" },
    { personId: "person_nameless", displayName: "", ageBand: "" },
  ]);
  assert.equal(lastWxRequest.method, "GET");
  assert.match(lastWxRequest.url, /\/v1\/device-bindings\/subject-candidates$/);

  nextResponse = successResponse({});
  assert.deepEqual(await realGetBindingSubjectCandidates(), []);

  // A backend that does not have the route yet: nobody to offer, and no error.
  nextResponse = { statusCode: 404, data: { detail: "Not Found" } };
  assert.deepEqual(await realGetBindingSubjectCandidates(), []);

  // Anything else is a real failure for the caller to absorb.
  nextResponse = { statusCode: 500, data: { detail: "boom" } };
  await assert.rejects(realGetBindingSubjectCandidates(), (error) => error.status === 500);
});

test("the bind page tells a parent that an earlier child profile can be reused", () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(
    template,
    /wx:if="\{\{form\.subjectSource === 'new' && existingSubjectOptions\.length > 0\}\}" class="caption">之前建过「\{\{existingSubjectOptions\[0\]\.label\}\}」/,
  );
  assert.match(template, /选「使用已有档案」就能沿用/);
});

test("the finish step shows the companion picked while binding, not a generic robot", async () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const done = template.slice(template.indexOf("<!-- 步骤 5"));
  assert.match(done, /<face-badge role="\{\{personaOptions\[personaIndex\]\.id\}\}" size="xl"/);
  assert.match(done, /陪伴伙伴<\/text><text class="summary-value">\{\{personaOptions\[personaIndex\]\.name\}\}/);
  assert.doesNotMatch(done, /<device-screen/);
  const components = JSON.parse(fs.readFileSync(path.join(root, "pages/bind/index.json"), "utf8")).usingComponents;
  assert.ok(components["face-badge"]);
  assert.equal(components["device-screen"], undefined);

  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady("阿宁");
  const index = page.data.personaOptions.findIndex((option) => option.id === "taoxi");
  assert.ok(index >= 0);
  page.onPersonaSwipe({ detail: { current: index } });
  page.goToReview();
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  // The two bindings the finish card reads.
  const picked = page.data.personaOptions[page.data.personaIndex];
  assert.equal(picked.id, "taoxi");
  assert.equal(picked.name, "桃喜");
});

test("continuing to activation hands the picked companion to the onboarding page", async () => {
  nextResponse = successResponse(defaultManifestResponse("self_use"));
  const page = await bootSelfUseReady("阿宁");
  const index = page.data.personaOptions.findIndex((option) => option.id === "taoxi");
  page.onPersonaSwipe({ detail: { current: index } });
  page.goToReview();
  await page.submitBinding();

  const handedOver = [];
  const previousPages = global.getCurrentPages;
  const previousBack = wx.navigateBack;
  global.getCurrentPages = () => [
    { onBindingCreated: (manifest, extra) => handedOver.push({ manifest, extra }) },
    page,
  ];
  wx.navigateBack = () => {};
  try {
    page.continueActivation();
  } finally {
    if (previousPages === undefined) delete global.getCurrentPages;
    else global.getCurrentPages = previousPages;
    if (previousBack === undefined) delete wx.navigateBack;
    else wx.navigateBack = previousBack;
  }
  assert.equal(handedOver.length, 1);
  assert.equal(handedOver[0].manifest.binding_id, "bd_test_1");
  assert.deepEqual(handedOver[0].extra, { companionId: "taoxi" });
});

test("child_for_parent defaults proxy memory consent off and keeps admin scope", async () => {
  const template = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  assert.match(template, /由你代父母决定/);
  assert.match(template, /由你代父母同意，可随时在小程序里撤回/);
  // 代为同意必须如实表述，不能声称父母本人已经确认。
  assert.doesNotMatch(template, /父母本人已确认|父母已同意/);

  nextResponse = successResponse(defaultManifestResponse("child_for_parent"));
  const page = await bootToMode("child_for_parent");
  assert.equal(page.data.offers.some((offer) => offer.requiresParentSelfAcceptance), false);
  assert.equal(
    page.data.offers.some((offer) => offer.id === "offer_senior_service_acceptance_v1"),
    false,
  );
  const memoryOffer = page.data.offers.find(
    (offer) => offer.id === "offer_senior_memory_retention_v1",
  );
  assert.equal(memoryOffer.checked, false);
  assert.equal(memoryOffer.proxyConsent, true);
  assert.match(memoryOffer.label, /我代父母同意/);

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

test("child_for_parent binder can tick proxy memory consent on the parent's behalf", async () => {
  nextResponse = successResponse(defaultManifestResponse("child_for_parent"));
  const page = await bootToMode("child_for_parent");
  page.setData({ "form.parentNickname": "妈妈" });
  page.goToReview();
  page.toggleOffer({ currentTarget: { dataset: { id: "offer_senior_memory_retention_v1" } } });
  assert.ok(page.data.acceptedOfferIds.includes("offer_senior_memory_retention_v1"));
  await page.submitBinding();
  assert.equal(page.data.step, "done");
  const payload = lastWxRequest.data;
  assert.ok(payload.consent_offer_ids.includes("offer_senior_memory_retention_v1"));
  // 父母模式沿用现有 memory_level 字段与取值（none / personal）。
  assert.equal(payload.service_preferences.memory_level, "personal");
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
