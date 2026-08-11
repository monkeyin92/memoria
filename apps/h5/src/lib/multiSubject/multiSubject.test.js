import { beforeEach, describe, expect, it } from "vitest";
import {
  Capability,
  DEVICE_DECLARED_MODE_VALUES,
  PolicyObligation,
  ServiceMode,
  SpeakerState,
  SubjectCategory,
} from "./contracts.js";
import { buildBindingRequest } from "./bindingRequest.js";
import {
  isNewerRuntimeProfile,
  normalizeRuntimeProfileV2,
  runtimeProfileWirePayload,
} from "./runtimeProfile.js";
import { normalizeSubjectResolution } from "./subjectResolution.js";
import {
  readLatestCachedRuntimeProfile,
  saveCachedRuntimeProfile,
  saveBindingManifest,
  validateBindingManifestPayload,
} from "./bindingManifest.js";
import { MODE_CARDS, MODE_TITLES } from "./modeMeta.js";
import {
  isSensitiveEntryUsable,
  sensitiveEntriesFor,
} from "./gates.js";

const NOW = Date.parse("2026-08-10T02:00:00Z");
const HEX64 = "a".repeat(64);

function obligation(code) {
  return {
    code,
    params: {
      max_session_seconds: null,
      retention_ttl_seconds: null,
      quiet_hours: null,
      extras: [],
    },
  };
}

function signedProfileV2(overrides = {}) {
  return {
    signature_schema: "runtime-profile-v2",
    runtime_profile_id: "rp-1",
    device_id: "dev-1",
    session_id: "ses-1",
    actor_id: "acct-1",
    binding_id: "bd-1",
    binding_version: 1,
    active_subject_id: "person-child",
    subject_revision: 3,
    subject_category: SubjectCategory.Minor,
    age_band: "under_14",
    speaker_state: SpeakerState.Confirmed,
    speaker_confidence: 0.91,
    service_mode: ServiceMode.StudentMinor,
    persona_assignment_id: "pa-1",
    persona: {
      persona_id: "starlight",
      version: 4,
      relationship_stage: "familiar",
    },
    policy_bundle_version: "student-cn-v3",
    capabilities: [Capability.Chat, Capability.Tutor, Capability.EnglishPractice],
    obligations: [obligation(PolicyObligation.MAXSESSIONSECONDS)],
    policy_receipt_ids: ["receipt-1"],
    session_epoch: 7,
    issued_at: "2026-08-10T01:00:00Z",
    expires_at: "2026-08-10T03:00:00Z",
    signature: HEX64,
    ...overrides,
  };
}

function bindingRequest(overrides = {}) {
  return {
    device_claim_token: "claim-token",
    declared_mode: "parent_for_child",
    account_owner_person_id: "person-parent",
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
    },
    consent_offer_ids: ["offer_minor_voice_session_v1"],
    ...overrides,
  };
}

describe("canonical DeviceDeclaredMode 单一来源", () => {
  it("mode cards derive from canonical values and use the four required Chinese titles", () => {
    expect(MODE_CARDS.map((card) => card.mode)).toEqual([
      "parent_for_child",
      "self_use",
      "child_for_parent",
      "family_shared",
    ]);
    expect(MODE_CARDS.map((card) => card.title)).toEqual([
      "给孩子使用",
      "给自己使用",
      "给父母使用",
      "家庭共同使用",
    ]);
    // canonical 值必须与自然语言误写不同，防止请求序列化混入漂移值。
    expect(DEVICE_DECLARED_MODE_VALUES).not.toContain("give_to_child");
    expect(DEVICE_DECLARED_MODE_VALUES).not.toContain("use_myself");
    expect(DEVICE_DECLARED_MODE_VALUES).not.toContain("give_to_parents");
    expect(MODE_TITLES.self_use).toBe("给自己使用");
  });
});

