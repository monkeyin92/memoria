import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  addSelfModelClaimCounterexample: vi.fn(),
  approveDigitalSelfVersion: vi.fn(),
  beginDigitalSelfTesting: vi.fn(),
  buildDigitalSelfVersion: vi.fn(),
  createGrowthTask: vi.fn(),
  createLegacyGrant: vi.fn(),
  activateLegacyGrant: vi.fn(),
  activateVoiceProfile: vi.fn(),
  createVoiceBlindTrial: vi.fn(),
  enrollSpeakerProfiles: vi.fn(),
  enrollVoiceProfile: vi.fn(),
  evaluateVoiceProfile: vi.fn(),
  exportAccountArchive: vi.fn(),
  freezeDigitalSelfVersion: vi.fn(),
  getDigitalSelfVersions: vi.fn(),
  getGrowthOverview: vi.fn(),
  getGrowthTasks: vi.fn(),
  getInteractionCapabilities: vi.fn(),
  getLegacyGrants: vi.fn(),
  getLegacyShellPreferences: vi.fn(),
  getSelfModel: vi.fn(),
  getPersonaStatus: vi.fn(),
  getPersonaTraits: vi.fn(),
  getPersonaVersions: vi.fn(),
  getSpeakerProfiles: vi.fn(),
  getVoiceProfiles: vi.fn(),
  grantPersonaConsent: vi.fn(),
  grantVoiceConsent: vi.fn(),
  respondGrowthTask: vi.fn(),
  reviewGrowthOwnerAction: vi.fn(),
  previewVoiceBlindTrial: vi.fn(),
  reviewPersonaTrait: vi.fn(),
  reviewSelfModelClaim: vi.fn(),
  reviewSelfModelDecisionCase: vi.fn(),
  reviewSelfModelRelationshipProfile: vi.fn(),
  revokePersonaConsent: vi.fn(),
  revokeDigitalSelfVersion: vi.fn(),
  revokeLegacyGrant: vi.fn(),
  revokeSpeakerProfile: vi.fn(),
  revokeVoiceConsent: vi.fn(),
  revokeVoiceProfile: vi.fn(),
  rollbackDigitalSelfVersion: vi.fn(),
  rollbackPersonaVersion: vi.fn(),
  transitionGrowthTask: vi.fn(),
  updateLegacyShellPreferences: vi.fn(),
  deleteAccountData: vi.fn(),
  prepareSpeakerEnrollment: vi.fn(),
  prepareVoiceCloneSample: vi.fn(),
}));

vi.mock("../api.js", () => ({
  addSelfModelClaimCounterexample: mocks.addSelfModelClaimCounterexample,
  approveDigitalSelfVersion: mocks.approveDigitalSelfVersion,
  beginDigitalSelfTesting: mocks.beginDigitalSelfTesting,
  buildDigitalSelfVersion: mocks.buildDigitalSelfVersion,
  createGrowthTask: mocks.createGrowthTask,
  createLegacyGrant: mocks.createLegacyGrant,
  activateLegacyGrant: mocks.activateLegacyGrant,
  activateVoiceProfile: mocks.activateVoiceProfile,
  createVoiceBlindTrial: mocks.createVoiceBlindTrial,
  enrollSpeakerProfiles: mocks.enrollSpeakerProfiles,
  enrollVoiceProfile: mocks.enrollVoiceProfile,
  evaluateVoiceProfile: mocks.evaluateVoiceProfile,
  exportAccountArchive: mocks.exportAccountArchive,
  freezeDigitalSelfVersion: mocks.freezeDigitalSelfVersion,
  getDigitalSelfVersions: mocks.getDigitalSelfVersions,
  getGrowthOverview: mocks.getGrowthOverview,
  getGrowthTasks: mocks.getGrowthTasks,
  getInteractionCapabilities: mocks.getInteractionCapabilities,
  getLegacyGrants: mocks.getLegacyGrants,
  getLegacyShellPreferences: mocks.getLegacyShellPreferences,
  getSelfModel: mocks.getSelfModel,
  getPersonaStatus: mocks.getPersonaStatus,
  getPersonaTraits: mocks.getPersonaTraits,
  getPersonaVersions: mocks.getPersonaVersions,
  getSpeakerProfiles: mocks.getSpeakerProfiles,
  getVoiceProfiles: mocks.getVoiceProfiles,
  grantPersonaConsent: mocks.grantPersonaConsent,
  grantVoiceConsent: mocks.grantVoiceConsent,
  respondGrowthTask: mocks.respondGrowthTask,
  reviewGrowthOwnerAction: mocks.reviewGrowthOwnerAction,
  previewVoiceBlindTrial: mocks.previewVoiceBlindTrial,
  reviewPersonaTrait: mocks.reviewPersonaTrait,
  reviewSelfModelClaim: mocks.reviewSelfModelClaim,
  reviewSelfModelDecisionCase: mocks.reviewSelfModelDecisionCase,
  reviewSelfModelRelationshipProfile: mocks.reviewSelfModelRelationshipProfile,
  revokePersonaConsent: mocks.revokePersonaConsent,
  revokeDigitalSelfVersion: mocks.revokeDigitalSelfVersion,
  revokeLegacyGrant: mocks.revokeLegacyGrant,
  revokeSpeakerProfile: mocks.revokeSpeakerProfile,
  revokeVoiceConsent: mocks.revokeVoiceConsent,
  revokeVoiceProfile: mocks.revokeVoiceProfile,
  rollbackDigitalSelfVersion: mocks.rollbackDigitalSelfVersion,
  rollbackPersonaVersion: mocks.rollbackPersonaVersion,
  transitionGrowthTask: mocks.transitionGrowthTask,
  updateLegacyShellPreferences: mocks.updateLegacyShellPreferences,
  deleteAccountData: mocks.deleteAccountData,
}));

