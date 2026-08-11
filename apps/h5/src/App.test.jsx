import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { localDateKey } from "./lib/date.js";
import {
  saveBindingManifest,
  saveCachedRuntimeProfile,
} from "./lib/multiSubject/bindingManifest.js";
import { normalizeRuntimeProfileV2 } from "./lib/multiSubject/runtimeProfile.js";

const mocks = vi.hoisted(() => ({
  approveDigitalSelfVersion: vi.fn(),
  beginDigitalSelfTesting: vi.fn(),
  buildDigitalSelfVersion: vi.fn(),
  createGrowthTask: vi.fn(),
  bootstrapIdentity: vi.fn(),
  cachePendingMessage: vi.fn(),
  chooseFidelityTrial: vi.fn(),
  completeFidelityEvaluation: vi.fn(),
  createLegacyGrant: vi.fn(),
  deleteAccountData: vi.fn(),
  endVoice: vi.fn().mockResolvedValue(undefined),
  resetVoice: vi.fn().mockResolvedValue(undefined),
  exportAccountArchive: vi.fn(),
  freezeDigitalSelfVersion: vi.fn(),
  flushPendingMessages: vi.fn().mockResolvedValue(undefined),
  getMemoryDays: vi.fn().mockResolvedValue({ items: [] }),
  getLifeTimeline: vi.fn().mockResolvedValue({ items: [] }),
  getMemoryReviewQueue: vi.fn().mockResolvedValue({ items: [] }),
  getInteractionCapabilities: vi.fn().mockResolvedValue({
    selected_companion_id: "starlight",
    modes: {
      companion: { status: "available", conversational: true },
      archive: { status: "available", conversational: false },
      self_preview: { status: "blocked", conversational: true },
      legacy: { status: "blocked", conversational: true },
    },
  }),
  getLegacyGrants: vi.fn().mockResolvedValue({ role: "owner", items: [] }),
  getLegacyShellPreferences: vi.fn(),
  getDigitalSelfVersions: vi.fn().mockResolvedValue({ items: [] }),
  getFidelityEvaluations: vi.fn().mockResolvedValue({ items: [] }),
  getGrowthOverview: vi.fn().mockResolvedValue({ dimensions: [] }),
  getGrowthTasks: vi.fn().mockResolvedValue({ items: [] }),
  getRawVoiceConsent: vi.fn().mockResolvedValue({ consent: null }),
  createDeviceBinding: vi.fn(),
  createDeviceSession: vi.fn(),
  getDeviceBinding: vi.fn(),
  getRuntimeProfile: vi.fn().mockResolvedValue(null),
  resolveSessionSubject: vi.fn().mockResolvedValue(null),
  setActiveSubject: vi.fn(),
  getProfile: vi.fn(),
  getSelfPreviewCapability: vi.fn().mockResolvedValue({
    status: "blocked",
    conversational: false,
    registered_owner: true,
    active_owner_voice: false,
    missing: ["verified_owner_voice"],
    versions: [],
  }),
  getSelfPreviewSources: vi.fn(),
  issueSelfPreviewGrant: vi.fn(),
  loginAccount: vi.fn(),
  logoutAllDevices: vi.fn(),
  logoutCurrentDevice: vi.fn(),
  registerAccount: vi.fn(),
  revokeSelfPreviewGrant: vi.fn(),
  activateLegacyGrant: vi.fn(),
  revokeLegacyGrant: vi.fn(),
  reviewMemoryClaim: vi.fn(),
  saveMessage: vi.fn().mockResolvedValue(undefined),
  searchLifeArchive: vi.fn().mockResolvedValue({ items: [] }),
  summarizeDay: vi.fn().mockResolvedValue(undefined),
  startFidelityEvaluation: vi.fn(),
  submitSelfPreviewFeedback: vi.fn(),
  updateProfile: vi.fn().mockResolvedValue(undefined),
  updateLegacyShellPreferences: vi.fn(),
  getPersonaStatus: vi.fn().mockResolvedValue({ learning_allowed: false }),
  getPersonaTraits: vi.fn().mockResolvedValue({ items: [] }),
  getPersonaVersions: vi.fn().mockResolvedValue({ items: [] }),
  getSpeakerProfiles: vi.fn().mockResolvedValue({ items: [] }),
  getVoiceProfiles: vi.fn().mockResolvedValue({ consent: null, items: [] }),
  grantPersonaConsent: vi.fn(),
  grantRawVoiceConsent: vi.fn(),
  revokePersonaConsent: vi.fn(),
  revokeDigitalSelfVersion: vi.fn(),
  revokeRawVoiceConsent: vi.fn(),
  reviewPersonaTrait: vi.fn(),
  rollbackPersonaVersion: vi.fn(),
  enrollSpeakerProfiles: vi.fn(),
  revokeSpeakerProfile: vi.fn(),
  grantVoiceConsent: vi.fn(),
  respondGrowthTask: vi.fn(),
  reviewGrowthOwnerAction: vi.fn(),
  revokeVoiceConsent: vi.fn(),
  enrollVoiceProfile: vi.fn(),
  previewVoiceProfile: vi.fn(),
  evaluateVoiceProfile: vi.fn(),
  activateVoiceProfile: vi.fn(),
  revokeVoiceProfile: vi.fn(),
  rollbackDigitalSelfVersion: vi.fn(),
  transitionGrowthTask: vi.fn(),
  useVoiceSession: vi.fn(),
  resumeAudio: vi.fn().mockResolvedValue(true),
}));

