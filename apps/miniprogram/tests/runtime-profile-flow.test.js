const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
const pendingRequests = [];

global.wx = {
  getStorageSync: (key) => storage[key],
  setStorageSync: (key, value) => {
    storage[key] = value;
  },
  removeStorageSync: (key) => {
    delete storage[key];
  },
  request(options) {
    pendingRequests.push(options);
  },
  showToast() {},
  navigateTo() {},
};

let clearCount = 0;
global.getApp = () => ({
  globalData: {
    identity: { user_id: "person_owner", display_name: "主人", account_type: "registered" },
    accessToken: "test-token",
    accessTokenExpiresAt: Date.now() + 3600_000,
    authEpoch: 0,
  },
  clearAuthenticatedIdentity() {
    clearCount += 1;
  },
});

function resolveRequest(index, payload, statusCode = 200) {
  const options = pendingRequests.splice(index, 1)[0];
  assert.ok(options, "缺少可响应的请求");
  options.success({ statusCode, data: payload });
  return options;
}

function currentPaths() {
  return pendingRequests.map((options) =>
    options.url.replace("https://aigcnice.com:8443/memoria-api", ""),
  );
}

function validWireProfile(overrides = {}) {
  const now = Date.now();
  return {
    signature_schema: "runtime-profile-v1",
    runtime_profile_id: "rp_flow",
    device_id: "dev_flow",
    session_id: "ses_flow",
    actor_id: "person_owner",
    binding_id: "bd_flow",
    binding_version: 1,
    active_subject_id: "person_owner",
    subject_revision: 1,
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    speaker_confidence: 0.9,
    service_mode: "adult_companion",
    persona_assignment_id: "pa_flow",
    persona: { persona_id: "starlight", version: 4, relationship_stage: "familiar" },
    policy_bundle_version: "policy-cn-adult-v2",
    capabilities: ["chat", "digital_self_preview"],
    obligations: [],
    policy_receipt_ids: [],
    session_epoch: 1,
    issued_at: new Date(now - 60_000).toISOString(),
    expires_at: new Date(now + 60_000).toISOString(),
    signature: "b".repeat(64),
    ...overrides,
  };
}

function bindDevice() {
  binding.saveBindingManifest(
    canonicalManifest({
      binding_id: "bd_flow",
      device_id: "dev_flow",
      declared_mode: "self_use",
      binding_version: 1,
    }),
  );
}

test("refresh accepts fully idempotent same-epoch response after rebuild", async () => {
  bindDevice();
  // 完全相同的 wire 内容（复用同一对象，避免 issued/expires 毫秒漂移）。
  const wire = validWireProfile({ session_id: "ses_idem", session_epoch: 1 });
  const first = api.getRuntimeProfile("dev_flow", { sessionId: "ses_idem" });
  resolveRequest(0, wire);
  const profile1 = await first;
  assert.equal(profile1.session_epoch, 1);

  // 页面重建后再次拉取：同 session + 同 epoch + 同内容（幂等）必须放行。
  const second = api.getRuntimeProfile("dev_flow", { sessionId: "ses_idem" });
  resolveRequest(0, wire);
  const profile2 = await second;
  assert.equal(profile2.runtime_profile_id, "rp_flow");
});