vi.mock("../lib/audioEnrollment.js", () => ({
  prepareSpeakerEnrollment: mocks.prepareSpeakerEnrollment,
  prepareVoiceCloneSample: mocks.prepareVoiceCloneSample,
}));

import { DigitalSelfPanel } from "./DigitalSelfPanel.jsx";

let personaAllowed;
let personaTraits;
let personaVersions;
let digitalSelfVersions;
let speakerProfiles;
let voiceConsent;
let voiceProfiles;

function candidateVoice(overrides = {}) {
  return {
    profile_id: "voice-1",
    version_number: 1,
    status: "candidate",
    evaluation_status: "pending",
    quality_status: "pending",
    deletion_status: "not_requested",
    provider: "volcengine_doubao",
    target_model: "cosyvoice-v3.5-plus",
    created_at: "2026-07-19T00:00:00Z",
    ...overrides,
  };
}

function digitalSelfVersion(overrides = {}) {
  return {
    version_id: "digital-self-1",
    version_number: 1,
    status: "draft",
    manifest_sha256: "a".repeat(64),
    manifest: {
      schema_version: "digital-self-manifest-v1",
      entries: [],
    },
    source_summary: {
      memory_claim_count: 2,
      persona_trait_count: 1,
      persona_version_id: "persona-1",
      source_summary_sha256: "b".repeat(64),
    },
    parent_version_id: null,
    created_at: "2026-07-22T00:00:00Z",
    ...overrides,
  };
}

function legacyGrant(role, overrides = {}) {
  return {
    grant_id: `${role}-legacy-grant`,
    role,
    grantee_username: role === "owner" ? "family-member" : "memorykeeper",
    version_id: role === "owner" ? "frozen-owner-7" : "frozen-grantee-9",
    version_number: role === "owner" ? 7 : 9,
    allowed_items: [{ kind: "memory_claim", item_id: "memory-1" }],
    grant_snapshot_sha256: "d".repeat(64),
    voice_allowed: false,
    status: "active",
    shell: null,
    ...overrides,
  };
}