vi.mock("./api.js", () => ({
  approveDigitalSelfVersion: mocks.approveDigitalSelfVersion,
  beginDigitalSelfTesting: mocks.beginDigitalSelfTesting,
  buildDigitalSelfVersion: mocks.buildDigitalSelfVersion,
  createGrowthTask: mocks.createGrowthTask,
  bootstrapIdentity: mocks.bootstrapIdentity,
  cachePendingMessage: mocks.cachePendingMessage,
  chooseFidelityTrial: mocks.chooseFidelityTrial,
  completeFidelityEvaluation: mocks.completeFidelityEvaluation,
  createLegacyGrant: mocks.createLegacyGrant,
  deleteAccountData: mocks.deleteAccountData,
  exportAccountArchive: mocks.exportAccountArchive,
  freezeDigitalSelfVersion: mocks.freezeDigitalSelfVersion,
  flushPendingMessages: mocks.flushPendingMessages,
  getMemoryDays: mocks.getMemoryDays,
  getLifeTimeline: mocks.getLifeTimeline,
  getMemoryReviewQueue: mocks.getMemoryReviewQueue,
  getInteractionCapabilities: mocks.getInteractionCapabilities,
  getLegacyGrants: mocks.getLegacyGrants,
  getLegacyShellPreferences: mocks.getLegacyShellPreferences,
  getDigitalSelfVersions: mocks.getDigitalSelfVersions,
  getFidelityEvaluations: mocks.getFidelityEvaluations,
  getGrowthOverview: mocks.getGrowthOverview,
  getGrowthTasks: mocks.getGrowthTasks,
  getRawVoiceConsent: mocks.getRawVoiceConsent,
  createDeviceBinding: mocks.createDeviceBinding,
  createDeviceSession: mocks.createDeviceSession,
  getDeviceBinding: mocks.getDeviceBinding,
  getRuntimeProfile: mocks.getRuntimeProfile,
  resolveSessionSubject: mocks.resolveSessionSubject,
  setActiveSubject: mocks.setActiveSubject,
  getProfile: mocks.getProfile,
  getSelfPreviewCapability: mocks.getSelfPreviewCapability,
  getSelfPreviewSources: mocks.getSelfPreviewSources,
  issueSelfPreviewGrant: mocks.issueSelfPreviewGrant,
  loginAccount: mocks.loginAccount,
  logoutAllDevices: mocks.logoutAllDevices,
  logoutCurrentDevice: mocks.logoutCurrentDevice,
  registerAccount: mocks.registerAccount,
  revokeSelfPreviewGrant: mocks.revokeSelfPreviewGrant,
  activateLegacyGrant: mocks.activateLegacyGrant,
  revokeLegacyGrant: mocks.revokeLegacyGrant,
  reviewMemoryClaim: mocks.reviewMemoryClaim,
  saveMessage: mocks.saveMessage,
  searchLifeArchive: mocks.searchLifeArchive,
  summarizeDay: mocks.summarizeDay,
  startFidelityEvaluation: mocks.startFidelityEvaluation,
  submitSelfPreviewFeedback: mocks.submitSelfPreviewFeedback,
  updateProfile: mocks.updateProfile,
  updateLegacyShellPreferences: mocks.updateLegacyShellPreferences,
  getPersonaStatus: mocks.getPersonaStatus,
  getPersonaTraits: mocks.getPersonaTraits,
  getPersonaVersions: mocks.getPersonaVersions,
  getSpeakerProfiles: mocks.getSpeakerProfiles,
  getVoiceProfiles: mocks.getVoiceProfiles,
  grantPersonaConsent: mocks.grantPersonaConsent,
  grantRawVoiceConsent: mocks.grantRawVoiceConsent,
  revokePersonaConsent: mocks.revokePersonaConsent,
  revokeDigitalSelfVersion: mocks.revokeDigitalSelfVersion,
  revokeRawVoiceConsent: mocks.revokeRawVoiceConsent,
  reviewPersonaTrait: mocks.reviewPersonaTrait,
  rollbackPersonaVersion: mocks.rollbackPersonaVersion,
  enrollSpeakerProfiles: mocks.enrollSpeakerProfiles,
  revokeSpeakerProfile: mocks.revokeSpeakerProfile,
  grantVoiceConsent: mocks.grantVoiceConsent,
  respondGrowthTask: mocks.respondGrowthTask,
  reviewGrowthOwnerAction: mocks.reviewGrowthOwnerAction,
  revokeVoiceConsent: mocks.revokeVoiceConsent,
  enrollVoiceProfile: mocks.enrollVoiceProfile,
  previewVoiceProfile: mocks.previewVoiceProfile,
  evaluateVoiceProfile: mocks.evaluateVoiceProfile,
  activateVoiceProfile: mocks.activateVoiceProfile,
  revokeVoiceProfile: mocks.revokeVoiceProfile,
  rollbackDigitalSelfVersion: mocks.rollbackDigitalSelfVersion,
  transitionGrowthTask: mocks.transitionGrowthTask,
}));

vi.mock("./hooks/useVoiceSession.js", () => ({
  useVoiceSession: mocks.useVoiceSession,
}));

import { App } from "./App.jsx";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function voiceState() {
  return {
    session: null,
    uiState: "idle",
    statusLabel: "轻触我，开始聊聊",
    micEnabled: true,
    latestTranscript: null,
    transcripts: [],
    emotionHint: null,
    assistantExpression: null,
    error: "",
    audioBlocked: false,
    inputMode: "voice",
    audioContainerRef: { current: null },
    start: vi.fn(),
    resumeAudio: mocks.resumeAudio,
    toggleMic: vi.fn(),
    stopAssistant: vi.fn(),
    sendText: vi.fn(),
    end: mocks.endVoice,
    reset: mocks.resetVoice,
  };
}

const HEX64 = "a".repeat(64);

function deviceManifest(overrides = {}) {
  return {
    binding_id: "bd-1",
    device_id: "dev-1",
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
      { person_id: "person-self", role: "device_admin", permissions: [] },
      { person_id: "person-self", role: "primary_subject", permissions: [] },
    ],
    service_profile_version: "adult-companion-v1",
    policy_bundle_version: "policy-adult-v1",
    consent_snapshot_id: "cs-1",
    persona_assignment_id: "pa-1",
    valid_from: "2026-01-01T00:00:00Z",
    valid_until: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function signedProfileV2(overrides = {}) {
  return {
    signature_schema: "runtime-profile-v2",
    runtime_profile_id: "rp-1",
    device_id: "dev-1",
    session_id: "ses-device",
    actor_id: "person-self",
    binding_id: "bd-1",
    binding_version: 1,
    active_subject_id: "person-self",
    subject_revision: 1,
    subject_category: "adult",
    age_band: "adult",
    speaker_state: "confirmed",
    speaker_confidence: 0.96,
    service_mode: "adult_companion",
    persona_assignment_id: "pa-1",
    persona: {
      persona_id: "starlight",
      version: 4,
      relationship_stage: "familiar",
    },
    policy_bundle_version: "adult-companion-v1",
    capabilities: [
      "chat",
      "memory_recall_private",
      "digital_self_preview",
      "voice_profile_create",
      "raw_audio_retention",
    ],
    obligations: [],
    policy_receipt_ids: [],
    session_epoch: 5,
    issued_at: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T00:00:00Z",
    signature: HEX64,
    ...overrides,
  };
}

function defaultResolution(overrides = {}) {
  return {
    valid: true,
    resolution: "confirmed",
    candidate_subjects: [
      { person_id: "person-self", display_name: "小忆", confidence: 0.96 },
    ],
    temporary_service_mode: "adult_companion",
    allowed_confirmation_methods: ["app_confirm"],
    runtime_profile_id: "rp-1",
    ...overrides,
  };
}

/**
 * 让敏感入口通过展示门禁：保存 canonical 绑定清单 + 提供带会话的 voice
 * mock + 返回带目标 capabilities 的有效签名 profile。
 */
function configureDeviceBinding({
  capabilities = signedProfileV2().capabilities,
  profileOverrides = {},
  resolutionOverrides = {},
  sessionId = "ses-device",
  withSession = true,
  profileStatus = "ok",
} = {}) {
  saveBindingManifest(deviceManifest());
  mocks.resolveSessionSubject.mockResolvedValue({
    ...defaultResolution(resolutionOverrides),
    candidate_subjects: [
      { person_id: "person-self", display_name: "小忆", confidence: 0.96 },
      { person_id: "person-other", display_name: "另一位家人", confidence: 0.3 },
    ],
  });
  const profile = normalizeRuntimeProfileV2(
    signedProfileV2({ session_id: sessionId, capabilities, ...profileOverrides }),
  );
  if (profileStatus === "unavailable") {
    const error = new Error("upstream down");
    error.status = 503;
    mocks.getRuntimeProfile.mockRejectedValue(error);
  } else if (profileStatus === "degraded") {
    mocks.getRuntimeProfile.mockResolvedValue(
      normalizeRuntimeProfileV2(
        signedProfileV2({
          session_id: sessionId,
          capabilities,
          expires_at: "2020-01-01T00:00:00Z",
          ...profileOverrides,
        }),
      ),
    );
  } else {
    mocks.getRuntimeProfile.mockResolvedValue(profile);
  }
  if (withSession) {
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: {
        session_id: sessionId,
        interaction: { interaction_mode: "companion" },
      },
    }));
  } else {
    // 无活跃会话：把严格校验过的 profile 写入缓存，供展示门禁回退读取。
    saveCachedRuntimeProfile(profile);
  }
}