describe("buildBindingRequest 四模式请求约束", () => {
  it("accepts the canonical parent_for_child request with expected relationship", () => {
    const built = buildBindingRequest(bindingRequest());
    expect(built.declared_mode).toBe("parent_for_child");
    expect(built.primary_subject.relationship).toBe("guardian_of");
    expect(built.primary_subject.subject_draft).toEqual({
      display_name: "小乐",
      age_band: "under_14",
    });
    expect(built.consent_offer_ids).toEqual(["offer_minor_voice_session_v1"]);
  });

  it("accepts self_use with self relationship and whitelisted preferences", () => {
    const built = buildBindingRequest(
      bindingRequest({
        declared_mode: "self_use",
        primary_subject: { person_id: "person-self", relationship: "self" },
        service_preferences: {
          memory_level: "personal",
          interview_frequency: "low",
        },
        consent_offer_ids: ["offer_self_memory_retention_v1"],
      }),
    );
    expect(built.declared_mode).toBe("self_use");
    expect(built.primary_subject.relationship).toBe("self");
    expect(built.service_preferences).toEqual({
      memory_level: "personal",
      interview_frequency: "low",
    });
  });

  it("accepts child_for_parent with child_of relationship", () => {
    const built = buildBindingRequest(
      bindingRequest({
        declared_mode: "child_for_parent",
        primary_subject: {
          person_id: "new",
          relationship: "child_of",
          subject_draft: { display_name: "爸爸", age_band: "adult" },
        },
        service_preferences: { speech_speed: "slow", memory_level: "none" },
        consent_offer_ids: ["offer_admin_device_management_v1"],
      }),
    );
    expect(built.primary_subject.relationship).toBe("child_of");
    expect(built.service_preferences.speech_speed).toBe("slow");
  });

  it("accepts family_shared with family_member_of relationship", () => {
    const built = buildBindingRequest(
      bindingRequest({
        declared_mode: "family_shared",
        primary_subject: {
          person_id: "new",
          relationship: "family_member_of",
          subject_draft: { display_name: "家庭", age_band: "adult" },
        },
        service_preferences: { shared_persona_enabled: true },
        consent_offer_ids: ["offer_family_space_v1"],
      }),
    );
    expect(built.primary_subject.relationship).toBe("family_member_of");
    expect(built.service_preferences).toEqual({ shared_persona_enabled: true });
  });

  it("rejects non-canonical declared_mode values (drift protection)", () => {
    for (const bad of [
      "give_to_child",
      "use_myself",
      "give_to_parents",
      "for_my_kid",
      "self",
      "",
      null,
      42,
    ]) {
      expect(() =>
        buildBindingRequest(bindingRequest({ declared_mode: bad })),
      ).toThrow(/declared_mode 取值无效/);
    }
  });

  it("rejects non-canonical relationship and age_band values", () => {
    expect(() =>
      buildBindingRequest(
        bindingRequest({
          primary_subject: { person_id: "x", relationship: "owner" },
        }),
      ),
    ).toThrow(/relationship 取值无效/);
    expect(() =>
      buildBindingRequest(
        bindingRequest({
          primary_subject: {
            person_id: "new",
            relationship: "guardian_of",
            subject_draft: { display_name: "小乐", age_band: "kid" },
          },
        }),
      ),
    ).toThrow(/age_band 取值无效/);
  });

  it("rejects any forged authorization field (policy_version / accepted / authorized)", () => {
    for (const key of [
      "policy_version",
      "guardian_accepted",
      "consent_granted",
      "authorized",
      "parent_authorized",
    ]) {
      expect(() =>
        buildBindingRequest(bindingRequest({ [key]: true })),
      ).toThrow(/禁止客户端提交授权声明字段/);
    }
  });

  it("buyer cannot submit parent-self-acceptance offers for child_for_parent", () => {
    for (const offerId of [
      "offer_senior_service_acceptance_v1",
      "offer_senior_memory_retention_v1",
    ]) {
      expect(() =>
        buildBindingRequest(
          bindingRequest({
            declared_mode: "child_for_parent",
            primary_subject: {
              person_id: "new",
              relationship: "child_of",
              subject_draft: { display_name: "爸爸", age_band: "adult" },
            },
            service_preferences: { speech_speed: "slow" },
            consent_offer_ids: [offerId],
          }),
        ),
      ).toThrow(/需要父母本人确认，客户端不能代为提交/);
    }
  });

  it("rejects unknown consent offers, offers for other modes, and unknown preference keys", () => {
    expect(() =>
      buildBindingRequest(
        bindingRequest({ consent_offer_ids: ["offer_made_up_v1"] }),
      ),
    ).toThrow(/未知的 consent offer/);
    expect(() =>
      buildBindingRequest(
        bindingRequest({
          declared_mode: "self_use",
          primary_subject: { person_id: "p", relationship: "self" },
          service_preferences: { memory_level: "none" },
          consent_offer_ids: ["offer_minor_voice_session_v1"],
        }),
      ),
    ).toThrow(/不适用于/);
    expect(() =>
      buildBindingRequest(
        bindingRequest({ service_preferences: { raw_audio_enabled: true } }),
      ),
    ).toThrow(/service_preferences 不允许字段/);
  });

  it("rejects wrong relationship for a mode and unknown request keys", () => {
    expect(() =>
      buildBindingRequest(
        bindingRequest({
          primary_subject: { person_id: "x", relationship: "self" },
        }),
      ),
    ).toThrow(/relationship 必须是 guardian_of/);
    expect(() =>
      buildBindingRequest(bindingRequest({ device_secret: "hack" })),
    ).toThrow(/绑定请求不允许字段/);
  });
});