describe("DigitalSelfPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    personaAllowed = false;
    personaTraits = [];
    personaVersions = [];
    digitalSelfVersions = [];
    speakerProfiles = [];
    voiceConsent = null;
    voiceProfiles = [];
    mocks.getPersonaStatus.mockImplementation(async () => ({
      learning_allowed: personaAllowed,
    }));
    mocks.getInteractionCapabilities.mockResolvedValue({
      selected_companion_id: "starlight",
      modes: {
        companion: { status: "available", conversational: true },
        archive: { status: "available", conversational: false },
        self_preview: {
          status: "blocked",
          conversational: true,
          missing: ["self_preview_runtime"],
        },
        legacy: {
          status: "blocked",
          conversational: true,
          missing: [
            "frozen_digital_self_version",
            "relationship_profile",
            "legacy_grant",
          ],
        },
      },
    });
    mocks.getSelfModel.mockResolvedValue({
      claims: [],
      decision_cases: [],
      relationship_profiles: [],
    });
    mocks.getPersonaTraits.mockImplementation(async () => ({ items: personaTraits }));
    mocks.getPersonaVersions.mockImplementation(async () => ({ items: personaVersions }));
    mocks.getDigitalSelfVersions.mockImplementation(async () => ({ items: digitalSelfVersions }));
    mocks.getLegacyGrants.mockImplementation(async (role) => ({
      role,
      items: [],
    }));
    mocks.getLegacyShellPreferences.mockResolvedValue(null);
    mocks.getSpeakerProfiles.mockImplementation(async () => ({ items: speakerProfiles }));
    mocks.getVoiceProfiles.mockImplementation(async () => ({
      consent: voiceConsent,
      items: voiceProfiles,
    }));
    mocks.prepareSpeakerEnrollment.mockResolvedValue([
      { audio_base64: "AA==", sample_rate: 16000 },
      { audio_base64: "AQ==", sample_rate: 16000 },
      { audio_base64: "Ag==", sample_rate: 16000 },
    ]);
    mocks.prepareVoiceCloneSample.mockResolvedValue({
      audio_base64: "UklGRg==",
      media_type: "audio/wav",
      duration_ms: 12000,
      sample_rate: 24000,
    });
    mocks.createVoiceBlindTrial.mockResolvedValue({
      trial_id: "trial-1",
      slots: ["A", "B"],
    });
    mocks.previewVoiceBlindTrial.mockResolvedValue(
      new Blob(["RIFF"], { type: "audio/wav" }),
    );
    mocks.exportAccountArchive.mockResolvedValue({
      format_version: 1,
      manifest_sha256: "a".repeat(64),
      sections: {},
    });
    mocks.deleteAccountData.mockResolvedValue({ status: "completed" });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:voice-preview"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(cleanup);

  it("separates persona, speaker identity and cloned voice with clear safety boundaries", async () => {
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "数字心智与声音" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "人格学习" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "声纹识别" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "声音复刻" })).toBeInTheDocument();
    expect(screen.getByText(/声纹只用于区分主人、访客或不确定/)).toBeInTheDocument();
    expect(screen.getByText(/“过滤明显旁人（实验）”可减少旁人插话/))
      .toBeInTheDocument();
    expect(screen.getByText(/不能单独授权删除、导出或其他敏感操作/)).toBeInTheDocument();
  });

  it("opens the owner-only Self Preview overlay without making an unavailable callback look usable", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "approved" })];
    mocks.getInteractionCapabilities.mockResolvedValue({
      selected_companion_id: "starlight",
      modes: {
        companion: { status: "available", conversational: true },
        archive: { status: "available", conversational: false },
        self_preview: { status: "available", conversational: true },
        legacy: { status: "blocked", conversational: true },
      },
    });

    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        accountType="registered"
        selfPreviewCapability={{
          status: "available",
          conversational: true,
          registered_owner: true,
          active_owner_voice: true,
          missing: [],
          versions: [{
            version_id: "digital-self-1",
            status: "approved",
            manifest_sha256: "a".repeat(64),
            version_stale: false,
            fidelity_verdict: "approve",
            fidelity_eligible: true,
            preview_eligible: true,
          }],
        }}
        onStartPreview={vi.fn()}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "打开数字分身预览" }),
    );
    expect(
      screen.getByRole("heading", { name: "数字分身预览" }),
    ).toBeInTheDocument();
    expect(screen.getByText("数字分身预览，不代表本人")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始数字分身预览" })).toBeDisabled();
  });

  it("loads owner and grantee Legacy views without loading companion identity into the Legacy surface", async () => {
    const onStartLegacy = vi.fn().mockResolvedValue({
      session_id: "legacy-session",
      interaction: {
        interaction_mode: "legacy",
        legacy_actor_role: "grantee",
        legacy_shell_id: "shell-grantee",
      },
    });
    mocks.getLegacyGrants.mockImplementation(async (role) => ({
      role,
      items: [legacyGrant(role)],
    }));
    mocks.getLegacyShellPreferences.mockResolvedValue({
      shell_id: "shell-grantee",
      grant_id: "grantee-legacy-grant",
      preferred_response_length: "balanced",
      question_frequency: "occasional",
      revision: 1,
    });

    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        accountType="registered"
        onStartLegacy={onStartLegacy}
      />,
    );

    await screen.findByRole("heading", { name: "互动模式" });
    const personaStatusCalls = mocks.getPersonaStatus.mock.calls.length;
    const personaTraitCalls = mocks.getPersonaTraits.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "管理传承授权" }));

    const dialog = screen.getByRole("dialog", { name: "传承模式" });
    await waitFor(() => expect(mocks.getLegacyGrants).toHaveBeenCalledWith("owner"));
    expect(within(dialog).getByText("数字分身版本 v7 · frozen-owner-7"))
      .toBeInTheDocument();
    expect(within(dialog).getByText("基于冻结资料生成的数字分身，不是本人"))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/只能使用批准的设计音色/))
      .toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("tab", { name: "接收人视角" }));
    await waitFor(() => expect(mocks.getLegacyGrants).toHaveBeenCalledWith("grantee"));
    expect(within(dialog).getByText("数字分身版本 v9 · frozen-grantee-9"))
      .toBeInTheDocument();
    expect(within(dialog).queryByText("数字分身版本 v7 · frozen-owner-7"))
      .not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "进入传承对话" }));
    await waitFor(() => expect(onStartLegacy).toHaveBeenCalledWith({
      interactionMode: "legacy",
      legacyGrantId: "grantee-legacy-grant",
      legacyActorRole: "grantee",
    }));
    expect(mocks.getLegacyShellPreferences).toHaveBeenCalledWith("shell-grantee");
    expect(mocks.getPersonaStatus).toHaveBeenCalledTimes(personaStatusCalls);
    expect(mocks.getPersonaTraits).toHaveBeenCalledTimes(personaTraitCalls);
  });

  it("routes a testing version into Fidelity instead of a premature approval dialog", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "testing" })];
    const onOpenFidelity = vi.fn();
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        accountType="registered"
        onOpenFidelity={onOpenFidelity}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "完成忠实度评测" }),
    );
    expect(onOpenFidelity).toHaveBeenCalledWith(
      expect.objectContaining({ status: "testing" }),
    );
    expect(screen.getByRole("heading", { name: "数字分身预览" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准此版本" })).not.toBeInTheDocument();
  });

  it("builds an immutable digital-self draft and requires a digest plus step-up for transitions", async () => {
    digitalSelfVersions = [digitalSelfVersion()];
    mocks.buildDigitalSelfVersion.mockResolvedValue(digitalSelfVersion());
    mocks.beginDigitalSelfTesting.mockImplementation(async () => {
      const testing = digitalSelfVersion({ status: "testing" });
      digitalSelfVersions = [testing];
      return testing;
    });
    mocks.approveDigitalSelfVersion.mockImplementation(async () => {
      const approved = digitalSelfVersion({ status: "approved" });
      digitalSelfVersions = [approved];
      return approved;
    });

    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    expect(await screen.findByRole("heading", { name: "数字分身版本" }))
      .toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "根据当前确认材料构建草稿" }),
    );
    await waitFor(() => {
      expect(mocks.buildDigitalSelfVersion).toHaveBeenCalledOnce();
    });

    fireEvent.click(screen.getByRole("button", { name: "进入测试" }));
    await waitFor(() => {
      expect(mocks.beginDigitalSelfTesting).toHaveBeenCalledWith(
        "digital-self-1",
        "a".repeat(64),
      );
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "批准此版本" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "批准此版本" }));
    const dialog = screen.getByRole("alertdialog", { name: "批准此版本" });
    const passwordInput = screen.getByLabelText("账号密码");
    expect(passwordInput).toHaveFocus();
    fireEvent.change(passwordInput, {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(dialog);
    await waitFor(() => {
      expect(mocks.approveDigitalSelfVersion).toHaveBeenCalledWith(
        "digital-self-1",
        "safe-passphrase",
        "a".repeat(64),
      );
    });
    await waitFor(() => {
      expect(dialog).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "冻结此版本" })).toHaveFocus();
    });
  });

  it("shows manifest-v2 cognitive, decision and relationship source counts", async () => {
    const sourceSummary = {
      memory_claim_count: 1,
      persona_trait_count: 2,
      cognitive_claim_count: 3,
      decision_case_count: 4,
      relationship_profile_count: 5,
      persona_version_id: "persona-1",
      source_summary_sha256: "b".repeat(64),
    };
    digitalSelfVersions = [digitalSelfVersion({
      version_number: 2,
      manifest: {
        schema_version: "digital-self-manifest-v2",
        compiler_version: "digital-self-compiler-v2",
        policy_version: "digital-self-policy-v2",
        parent_version_id: "digital-self-1",
        rollback_target_version_id: null,
        entries: [],
        source_summary: sourceSummary,
      },
      source_summary: sourceSummary,
      parent_version_id: "digital-self-1",
    })];

    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    const card = (await screen.findByText("版本 2")).closest(
      ".digital-version-card",
    );
    expect(card).not.toBeNull();
    const summary = Object.fromEntries(
      [...card.querySelectorAll(".digital-version-summary > div")].map((item) => [
        item.querySelector("dt")?.textContent,
        item.querySelector("dd")?.textContent,
      ]),
    );
    expect(summary).toMatchObject({
      已确认记忆: "1",
      人格特征: "2",
      认知主张: "3",
      真实决策: "4",
      关系画像: "5",
    });
  });

  it("keeps the mutation response visible when the version-list refresh fails", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "testing" })];
    mocks.approveDigitalSelfVersion.mockResolvedValue(
      digitalSelfVersion({ status: "approved" }),
    );
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    const approve = await screen.findByRole("button", { name: "批准此版本" });
    mocks.getDigitalSelfVersions.mockRejectedValueOnce(new Error("sync offline"));
    fireEvent.click(approve);
    fireEvent.change(screen.getByLabelText("账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(screen.getByRole("alertdialog", { name: "批准此版本" }));

    expect(await screen.findByText("已批准")).toBeInTheDocument();
    expect(
      screen.getByText(/版本操作已完成，但最新版本列表暂时无法同步/),
    ).toBeInTheDocument();
  });

  it("keeps the refreshed version authoritative when mutation response differs", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "testing" })];
    mocks.approveDigitalSelfVersion.mockResolvedValue(
      digitalSelfVersion({ status: "approved" }),
    );
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    const approve = await screen.findByRole("button", { name: "批准此版本" });
    fireEvent.click(approve);
    fireEvent.change(screen.getByLabelText("账号密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.submit(screen.getByRole("alertdialog", { name: "批准此版本" }));

    await waitFor(() => {
      expect(screen.getByText("测试中")).toBeInTheDocument();
    });
    expect(screen.queryByText("已批准")).not.toBeInTheDocument();
  });

  it("keeps Tab focus inside the sensitive version dialog", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "testing" })];
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "批准此版本" }));
    const passwordInput = screen.getByLabelText("账号密码");
    const cancel = screen.getByRole("button", { name: "取消" });
    const confirm = screen.getByRole("button", { name: "确认" });
    expect(passwordInput).toHaveFocus();
    fireEvent.change(passwordInput, {
      target: { value: "safe-passphrase" },
    });

    fireEvent.keyDown(passwordInput, { key: "Tab" });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(cancel, { key: "Tab", shiftKey: true });
    expect(passwordInput).toHaveFocus();
    fireEvent.keyDown(passwordInput, { key: "Tab", shiftKey: true });
    expect(confirm).toHaveFocus();
    fireEvent.keyDown(confirm, { key: "Tab" });
    expect(passwordInput).toHaveFocus();
  });

  it("closes a sensitive version dialog with Escape and restores its trigger focus", async () => {
    digitalSelfVersions = [digitalSelfVersion({ status: "testing" })];
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        fidelityByVersion={{ "digital-self-1": { verdict: "approve" } }}
      />,
    );

    const approve = await screen.findByRole("button", { name: "批准此版本" });
    fireEvent.click(approve);
    const dialog = screen.getByRole("alertdialog", { name: "批准此版本" });
    expect(screen.getByLabelText("账号密码")).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("alertdialog", { name: "批准此版本" }))
      .not.toBeInTheDocument();
    expect(approve).toHaveFocus();
  });

  it("separates the light companion from the evidence-grown digital self and exposes only ready modes", async () => {
    const onOpenArchive = vi.fn();
    const onChangeCompanion = vi.fn();
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        onOpenArchive={onOpenArchive}
        onChangeCompanion={onChangeCompanion}
      />,
    );

    expect(await screen.findByRole("heading", { name: "互动模式" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "陪伴模式" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "档案模式" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "数字自我预览" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "传承模式" })).toBeInTheDocument();
    expect(
      screen.getByText("数字自我预览运行时将在后续阶段开放"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "需要先冻结数字分身版本；需要已批准的关系档案；需要传承授权",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/伙伴说的话不会成为你的性格证据/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "更换陪伴方式" }));
    fireEvent.click(screen.getByRole("button", { name: "打开生活档案" }));
    expect(onChangeCompanion).toHaveBeenCalledOnce();
    expect(onOpenArchive).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /数字自我预览/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /传承模式/ })).not.toBeInTheDocument();
  });

  it("fails closed when the server mode authority cannot be loaded", async () => {
    mocks.getInteractionCapabilities.mockRejectedValueOnce(new Error("offline"));
    render(
      <DigitalSelfPanel
        onBack={vi.fn()}
        onOpenArchive={vi.fn()}
        onChangeCompanion={vi.fn()}
      />,
    );

    expect(await screen.findByRole("heading", { name: "互动模式" })).toBeInTheDocument();
    expect(screen.getByText(/部分状态暂时无法同步/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "更换陪伴方式" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开生活档案" })).not.toBeInTheDocument();
  });

  it("shows evidenced emphasis and emotional expression with Chinese labels", async () => {
    personaAllowed = true;
    personaTraits = [
      {
        trait_id: "emphasis-1",
        category: "emphasis_style",
        description: "讲重要事情时会重读关键结论",
        confidence: 0.78,
        observation_count: 2,
        status: "confirmed",
      },
      {
        trait_id: "emotion-style-1",
        category: "emotional_expression",
        description: "安慰家人时语气更柔和",
        confidence: 0.8,
        observation_count: 2,
        status: "confirmed",
      },
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("重音方式")).toBeInTheDocument();
    expect(screen.getByText("情绪表达")).toBeInTheDocument();
  });

  it("keeps persona learning in opt-in state until the owner grants consent", async () => {
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好"),
    ).toBeInTheDocument();
    expect(screen.queryByText("学习状态")).not.toBeInTheDocument();
  });

  it("shows persona learning as continuous before the first version exists", async () => {
    personaAllowed = true;

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByText("持续学习中（聊天越多越准确）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("尚未发布")).not.toBeInTheDocument();
  });

  it("keeps internal persona candidates out of the customer workflow", async () => {
    personaAllowed = true;
    personaTraits = [
      { trait_id: "candidate-1", category: "verbal_tic", status: "candidate" },
      { trait_id: "confirmed-1", category: "pause_style", status: "confirmed" },
      { trait_id: "candidate-2", category: "speech_rate", status: "candidate" },
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByText("持续学习中（聊天越多越准确）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("2 条待确认")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认这条特征" }))
      .not.toBeInTheDocument();
  });

  it("shows the active persona version with neutral wording", async () => {
    personaAllowed = true;
    personaTraits = [
      { trait_id: "candidate-1", category: "verbal_tic", status: "candidate" },
    ];
    personaVersions = [
      { version_id: "version-3", version_number: 3, status: "active" },
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("v3 已启用")).toBeInTheDocument();
    expect(screen.queryByText("v3 已自动更新")).not.toBeInTheDocument();
    expect(screen.queryByText("1 条待确认")).not.toBeInTheDocument();
  });

  it("keeps confirmed traits and version controls available after consent is revoked", async () => {
    personaAllowed = false;
    personaTraits = [
      {
        trait_id: "confirmed-1",
        category: "pause_style",
        description: "思考时会自然停顿",
        confidence: 0.84,
        observation_count: 8,
        status: "confirmed",
      },
    ];
    personaVersions = [
      { version_id: "version-2", version_number: 2, status: "active", reason: "trait_disable" },
      { version_id: "version-1", version_number: 1, status: "superseded", reason: "initial" },
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好"),
    ).toBeInTheDocument();
    expect(screen.getByText("v2 已启用")).toBeInTheDocument();
    expect(screen.getByText("思考时会自然停顿")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    await waitFor(() => {
      expect(mocks.reviewPersonaTrait).toHaveBeenCalledWith("confirmed-1", "disable");
    });

    fireEvent.click(screen.getByText("查看人格历史版本"));
    fireEvent.click(screen.getByRole("button", { name: /回退/ }));
    expect(
      screen.getByRole("alertdialog", { name: "回退到人格版本 v1" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认回退版本" }));
    await waitFor(() => {
      expect(mocks.rollbackPersonaVersion).toHaveBeenCalledWith("version-1");
    });
  });

  it("grants and revokes automatic persona learning without candidate review", async () => {
    personaTraits = [
      {
        trait_id: "trait-1",
        category: "verbal_tic",
        description: "常用“慢慢来”安慰别人",
        confidence: 0.82,
        observation_count: 6,
        status: "candidate",
        version_id: null,
      },
    ];
    mocks.grantPersonaConsent.mockImplementation(async () => {
      personaAllowed = true;
    });
    mocks.revokePersonaConsent.mockImplementation(async () => {
      personaAllowed = false;
    });
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    const consent = await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好");
    fireEvent.click(consent);
    fireEvent.click(screen.getByRole("button", { name: "开启人格学习" }));
    await waitFor(() => expect(mocks.grantPersonaConsent).toHaveBeenCalledOnce());

    expect(screen.queryByRole("button", { name: "确认这条特征" }))
      .not.toBeInTheDocument();
    expect(mocks.reviewPersonaTrait).not.toHaveBeenCalled();

    // 确定性等待：撤销入口只在 run() 内 reload 回读 learning_allowed=true
    // 之后才出现。之前用 waitFor(API mock) 后立即 getByRole 会在全量套件
    // 时序下竞态（grant 调用先于 reload 应用），这里以“UI 状态”为准。
    fireEvent.click(
      await screen.findByRole("button", { name: "撤销人格学习授权" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认撤销人格学习" }));
    await waitFor(() => expect(mocks.revokePersonaConsent).toHaveBeenCalledOnce());
    // 撤销后同样以回读状态为准：同意复选框重新出现。
    expect(
      await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好"),
    ).toBeInTheDocument();
  });

  it("never shows the revoke control before the refetched status confirms learning is on", async () => {
    // 回归：撤销入口的可见性必须由 run() 后的状态回读驱动，而不是由
    // “grant 接口被调用”驱动。即使 grant 成功，若回读仍为 false，
    // 撤销按钮不得出现（UI 不乐观显示服务端未确认的状态）。
    personaTraits = [
      {
        trait_id: "trait-1",
        category: "verbal_tic",
        description: "常用“慢慢来”安慰别人",
        confidence: 0.82,
        observation_count: 6,
        status: "candidate",
        version_id: null,
      },
    ];
    let statusCalls = 0;
    mocks.getPersonaStatus.mockImplementation(async () => {
      statusCalls += 1;
      // 第一次回读 false；grant 成功后的回读仍 false（模拟服务端尚未确认）。
      return { learning_allowed: false };
    });
    mocks.grantPersonaConsent.mockResolvedValue({ ok: true });
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    const consent = await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好");
    fireEvent.click(consent);
    fireEvent.click(screen.getByRole("button", { name: "开启人格学习" }));
    await waitFor(() => expect(mocks.grantPersonaConsent).toHaveBeenCalledOnce());
    await waitFor(() => expect(statusCalls).toBeGreaterThanOrEqual(2));

    expect(
      screen.queryByRole("button", { name: "撤销人格学习授权" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好"),
    ).toBeInTheDocument();
  });

  it("does not expose sensitive decision candidates for customer confirmation", async () => {
    personaAllowed = true;
    personaTraits = [
      {
        trait_id: "decision-1",
        category: "decision_habit",
        description: "做决定前习惯先收集足够信息",
        counterexample: "",
        confidence: 0.86,
        observation_count: 7,
        status: "candidate",
      },
    ];
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByText("持续学习中（聊天越多越准确）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("做决定前习惯先收集足够信息"))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认这条特征" }))
      .not.toBeInTheDocument();
  });

  it("shows an already active value trait without asking for confirmation", async () => {
    personaAllowed = true;
    personaTraits = [
      {
        trait_id: "value-1",
        category: "value_priority",
        description: "通常优先兑现承诺",
        counterexample: "承诺会伤害家人安全时会重新协商。",
        confidence: 0.9,
        observation_count: 9,
        status: "confirmed",
      },
    ];
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("承诺会伤害家人安全时会重新协商。"))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认这条特征" }))
      .not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停用" })).toBeEnabled();
  });

  it("prepares three recordings, registers a shadow speaker profile and supports revocation", async () => {
    mocks.enrollSpeakerProfiles.mockImplementation(async () => {
      speakerProfiles = [
        {
          profile_id: "speaker-1",
          template_version: 1,
          model_version: "campplus-v1",
          sample_count: 3,
          status: "shadow",
        },
      ];
    });
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    const recordings = [1, 2, 3].map(
      (number) => new File([`clip-${number}`], `clip-${number}.wav`, { type: "audio/wav" }),
    );
    fireEvent.click(
      await screen.findByLabelText("我确认这些录音均为本人声音并同意用于声纹登记"),
    );
    fireEvent.change(screen.getByLabelText("选择 3–10 段声纹录音"), {
      target: { files: recordings },
    });
    fireEvent.click(screen.getByRole("button", { name: "登记声纹" }));

    await waitFor(() => {
      expect(mocks.prepareSpeakerEnrollment).toHaveBeenCalledWith(recordings);
      expect(mocks.enrollSpeakerProfiles).toHaveBeenCalledWith(
        expect.arrayContaining([expect.objectContaining({ sample_rate: 16000 })]),
      );
    });
    expect(await screen.findByText(/仍需至少 200 条正式评估样本/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "撤销声纹档案" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销声纹" }));
    await waitFor(() => {
      expect(mocks.revokeSpeakerProfile).toHaveBeenCalledWith("speaker-1");
    });
  });

  it("runs a server-mapped blind voice trial without client-provided quality metrics", async () => {
    mocks.grantVoiceConsent.mockImplementation(async () => {
      voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    });
    mocks.enrollVoiceProfile.mockImplementation(async () => {
      voiceProfiles = [candidateVoice()];
    });
    mocks.evaluateVoiceProfile.mockImplementation(async () => {
      voiceProfiles = [
        candidateVoice({ evaluation_status: "passed", quality_status: "pending" }),
      ];
      return { status: "passed" };
    });
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    fireEvent.click(
      await screen.findByLabelText("我同意使用本人录音创建可撤销的声音档案"),
    );
    fireEvent.click(screen.getByRole("button", { name: "授权声音复刻" }));
    await waitFor(() => expect(mocks.grantVoiceConsent).toHaveBeenCalledOnce());

    const sample = new File(["voice"], "voice.wav", { type: "audio/wav" });
    fireEvent.change(screen.getByLabelText("选择 10–20 秒本人录音"), {
      target: { files: [sample] },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建候选声音" }));
    await waitFor(() => {
      expect(mocks.prepareVoiceCloneSample).toHaveBeenCalledWith(sample);
      expect(mocks.enrollVoiceProfile).toHaveBeenCalledWith(
        expect.objectContaining({ duration_ms: 12000 }),
      );
    });

    fireEvent.click(await screen.findByRole("button", { name: "开始 A/B 盲测" }));
    await waitFor(() => {
      expect(mocks.createVoiceBlindTrial).toHaveBeenCalledWith("voice-1");
    });
    expect(screen.queryByText("基线声音")).not.toBeInTheDocument();
    expect(screen.queryByText("候选声音")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "试听 A" }));
    fireEvent.click(screen.getByRole("button", { name: "试听 B" }));
    await waitFor(() => {
      expect(mocks.previewVoiceBlindTrial).toHaveBeenNthCalledWith(
        1,
        "trial-1",
        "A",
        "今天也想听你讲讲。",
      );
      expect(mocks.previewVoiceBlindTrial).toHaveBeenNthCalledWith(
        2,
        "trial-1",
        "B",
        "今天也想听你讲讲。",
      );
    });

    fireEvent.click(screen.getByLabelText("更喜欢声音 A"));
    expect(screen.getByLabelText("口音相似度（1–5）")).toBeInTheDocument();
    expect(screen.getByLabelText("情绪遵循度（1–5）")).toBeInTheDocument();
    expect(screen.getByLabelText("指令遵循度（1–5）")).toBeInTheDocument();
    expect(screen.getByText(/五项正向评分均 ≥3.5/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提交 A/B 评估" }));
    await waitFor(() => {
      expect(mocks.evaluateVoiceProfile).toHaveBeenCalledWith(
        "voice-1",
        {
          trial_id: "trial-1",
          preferred_slot: "A",
          similarity: 4,
          naturalness: 4,
          accent_similarity: 4,
          emotion_adherence: 4,
          instruction_adherence: 4,
          uncanny: 2,
          notes: "",
        },
      );
    });
    expect(await screen.findByText(/等待服务端质量探针/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活这个声音" })).not.toBeInTheDocument();
    expect(mocks.activateVoiceProfile).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "撤销声音档案" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销声音档案" }));
    await waitFor(() => expect(mocks.revokeVoiceProfile).toHaveBeenCalledWith("voice-1"));
  });

  it("keeps an evaluated clone as history without offering activation", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({ evaluation_status: "passed", quality_status: "passed" }),
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText(/历史档案已通过评估，但暂不应用于当前豆包语音/))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活这个声音" }))
      .not.toBeInTheDocument();
    expect(mocks.activateVoiceProfile).not.toHaveBeenCalled();
  });

  it("allows a fully evaluated Doubao seed-icl-2.0 candidate to activate", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({
        provider: "volcengine_doubao",
        target_model: "seed-icl-2.0",
        evaluation_status: "passed",
        quality_status: "passed",
      }),
    ];
    mocks.activateVoiceProfile.mockImplementation(async () => {
      voiceProfiles = [
        candidateVoice({
          provider: "volcengine_doubao",
          target_model: "seed-icl-2.0",
          status: "active",
          evaluation_status: "passed",
          quality_status: "passed",
        }),
      ];
    });

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "启用个人声音" }));
    await waitFor(() => expect(mocks.activateVoiceProfile).toHaveBeenCalledWith("voice-1"));
    expect(
      await screen.findAllByText(
        /请重建并批准新的数字分身版本；只有该版本的数字分身预览会使用本人声音/,
      ),
    ).not.toHaveLength(0);
  });

  it("shows an active Doubao personal voice and its next-session boundary", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({
        provider: "volcengine_doubao",
        target_model: "seed-icl-2.0",
        status: "active",
        evaluation_status: "passed",
        quality_status: "passed",
      }),
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByText(/个人声音已启用。请重建并批准新的数字分身版本/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "启用个人声音" })).not.toBeInTheDocument();
  });

  it("marks a previously active clone as historical and not applied to Doubao", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({
        status: "active",
        evaluation_status: "passed",
        quality_status: "passed",
      }),
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText(/此档案保留原激活状态，但暂不应用于当前豆包语音/))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活这个声音" }))
      .not.toBeInTheDocument();
  });

  it("shows revoked Doubao profiles as provider-cleanup pending without retrying or claiming completion", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({
        provider: "volcengine_doubao",
        target_model: "seed-icl-2.0",
        status: "revoked",
        deletion_status: "pending",
      }),
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("供应商清理待处理")).toBeInTheDocument();
    expect(
      screen.getByText(/声音档案已停止使用；供应商清理待处理，需人工确认/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试删除声音资产" })).not.toBeInTheDocument();
    expect(screen.queryByText("删除未完成")).not.toBeInTheDocument();
  });

  it("shows incomplete voice cleanup and retries revoked consent deletion", async () => {
    const revokedAt = "2026-07-19T15:00:00Z";
    voiceConsent = {
      policy_version: "voice-clone-v1",
      granted_at: "2026-07-19T14:00:00Z",
      revoked_at: null,
    };
    voiceProfiles = [candidateVoice()];
    mocks.revokeVoiceConsent
      .mockImplementationOnce(async () => {
        voiceConsent = { ...voiceConsent, revoked_at: revokedAt };
        voiceProfiles = [
          candidateVoice({
            status: "revoked",
            deletion_status: "failed",
            revoked_at: revokedAt,
          }),
        ];
        throw new Error("声音资产删除未完成，请稍后重试");
      });
    mocks.revokeVoiceProfile.mockImplementationOnce(async () => {
      voiceProfiles = [
        candidateVoice({
          status: "revoked",
          deletion_status: "completed",
          revoked_at: revokedAt,
        }),
      ];
    });

    render(<DigitalSelfPanel onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "撤销总授权" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销声音授权" }));

    expect(
      await screen.findByText("供应商声音删除未完成。声音档案已停止使用，请重试清理。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试删除声音资产" }));

    await waitFor(() => expect(mocks.revokeVoiceProfile).toHaveBeenCalledWith("voice-1"));
    expect(mocks.revokeVoiceConsent).toHaveBeenCalledOnce();
    expect(await screen.findByRole("button", { name: "授权声音复刻" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试删除声音资产" })).not.toBeInTheDocument();
  });

  it("shows and retries an incomplete single voice profile deletion", async () => {
    const revokedAt = "2026-07-19T15:30:00Z";
    voiceConsent = {
      policy_version: "voice-clone-v1",
      granted_at: "2026-07-19T14:00:00Z",
      revoked_at: null,
    };
    voiceProfiles = [candidateVoice()];
    mocks.revokeVoiceProfile
      .mockImplementationOnce(async () => {
        voiceProfiles = [
          candidateVoice({
            status: "revoked",
            deletion_status: "failed",
            revoked_at: revokedAt,
          }),
        ];
        throw new Error("声音资产删除未完成，请稍后重试");
      })
      .mockImplementationOnce(async () => {
        voiceProfiles = [
          candidateVoice({
            status: "revoked",
            deletion_status: "completed",
            revoked_at: revokedAt,
          }),
        ];
      });

    render(<DigitalSelfPanel onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "撤销声音档案" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销声音档案" }));

    expect(
      await screen.findByText("供应商声音删除未完成。声音档案已停止使用，请重试清理。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试删除声音资产" }));

    await waitFor(() => expect(mocks.revokeVoiceProfile).toHaveBeenCalledTimes(2));
    expect(mocks.revokeVoiceProfile).toHaveBeenLastCalledWith("voice-1");
    expect(screen.queryByRole("button", { name: "重试删除声音资产" })).not.toBeInTheDocument();
  });

  it("exports portable data and requires password plus an exact phrase for account deletion", async () => {
    const onAccountDeleted = vi.fn();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    render(
      <DigitalSelfPanel onBack={vi.fn()} onAccountDeleted={onAccountDeleted} />,
    );

    expect(
      await screen.findByRole("heading", { name: "数据与账户" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("导出验证密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "下载完整档案" }));
    await waitFor(() => {
      expect(mocks.exportAccountArchive).toHaveBeenCalledWith("safe-passphrase");
      expect(anchorClick).toHaveBeenCalledOnce();
    });

    fireEvent.change(screen.getByLabelText("删除验证密码"), {
      target: { value: "safe-passphrase" },
    });
    const deleteButton = screen.getByRole("button", { name: "永久删除全部数据" });
    expect(deleteButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("输入“永久删除我的全部数据”"), {
      target: { value: "永久删除我的全部数据" },
    });
    fireEvent.click(deleteButton);
    await waitFor(() => {
      expect(mocks.deleteAccountData).toHaveBeenCalledWith(
        "safe-passphrase",
        "永久删除我的全部数据",
      );
      expect(onAccountDeleted).toHaveBeenCalledOnce();
    });
    anchorClick.mockRestore();
  });
});
