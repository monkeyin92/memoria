import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  createSession: vi.fn(),
}));

vi.mock("./client.js", () => ({
  request: mocks.request,
}));

vi.mock("./session.js", () => ({
  createSession: mocks.createSession,
}));

import {
  Capability,
  PolicyObligation,
  ServiceMode,
  SpeakerState,
  SubjectCategory,
} from "../lib/multiSubject/contracts.js";
import {
  clearBindingManifest,
  readBindingManifest,
  saveBindingManifest,
} from "../lib/multiSubject/bindingManifest.js";
import {
  createDeviceBinding,
  createDeviceSession,
  getDeviceBinding,
  getRuntimeProfile,
  requireRuntimeCapability,
  setActiveSubject,
} from "./device.js";

const HEX64 = "a".repeat(64);

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

function signedProfileV2(overrides = {}) {
  return {
    signature_schema: "runtime-profile-v2",
    runtime_profile_id: "rp-1",
    device_id: "dev-1",
    session_id: "ses-1",
    actor_id: "acct-owner",
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
    capabilities: [
      Capability.Chat,
      Capability.Tutor,
      Capability.MemoryRecallPrivate,
    ],
    obligations: [
      {
        code: PolicyObligation.MAXSESSIONSECONDS,
        params: {
          max_session_seconds: 1800,
          retention_ttl_seconds: null,
          quiet_hours: null,
          extras: [],
        },
      },
    ],
    policy_receipt_ids: ["receipt-1"],
    session_epoch: 7,
    issued_at: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T00:00:00Z",
    signature: HEX64,
    ...overrides,
  };
}