describe("BindingManifest canonical validation", () => {
  function manifest(overrides = {}) {
    return {
      binding_id: "bd-1",
      device_id: "dev-1",
      declared_mode: "parent_for_child",
      binding_version: 1,
      status: "active",
      reason: "create",
      supersedes_binding_id: null,
      family_space_id: null,
      account_owner_id: "person-parent",
      device_admin_ids: ["person-parent"],
      primary_subject_ids: ["person-child"],
      guardian_ids: ["person-parent"],
      delegate_ids: [],
      emergency_contact_ids: [],
      member_ids: [],
      roles: [
        { person_id: "person-parent", role: "account_owner", permissions: [] },
        { person_id: "person-parent", role: "device_admin", permissions: [] },
        { person_id: "person-parent", role: "guardian", permissions: [] },
        { person_id: "person-child", role: "primary_subject", permissions: [] },
      ],
      service_profile_version: "student-cn-v3",
      policy_bundle_version: "policy-cn-minor-v5",
      consent_snapshot_id: "cs-1",
      persona_assignment_id: "pa-1",
      valid_from: "2026-08-09T10:00:00+09:00",
      valid_until: null,
      created_at: "2026-08-09T10:00:00+09:00",
      ...overrides,
    };
  }

  it("accepts a canonical manifest", () => {
    const result = validateBindingManifestPayload(manifest());
    expect(result.valid).toBe(true);
    expect(result.manifest.declared_mode).toBe("parent_for_child");
  });

  it("rejects unknown fields, missing fields, and unknown enums", () => {
    expect(validateBindingManifestPayload(manifest({ hacked: true })).valid).toBe(false);
    const { binding_id, ...missing } = manifest();
    expect(validateBindingManifestPayload(missing).valid).toBe(false);
    expect(
      validateBindingManifestPayload(manifest({ declared_mode: "give_to_child" })).valid,
    ).toBe(false);
    expect(
      validateBindingManifestPayload(manifest({ status: "active-ish" })).valid,
    ).toBe(false);
    expect(
      validateBindingManifestPayload(manifest({ binding_version: "1" })).valid,
    ).toBe(false);
  });
});

