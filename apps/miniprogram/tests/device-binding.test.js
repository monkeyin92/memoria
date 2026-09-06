const assert = require("node:assert/strict");
const test = require("node:test");

const binding = require("../utils/device-binding");
const contracts = require("../utils/multi-subject-contracts");
const { canonicalManifest } = require("./manifest-fixtures");
const subjectLabel = require("../utils/subject-label");

function validRequest(overrides = {}) {
  return {
    claim_id: "claim_01",
    onboarding_session_id: "onb_01",
    declared_mode: "parent_for_child",
    account_owner_person_id: "person_owner",
    primary_subject: {
      person_id: "new",
      relationship: "guardian_of",
      subject_draft: { display_name: "小乐", age_band: "under_14" },
    },
    persona_selection: "starlight",
    service_preferences: {
      tutor_enabled: true,
      english_practice_enabled: true,
      memory_level: "growth_summary",
      max_session_minutes: 30,
      quiet_hours: { start: "21:00", end: "07:00" },
    },
    consent_offer_ids: [
      "offer_minor_voice_session_v1",
      "offer_minor_memory_retention_v1",
      "offer_guardian_weekly_summary_v1",
    ],
    ...overrides,
  };
}

test("builds the documented payload for parent_for_child", () => {
  const payload = binding.buildBindingRequest(validRequest());
  assert.deepEqual(payload, {
    claim_id: "claim_01",
    onboarding_session_id: "onb_01",
    declared_mode: "parent_for_child",
    account_owner_person_id: "person_owner",
    primary_subject: {
      person_id: "new",
      relationship: "guardian_of",
      subject_draft: { display_name: "小乐", age_band: "under_14" },
    },
    persona_selection: "starlight",
    service_preferences: {
      tutor_enabled: true,
      english_practice_enabled: true,
      memory_level: "growth_summary",
      max_session_minutes: 30,
      quiet_hours: { start: "21:00", end: "07:00" },
    },
    consent_offer_ids: [
      "offer_minor_voice_session_v1",
      "offer_minor_memory_retention_v1",
      "offer_guardian_weekly_summary_v1",
    ],
  });
});

test("self_use uses the owner person and no draft", () => {
  const payload = binding.buildBindingRequest(
    validRequest({
      declared_mode: "self_use",
      primary_subject: { person_id: "person_owner", relationship: "self" },
      service_preferences: { memory_level: "personal", interview_frequency: "low" },
      consent_offer_ids: ["offer_self_memory_retention_v1", "offer_self_voice_profile_v1"],
    }),
  );
  assert.equal(payload.primary_subject.person_id, "person_owner");
  assert.equal(payload.primary_subject.relationship, "self");
  assert.equal(payload.primary_subject.subject_draft, undefined);
  assert.equal(payload.service_preferences.memory_level, "personal");
});

test("child_for_parent keeps admin scope minimal and memory off by default", () => {
  const payload = binding.buildBindingRequest(
    validRequest({
      declared_mode: "child_for_parent",
      primary_subject: {
        person_id: "new",
        relationship: "child_of",
        subject_draft: { display_name: "妈妈", age_band: "adult" },
      },
      service_preferences: {
        speech_speed: "slow",
        memory_level: "none",
        admin_visibility: "device_status",
      },
      consent_offer_ids: ["offer_admin_device_management_v1", "offer_senior_anti_fraud_v1"],
    }),
  );
  assert.equal(payload.primary_subject.relationship, "child_of");
  assert.equal(payload.service_preferences.memory_level, "none");
  assert.equal(payload.service_preferences.admin_visibility, "device_status");
});

test("family_shared binds the owner as a family member", () => {
  const payload = binding.buildBindingRequest(
    validRequest({
      declared_mode: "family_shared",
      primary_subject: { person_id: "person_owner", relationship: "family_member_of" },
      service_preferences: { memory_level: "family_shared", shared_persona_enabled: true },
      consent_offer_ids: ["offer_family_space_v1"],
    }),
  );
  assert.equal(payload.primary_subject.relationship, "family_member_of");
  assert.equal(payload.service_preferences.memory_level, "family_shared");
});

test("rejects fake authorization fields fail-closed", () => {
  for (const key of [
    "policy_version",
    "consent_policy_version",
    "parent_accepted",
    "owner_authorized",
    "consent_granted",
  ]) {
    assert.throws(
      () => binding.buildBindingRequest(validRequest({ [key]: true })),
      /禁止客户端提交授权声明字段/,
      `${key} 应被拒绝`,
    );
  }
});