test("refresh rejects same-epoch different-content response", async () => {
  bindDevice();
  const wire = validWireProfile({ session_id: "ses_diff", session_epoch: 1 });
  const first = api.getRuntimeProfile("dev_flow", { sessionId: "ses_diff" });
  resolveRequest(0, wire);
  const profile1 = await first;
  assert.equal(profile1.session_epoch, 1);

  // 同 epoch 但 canonical 24 字段 + signature 任一安全字段变化：拒绝。
  const mutations = [
    ["runtime_profile_id", "rp_different"],
    ["capabilities", ["chat", "memory_recall_private"]],
    ["obligations", ["DO_NOT_PERSIST"]],
    ["speaker_state", "unconfirmed"],
    ["speaker_confidence", 0.4],
    ["service_mode", "unknown_safe"],
    ["persona", { persona_id: "other", version: 9, relationship_stage: "new" }],
    ["subject_category", "minor"],
    ["age_band", "under_14"],
    ["active_subject_id", "person_child"],
    ["subject_revision", 9],
    ["policy_bundle_version", "policy-cn-minor-v5"],
    ["policy_receipt_ids", ["rc_other"]],
    ["issued_at", new Date(Date.now() - 120_000).toISOString()],
    ["expires_at", new Date(Date.now() + 120_000).toISOString()],
    ["signature", "c".repeat(64)],
  ];
  for (const [key, value] of mutations) {
    const mutated = { ...wire, [key]: value };
    // 结构上保持自洽（否则会在结构校验层 fail，测不到幂等比较）。
    if (key === "speaker_state") {
      mutated.active_subject_id = null;
      mutated.service_mode = "unknown_safe";
      mutated.capabilities = ["chat"];
      mutated.obligations = ["DO_NOT_PERSIST"];
      mutated.speaker_confidence = null;
      mutated.subject_category = "unknown";
      mutated.age_band = "unknown";
    }
    if (key === "subject_category" || key === "age_band") {
      mutated.age_band = "under_14";
      mutated.subject_category = "minor";
      mutated.active_subject_id = "person_child";
      mutated.service_mode = "student_minor";
      mutated.capabilities = ["chat", "tutor"];
    }
    if (key === "service_mode") {
      mutated.capabilities = ["chat"];
      mutated.obligations = ["DO_NOT_PERSIST"];
    }
    const attempt = api.getRuntimeProfile("dev_flow", { sessionId: "ses_diff" });
    resolveRequest(0, mutated);
    const result = await attempt;
    assert.equal(result, null, `同 epoch 修改 ${key} 必须按非幂等拒绝`);
  }

  // 完全幂等的重复响应仍放行（证明是字段级比较而非一律拒绝）。
  const idem = api.getRuntimeProfile("dev_flow", { sessionId: "ses_diff" });
  resolveRequest(0, wire);
  assert.ok((await idem).valid);
});

test("responses from another device/binding/session are rejected and clear the cache", async () => {
  bindDevice(); // dev_flow / bd_flow / v1
  const wire = validWireProfile({ session_id: "ses_ctx_ok", session_epoch: 1 });

  const wrongDevice = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_ok" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_ctx_ok",
      device_id: "dev_other",
      runtime_profile_id: "rp_other_device",
    }),
  );
  assert.equal(await wrongDevice, null, "device_id 不匹配请求设备必须拒绝");

  const wrongBinding = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_ok" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_ctx_ok",
      binding_id: "bd_other",
      runtime_profile_id: "rp_other_binding",
    }),
  );
  assert.equal(await wrongBinding, null, "binding_id 不匹配当前 BindingManifest 必须拒绝");

  const wrongVersion = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_ok" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_ctx_ok",
      binding_version: 99,
      runtime_profile_id: "rp_other_version",
    }),
  );
  assert.equal(await wrongVersion, null, "binding_version 不匹配必须拒绝");

  const wrongSession = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_ok" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_diff",
      runtime_profile_id: "rp_other_session",
    }),
  );
  assert.equal(await wrongSession, null, "session_id 与请求不一致必须拒绝");

  // 上述违规响应都会清理缓存；正确上下文的新响应可以重新建立缓存。
  const ok = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_ok" });
  resolveRequest(0, wire);
  const accepted = await ok;
  assert.equal(accepted.runtime_profile_id, "rp_flow");
  const cached = binding.readCachedRuntimeProfile({
    deviceId: "dev_flow",
    bindingId: "bd_flow",
    bindingVersion: 1,
    sessionId: "ses_ctx_ok",
  });
  assert.equal(cached.runtime_profile_id, "rp_flow");
});

test("setActiveSubject rejects profiles that do not match the binding manifest", async () => {
  bindDevice();
  const switchCall = api.setActiveSubject("ses_switch_bad", { personId: "person_owner" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_switch_bad",
      session_epoch: 1,
      device_id: "dev_evil",
      runtime_profile_id: "rp_evil",
    }),
  );
  assert.equal(await switchCall, null, "切换响应 device 不匹配当前绑定必须拒绝");
  assert.equal(
    binding.readCachedRuntimeProfile({
      deviceId: "dev_flow",
      bindingId: "bd_flow",
      bindingVersion: 1,
      sessionId: "ses_switch_bad",
    }),
    null,
  );
});