function activeLegacyGrant(overrides = {}) {
  return {
    grant_id: "opaque-legacy-grant",
    role: "grantee",
    grantee_username: "memorykeeper",
    version_id: "frozen-self-7",
    version_number: 7,
    allowed_items: [{ kind: "memory_claim", item_id: "memory-1" }],
    grant_snapshot_sha256: "a".repeat(64),
    voice_allowed: false,
    status: "active",
    shell: null,
    ...overrides,
  };
}

async function enterLegacyFromMy() {
  fireEvent.click(screen.getByRole("button", { name: "我的" }));
  fireEvent.click(
    await screen.findByRole("button", { name: /数字心智与声音/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "管理传承授权" }),
  );
  fireEvent.click(screen.getByRole("tab", { name: "接收人视角" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "进入传承对话" }),
  );
}

function configureActiveLegacyVoice(sessionId = "legacy-session") {
  let currentSession = null;
  const start = vi.fn(async () => {
    currentSession = {
      session_id: sessionId,
      interaction: {
        interaction_mode: "legacy",
        legacy_actor_role: "grantee",
        legacy_shell_id: `${sessionId}-shell`,
        resource_owner_account_id: "owner-account",
        companion_style_id: null,
        companion_style_version: null,
      },
    };
    return currentSession;
  });
  const clearSession = async () => {
    currentSession = null;
  };
  mocks.bootstrapIdentity.mockResolvedValue({
    user_id: "legacy-grantee",
    username: "memorykeeper",
    account_type: "registered",
    access_token: "token",
  });
  mocks.getLegacyGrants.mockImplementation(async (role) => ({
    role,
    items: role === "grantee" ? [activeLegacyGrant()] : [],
  }));
  mocks.endVoice.mockImplementation(clearSession);
  mocks.resetVoice.mockImplementation(clearSession);
  mocks.useVoiceSession.mockImplementation(() => ({
    ...voiceState(),
    session: currentSession,
    start,
  }));
  return start;
}