function companionSession(overrides = {}) {
  return {
    session_id: "ses-1",
    interaction: {
      interaction_mode: "companion",
      mode_policy_version: "companion-v1",
      companion_style_id: "starlight",
      companion_style_version: "v4",
      digital_self_version_id: null,
      manifest_sha256: null,
      preview_grant_id: null,
      perspective: null,
      relationship_profile_id: null,
      legacy_grant_id: null,
      actor_account_id: null,
      resource_owner_account_id: null,
      relationship_profile_version: null,
      legacy_actor_role: null,
      legacy_grantee_account_id: null,
      legacy_shell_id: null,
      legacy_grant_snapshot_sha256: null,
      legacy_scope_sha256: null,
      legacy_voice_allowed: null,
      legacy_expires_at: null,
      voice_profile_id: null,
      voice_profile_version: null,
      voice_provider: null,
      voice_model: null,
      voice_resource_id: null,
      voice_provider_expires_at: null,
      voice_speaker_sha256: null,
      fallback_voice_profile_id: null,
      fallback_voice_provider: null,
      fallback_voice_model: null,
      fallback_voice_resource_id: null,
    },
    media_runtime: "livekit",
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("device API adapter", () => {
  beforeEach(() => {
    // mockReset 同时清空 Once 队列，防止上一个失败测试的实现泄漏。
    mocks.request.mockReset();
    mocks.createSession.mockReset();
    window.localStorage.clear();
  });

  describe("createDeviceBinding", () => {
    it("serializes a canonical request and never sends forged authorization fields", async () => {
      let capturedBody = null;
      mocks.request.mockImplementation(async (_path, options) => {
        capturedBody = JSON.parse(options.body);
        return manifest();
      });
      const result = await createDeviceBinding({
        device_claim_token: "claim",
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
          memory_level: "growth_summary",
          max_session_minutes: 30,
        },
        consent_offer_ids: ["offer_minor_voice_session_v1"],
      });
      expect(result.binding_id).toBe("bd-1");
      expect(capturedBody.declared_mode).toBe("parent_for_child");
      expect(capturedBody.primary_subject.relationship).toBe("guardian_of");
      // 请求体绝不包含任何授权声明/策略版本字段。
      const serialized = JSON.stringify(capturedBody);
      expect(serialized).not.toMatch(/policy_version|accepted|authorized|consent_granted/i);
      expect(readBindingManifest().binding_id).toBe("bd-1");
    });

    it("rejects forged authorization fields before any request is sent", async () => {
      await expect(
        createDeviceBinding({
          device_claim_token: "claim",
          declared_mode: "self_use",
          account_owner_person_id: "person-self",
          primary_subject: { person_id: "person-self", relationship: "self" },
          persona_selection: "starlight",
          service_preferences: { memory_level: "personal" },
          consent_offer_ids: ["offer_self_memory_retention_v1"],
          guardian_accepted: true,
        }),
      ).rejects.toThrow(/禁止客户端提交授权声明字段/);
      expect(mocks.request).not.toHaveBeenCalled();
    });

    it("passes Idempotency-Key header when provided", async () => {
      let capturedHeaders = null;
      mocks.request.mockImplementation(async (_path, options) => {
        capturedHeaders = options.headers;
        return manifest();
      });
      await createDeviceBinding(
        {
          device_claim_token: "claim",
          declared_mode: "self_use",
          account_owner_person_id: "person-self",
          primary_subject: { person_id: "person-self", relationship: "self" },
          persona_selection: "starlight",
          service_preferences: { memory_level: "personal" },
          consent_offer_ids: [],
        },
        { idempotencyKey: "idem-1" },
      );
      expect(capturedHeaders["Idempotency-Key"]).toBe("idem-1");
    });
  });

  describe("getDeviceBinding", () => {
    it("validates the canonical manifest before caching/returning", async () => {
      mocks.request.mockResolvedValue(manifest());
      const result = await getDeviceBinding("dev-1");
      expect(result.binding_version).toBe(1);
      expect(readBindingManifest().device_id).toBe("dev-1");
    });

    it("fails closed (502) when the response is not a canonical manifest", async () => {
      mocks.request.mockResolvedValue({ device_id: "dev-1", hacked: true });
      await expect(getDeviceBinding("dev-1")).rejects.toThrow(/校验失败/);
      expect(readBindingManifest()).toBeNull();
    });
  });

  describe("getRuntimeProfile", () => {
    it("requires a device binding and a real session id (fail closed)", async () => {
      await expect(getRuntimeProfile("dev-1", {})).rejects.toThrow(/还没有绑定设备/);
      clearBindingManifest();
      window.localStorage.clear();
      // 绑定存在但无会话：同样 fail closed。
      saveManifestForTest();
      await expect(
        getRuntimeProfile("dev-1", { sessionId: null }),
      ).rejects.toThrow(/请先开始一次语音对话/);
    });

    it("normalizes and caches a valid signed v2 profile for the session", async () => {
      saveManifestForTest();
      mocks.request.mockResolvedValue(signedProfileV2());
      const profile = await getRuntimeProfile("dev-1", { sessionId: "ses-1" });
      expect(profile.valid).toBe(true);
      expect(profile.session_epoch).toBe(7);
      expect(mocks.request).toHaveBeenCalledWith(
        "/v1/devices/dev-1/runtime-profile?session_id=ses-1",
      );
    });

    it("drops late responses when a newer request supersedes them", async () => {
      saveManifestForTest();
      const older = deferred();
      const newer = deferred();
      mocks.request
        .mockImplementationOnce(() => older.promise)
        .mockImplementationOnce(() => newer.promise);
      const olderCall = getRuntimeProfile("dev-1", { sessionId: "ses-late" });
      const newerCall = getRuntimeProfile("dev-1", { sessionId: "ses-late" });
      newer.resolve(
        signedProfileV2({ session_id: "ses-late", session_epoch: 8 }),
      );
      expect((await newerCall).session_epoch).toBe(8);
      older.resolve(
        signedProfileV2({ session_id: "ses-late", session_epoch: 5 }),
      );
      // 晚到响应不得改写 UI / 缓存。
      expect(await olderCall).toBeNull();
    });

    it("rejects epoch regression on refresh", async () => {
      saveManifestForTest();
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-regress", session_epoch: 6 }),
      );
      const first = await getRuntimeProfile("dev-1", { sessionId: "ses-regress" });
      expect(first.session_epoch).toBe(6);
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-regress", session_epoch: 5 }),
      );
      expect(
        await getRuntimeProfile("dev-1", { sessionId: "ses-regress" }),
      ).toBeNull();
      // 同 epoch 幂等重取可接受。
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-regress", session_epoch: 6 }),
      );
      expect(
        (await getRuntimeProfile("dev-1", { sessionId: "ses-regress" }))
          .session_epoch,
      ).toBe(6);
    });

    it("fails closed on expired or schema-v1 profiles", async () => {
      saveManifestForTest();
      mocks.request.mockResolvedValue(
        signedProfileV2({
          session_id: "ses-expired",
          expires_at: "2020-01-01T00:00:00Z",
        }),
      );
      const expired = await getRuntimeProfile("dev-1", {
        sessionId: "ses-expired",
      });
      expect(expired.valid).toBe(false);
      expect(expired.capabilities).toEqual([]);
      mocks.request.mockResolvedValue(
        signedProfileV2({
          session_id: "ses-expired",
          signature_schema: "runtime-profile-v1",
        }),
      );
      const v1 = await getRuntimeProfile("dev-1", {
        sessionId: "ses-expired",
      });
      expect(v1.valid).toBe(false);
    });
  });

  describe("setActiveSubject", () => {
    it("fails closed without a cached session profile to compare against", async () => {
      saveManifestForTest();
      await expect(
        setActiveSubject("ses-1", { personId: "person-parent" }),
      ).rejects.toThrow(/还没有可验证的当前会话 Profile/);
      expect(mocks.request).not.toHaveBeenCalled();
    });

    it("only accepts a strictly advancing epoch from the previous profile", async () => {
      saveManifestForTest();
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-switch", session_epoch: 7 }),
      );
      const baseline = await getRuntimeProfile("dev-1", {
        sessionId: "ses-switch",
      });
      expect(baseline.valid).toBe(true);

      // 同 epoch：拒绝。
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-switch", session_epoch: 7 }),
      );
      expect(
        await setActiveSubject("ses-switch", { personId: "person-parent" }),
      ).toBeNull();
      // 倒退：拒绝。
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-switch", session_epoch: 6 }),
      );
      expect(
        await setActiveSubject("ses-switch", { personId: "person-parent" }),
      ).toBeNull();
      // 严格前进：接受并缓存。
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({
          session_id: "ses-switch",
          session_epoch: 8,
          active_subject_id: "person-parent",
        }),
      );
      const switched = await setActiveSubject("ses-switch", {
        personId: "person-parent",
      });
      expect(switched.session_epoch).toBe(8);
      expect(switched.active_subject_id).toBe("person-parent");
    });

    it("drops late switch responses superseded by a newer request", async () => {
      saveManifestForTest();
      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-switch-late", session_epoch: 7 }),
      );
      await getRuntimeProfile("dev-1", { sessionId: "ses-switch-late" });

      const older = deferred();
      const newer = deferred();
      mocks.request
        .mockImplementationOnce(() => older.promise)
        .mockImplementationOnce(() => newer.promise);
      const olderCall = setActiveSubject("ses-switch-late", {
        personId: "person-parent",
      });
      const newerCall = setActiveSubject("ses-switch-late", {
        personId: "person-child",
      });
      newer.resolve(
        signedProfileV2({
          session_id: "ses-switch-late",
          session_epoch: 9,
          active_subject_id: "person-child",
        }),
      );
      expect((await newerCall).session_epoch).toBe(9);
      older.resolve(
        signedProfileV2({
          session_id: "ses-switch-late",
          session_epoch: 8,
          active_subject_id: "person-parent",
        }),
      );
      expect(await olderCall).toBeNull();
    });
  });

  describe("requireRuntimeCapability gate", () => {
    it("returns no_binding / no_session / unavailable / capability_missing / allowed", async () => {
      expect(await requireRuntimeCapability(Capability.MemoryRecallPrivate)).toEqual(
        expect.objectContaining({ allowed: false, reason: "no_binding" }),
      );
      saveManifestForTest();
      expect(
        await requireRuntimeCapability(Capability.MemoryRecallPrivate),
      ).toEqual(expect.objectContaining({ allowed: false, reason: "no_session" }));

      const serverError = new Error("upstream down");
      serverError.status = 503;
      mocks.request.mockRejectedValueOnce(serverError);
      expect(
        await requireRuntimeCapability(Capability.MemoryRecallPrivate, {
          sessionId: "ses-gate",
        }),
      ).toEqual(expect.objectContaining({ allowed: false, reason: "unavailable" }));

      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-gate", capabilities: [Capability.Chat] }),
      );
      expect(
        await requireRuntimeCapability(Capability.MemoryRecallPrivate, {
          sessionId: "ses-gate",
        }),
      ).toEqual(
        expect.objectContaining({ allowed: false, reason: "capability_missing" }),
      );

      mocks.request.mockResolvedValueOnce(
        signedProfileV2({ session_id: "ses-gate" }),
      );
      expect(
        await requireRuntimeCapability(Capability.MemoryRecallPrivate, {
          sessionId: "ses-gate",
        }),
      ).toEqual(expect.objectContaining({ allowed: true, reason: "allowed" }));
    });
  });

  describe("createDeviceSession", () => {
    it("fails closed when the session response has no canonical runtime profile", async () => {
      saveManifestForTest();
      mocks.createSession.mockResolvedValue(companionSession());
      await expect(createDeviceSession("acct-owner")).rejects.toThrow(
        /缺少签名 Runtime Profile/,
      );
    });

    it("fails closed on old v1 schema or 503 upstream", async () => {
      saveManifestForTest();
      mocks.createSession.mockResolvedValue(
        companionSession({
          runtime_profile: signedProfileV2({
            session_id: "ses-cds",
            signature_schema: "runtime-profile-v1",
          }),
        }),
      );
      await expect(createDeviceSession("acct-owner")).rejects.toThrow(
        /校验失败/,
      );
      const serverError = new Error("upstream down");
      serverError.status = 503;
      mocks.createSession.mockRejectedValueOnce(serverError);
      await expect(createDeviceSession("acct-owner")).rejects.toMatchObject({
        status: 503,
      });
    });

    it("consumes the canonical signed profile and separates actor from active subject", async () => {
      saveManifestForTest();
      mocks.createSession.mockResolvedValue(
        companionSession({
          session_id: "ses-cds",
          runtime_profile: signedProfileV2({ session_id: "ses-cds" }),
        }),
      );
      const { session, profile } = await createDeviceSession("acct-owner");
      expect(session.session_id).toBe("ses-cds");
      expect(profile.valid).toBe(true);
      expect(profile.actor_id).toBe("acct-owner");
      expect(profile.active_subject_id).toBe("person-child");
      expect(profile.actor_id).not.toBe(profile.active_subject_id);
    });

    it("fails closed when the profile context does not match the current binding", async () => {
      saveManifestForTest();
      mocks.createSession.mockResolvedValue(
        companionSession({
          runtime_profile: signedProfileV2({ device_id: "dev-other" }),
        }),
      );
      await expect(createDeviceSession("acct-owner")).rejects.toThrow(
        /上下文不一致/,
      );
    });
  });
});

function saveManifestForTest() {
  saveBindingManifest(manifest());
}