describe("normalizeRuntimeProfileV2 fail-closed", () => {
  it("accepts a valid signed profile v2", () => {
    const profile = normalizeRuntimeProfileV2(signedProfileV2(), { now: NOW });
    expect(profile.valid).toBe(true);
    expect(profile.degraded).toBe(false);
    expect(profile.session_epoch).toBe(7);
    expect(profile.capabilities).toEqual([
      Capability.Chat,
      Capability.Tutor,
      Capability.EnglishPractice,
    ]);
  });

  it("fails closed on forged/unknown schema, missing signature, and unknown fields", () => {
    const cases = [
      signedProfileV2({ signature_schema: "runtime-profile-v1" }),
      signedProfileV2({ signature_schema: "runtime-profile-v3" }),
      signedProfileV2({ signature: null }),
      signedProfileV2({ signature: "zz".repeat(32) }),
      signedProfileV2({ extra_field: true }),
      signedProfileV2({ capabilities: ["can_do_everything"] }),
      signedProfileV2({ service_mode: "super_mode" }),
    ];
    for (const payload of cases) {
      const profile = normalizeRuntimeProfileV2(payload, { now: NOW });
      expect(profile.valid).toBe(false);
      expect(profile.capabilities).toEqual([]);
      expect(profile.service_mode).toBe(ServiceMode.UnknownSafe);
      expect(profile.fail_reasons.length).toBeGreaterThan(0);
    }
  });

  it("fails closed on expiry, future issued_at, and backwards epoch", () => {
    const expired = normalizeRuntimeProfileV2(
      signedProfileV2({ expires_at: "2026-08-09T23:00:00Z" }),
      { now: NOW },
    );
    expect(expired.valid).toBe(false);
    expect(expired.fail_reasons.join()).toMatch(/已过期/);

    const future = normalizeRuntimeProfileV2(
      signedProfileV2({ issued_at: "2026-08-10T03:00:00Z" }),
      { now: NOW },
    );
    expect(future.valid).toBe(false);
    expect(future.fail_reasons.join()).toMatch(/issued_at/);

    const epochZero = normalizeRuntimeProfileV2(
      signedProfileV2({ session_epoch: 0 }),
      { now: NOW },
    );
    expect(epochZero.valid).toBe(false);
  });

  /**
   * 跨字段授权语义（年龄带/关系/unknown_safe 白名单/成人专属能力）由
   * 服务端签名 profile 与生成合同负责：H5 不维护客户端 Policy 矩阵，
   * 因此这些组合只要通过 canonical 校验与时间不变量就按服务端授权接受，
   * UI 只按 capabilities 逐入口门禁。
   */
  it("accepts server-signed cross-field combinations without a client policy matrix", () => {
    const unconfirmedWithSubject = normalizeRuntimeProfileV2(
      signedProfileV2({
        speaker_state: SpeakerState.Unconfirmed,
        active_subject_id: "person-child",
      }),
      { now: NOW },
    );
    expect(unconfirmedWithSubject.valid).toBe(true);

    const adultCategory = normalizeRuntimeProfileV2(
      signedProfileV2({ subject_category: SubjectCategory.Adult }),
      { now: NOW },
    );
    expect(adultCategory.valid).toBe(true);

    const unknownSafeWithSensitive = normalizeRuntimeProfileV2(
      signedProfileV2({
        active_subject_id: null,
        subject_category: SubjectCategory.Unknown,
        age_band: "unknown",
        speaker_state: SpeakerState.Unknown,
        service_mode: ServiceMode.UnknownSafe,
        capabilities: [Capability.Chat, Capability.MemoryRecallPrivate],
        obligations: [],
      }),
      { now: NOW },
    );
    expect(unknownSafeWithSensitive.valid).toBe(true);
    expect(unknownSafeWithSensitive.degraded).toBe(true);

    const minorWithClone = normalizeRuntimeProfileV2(
      signedProfileV2({ capabilities: [Capability.Chat, Capability.VoiceCloneUse] }),
      { now: NOW },
    );
    expect(minorWithClone.valid).toBe(true);
  });

  it("runtimeProfileWirePayload round-trips the canonical wire fields", () => {
    const profile = normalizeRuntimeProfileV2(signedProfileV2(), { now: NOW });
    const wire = runtimeProfileWirePayload(profile);
    expect(wire).not.toBeNull();
    expect(wire.valid).toBeUndefined();
    expect(wire.session_epoch).toBe(7);
    expect(Object.keys(wire).sort()).toEqual(
      [
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
      ].sort(),
    );
  });
});

describe("isNewerRuntimeProfile epoch guard", () => {
  const make = (sessionId, epoch) =>
    normalizeRuntimeProfileV2(signedProfileV2({ session_id: sessionId, session_epoch: epoch }), {
      now: NOW,
    });

  it("requires strictly advancing epoch in the same session", () => {
    expect(isNewerRuntimeProfile(make("ses-1", 5), make("ses-1", 6))).toBe(true);
    expect(isNewerRuntimeProfile(make("ses-1", 6), make("ses-1", 6))).toBe(false);
    expect(isNewerRuntimeProfile(make("ses-1", 7), make("ses-1", 6))).toBe(false);
    expect(isNewerRuntimeProfile(make("ses-1", 5), make("ses-2", 6))).toBe(false);
  });

  it("rejects invalid next profiles even when epoch advances", () => {
    const next = normalizeRuntimeProfileV2(
      signedProfileV2({ session_epoch: 8, expires_at: "2026-08-09T00:00:00Z" }),
      { now: NOW },
    );
    expect(isNewerRuntimeProfile(make("ses-1", 5), next)).toBe(false);
  });

  it("accepts the first valid profile as baseline", () => {
    expect(isNewerRuntimeProfile(null, make("ses-1", 1))).toBe(true);
  });
});