test("rejects unknown request keys and invalid declared modes", () => {
  assert.throws(
    () => binding.buildBindingRequest(validRequest({ service_profile_version: "x" })),
    /绑定请求不允许字段/,
  );
  assert.throws(
    () => binding.buildBindingRequest(validRequest({ declared_mode: "child_primary" })),
    /declared_mode 取值无效/,
  );
  assert.throws(() => binding.buildBindingRequest({}), /claim_id 不能为空/);
});

test("rejects unknown, mismatched, and parent-self-acceptance consent offers", () => {
  assert.throws(
    () => binding.buildBindingRequest(validRequest({ consent_offer_ids: ["offer_fake_v1"] })),
    /未知的 consent offer/,
  );
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({ consent_offer_ids: ["offer_self_digital_self_v1"] }),
      ),
    /不适用于/,
  );
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({
          declared_mode: "child_for_parent",
          primary_subject: {
            person_id: "new",
            relationship: "child_of",
            subject_draft: { display_name: "妈妈", age_band: "adult" },
          },
          service_preferences: { memory_level: "none" },
          consent_offer_ids: ["offer_senior_service_acceptance_v1"],
        }),
      ),
    /需要父母本人确认/,
  );
});

test("rejects unknown service preference keys and invalid values", () => {
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({ service_preferences: { policy_bundle_version: "x" } }),
      ),
    /service_preferences 不允许字段/,
  );
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({ service_preferences: { max_session_minutes: 240 } }),
      ),
    /5 到 120/,
  );
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({
          service_preferences: { quiet_hours: { start: "21:00", end: "oops" } },
        }),
      ),
    /HH:MM/,
  );
});

test("validates subject drafts against mode age bands", () => {
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({
          primary_subject: {
            person_id: "new",
            relationship: "guardian_of",
            subject_draft: { display_name: "小乐", age_band: "adult" },
          },
        }),
      ),
    /不适用于 parent_for_child/,
  );
  assert.throws(
    () =>
      binding.buildBindingRequest(
        validRequest({
          primary_subject: {
            person_id: "person_child",
            relationship: "guardian_of",
            subject_draft: { display_name: "小乐", age_band: "under_14" },
          },
        }),
      ),
    /person_id=new/,
  );
});

test("consent catalogs keep sensitive capabilities off by default", () => {
  const selfOffers = binding.consentOffersFor("self_use");
  const byId = Object.fromEntries(selfOffers.map((offer) => [offer.id, offer]));
  assert.equal(byId.offer_self_memory_retention_v1.defaultChecked, true);
  assert.equal(byId.offer_self_voice_profile_v1.defaultChecked, true);
  assert.equal(byId.offer_self_raw_audio_v1.defaultChecked, false);
  assert.equal(byId.offer_self_voice_clone_v1.defaultChecked, false);
  assert.equal(byId.offer_self_digital_self_v1.defaultChecked, false);
  assert.equal(byId.offer_self_legacy_v1.defaultChecked, false);

  const seniorOffers = binding.consentOffersFor("child_for_parent");
  const seniorAcceptance = seniorOffers.find(
    (offer) => offer.id === "offer_senior_service_acceptance_v1",
  );
  assert.equal(seniorAcceptance.requiresParentSelfAcceptance, true);
});

function validRuntimeProfile(overrides = {}) {
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
    subject_revision: 3,
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    speaker_confidence: 0.96,
    service_mode: "adult_companion",
    persona_assignment_id: "pa_1",
    persona: { persona_id: "starlight", version: 4, relationship_stage: "familiar" },
    policy_bundle_version: "policy-cn-adult-v2",
    capabilities: ["chat", "digital_self_preview"],
    obligations: ["REQUIRE_SPEAKER_CONFIRMATION"],
    policy_receipt_ids: ["rc_1"],
    session_epoch: 1,
    issued_at: new Date(now - 60_000).toISOString(),
    expires_at: new Date(now + 60_000).toISOString(),
    signature: "a".repeat(64),
    ...overrides,
  };
}

function validUnknownSafeProfile(overrides = {}) {
  return validRuntimeProfile({
    service_mode: "unknown_safe",
    speaker_state: "unconfirmed",
    active_subject_id: null,
    subject_category: "unknown",
    age_band: "unknown",
    capabilities: ["chat", "english_practice"],
    obligations: ["DO_NOT_PERSIST"],
    ...overrides,
  });
}

test("null or non-object runtime profile fails closed", () => {
  for (const payload of [null, undefined, "string", 42, []]) {
    const profile = binding.normalizeRuntimeProfile(payload);
    assert.equal(profile.valid, false);
    assert.equal(profile.service_mode, "unknown_safe");
    assert.equal(profile.speaker_state, "unknown");
    assert.deepEqual(profile.capabilities, []);
    assert.equal(profile.degraded, true);
  }
});