test("setActiveSubject requires strict epoch increase", async () => {
  bindDevice();
  const refresh = api.getRuntimeProfile("dev_flow", { sessionId: "ses_switch" });
  resolveRequest(0, validWireProfile({ session_id: "ses_switch", session_epoch: 1 }));
  await refresh;

  // 切换返回同 epoch：拒绝。
  const stale = api.setActiveSubject("ses_switch", { personId: "person_owner" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_switch",
      session_epoch: 1,
      runtime_profile_id: "rp_switch_stale",
      signature: "d".repeat(64),
    }),
  );
  assert.equal(await stale, null);

  // 切换返回 epoch 2：接受并写缓存。
  const next = api.setActiveSubject("ses_switch", { personId: "person_owner" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_switch",
      session_epoch: 2,
      runtime_profile_id: "rp_switch_ok",
      signature: "e".repeat(64),
    }),
  );
  const accepted = await next;
  assert.equal(accepted.session_epoch, 2);
  assert.equal(accepted.runtime_profile_id, "rp_switch_ok");
  const cached = binding.readCachedRuntimeProfile({
    deviceId: "dev_flow",
    bindingId: "bd_flow",
    bindingVersion: 1,
    sessionId: "ses_switch",
  });
  assert.equal(cached.session_epoch, 2);
});

test("refresh vs switch out-of-order: late refresh response is dropped", async () => {
  bindDevice();
  const refresh = api.getRuntimeProfile("dev_flow", { sessionId: "ses_race" });
  const switchCall = api.setActiveSubject("ses_race", { personId: "person_owner" });
  assert.deepEqual(currentPaths(), [
    "/v1/devices/dev_flow/runtime-profile?session_id=ses_race",
    "/v1/sessions/ses_race/active-subject",
  ]);

  // 切换先返回（epoch 2），refresh 后到（epoch 1）：晚到响应必须丢弃。
  resolveRequest(
    1,
    validWireProfile({
      session_id: "ses_race",
      session_epoch: 2,
      runtime_profile_id: "rp_race_new",
      signature: "f".repeat(64),
    }),
  );
  const switched = await switchCall;
  assert.equal(switched.session_epoch, 2);

  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_race",
      session_epoch: 1,
      runtime_profile_id: "rp_race_old",
      signature: "g".repeat(64),
    }),
  );
  assert.equal(await refresh, null, "晚到的 refresh 响应应被丢弃");

  const cached = binding.readCachedRuntimeProfile({
    deviceId: "dev_flow",
    bindingId: "bd_flow",
    bindingVersion: 1,
    sessionId: "ses_race",
  });
  assert.equal(cached.runtime_profile_id, "rp_race_new", "旧响应不得覆盖新响应");
});

test("request generation is isolated per device/session context", async () => {
  bindDevice();
  const refreshA = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_a" });
  const refreshB = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_b" });
  // B 先返回、A 后返回：不同 session 互不取消。
  resolveRequest(
    1,
    validWireProfile({ session_id: "ses_ctx_b", runtime_profile_id: "rp_b" }),
  );
  const profileB = await refreshB;
  assert.equal(profileB.runtime_profile_id, "rp_b");
  resolveRequest(
    0,
    validWireProfile({ session_id: "ses_ctx_a", runtime_profile_id: "rp_a" }),
  );
  const profileA = await refreshA;
  assert.equal(profileA.runtime_profile_id, "rp_a");

  // 同一 session 内：switch 会 supersede 同 session 的 refresh。
  const refreshSame = api.getRuntimeProfile("dev_flow", { sessionId: "ses_ctx_a" });
  const switchSame = api.setActiveSubject("ses_ctx_a", { personId: "person_owner" });
  resolveRequest(
    1,
    validWireProfile({
      session_id: "ses_ctx_a",
      session_epoch: 3,
      runtime_profile_id: "rp_a3",
    }),
  );
  await switchSame;
  resolveRequest(
    0,
    validWireProfile({ session_id: "ses_ctx_a", session_epoch: 1, runtime_profile_id: "rp_a" }),
  );
  assert.equal(await refreshSame, null);
});