describe("normalizeSubjectResolution", () => {
  it("normalizes a canonical confirmation_required response", () => {
    const resolution = normalizeSubjectResolution({
      resolution: "confirmation_required",
      candidate_subjects: [
        { person_id: "person-child", display_name: "小乐", confidence: 0.71 },
        { person_id: "person-parent", display_name: "妈妈", confidence: 0.23 },
      ],
      temporary_service_mode: ServiceMode.UnknownSafe,
      allowed_confirmation_methods: ["voice_question", "app_confirm"],
      runtime_profile_id: "rp-temp-1",
    });
    expect(resolution.valid).toBe(true);
    expect(resolution.candidate_subjects).toHaveLength(2);
    expect(resolution.allowed_confirmation_methods).toEqual([
      "voice_question",
      "app_confirm",
    ]);
  });

  it("fails closed on unknown resolution / method / extra fields", () => {
    const bad = normalizeSubjectResolution({
      resolution: "maybe_parent",
      candidate_subjects: [{ person_id: "x", display_name: "X", confidence: 0.5 }],
      temporary_service_mode: ServiceMode.UnknownSafe,
      allowed_confirmation_methods: ["tap_to_claim"],
      runtime_profile_id: "rp-1",
    });
    expect(bad.valid).toBe(false);
    expect(bad.candidate_subjects).toEqual([]);
    expect(bad.allowed_confirmation_methods).toEqual([]);
  });
});

describe("展示层红线：降级/未确认/离线/多人时敏感入口仍不可进入", () => {
  it("hides MemoryRecallPrivate entry even when an unknown_safe profile carries the capability", () => {
    const unknownSafeWithSensitive = normalizeRuntimeProfileV2(
      signedProfileV2({
        active_subject_id: null,
        subject_category: SubjectCategory.Unknown,
        age_band: "unknown",
        speaker_state: SpeakerState.Unknown,
        service_mode: ServiceMode.UnknownSafe,
        capabilities: [Capability.Chat, Capability.MemoryRecallPrivate],
        obligations: [],
      }),
      { now: NOW },
    );
    // canonical 结构层接受（服务端签名），展示层必须拒绝。
    expect(unknownSafeWithSensitive.valid).toBe(true);
    expect(unknownSafeWithSensitive.degraded).toBe(true);
    const entries = sensitiveEntriesFor(unknownSafeWithSensitive);
    expect(entries.map((entry) => entry.key)).not.toContain("memory_recall");
    expect(
      isSensitiveEntryUsable(
        { capability: Capability.MemoryRecallPrivate, requiresConfirmedSubject: true },
        unknownSafeWithSensitive,
      ),
    ).toBe(false);
  });

  it("hides sensitive entries when speaker_state is not confirmed despite signed capabilities", () => {
    const unconfirmed = normalizeRuntimeProfileV2(
      signedProfileV2({
        speaker_state: SpeakerState.Unconfirmed,
        service_mode: ServiceMode.UnknownSafe,
      }),
      { now: NOW },
    );
    expect(unconfirmed.valid).toBe(true);
    expect(sensitiveEntriesFor(unconfirmed)).toEqual([]);
  });

  it("blocks sensitive entries during offline or multiple-speakers display contexts", () => {
    const confirmed = normalizeRuntimeProfileV2(
      signedProfileV2({
        capabilities: [
          Capability.Chat,
          Capability.Tutor,
          Capability.MemoryRecallPrivate,
        ],
      }),
      { now: NOW },
    );
    expect(sensitiveEntriesFor(confirmed).map((entry) => entry.key)).toContain(
      "memory_recall",
    );
    expect(sensitiveEntriesFor(confirmed, { offline: true })).toEqual([]);
    expect(sensitiveEntriesFor(confirmed, { multipleSpeakers: true })).toEqual([]);
  });

  it("keeps plain chat available in degraded contexts", () => {
    const unknownSafe = normalizeRuntimeProfileV2(
      signedProfileV2({
        active_subject_id: null,
        subject_category: SubjectCategory.Unknown,
        age_band: "unknown",
        speaker_state: SpeakerState.Unknown,
        service_mode: ServiceMode.UnknownSafe,
        capabilities: [Capability.Chat, Capability.EnglishPractice],
        obligations: [],
      }),
      { now: NOW },
    );
    expect(unknownSafe.valid).toBe(true);
    expect(unknownSafe.capabilities).toContain(Capability.Chat);
    expect(sensitiveEntriesFor(unknownSafe)).toEqual([]);
  });
});