test("missing any required runtime profile field fails closed", () => {
  const required = [
    "signature_schema",
    "runtime_profile_id",
    "device_id",
    "session_id",
    "actor_id",
    "binding_id",
    "binding_version",
    "active_subject_id",
    "subject_revision",
    "subject_category",
    "age_band",
    "speaker_state",
    "speaker_confidence",
    "service_mode",
    "persona_assignment_id",
    "persona",
    "policy_bundle_version",
    "capabilities",
    "obligations",
    "policy_receipt_ids",
    "session_epoch",
    "issued_at",
    "expires_at",
    "signature",
  ];
  for (const key of required) {
    const payload = validRuntimeProfile();
    delete payload[key];
    const profile = binding.normalizeRuntimeProfile(payload);
    assert.equal(profile.valid, false, `缺少 ${key} 应 fail closed`);
    assert.deepEqual(profile.capabilities, []);
    assert.equal(profile.service_mode, "unknown_safe");
  }
});

test("extra fields on runtime profile fail closed", () => {
  for (const extra of [
    { profile_id: "rp_old" },
    { admin: true },
    { can_use_adult: true },
    { policy_version: "x" },
  ]) {
    const profile = binding.normalizeRuntimeProfile(validRuntimeProfile(extra));
    assert.equal(profile.valid, false, `额外字段 ${Object.keys(extra)[0]} 应 fail closed`);
    assert.deepEqual(profile.capabilities, []);
  }
});

test("expired or future-issued runtime profiles fail closed", () => {
  const now = Date.now();
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ expires_at: new Date(now - 1).toISOString() }),
    ).valid,
    false,
    "已过期应 fail closed",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: new Date(now + 60_000).toISOString(),
        expires_at: new Date(now + 120_000).toISOString(),
      }),
    ).valid,
    false,
    "issued_at 晚于当前时间应 fail closed",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: new Date(now + 60_000).toISOString(),
        expires_at: new Date(now + 60_000).toISOString(),
      }),
    ).valid,
    false,
    "expires_at <= issued_at 应 fail closed",
  );
});

test("signature shape errors fail closed (TLS boundary + envelope only, no HMAC key)", () => {
  for (const signature of [undefined, "", "ABC", "z".repeat(64), "a".repeat(63), 42, null]) {
    const profile = binding.normalizeRuntimeProfile(validRuntimeProfile({ signature }));
    assert.equal(profile.valid, false, `signature=${String(signature).slice(0, 8)} 应 fail closed`);
    assert.deepEqual(profile.capabilities, []);
  }
});

test("string/null/0 binding_version fails closed", () => {
  for (const binding_version of ["2", null, 0, -1, 1.5, true]) {
    const profile = binding.normalizeRuntimeProfile(validRuntimeProfile({ binding_version }));
    assert.equal(profile.valid, false, `binding_version=${String(binding_version)} 应 fail closed`);
  }
});

test("session_epoch 0 or regression fails closed", () => {
  for (const session_epoch of [0, -1, 1.5, "1"]) {
    const profile = binding.normalizeRuntimeProfile(validRuntimeProfile({ session_epoch }));
    assert.equal(profile.valid, false, `session_epoch=${String(session_epoch)} 应 fail closed`);
  }
});

test("illegal speaker/subject relationships fail closed", () => {
  const cases = [
    { name: "confirmed 却无 active_subject_id", overrides: { active_subject_id: null } },
    { name: "unconfirmed 却带 active_subject_id", overrides: { speaker_state: "unconfirmed", active_subject_id: "person_child", service_mode: "unknown_safe", subject_category: "unknown", age_band: "unknown", capabilities: ["chat"], obligations: ["DO_NOT_PERSIST"] } },
    { name: "adult category 与 under_14 矛盾", overrides: { age_band: "under_14" } },
    { name: "unknown category 与 adult band 矛盾", overrides: { subject_category: "unknown", age_band: "adult", active_subject_id: null, speaker_state: "unconfirmed", service_mode: "unknown_safe", capabilities: ["chat"], obligations: ["DO_NOT_PERSIST"] } },
    { name: "minor category 与 adult band 矛盾", overrides: { subject_category: "minor", age_band: "adult" } },
    { name: "unconfirmed 携带 memory_recall_private", overrides: { speaker_state: "unconfirmed", active_subject_id: null, subject_category: "unknown", age_band: "unknown", service_mode: "unknown_safe", capabilities: ["chat", "memory_recall_private"], obligations: ["DO_NOT_PERSIST"] } },
    { name: "unknown category 携带 raw_audio_retention", overrides: { subject_category: "unknown", age_band: "unknown", active_subject_id: null, speaker_state: "unconfirmed", service_mode: "unknown_safe", capabilities: ["chat", "raw_audio_retention"], obligations: ["DO_NOT_PERSIST"] } },
    { name: "unconfirmed 却 adult_companion 模式", overrides: { speaker_state: "unconfirmed", active_subject_id: null, subject_category: "unknown", age_band: "unknown", capabilities: ["chat"], obligations: ["DO_NOT_PERSIST"] } },
  ];
  for (const item of cases) {
    const profile = binding.normalizeRuntimeProfile(validRuntimeProfile(item.overrides));
    assert.equal(profile.valid, false, `${item.name} 应 fail closed`);
    assert.equal(profile.service_mode, "unknown_safe");
    assert.deepEqual(profile.capabilities, []);
  }
});