test("createMiniProgramSession carries bound device_id from the validated manifest", async () => {
  bindDevice();
  const request = api.createMiniProgramSession({
    userId: "person_owner",
    interactionMode: "companion",
    sessionFocus: "chat",
  });
  const options = resolveRequest(0, {
    session_id: "ses_new_1",
    voice_backend: "cascade",
    media_gateway: { websocket_url: "wss://gateway.example/m", ticket: "ticket-1" },
  });
  const session = await request;
  assert.equal(session.session_id, "ses_new_1");
  assert.equal(options.data.client.device_id, "dev_flow");
  assert.equal(options.data.client.platform, "miniprogram");
  assert.equal(options.data.client.session_scope, undefined, "有绑定时不需要 unknown_safe 声明");
  assert.equal(options.data.user_id, "person_owner");
  assert.ok(!JSON.stringify(options.data).includes("policy_version"));
  assert.ok(!JSON.stringify(options.data).includes("_accepted"));
});

test("createMiniProgramSession without binding is explicitly unknown_safe", async () => {
  binding.clearBindingManifest();
  binding.clearCachedRuntimeProfile();
  const request = api.createMiniProgramSession({
    userId: "person_owner",
    interactionMode: "companion",
    sessionFocus: "chat",
  });
  const options = resolveRequest(0, {
    session_id: "ses_new_2",
    voice_backend: "cascade",
    media_gateway: { websocket_url: "wss://gateway.example/m", ticket: "ticket-2" },
  });
  await request;
  assert.equal(options.data.client.device_id, null);
  assert.equal(options.data.client.session_scope, "unknown_safe");
});

test("logout clears the cached runtime profile", async () => {
  bindDevice();
  const request = api.getRuntimeProfile("dev_flow", { sessionId: "ses_logout" });
  resolveRequest(
    0,
    validWireProfile({ session_id: "ses_logout", runtime_profile_id: "rp_logout" }),
  );
  await request;
  assert.ok(
    binding.readCachedRuntimeProfile({
      deviceId: "dev_flow",
      bindingId: "bd_flow",
      bindingVersion: 1,
      sessionId: "ses_logout",
    }),
  );
  api.logoutLocal();
  assert.equal(
    binding.readCachedRuntimeProfile({
      deviceId: "dev_flow",
      bindingId: "bd_flow",
      bindingVersion: 1,
      sessionId: "ses_logout",
    }),
    null,
  );
  assert.equal(clearCount >= 1, true);
});

test("logout clears in-memory epoch floors and request seqs", async () => {
  bindDevice();
  const wire = validWireProfile({ session_id: "ses_mem", session_epoch: 1 });
  const first = api.getRuntimeProfile("dev_flow", { sessionId: "ses_mem" });
  resolveRequest(0, wire);
  await first;
  api.logoutLocal();
  // 内存 floor 清空后，同 session 同 epoch 不同内容也应被接受（而非被旧 floor 拒绝）。
  const again = api.getRuntimeProfile("dev_flow", { sessionId: "ses_mem" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_mem",
      session_epoch: 1,
      runtime_profile_id: "rp_mem_after_logout",
    }),
  );
  const profile = await again;
  assert.equal(profile.runtime_profile_id, "rp_mem_after_logout");
});

test("auth 401 clears in-memory runtime profile guards", async () => {
  bindDevice();
  const wire = validWireProfile({ session_id: "ses_401", session_epoch: 1 });
  const first = api.getRuntimeProfile("dev_flow", { sessionId: "ses_401" });
  resolveRequest(0, wire);
  await first;

  const failing = api.getRuntimeProfile("dev_flow", { sessionId: "ses_401" });
  const options = pendingRequests.splice(0, 1)[0];
  options.success({ statusCode: 401, data: { detail: { code: "unauthorized" } } });
  await assert.rejects(failing);

  // 401 清理后同 session 同 epoch 不同内容应被接受；旧 floor 残留会被误拒。
  const again = api.getRuntimeProfile("dev_flow", { sessionId: "ses_401" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_401",
      session_epoch: 1,
      runtime_profile_id: "rp_after_401",
    }),
  );
  const profile = await again;
  assert.equal(profile.runtime_profile_id, "rp_after_401");
});