describe("App identity and profile preferences", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mocks.flushPendingMessages.mockResolvedValue(undefined);
    mocks.logoutAllDevices.mockResolvedValue(undefined);
    mocks.logoutCurrentDevice.mockResolvedValue(undefined);
    mocks.deleteAccountData.mockResolvedValue({ status: "completed" });
    mocks.endVoice.mockResolvedValue(undefined);
    mocks.resetVoice.mockResolvedValue(undefined);
    mocks.exportAccountArchive.mockResolvedValue({ sections: {} });
    mocks.getMemoryDays.mockResolvedValue({ items: [] });
    mocks.getFidelityEvaluations.mockResolvedValue({ items: [] });
    mocks.getSelfPreviewCapability.mockResolvedValue({
      status: "blocked",
      conversational: false,
      registered_owner: true,
      active_owner_voice: false,
      missing: ["verified_owner_voice"],
      versions: [],
    });
    mocks.revokeSelfPreviewGrant.mockResolvedValue({
      grant_id: "preview-grant",
      status: "revoked",
    });
    mocks.getGrowthTasks.mockResolvedValue({ items: [] });
    mocks.getLegacyGrants.mockImplementation(async (role) => ({
      role,
      items: [],
    }));
    mocks.getLegacyShellPreferences.mockResolvedValue(null);
    mocks.updateProfile.mockResolvedValue(undefined);
    mocks.resumeAudio.mockResolvedValue(true);
    mocks.useVoiceSession.mockImplementation(() => voiceState());
    mocks.getProfile.mockResolvedValue({
      user_id: "anonymous-user",
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: false,
    });
  });

  afterEach(cleanup);

  it("uses one voice start/end control and keeps text chat as the accessible alternative", async () => {
    const start = vi.fn();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      start,
    }));

    const view = render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    expect(screen.getAllByRole("button", { name: "开始语音对话" }))
      .toHaveLength(1);
    expect(screen.queryByRole("button", { name: /轻触.*开始实时对话/ }))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "使用文字对话" }));
    expect(start).toHaveBeenCalledWith({ inputMode: "text" });
  });

  it("stops only the current spoken answer without ending the conversation", async () => {
    const stopAssistant = vi.fn().mockResolvedValue(undefined);
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "voice-session" },
      inputMode: "voice",
      uiState: "speaking",
      stopAssistant,
    }));

    const view = render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "停止回答" }));

    expect(stopAssistant).toHaveBeenCalledTimes(1);
    expect(mocks.endVoice).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "结束对话" })).toBeInTheDocument();
  });

  it("sends typed text without exposing microphone controls", async () => {
    const sendText = vi.fn(async () => true);
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "text-session" },
      inputMode: "text",
      uiState: "listening",
      sendText,
    }));

    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.change(screen.getByLabelText("输入你想说的话"), {
      target: { value: "今天星期几？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送文字消息" }));

    await waitFor(() => expect(sendText).toHaveBeenCalledWith("今天星期几？"));
    fireEvent.change(screen.getByLabelText("输入你想说的话"), {
      target: { value: "第二条不应连续发送" },
    });
    const sendButton = screen.getByRole("button", { name: "发送文字消息" });
    expect(sendButton).toBeDisabled();
    fireEvent.click(sendButton);
    expect(sendText).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "关闭麦克风" }))
      .not.toBeInTheDocument();
    const exitButton = screen.getByRole("button", { name: "退出文字对话" });
    expect(exitButton).toHaveTextContent("退出文字对话");
    expect(exitButton.querySelector("svg")).not.toBeNull();
  });

  it("shows only the current speaker when the assistant starts streaming", async () => {
    const userLine = {
      key: "user:1:1",
      speaker: "user",
      text: "这句用户内容随后应被替换",
      final: true,
    };
    const assistantLine = {
      key: "assistant:1:1",
      speaker: "assistant",
      text: "这是助手正在流式回答",
      final: false,
    };
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "text-session" },
      inputMode: "text",
      uiState: "speaking",
      transcripts: [userLine, assistantLine],
      latestTranscript: assistantLine,
    }));

    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    expect(screen.getByText("这是助手正在流式回答")).toBeInTheDocument();
    expect(
      screen.queryByText("这句用户内容随后应被替换"),
    ).not.toBeInTheDocument();
  });

  it("reflects the provisional floor state on the current speaker label", async () => {
    const provisionalLine = {
      key: "provisional:p-1",
      speaker: "user",
      text: "你",
      final: false,
      provisional: true,
      floorState: "user_holds_floor",
    };
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "voice-session" },
      uiState: "listening",
      transcripts: [provisionalLine],
      latestTranscript: provisionalLine,
    }));

    const view = render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    expect(screen.getByText("你 · 正在说")).toBeInTheDocument();

    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "voice-session" },
      uiState: "listening",
      transcripts: [{ ...provisionalLine, floorState: "uncertain" }],
      latestTranscript: { ...provisionalLine, floorState: "uncertain" },
    }));
    view.rerender(<App />);
    expect(screen.getByText("你 · 正在确认")).toBeInTheDocument();
  });

  it("does not load user data or expose the app before identity is ready", async () => {
    const identity = deferred();
    mocks.bootstrapIdentity.mockReturnValue(identity.promise);

    render(<App />);

    expect(screen.getByRole("heading", { name: "正在准备你的陪伴空间" }))
      .toBeInTheDocument();
    expect(mocks.getProfile).not.toHaveBeenCalled();
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ userId: "" }),
    );

    await act(async () => {
      identity.resolve({ user_id: "anonymous-user", access_token: "token" });
      await identity.promise;
    });

    await waitFor(() => {
      expect(mocks.getProfile).toHaveBeenCalledWith("anonymous-user");
    });
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ userId: "anonymous-user" }),
    );
    expect(await screen.findByRole("heading", { name: /小忆/ })).toBeInTheDocument();
  });

  it("sends a newly registered account into companion onboarding", async () => {
    mocks.bootstrapIdentity.mockResolvedValue(null);
    mocks.registerAccount.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "registered-token",
    });
    mocks.getProfile.mockResolvedValue({
      user_id: "registered-user",
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: false,
      companion_id: null,
    });

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "创建你的 Memoria 账号" }),
    ).toBeInTheDocument();
    expect(mocks.getProfile).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("怎么称呼你？"), {
      target: { value: "朋友" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    await waitFor(() => {
      expect(mocks.registerAccount).toHaveBeenCalledWith(
        "memorykeeper",
        "safe-passphrase",
        "朋友",
      );
    });
    expect(
      await screen.findByRole("heading", { name: "选择喜欢的陪伴方式" }),
    ).toBeInTheDocument();
    expect(mocks.getProfile).toHaveBeenCalledWith("registered-user");
  });

  it("keeps the loaded profile when an anonymous identity is upgraded in place", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "anonymous",
      access_token: "anonymous-token",
    });
    mocks.registerAccount.mockResolvedValue({
      user_id: "anonymous-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "registered-token",
    });
    mocks.getProfile.mockResolvedValueOnce({
      user_id: "anonymous-user",
      display_name: "新朋友",
      bio: "慢慢说",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: false,
    });

    render(<App />);
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("怎么称呼你？"), {
      target: { value: "朋友" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    expect(
      await screen.findByRole("heading", { name: /新朋友/ }),
    ).toBeInTheDocument();
    expect(mocks.getProfile).toHaveBeenCalledOnce();
  });

  it("hides the previous profile while logging into a different account", async () => {
    const returningProfile = deferred();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "anonymous",
      access_token: "anonymous-token",
    });
    mocks.loginAccount.mockResolvedValue({
      user_id: "returning-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "returning-token",
    });
    mocks.getProfile
      .mockResolvedValueOnce({
        user_id: "anonymous-user",
        display_name: "匿名资料",
        bio: "只属于匿名身份",
        auto_summary: true,
        voice_reply: true,
        gentle_reminders: false,
      })
      .mockReturnValueOnce(returningProfile.promise);

    render(<App />);
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });
    fireEvent.click(screen.getByRole("button", { name: "登录已有账号" }));
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(
      await screen.findByRole("heading", { name: "正在准备你的陪伴空间" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("只属于匿名身份")).not.toBeInTheDocument();

    await act(async () => {
      returningProfile.resolve({
        user_id: "returning-user",
        display_name: "老朋友",
        bio: "又见面了",
        auto_summary: true,
        voice_reply: true,
        gentle_reminders: false,
      });
      await returningProfile.promise;
    });
    expect(
      await screen.findByRole("heading", { name: /老朋友/ }),
    ).toBeInTheDocument();
  });

  it("logs a returning user back into the same personal memory space", async () => {
    mocks.bootstrapIdentity.mockResolvedValue(null);
    mocks.loginAccount.mockResolvedValue({
      user_id: "returning-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "returning-token",
    });
    mocks.getProfile.mockResolvedValue({
      user_id: "returning-user",
      display_name: "老朋友",
      bio: "又见面了",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: false,
    });

    render(<App />);
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });
    fireEvent.click(screen.getByRole("button", { name: "登录已有账号" }));

    expect(
      screen.getByRole("heading", { name: "欢迎回到 Memoria" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mocks.loginAccount).toHaveBeenCalledWith(
        "memorykeeper",
        "safe-passphrase",
      );
    });
    expect(
      await screen.findByRole("heading", { name: /老朋友/ }),
    ).toBeInTheDocument();
    expect(mocks.getProfile).toHaveBeenCalledWith("returning-user");
  });

  it("does not show another account's legacy local profile when the server is offline", async () => {
    window.localStorage.setItem(
      "memoria:profile",
      JSON.stringify({
        display_name: "账号 A 的名字",
        bio: "只属于账号 A",
        auto_summary: true,
        voice_reply: true,
        gentle_reminders: false,
      }),
    );
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "account-b",
      username: "account-b",
      account_type: "registered",
      access_token: "account-b-token",
    });
    mocks.getProfile.mockRejectedValue(new TypeError("network unavailable"));

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: /新朋友/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("只属于账号 A")).not.toBeInTheDocument();
  });

  it("shows the username conflict next to the registration form", async () => {
    mocks.bootstrapIdentity.mockResolvedValue(null);
    mocks.registerAccount.mockRejectedValue(
      new Error("用户名已存在，请换一个"),
    );

    render(<App />);
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("怎么称呼你？"), {
      target: { value: "朋友" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("用户名已存在，请换一个");
    expect(screen.getByRole("button", { name: "创建账号" })).toBeEnabled();
  });

  it("uses server preferences, wires voice reply, and disables unavailable reminders", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const voiceReply = await screen.findByRole("switch", { name: /语音回应/ });
    const reminders = screen.getByRole("switch", { name: /温柔提醒/ });

    expect(voiceReply).toHaveAttribute("aria-checked", "true");
    expect(reminders).toBeDisabled();
    expect(screen.getByText(/即将开放/)).toBeInTheDocument();
    const companionEntry = screen.getByRole("button", {
      name: "更换陪伴方式，当前是星澜",
    });
    expect(companionEntry).toBeInTheDocument();
    fireEvent.click(companionEntry);
    fireEvent.click(
      await screen.findByRole("button", { name: "返回我的" }),
    );
    expect(await screen.findByRole("heading", { name: "我的" }))
      .toBeInTheDocument();
    // 未绑定设备时敏感入口按 fail-closed 展示可解释提示。
    expect(
      screen.getAllByText(/还没有绑定设备，无法取得 Runtime Profile/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("专属凭证保护你的对话")).not.toBeInTheDocument();
    expect(screen.queryByText("你的对话只属于你")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: /语音回应/ }));
    await waitFor(() => {
      expect(mocks.updateProfile).toHaveBeenLastCalledWith(
        "anonymous-user",
        expect.objectContaining({ voice_reply: false }),
      );
    });
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ voiceReplyEnabled: false }),
    );

    fireEvent.click(screen.getByRole("switch", { name: /语音回应/ }));
    await waitFor(() => expect(mocks.resumeAudio).toHaveBeenCalledWith(true));
  });

  it("defaults to bystander filtering and commits changes only after the server confirms", async () => {
    const update = deferred();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.updateProfile.mockReturnValueOnce(update.promise);
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const onlyOwner = await screen.findByRole("switch", {
      name: /过滤明显旁人（实验）/,
    });
    expect(onlyOwner).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/关闭后访客可聊，但仍不能访问或写入主人回顾/))
      .toBeInTheDocument();

    fireEvent.click(onlyOwner);
    expect(onlyOwner).toHaveAttribute("aria-checked", "true");
    expect(mocks.updateProfile).toHaveBeenLastCalledWith(
      "anonymous-user",
      expect.objectContaining({ reject_non_owner_voice: false }),
    );

    await act(async () => {
      update.resolve({ reject_non_owner_voice: false });
      await update.promise;
    });
    await waitFor(() =>
      expect(onlyOwner).toHaveAttribute("aria-checked", "false"),
    );
  });

  it("keeps the confirmed only-owner setting when the profile update fails", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.updateProfile.mockRejectedValueOnce(new Error("offline"));
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const onlyOwner = await screen.findByRole("switch", {
      name: /过滤明显旁人（实验）/,
    });
    fireEvent.click(onlyOwner);

    expect(await screen.findByRole("alert")).toHaveTextContent("偏好保存失败");
    expect(onlyOwner).toHaveAttribute("aria-checked", "true");
  });

  it("does not carry a late preference update into the next account", async () => {
    const lateUpdate = deferred();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "old-account",
      username: "old-account",
      account_type: "registered",
      access_token: "old-token",
    });
    mocks.registerAccount.mockResolvedValue({
      user_id: "new-account",
      username: "new-account",
      account_type: "registered",
      access_token: "new-token",
    });
    mocks.getProfile.mockImplementation(async (requestedUserId) => ({
      user_id: requestedUserId,
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: false,
      reject_non_owner_voice: true,
      companion_id: "starlight",
    }));
    mocks.updateProfile.mockReturnValueOnce(lateUpdate.promise);
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const oldSwitch = await screen.findByRole("switch", {
      name: /过滤明显旁人（实验）/,
    });
    fireEvent.click(oldSwitch);
    expect(oldSwitch).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /注销账号/ }));
    fireEvent.change(screen.getByLabelText("删除验证密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.change(screen.getByLabelText("输入“永久删除我的全部数据”"), {
      target: { value: "永久删除我的全部数据" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "注销账号并删除全部数据" }),
    );
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });

    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "new-account" },
    });
    fireEvent.change(screen.getByLabelText("怎么称呼你？"), {
      target: { value: "朋友" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));
    await screen.findByRole("heading", { name: /小忆/ });
    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const newSwitch = await screen.findByRole("switch", {
      name: /过滤明显旁人（实验）/,
    });
    expect(newSwitch).toBeEnabled();
    expect(newSwitch).toHaveAttribute("aria-checked", "true");

    await act(async () => {
      lateUpdate.resolve({ reject_non_owner_voice: false });
      await lateUpdate.promise;
    });
    expect(newSwitch).toHaveAttribute("aria-checked", "true");
  });

  it("opens digital-self controls from My as a full-screen detail and returns predictably", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "registered",
      access_token: "token",
    });
    configureDeviceBinding();
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /数字心智与声音/ }),
    );

    expect(
      await screen.findByRole("heading", { name: "数字心智与声音" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    expect(await screen.findByRole("heading", { name: "我的" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument();
  });

  it("closes sensitive entries and shows unknown_safe when resolution reports an unsafe crowd", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "registered",
      access_token: "token",
    });
    configureDeviceBinding({
      withSession: false,
      resolutionOverrides: { temporary_service_mode: "unknown_safe" },
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));

    expect(
      await screen.findAllByText(/unknown_safe|多人同时说话/),
    ).toHaveLength(3);
    expect(
      screen.queryByRole("button", { name: /数字心智与声音/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /主人声纹/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/安全模式中/)).toBeInTheDocument();
  });

  it("opens owner voiceprint enrollment directly from My and returns predictably", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "registered",
      access_token: "token",
    });
    // 声纹录取需要无活跃会话（麦克风独占），门禁走严格写入的缓存 profile。
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /主人声纹/ }),
    );

    expect(
      await screen.findByRole("heading", { name: "用四种说话状态录取" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/保持是你自己的声音，只改变轻重、语速和情绪/))
      .toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    expect(await screen.findByRole("heading", { name: "我的" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument();
  });

  it("keeps sensitive entries closed when the cached profile cannot be revalidated (503)", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      account_type: "registered",
      access_token: "token",
    });
    // 有严格写入的本地缓存 profile，但服务端刷新 503：绝不能只信缓存。
    configureDeviceBinding({ withSession: false, profileStatus: "unavailable" });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    expect(
      await screen.findAllByText(/服务端暂时无法提供有效的 Runtime Profile/),
    ).toHaveLength(3);
    expect(
      screen.queryByRole("button", { name: /数字心智与声音/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /主人声纹/ }),
    ).not.toBeInTheDocument();
  });

  it("runs an active grantee Legacy session with an opaque grant and no history side effects", async () => {
    const startVoice = configureActiveLegacyVoice("legacy-session-1");
    mocks.getInteractionCapabilities.mockResolvedValue({
      selected_companion_id: "starlight",
      modes: {
        companion: { status: "available", conversational: true },
        archive: { status: "available", conversational: false },
        self_preview: { status: "available", conversational: true },
        legacy: { status: "available", conversational: true },
      },
    });
    mocks.getDigitalSelfVersions.mockResolvedValue({
      items: [{
        version_id: "approved-self-8",
        version_number: 8,
        status: "approved",
        manifest_sha256: "b".repeat(64),
        manifest: { entries: [] },
        source_summary: {
          memory_claim_count: 1,
          persona_trait_count: 0,
          persona_version_id: null,
          source_summary_sha256: "c".repeat(64),
        },
        parent_version_id: null,
        created_at: "2026-07-23T00:00:00Z",
      }],
    });
    mocks.getSelfPreviewCapability.mockResolvedValue({
      status: "available",
      conversational: true,
      registered_owner: true,
      active_owner_voice: true,
      missing: [],
      versions: [{
        version_id: "approved-self-8",
        status: "approved",
        manifest_sha256: "b".repeat(64),
        version_stale: false,
        fidelity_verdict: "approve",
        fidelity_eligible: true,
        preview_eligible: true,
      }],
    });
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });
    await enterLegacyFromMy();

    await waitFor(() => expect(startVoice).toHaveBeenCalledWith({
      interactionMode: "legacy",
      legacyGrantId: "opaque-legacy-grant",
    }));
    expect(startVoice).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText("冻结数字分身，不是本人"),
    ).toBeInTheDocument();
    expect(screen.getByText(/授权接收人会话.*独立关系外壳.*不改写主人核心/))
      .toBeInTheDocument();
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({
        interactionMode: "legacy",
        legacyGrantId: "opaque-legacy-grant",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /数字心智与声音/ }));
    fireEvent.click(await screen.findByRole("button", { name: "打开数字分身预览" }));
    const previewDialog = screen.getByRole("dialog", { name: "数字分身预览" });
    fireEvent.change(within(previewDialog).getAllByLabelText("当前账号密码")[0], {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(within(previewDialog).getByRole("button", { name: "开始数字分身预览" }));
    expect(await within(previewDialog).findByRole("alert")).toHaveTextContent(
      "请先结束当前陪伴对话，再进入数字分身预览",
    );
    expect(mocks.issueSelfPreviewGrant).not.toHaveBeenCalled();
    expect(startVoice).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "关闭数字分身预览" }));
    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    fireEvent.click(screen.getByRole("button", { name: "陪伴" }));
    fireEvent.click(await screen.findByRole("button", { name: "结束对话" }));

    await waitFor(() => {
      expect(screen.queryByText("冻结数字分身，不是本人"))
        .not.toBeInTheDocument();
      expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
        expect.objectContaining({
          interactionMode: "companion",
          legacyGrantId: null,
        }),
      );
    });
    expect(mocks.endVoice).toHaveBeenCalledOnce();
    expect(mocks.summarizeDay).not.toHaveBeenCalled();
    expect(mocks.transitionGrowthTask).not.toHaveBeenCalled();
    expect(mocks.saveMessage).not.toHaveBeenCalled();
  });

  it("binds a natural-chat growth task to voice and completes it when the chat ends", async () => {
    const naturalTask = {
      task_id: "natural-task",
      kind: "natural_chat",
      status: "draft",
      revision: 0,
      prompt_id: "natural-chat-v1",
      prompt: "聊聊今天发生的事。",
      created_at: "2026-07-22T00:00:00Z",
      updated_at: "2026-07-22T00:00:00Z",
    };
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      account_type: "registered",
      access_token: "token",
    });
    mocks.createGrowthTask.mockResolvedValue(naturalTask);
    mocks.transitionGrowthTask.mockImplementation(
      (_taskId, _eventId, toStatus, revision) => Promise.resolve({
        ...naturalTask,
        status: toStatus,
        revision: revision + 1,
      }),
    );
    configureDeviceBinding({ withSession: false });
    mocks.useVoiceSession.mockImplementation((options) => ({
      ...voiceState(),
      session: options.learningTaskId ? {
        session_id: "voice-natural",
        learning_task_id: options.learningTaskId,
      } : null,
    }));
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /数字心智与声音/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始自然聊天" }));

    await waitFor(() => expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ learningTaskId: "natural-task" }),
    ));
    fireEvent.click(await screen.findByRole("button", { name: "结束对话" }));

    await waitFor(() => expect(mocks.transitionGrowthTask).toHaveBeenLastCalledWith(
      "natural-task",
      expect.any(String),
      "completed",
      1,
    ));
    expect(mocks.endVoice).toHaveBeenCalledOnce();
  });

  it("reconciles a lost natural-chat completion response without keeping a stale binding", async () => {
    const naturalTask = {
      task_id: "natural-retry",
      kind: "natural_chat",
      status: "draft",
      revision: 0,
      prompt_id: "natural-chat-v1",
      prompt: "聊聊今天发生的事。",
      created_at: "2026-07-22T00:00:00Z",
      updated_at: "2026-07-22T00:00:00Z",
    };
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      account_type: "registered",
      access_token: "token",
    });
    mocks.createGrowthTask.mockResolvedValue(naturalTask);
    let completionAttempts = 0;
    mocks.transitionGrowthTask.mockImplementation(
      (_taskId, _eventId, toStatus, revision) => {
        if (toStatus === "completed" && completionAttempts++ === 0) {
          return Promise.reject(new Error("response lost"));
        }
        return Promise.resolve({
          ...naturalTask,
          status: toStatus,
          revision: revision + 1,
        });
      },
    );
    mocks.getGrowthTasks
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValue({
        items: [{ ...naturalTask, status: "active", revision: 1 }],
      });
    configureDeviceBinding({ withSession: false });
    mocks.useVoiceSession.mockImplementation((options) => ({
      ...voiceState(),
      session: options.learningTaskId ? {
        session_id: "voice-natural-retry",
        learning_task_id: options.learningTaskId,
      } : null,
    }));
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /数字心智与声音/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始自然聊天" }));
    fireEvent.click(await screen.findByRole("button", { name: "结束对话" }));

    await waitFor(() => {
      const completionCalls = mocks.transitionGrowthTask.mock.calls.filter(
        (call) => call[2] === "completed",
      );
      expect(completionCalls).toHaveLength(2);
      expect(completionCalls[0][1]).toBe(completionCalls[1][1]);
    });
  });

  it("does not complete a natural-chat task from an unbound voice session", async () => {
    const naturalTask = {
      task_id: "natural-unbound",
      kind: "natural_chat",
      status: "draft",
      revision: 0,
      prompt_id: "natural-chat-v1",
      prompt: "聊聊今天发生的事。",
      created_at: "2026-07-22T00:00:00Z",
      updated_at: "2026-07-22T00:00:00Z",
    };
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      account_type: "registered",
      access_token: "token",
    });
    mocks.createGrowthTask.mockResolvedValue(naturalTask);
    mocks.transitionGrowthTask.mockImplementation(
      (_taskId, _eventId, toStatus, revision) => Promise.resolve({
        ...naturalTask,
        status: toStatus,
        revision: revision + 1,
      }),
    );
    mocks.useVoiceSession.mockImplementation((options) => ({
      ...voiceState(),
      session: options.learningTaskId ? {
        session_id: "voice-other",
        learning_task_id: "another-task",
      } : null,
    }));
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /数字心智与声音/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始自然聊天" }));
    fireEvent.click(await screen.findByRole("button", { name: "结束对话" }));

    await waitFor(() => expect(mocks.endVoice).toHaveBeenCalledOnce());
    expect(
      mocks.transitionGrowthTask.mock.calls.filter((call) => call[2] === "completed"),
    ).toHaveLength(0);
  });

  it("opens companion switching from interaction mode and saves only the next-session style", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "registered",
      access_token: "token",
    });
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /数字心智与声音/ }));
    fireEvent.click(await screen.findByRole("button", { name: "更换陪伴方式" }));

    expect(await screen.findByRole("region", { name: "更换陪伴方式" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", {
      name: "选择玄墨，克制回应 · 很少追问 · 1–2 句",
    }));
    fireEvent.click(screen.getByRole("button", { name: "保存玄墨的陪伴方式" }));

    await waitFor(() => {
      expect(mocks.updateProfile).toHaveBeenCalledWith("anonymous-user", {
        companion_id: "xuanmo",
      });
    });
    expect(await screen.findByRole("heading", { name: "数字心智与声音" })).toBeInTheDocument();
  });

  it("opens raw voice consent from My through Privacy and Data", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      account_type: "registered",
      access_token: "token",
    });
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /隐私与数据/ }));

    expect(await screen.findByRole("heading", { name: "隐私与数据" }))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "原始语音归档" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "主导航" }))
      .not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    expect(await screen.findByRole("heading", { name: "我的" })).toBeInTheDocument();
  });

  it("logs out the current device and returns to the account gate", async () => {
    configureActiveLegacyVoice("legacy-session-logout");
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });
    await enterLegacyFromMy();
    expect(await screen.findByText("冻结数字分身，不是本人"))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "我的" }));

    fireEvent.click(await screen.findByRole("button", { name: /退出当前设备/ }));

    await waitFor(() => {
      expect(mocks.endVoice).toHaveBeenCalledOnce();
      expect(mocks.logoutCurrentDevice).toHaveBeenCalledOnce();
      expect(mocks.resetVoice).toHaveBeenCalledOnce();
      expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
        expect.objectContaining({
          interactionMode: "companion",
          legacyGrantId: null,
        }),
      );
    });
    expect(await screen.findByRole("heading", { name: "创建你的 Memoria 账号" }))
      .toBeInTheDocument();
    expect(screen.queryByText("冻结数字分身，不是本人"))
      .not.toBeInTheDocument();
  });

  it("requires confirmation before logging out every device and reports failures", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    });
    mocks.logoutAllDevices.mockRejectedValueOnce(new Error("network unavailable"));
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });
    fireEvent.click(screen.getByRole("button", { name: "我的" }));

    fireEvent.click(await screen.findByRole("button", { name: /退出所有设备/ }));
    expect(mocks.logoutAllDevices).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog", { name: "确认退出所有设备" }))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认退出所有设备" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("退出失败，请检查网络后重试");
    expect(screen.getByRole("heading", { name: "我的" })).toBeInTheDocument();
  });

  it("ends realtime voice and returns to the account gate after permanent deletion", async () => {
    configureActiveLegacyVoice("legacy-session-delete");
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    await enterLegacyFromMy();
    expect(await screen.findByText("冻结数字分身，不是本人"))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /注销账号/ }));
    expect(
      screen.getByRole("dialog", { name: "注销账号" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "主导航" }))
      .not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("删除验证密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.change(screen.getByLabelText("输入“永久删除我的全部数据”"), {
      target: { value: "永久删除我的全部数据" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "注销账号并删除全部数据" }),
    );

    await waitFor(() => {
      expect(mocks.deleteAccountData).toHaveBeenCalledWith(
        "safe-passphrase",
        "永久删除我的全部数据",
      );
      expect(mocks.resetVoice).toHaveBeenCalledOnce();
      expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
        expect.objectContaining({
          interactionMode: "companion",
          legacyGrantId: null,
        }),
      );
    });
    expect(
      await screen.findByRole("heading", { name: "创建你的 Memoria 账号" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("冻结数字分身，不是本人"))
      .not.toBeInTheDocument();
  });

  it("does not cache a transcript that fails after its account was deleted", async () => {
    const lateSave = deferred();
    let oldAccountTranscript;
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "token",
    });
    mocks.saveMessage.mockReturnValue(lateSave.promise);
    mocks.useVoiceSession.mockImplementation((options) => {
      if (options.userId === "registered-user") {
        oldAccountTranscript = options.onFinalTranscript;
      }
      return voiceState();
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    let saving;
    await act(async () => {
      saving = oldAccountTranscript({
        speaker: "user",
        text: "不要带到新账号",
        history_eligible: true,
      });
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    fireEvent.click(await screen.findByRole("button", { name: /注销账号/ }));
    fireEvent.change(screen.getByLabelText("删除验证密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.change(screen.getByLabelText("输入“永久删除我的全部数据”"), {
      target: { value: "永久删除我的全部数据" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "注销账号并删除全部数据" }),
    );
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });

    await act(async () => {
      lateSave.reject(new Error("late write rejected"));
      await saving;
    });
    expect(mocks.cachePendingMessage).not.toHaveBeenCalled();
  });

  it("never sends a history-ineligible guest transcript to save or pending cache", async () => {
    let onFinalTranscript;
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation((options) => {
      onFinalTranscript = options.onFinalTranscript;
      return voiceState();
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    await act(async () => {
      await onFinalTranscript({
        speaker: "user",
        text: "访客消息",
        history_eligible: false,
      });
    });

    expect(mocks.saveMessage).not.toHaveBeenCalled();
    expect(mocks.cachePendingMessage).not.toHaveBeenCalled();
  });

  it("ignores memories returned after their account was deleted", async () => {
    const oldMemories = deferred();
    const newMemories = deferred();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "old-account",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "old-token",
    });
    mocks.registerAccount.mockResolvedValue({
      user_id: "new-account",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "new-token",
    });
    mocks.getProfile
      .mockResolvedValueOnce({
        user_id: "old-account",
        display_name: "旧账号",
        bio: "旧资料",
        companion_id: "starlight",
      })
      .mockResolvedValueOnce({
        user_id: "new-account",
        display_name: "全新账号",
        bio: "空白开始",
        companion_id: "starlight",
      });
    mocks.getMemoryDays
      .mockReturnValueOnce(oldMemories.promise)
      .mockReturnValueOnce(newMemories.promise);
    render(<App />);
    await screen.findByRole("heading", { name: /旧账号/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    await waitFor(() => expect(mocks.getMemoryDays).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: /注销账号/ }));
    fireEvent.change(screen.getByLabelText("删除验证密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.change(screen.getByLabelText("输入“永久删除我的全部数据”"), {
      target: { value: "永久删除我的全部数据" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "注销账号并删除全部数据" }),
    );
    await screen.findByRole("heading", { name: "创建你的 Memoria 账号" });
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "memorykeeper" },
    });
    fireEvent.change(screen.getByLabelText("怎么称呼你？"), {
      target: { value: "朋友" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));
    await screen.findByRole("heading", { name: /全新账号/ });

    await act(async () => {
      oldMemories.resolve({
        items: [{ day: "2026-07-19", message_count: 99, title: "旧回顾" }],
      });
      await oldMemories.promise;
    });
    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    await waitFor(() => expect(mocks.getMemoryDays).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("99")).not.toBeInTheDocument();
    expect(screen.queryByText("旧回顾")).not.toBeInTheDocument();

    await act(async () => {
      newMemories.resolve({ items: [] });
      await newMemories.promise;
    });
  });

  it("preserves the server reminder preference while the control is unavailable", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.getProfile.mockResolvedValue({
      user_id: "anonymous-user",
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: true,
      voice_reply: true,
      gentle_reminders: true,
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "我的" }));
    const reminders = await screen.findByRole("switch", { name: /温柔提醒/ });
    expect(reminders).toHaveAttribute("aria-checked", "true");
    expect(reminders).toBeDisabled();
  });

  it("offers a user-gesture recovery control when browser audio is blocked", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      audioBlocked: true,
    }));
    render(<App />);

    const recovery = await screen.findByRole("button", {
      name: "轻触恢复声音",
    });
    fireEvent.click(recovery);
    expect(mocks.resumeAudio).toHaveBeenCalledWith(true);
  });

  it("uses cascade only (E2E Omni/Audio backends temporarily disabled)", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    expect(screen.queryByRole("radio", { name: "级联" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "Omni Flash" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "Audio Flash" })).toBeNull();
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ voiceBackend: "cascade" }),
    );
  });

  it("labels both Qwen-backed daily summaries as LLM output", async () => {
    const currentDate = localDateKey();
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.getMemoryDays.mockResolvedValue({
      items: [
        {
          day: currentDate,
          message_count: 2,
          source: "qwen",
          summary: {
            title: "今天的回顾",
            overview: "把今天的重要片刻整理在一起。",
            highlights: ["完成了重要工作"],
            mood: "calm",
            suggestion: "早点休息。",
          },
        },
      ],
    });
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "回顾" }));
    expect(await screen.findByText("由 LLM 从今日对话中整理"))
      .toBeInTheDocument();
  });

  it("opens the review on the current local date without relabeling an older day as today", async () => {
    const now = new Date();
    const currentDate = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, "0"),
      String(now.getDate()).padStart(2, "0"),
    ].join("-");
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.getMemoryDays.mockResolvedValue({
      items: [
        {
          day: "2024-12-18",
          message_count: 2,
          source: "qwen",
          summary: {
            title: "旧日回顾",
            overview: "这是较早的一天。",
            highlights: ["完成了旧计划"],
            mood: "calm",
            suggestion: "继续保持。",
          },
        },
      ],
    });
    configureDeviceBinding({ withSession: false });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "回顾" }));

    const currentDayButton = await screen.findByRole("button", { name: /0 段/ });
    expect(currentDayButton).toHaveAttribute("aria-pressed", "true");
    expect(currentDayButton).toHaveAttribute("data-date", currentDate);
    expect(screen.queryByText("旧日回顾")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /2 段/ }));
    expect(await screen.findByText("旧日回顾")).toBeInTheDocument();
    expect(currentDayButton).toBeInTheDocument();
    expect(currentDayButton).toHaveAttribute("data-date", currentDate);
    expect(screen.getByRole("heading", { name: "当天的重要片刻" }))
      .toBeInTheDocument();
    expect(screen.getByText("由 LLM 从当日对话中整理"))
      .toBeInTheDocument();
  });

  it("waits for a final voice save before generating the daily review", async () => {
    const save = deferred();
    let voiceOptions = null;
    let activeSession = true;
    const end = vi.fn(async () => {
      activeSession = false;
    });
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.saveMessage.mockReturnValue(save.promise);
    mocks.useVoiceSession.mockImplementation((options) => {
      voiceOptions = options;
      return {
        ...voiceState(),
        session: activeSession ? { session_id: "voice-session" } : null,
        uiState: activeSession ? "ready" : "closed",
        end,
      };
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    let persistence;
    await act(async () => {
      persistence = voiceOptions.onFinalTranscript({
        speaker: "user",
        text: "今天完成了对话",
        history_eligible: true,
        turn_id: 1,
        generation_id: 1,
      });
      await Promise.resolve();
    });
    fireEvent.click(await screen.findByRole("button", { name: "结束对话" }));
    await waitFor(() => expect(end).toHaveBeenCalledOnce());
    expect(mocks.summarizeDay).not.toHaveBeenCalled();

    await act(async () => {
      save.resolve({});
      await persistence;
    });
    await waitFor(() => expect(mocks.summarizeDay).toHaveBeenCalledOnce());
    expect(mocks.getMemoryDays).toHaveBeenCalled();
  });
});