test("unknown_safe allows only chat/english_practice and requires DO_NOT_PERSIST", () => {
  assert.equal(
    binding.normalizeRuntimeProfile(
      validUnknownSafeProfile({ capabilities: ["chat", "tutor"] }),
    ).valid,
    false,
    "unknown_safe 不允许 tutor",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validUnknownSafeProfile({ capabilities: ["chat", "digital_self_preview"] }),
    ).valid,
    false,
    "unknown_safe 不允许敏感能力",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(validUnknownSafeProfile({ obligations: [] })).valid,
    false,
    "unknown_safe 必须携带 DO_NOT_PERSIST",
  );
  const ok = binding.normalizeRuntimeProfile(validUnknownSafeProfile());
  assert.equal(ok.valid, true);
  assert.equal(ok.degraded, true);
});

test("loose datetime strings and whitespace ids fail closed", () => {
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ issued_at: "2026-08-09" }),
    ).valid,
    false,
    "无时区日期应拒绝",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ issued_at: "2026-08-09T10:00:00" }),
    ).valid,
    false,
    "无时区 datetime 应拒绝",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ device_id: "  dev_1" }),
    ).valid,
    false,
    "首尾空白 device_id 应拒绝",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ session_id: "   " }),
    ).valid,
    false,
    "纯空白 session_id 应拒绝",
  );
  // RFC3339 §5.6：lowercase t/z 与 canonical 合同语义一致，必须放行。
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: "2024-02-29t10:00:00z",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      }),
    ).valid,
    true,
    "lowercase t/z 应放行",
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: "2024-02-29t10:00:00+08:00",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      }),
    ).valid,
    true,
    "lowercase t + 数字 offset 应放行",
  );
  // 空格分隔仍拒绝（RFC3339 不允许 "2026-08-09 10:00:00Z"）。
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({ issued_at: "2024-02-29 10:00:00Z" }),
    ).valid,
    false,
    "空格分隔必须拒绝",
  );
});