describe("readLatestCachedRuntimeProfile（缓存只作候选，必须经服务端重验）", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("returns the strictly validated cached profile when contexts match", () => {
    const profile = normalizeRuntimeProfileV2(signedProfileV2(), { now: NOW });
    expect(profile.valid).toBe(true);
    expect(saveCachedRuntimeProfile(profile)).toBe(true);
    const cached = readLatestCachedRuntimeProfile(
      { deviceId: "dev-1", bindingId: "bd-1", bindingVersion: 1 },
      { now: NOW },
    );
    expect(cached).not.toBeNull();
    expect(cached.valid).toBe(true);
    expect(cached.session_id).toBe("ses-1");
  });

  it("returns a fail-closed degraded object when the cached profile has expired", () => {
    const profile = normalizeRuntimeProfileV2(
      signedProfileV2({ expires_at: "2020-01-01T00:00:00Z" }),
      { now: NOW },
    );
    saveCachedRuntimeProfile({
      ...profile,
      valid: true,
      expires_at: "2099-01-01T00:00:00Z",
    });
    // 重写缓存为“当时有效”的条目，再在更晚时刻读取。
    const later = NOW + 1000 * 60 * 60 * 24 * 365 * 100;
    const cached = readLatestCachedRuntimeProfile(
      { deviceId: "dev-1", bindingId: "bd-1", bindingVersion: 1 },
      { now: later },
    );
    expect(cached.valid).toBe(false);
    expect(cached.capabilities).toEqual([]);
  });

  it("returns null when the binding context does not match", () => {
    const profile = normalizeRuntimeProfileV2(signedProfileV2(), { now: NOW });
    saveCachedRuntimeProfile(profile);
    const cached = readLatestCachedRuntimeProfile(
      { deviceId: "dev-other", bindingId: "bd-1", bindingVersion: 1 },
      { now: NOW },
    );
    expect(cached).toBeNull();
  });

  it("saveBindingManifest clears stale cached profiles on binding change", () => {
    const profile = normalizeRuntimeProfileV2(signedProfileV2(), { now: NOW });
    const firstManifest = {
      binding_id: "bd-1",
      device_id: "dev-1",
      declared_mode: "parent_for_child",
      binding_version: 1,
      status: "active",
      reason: "create",
      supersedes_binding_id: null,
      family_space_id: null,
      account_owner_id: "person-parent",
      device_admin_ids: ["person-parent"],
      primary_subject_ids: ["person-child"],
      guardian_ids: ["person-parent"],
      delegate_ids: [],
      emergency_contact_ids: [],
      member_ids: [],
      roles: [
        { person_id: "person-parent", role: "account_owner", permissions: [] },
      ],
      service_profile_version: "student-cn-v3",
      policy_bundle_version: "policy-cn-minor-v5",
      consent_snapshot_id: null,
      persona_assignment_id: null,
      valid_from: "2026-01-01T00:00:00Z",
      valid_until: null,
      created_at: "2026-01-01T00:00:00Z",
    };
    saveBindingManifest(firstManifest);
    saveCachedRuntimeProfile(profile);
    expect(
      readLatestCachedRuntimeProfile(
        { deviceId: "dev-1", bindingId: "bd-1", bindingVersion: 1 },
        { now: NOW },
      ),
    ).not.toBeNull();
    const manifest = {
      binding_id: "bd-2",
      device_id: "dev-2",
      declared_mode: "self_use",
      binding_version: 1,
      status: "active",
      reason: "create",
      supersedes_binding_id: null,
      family_space_id: null,
      account_owner_id: "person-self",
      device_admin_ids: ["person-self"],
      primary_subject_ids: ["person-self"],
      guardian_ids: [],
      delegate_ids: [],
      emergency_contact_ids: [],
      member_ids: [],
      roles: [
        { person_id: "person-self", role: "account_owner", permissions: [] },
      ],
      service_profile_version: "adult-companion-v1",
      policy_bundle_version: "policy-adult-v1",
      consent_snapshot_id: null,
      persona_assignment_id: null,
      valid_from: "2026-01-01T00:00:00Z",
      valid_until: null,
      created_at: "2026-01-01T00:00:00Z",
    };
    saveBindingManifest(manifest);
    const cached = readLatestCachedRuntimeProfile(
      { deviceId: "dev-2", bindingId: "bd-2", bindingVersion: 1 },
      { now: NOW },
    );
    expect(cached).toBeNull();
  });
});