test("rebinding to a new manifest version clears in-memory guards", async () => {
  bindDevice(); // bd_flow v1
  const wire = validWireProfile({ session_id: "ses_rebind", session_epoch: 1 });
  const first = api.getRuntimeProfile("dev_flow", { sessionId: "ses_rebind" });
  resolveRequest(0, wire);
  await first;

  const rebind = api.createDeviceBinding({
    claim_id: "claim-2",
    onboarding_session_id: "onb-2",
    declared_mode: "self_use",
    account_owner_person_id: "person_owner",
    primary_subject: { person_id: "person_owner", relationship: "self" },
    persona_selection: "starlight",
    service_preferences: {},
    consent_offer_ids: [],
  });
  resolveRequest(
    0,
    canonicalManifest({
      binding_id: "bd_flow",
      device_id: "dev_flow",
      binding_version: 2,
    }),
  );
  const manifest = await rebind;
  assert.equal(manifest.binding_version, 2);

  // 旧 floor 若残留会拒绝“同 session 同 epoch 不同内容”；清理后应接受。
  const again = api.getRuntimeProfile("dev_flow", { sessionId: "ses_rebind" });
  resolveRequest(
    0,
    validWireProfile({
      session_id: "ses_rebind",
      session_epoch: 1,
      binding_version: 2,
      runtime_profile_id: "rp_rebound",
    }),
  );
  const profile = await again;
  assert.equal(profile.runtime_profile_id, "rp_rebound");
});

test("requireRuntimeCapability gates all five capabilities fail-closed", async () => {
  bindDevice();
  const five = [
    "voice_profile_create",
    "digital_self_preview",
    "guardian_summary_view",
    "raw_audio_retention",
    "memory_recall_private",
  ];
  const requests = five.map((capability) =>
    api.requireRuntimeCapability(capability, { sessionId: `ses_gate_${capability}` }),
  );
  for (let index = 0; index < five.length; index += 1) {
    resolveRequest(
      0,
      validWireProfile({
        session_id: `ses_gate_${five[index]}`,
        runtime_profile_id: `rp_gate_${index}`,
        capabilities: index === 1 ? ["chat", "digital_self_preview"] : ["chat"],
      }),
    );
  }
  const results = await Promise.all(requests);
  for (let index = 0; index < five.length; index += 1) {
    if (index === 1) {
      assert.equal(results[index].allowed, true, `${five[index]} 应放行`);
    } else {
      assert.equal(results[index].allowed, false, `${five[index]} 未授权应拒绝`);
      assert.equal(results[index].reason, "capability_missing");
    }
  }
});

test("requireRuntimeCapability denies every capability without a binding or profile", async () => {
  binding.clearBindingManifest();
  for (const capability of binding.SENSITIVE_CAPABILITIES) {
    const result = await api.requireRuntimeCapability(capability, { sessionId: "ses_none" });
    assert.equal(result.allowed, false);
    assert.equal(result.reason, "no_binding");
  }

  bindDevice();
  const denied = api.requireRuntimeCapability("memory_recall_private", {
    sessionId: "ses_bad",
  });
  resolveRequest(0, { service_mode: "adult_companion" }); // 非法结构 -> invalid_profile
  const result = await denied;
  assert.equal(result.allowed, false);
  assert.equal(result.reason, "invalid_profile");
});

test("refresh failure fails closed through requireRuntimeCapability", async () => {
  bindDevice();
  const denied = api.requireRuntimeCapability("memory_recall_private", {
    sessionId: "ses_net",
  });
  const options = pendingRequests.splice(0, 1)[0];
  options.success({ statusCode: 500, data: { detail: "boom" } });
  const result = await denied;
  assert.equal(result.allowed, false);
  assert.equal(result.reason, "unavailable");
});