test("calendar truth: nonexistent dates and out-of-range offsets fail closed", () => {
  const now = Date.now();
  function offsetString(date, offsetMinutes = 480) {
    const wall = new Date(date + offsetMinutes * 60_000);
    const pad = (n) => String(n).padStart(2, "0");
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const abs = Math.abs(offsetMinutes);
    return `${wall.getUTCFullYear()}-${pad(wall.getUTCMonth() + 1)}-${pad(wall.getUTCDate())}T${pad(wall.getUTCHours())}:${pad(wall.getUTCMinutes())}:${pad(wall.getUTCSeconds())}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
  }
  const cases = [
    { value: "2026-02-30T10:00:00Z", name: "2 月 30 日不存在" },
    { value: "2026-02-29T10:00:00Z", name: "非闰年 2 月 29 日" },
    { value: "2026-04-31T10:00:00Z", name: "4 月 31 日不存在" },
    { value: "2026-08-09T24:00:00Z", name: "24:00 越界" },
    { value: "2026-08-09T10:60:00Z", name: "分钟越界" },
    { value: "2026-08-09T10:00:61Z", name: "秒越界" },
    { value: "2026-08-09T10:00:00+14:30", name: "offset 超过真实时区上限" },
    { value: "2026-08-09T10:00:00+99:99", name: "offset 格式越界" },
    { value: "2026-08-09T10:00:00+00:60", name: "offset 分钟越界" },
    { value: "2026-13-01T10:00:00Z", name: "月份越界" },
  ];
  for (const item of cases) {
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({ issued_at: item.value, expires_at: new Date(now + 60_000).toISOString() }),
    );
    assert.equal(profile.valid, false, `${item.name}（${item.value}）应 fail closed`);
  }
  // 合法值仍必须通过：闰年 2 月 29、真实 offset。
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: "2024-02-29T10:00:00Z",
        expires_at: new Date(now + 60_000).toISOString(),
      }),
    ).valid,
    true,
  );
  assert.equal(
    binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        issued_at: offsetString(now - 7_200_000),
        expires_at: offsetString(now + 3_600_000),
      }),
    ).valid,
    true,
  );
});

function minorWireProfile(overrides = {}) {
  return validRuntimeProfile({
    active_subject_id: "person_child",
    subject_revision: 1,
    subject_category: "minor",
    age_band: "under_14",
    speaker_state: "confirmed",
    service_mode: "student_minor",
    capabilities: ["chat", "tutor"],
    ...overrides,
  });
}

test("minor profile rejects every minor-forbidden capability (full matrix)", () => {
  const forbidden = binding.MINOR_FORBIDDEN_CAPABILITIES;
  assert.deepEqual(forbidden, [
    "voice_clone_use",
    "digital_self_preview",
    "legacy_grant_create",
    "device_ownership_transfer",
  ]);
  for (const capability of contracts.CAPABILITY_VALUES) {
    const profile = binding.normalizeRuntimeProfile(
      minorWireProfile({
        capabilities: capability === "chat" ? ["chat"] : ["chat", capability],
      }),
    );
    if (forbidden.includes(capability)) {
      assert.equal(profile.valid, false, `minor 携带 ${capability} 应 fail closed`);
    } else {
      assert.equal(profile.valid, true, `minor 携带 ${capability} 应允许`);
    }
  }
});

test("minor profile structurally accepts child-available sensitive capabilities", () => {
  // §4.2：声纹识别（主体分流）、长期个人记忆（分项同意/最小化）、
  // guardian_summary（actor=guardian, subject=minor）对儿童明确可用，
  // 客户端结构层不得冻结为禁止；最终允许仍由服务端权威 profile 决定。
  const allowedOnMinor = [
    contracts.Capability.VoiceProfileCreate,
    contracts.Capability.MemoryRecallPrivate,
    contracts.Capability.GuardianSummaryView,
  ];
  for (const capability of allowedOnMinor) {
    const profile = binding.normalizeRuntimeProfile(
      minorWireProfile({ capabilities: ["chat", capability] }),
    );
    assert.equal(
      profile.valid,
      true,
      `minor 携带 ${capability} 必须通过结构校验（允许与否由服务端决定）`,
    );
    assert.ok(profile.capabilities.includes(capability), "能力必须保留，不擅自裁剪");
  }
});

test("minor profile rejects every adult/senior service mode (full matrix)", () => {
  for (const mode of contracts.SERVICE_MODE_VALUES) {
    const profile = binding.normalizeRuntimeProfile(
      minorWireProfile({
        service_mode: mode,
        capabilities: mode === "unknown_safe" ? ["chat"] : ["chat", "tutor"],
        obligations: mode === "unknown_safe" ? ["DO_NOT_PERSIST"] : [],
      }),
    );
    if (binding.MINOR_FORBIDDEN_MODES.includes(mode)) {
      assert.equal(profile.valid, false, `minor + ${mode} 应 fail closed`);
    } else {
      assert.equal(profile.valid, true, `minor + ${mode} 应允许`);
    }
  }
});

test("adult profile keeps adult modes and capabilities open", () => {
  for (const mode of contracts.SERVICE_MODE_VALUES) {
    if (mode === "student_minor") continue;
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        service_mode: mode,
        capabilities: ["chat"],
        obligations: mode === "unknown_safe" ? ["DO_NOT_PERSIST"] : [],
      }),
    );
    assert.equal(profile.valid, true, `adult + ${mode} 应允许`);
  }
  for (const capability of binding.MINOR_FORBIDDEN_CAPABILITIES) {
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({ capabilities: [capability] }),
    );
    assert.equal(profile.valid, true, `adult + ${capability} 应允许`);
  }
});

test("tampered binding manifest is cleared on read and yields no_binding", () => {
  withWxStorage({}, () => {
    binding.saveBindingManifest(canonicalManifest());
    assert.ok(binding.readBindingManifest());
    // 本地结构篡改：缺字段 / 塞额外字段 / 非法枚举 / 非法角色 / 非法版本 / 非法日期。
    for (const tamper of [
      (manifest) => {
        const copy = { ...manifest };
        delete copy.account_owner_id;
        return copy;
      },
      (manifest) => ({ ...manifest, can_use_adult: true }),
      (manifest) => ({ ...manifest, declared_mode: "child_primary" }),
      (manifest) => ({ ...manifest, roles: [{ person_id: "x", role: "super_admin", permissions: [] }] }),
      (manifest) => ({ ...manifest, binding_version: "2" }),
      (manifest) => ({ ...manifest, created_at: "2026-02-30T10:00:00Z" }),
      (manifest) => ({ ...manifest, device_id: "  dev_evil" }),
    ]) {
      binding.saveBindingManifest(canonicalManifest());
      global.wx.setStorageSync(
        "memoria:miniprogram:device:binding-manifest",
        tamper(binding.readBindingManifest()),
      );
      assert.equal(binding.readBindingManifest(), null, "篡改清单读取必须返回 null");
      assert.equal(
        global.wx.getStorageSync("memoria:miniprogram:device:binding-manifest"),
        undefined,
        "篡改清单必须被清理",
      );
    }
  });
});

test("config action seam fails closed with explainable messages", () => {
  for (const action of ["guardian_manage", "raw_audio_consent"]) {
    const gate = binding.configActionGate(action);
    assert.equal(gate.allowed, false);
    assert.equal(gate.reason, "config_seam_pending");
    assert.ok(gate.message.includes("尚未接入"), `${action} 必须说明接口未接入`);
    assert.ok(!/年龄/.test(gate.message) || gate.message.includes("不会按本地年龄放开"));
  }
});

test("valid adult profile is accepted with capabilities intact", () => {
  const profile = binding.normalizeRuntimeProfile(validRuntimeProfile());
  assert.equal(profile.valid, true);
  assert.equal(profile.degraded, false);
  assert.equal(profile.runtime_profile_id, "rp_1");
  assert.equal(profile.binding_version, 2);
  assert.equal(profile.session_epoch, 1);
  assert.equal(profile.active_subject_id, "person_owner");
  assert.deepEqual(profile.capabilities, ["chat", "digital_self_preview"]);
  assert.deepEqual(profile.persona, {
    persona_id: "starlight",
    version: 4,
    relationship_stage: "familiar",
  });
  assert.equal(binding.hasCapability(profile, "digital_self_preview"), true);
  assert.equal(binding.hasCapability(profile, "guardian_summary_view"), false);
});

test("sensitive entries are driven by capabilities, not local inference", () => {
  const profile = binding.normalizeRuntimeProfile(
    validRuntimeProfile({
      capabilities: ["chat", "digital_self_preview", "memory_recall_private"],
    }),
  );
  const keys = binding.sensitiveEntriesFor(profile).map((entry) => entry.key);
  assert.ok(keys.includes("digital_self"));
  assert.ok(keys.includes("memory_recall"));
  assert.ok(!keys.includes("speaker_enrollment"));
  assert.ok(!keys.includes("guardian_summary"));
  assert.deepEqual(binding.sensitiveEntriesFor(null), []);
  assert.deepEqual(binding.sensitiveEntriesFor(binding.normalizeRuntimeProfile(null)), []);
});

test("each valid capability opens exactly its own sensitive entry", () => {
  const cases = [
    ["digital_self_preview", "digital_self"],
    ["guardian_summary_view", "guardian_summary"],
    ["raw_audio_retention", "raw_voice_consent"],
    ["memory_recall_private", "memory_recall"],
  ];
  for (const [capability, key] of cases) {
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({ capabilities: [capability] }),
    );
    assert.equal(profile.valid, true);
    const keys = binding.sensitiveEntriesFor(profile).map((entry) => entry.key);
    assert.deepEqual(keys, [key], `${capability} 应只开放 ${key}`);
    assert.deepEqual(binding.sensitiveCapabilitiesFor(profile), [capability]);
  }
});

test("voice_profile_create no longer opens a navigation entry", () => {
  // PR-02：手机声纹录取移除后，voice_profile_create 只作为「我的」页的服务端
  // 授权状态展示，不再映射到任何页面入口（说话人登记在机器人端完成）。
  const profile = binding.normalizeRuntimeProfile(
    validRuntimeProfile({ capabilities: ["voice_profile_create"] }),
  );
  assert.equal(profile.valid, true);
  assert.deepEqual(binding.sensitiveEntriesFor(profile), []);
  assert.deepEqual(binding.sensitiveCapabilitiesFor(profile), ["voice_profile_create"]);
  assert.equal(binding.entryForCapability("voice_profile_create"), null);
});
test("profile unavailable denies all five sensitive entries", () => {
  assert.deepEqual(binding.sensitiveEntriesFor(null), []);
  const invalid = binding.normalizeRuntimeProfile({ service_mode: "adult_companion" });
  assert.deepEqual(binding.sensitiveEntriesFor(invalid), []);
  assert.deepEqual(binding.sensitiveCapabilitiesFor(invalid), []);
  for (const capability of binding.SENSITIVE_CAPABILITIES) {
    assert.equal(binding.hasCapability(invalid, capability), false);
    assert.equal(binding.hasCapability(null, capability), false);
  }
});

test("degradation is explainable and only present when unsafe", () => {
  const confirmed = binding.normalizeRuntimeProfile(validRuntimeProfile());
  assert.equal(binding.degradationFor(confirmed), null);

  const safe = binding.normalizeRuntimeProfile(validUnknownSafeProfile());
  const degradation = binding.degradationFor(safe);
  assert.ok(degradation.reasons.some((reason) => reason.includes("安全模式")));
  assert.ok(degradation.reasons.some((reason) => reason.includes("尚未确认")));
  assert.ok(degradation.forbidden.includes("声音复刻"));
  assert.ok(degradation.forbidden.includes("数字自我"));
  assert.ok(degradation.allowed.includes("普通聊天"));

  const invalid = binding.normalizeRuntimeProfile({});
  assert.ok(
    binding.degradationFor(invalid).reasons.some((reason) => reason.includes("校验失败")),
  );
});

test("subject resolution normalization keeps only valid candidates", () => {
  const resolution = binding.normalizeSubjectResolution({
    resolution: "confirmation_required",
    candidate_subjects: [
      { person_id: "person_child", display_name: "小乐", confidence: 0.71 },
      { person_id: "person_parent", display_name: "妈妈", confidence: 0.23 },
      { display_name: "没有 id" },
      null,
    ],
    temporary_service_mode: "unknown_safe",
    allowed_confirmation_methods: ["voice_question", "app_confirm"],
    runtime_profile_id: "rp_tmp_1",
  });
  assert.equal(resolution.resolution, "confirmation_required");
  assert.equal(resolution.candidate_subjects.length, 2);
  assert.equal(resolution.candidate_subjects[1].person_id, "person_parent");
  assert.equal(resolution.temporary_service_mode, "unknown_safe");
  assert.ok(resolution.allowed_confirmation_methods.includes("app_confirm"));

  const bad = binding.normalizeSubjectResolution({ resolution: "trusted_forever" });
  assert.equal(bad.resolution, "unknown");
  assert.deepEqual(bad.candidate_subjects, []);
  assert.throws(() => binding.normalizeSubjectResolution(null), /主体解析响应无效/);
});

function withWxStorage(store, fn) {
  const previousWx = global.wx;
  global.wx = {
    getStorageSync: (key) => store[key],
    setStorageSync: (key, value) => {
      store[key] = value;
    },
    removeStorageSync: (key) => {
      delete store[key];
    },
  };
  try {
    return fn();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
  }
}

function cacheContext(overrides = {}) {
  return {
    deviceId: "dev_1",
    bindingId: "bd_1",
    bindingVersion: 2,
    sessionId: "ses_1",
    ...overrides,
  };
}

test("cache round-trips only with the exact device+binding+session context", () => {
  withWxStorage({}, () => {
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_cache",
        session_epoch: 4,
        session_id: "ses_cache",
        device_id: "dev_cache",
        binding_id: "bd_cache",
        binding_version: 3,
        capabilities: ["chat"],
      }),
    );
    assert.equal(binding.saveCachedRuntimeProfile(profile), true);
    const cached = binding.readCachedRuntimeProfile({
      deviceId: "dev_cache",
      bindingId: "bd_cache",
      bindingVersion: 3,
      sessionId: "ses_cache",
    });
    assert.ok(cached);
    assert.equal(cached.valid, true);
    assert.equal(cached.session_epoch, 4);
    assert.equal(cached.binding_version, 3);
    binding.clearCachedRuntimeProfile();
    assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null);
  });
});

test("cross device/session/binding replay is refused and clears the cache", () => {
  withWxStorage({}, () => {
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_replay",
        session_epoch: 5,
        session_id: "ses_replay",
        device_id: "dev_replay",
        binding_id: "bd_replay",
        binding_version: 1,
        capabilities: ["chat"],
      }),
    );
    binding.saveCachedRuntimeProfile(profile);
    const wrongContexts = [
      { deviceId: "dev_other" },
      { bindingId: "bd_other" },
      { bindingVersion: 99 },
      { sessionId: "ses_other" },
    ];
    for (const override of wrongContexts) {
      const cached = binding.readCachedRuntimeProfile(
        cacheContext({
          deviceId: "dev_replay",
          bindingId: "bd_replay",
          bindingVersion: 1,
          sessionId: "ses_replay",
          ...override,
        }),
      );
      assert.equal(cached, null, `${JSON.stringify(override)} 不应复用旧 profile`);
      assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null, "违规读取后缓存应清理");
      // 重新种入以便下一个错误上下文测试。
      binding.saveCachedRuntimeProfile(profile);
    }
    // 缺少完整上下文同样 fail closed 并清理。
    assert.equal(binding.readCachedRuntimeProfile({}), null);
    assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null);
  });
});

test("cache refuses epoch regression writes for the same session", () => {
  withWxStorage({}, () => {
    const epoch2 = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_new",
        session_epoch: 2,
        session_id: "ses_epoch",
        capabilities: ["chat"],
      }),
    );
    const epoch1 = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_old",
        session_epoch: 1,
        session_id: "ses_epoch",
        capabilities: ["chat"],
      }),
    );
    assert.equal(binding.saveCachedRuntimeProfile(epoch2), true);
    assert.equal(binding.saveCachedRuntimeProfile(epoch1), false, "同会话 epoch 回退写入应被拒绝");
    const cached = binding.readCachedRuntimeProfile(
      cacheContext({ sessionId: "ses_epoch" }),
    );
    assert.equal(cached.runtime_profile_id, "rp_new", "旧响应不得覆盖新响应");
    assert.equal(cached.session_epoch, 2);
  });
});

test("expired cached profile is never returned", () => {
  withWxStorage({}, () => {
    const cachedAt = Date.now();
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_expired",
        session_id: "ses_exp",
        expires_at: new Date(cachedAt + 60_000).toISOString(),
        capabilities: ["chat"],
      }),
    );
    assert.equal(binding.saveCachedRuntimeProfile(profile), true);
    assert.equal(
      // 缓存保存后时间推进到过期点之后：读取必须 fail closed。
      binding.readCachedRuntimeProfile(cacheContext({ sessionId: "ses_exp" }), {
        now: cachedAt + 120_000,
      }),
      null,
      "过期缓存不得返回",
    );
    assert.equal(binding.readCachedRuntimeProfile(cacheContext({ sessionId: "ses_exp" })), null);
  });
});

test("binding version change and manifest replacement clear the old profile cache", () => {
  withWxStorage({}, () => {
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_v1", device_id: "dev_1", binding_version: 1 }),
    );
    const profile = binding.normalizeRuntimeProfile(
      validRuntimeProfile({
        runtime_profile_id: "rp_v1",
        session_id: "ses_v1",
        device_id: "dev_1",
        binding_id: "bd_v1",
        binding_version: 1,
        capabilities: ["chat"],
      }),
    );
    binding.saveCachedRuntimeProfile(profile);
    // 同 binding 同版本：缓存保留。
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_v1", device_id: "dev_1", binding_version: 1 }),
    );
    assert.ok(
      binding.readCachedRuntimeProfile({
        deviceId: "dev_1",
        bindingId: "bd_v1",
        bindingVersion: 1,
        sessionId: "ses_v1",
      }),
    );
    // binding 版本变化：旧 profile 清理。
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_v1", device_id: "dev_1", binding_version: 2 }),
    );
    assert.equal(
      binding.readCachedRuntimeProfile({
        deviceId: "dev_1",
        bindingId: "bd_v1",
        bindingVersion: 1,
        sessionId: "ses_v1",
      }),
      null,
    );
    // 换设备/换 binding：旧 profile 清理。
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_v2", device_id: "dev_2", binding_version: 1 }),
    );
    assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null);
  });
});

test("binding manifest storage round-trips and fails closed without wx", () => {
  withWxStorage({}, () => {
    binding.saveBindingManifest(canonicalManifest({ binding_version: 2 }));
    const manifest = binding.readBindingManifest();
    assert.equal(manifest.binding_id, "bd_1");
    assert.equal(manifest.binding_version, 2);
    binding.clearBindingManifest();
    assert.equal(binding.readBindingManifest(), null);
    assert.throws(() => binding.saveBindingManifest({}), /绑定结果无效/);
    assert.throws(
      () => binding.saveBindingManifest(canonicalManifest({ binding_version: "2" })),
      /绑定结果无效/,
    );
    assert.throws(
      () => binding.saveBindingManifest(canonicalManifest({ binding_version: 0 })),
      /绑定结果无效/,
    );
  });

  delete global.wx;
  assert.equal(binding.readBindingManifest(), null);
  assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null);
  binding.saveBindingManifest(canonicalManifest());
  assert.equal(binding.readCachedRuntimeProfile(cacheContext()), null);
});

test("clearing or replacing a binding drops the local subject remark", () => {
  withWxStorage({}, () => {
    binding.saveBindingManifest(canonicalManifest());
    assert.equal(
      subjectLabel.saveSubjectLabel({ bindingId: "bd_1", deviceId: "dev_1", label: "老爸" }),
      true,
    );
    assert.equal(subjectLabel.readSubjectLabel(canonicalManifest()), "老爸");
    binding.clearBindingManifest();
    assert.equal(subjectLabel.readSubjectLabel(canonicalManifest()), "");

    binding.saveBindingManifest(canonicalManifest());
    subjectLabel.saveSubjectLabel({ bindingId: "bd_1", deviceId: "dev_1", label: "老爸" });
    binding.saveBindingManifest(
      canonicalManifest({ binding_id: "bd_2", device_id: "dev_2" }),
    );
    assert.equal(subjectLabel.readSubjectLabel(canonicalManifest()), "");
    assert.equal(
      subjectLabel.readSubjectLabel(
        canonicalManifest({ binding_id: "bd_2", device_id: "dev_2" }),
      ),
      "",
    );
  });
});
