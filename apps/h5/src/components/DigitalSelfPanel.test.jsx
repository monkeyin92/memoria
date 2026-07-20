import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  activateVoiceProfile: vi.fn(),
  createVoiceBlindTrial: vi.fn(),
  enrollSpeakerProfiles: vi.fn(),
  enrollVoiceProfile: vi.fn(),
  evaluateVoiceProfile: vi.fn(),
  exportAccountArchive: vi.fn(),
  getPersonaStatus: vi.fn(),
  getPersonaTraits: vi.fn(),
  getPersonaVersions: vi.fn(),
  getSpeakerProfiles: vi.fn(),
  getVoiceProfiles: vi.fn(),
  grantPersonaConsent: vi.fn(),
  grantVoiceConsent: vi.fn(),
  previewVoiceBlindTrial: vi.fn(),
  reviewPersonaTrait: vi.fn(),
  revokePersonaConsent: vi.fn(),
  revokeSpeakerProfile: vi.fn(),
  revokeVoiceConsent: vi.fn(),
  revokeVoiceProfile: vi.fn(),
  rollbackPersonaVersion: vi.fn(),
  deleteAccountData: vi.fn(),
  prepareSpeakerEnrollment: vi.fn(),
  prepareVoiceCloneSample: vi.fn(),
}));

vi.mock("../api.js", () => ({
  activateVoiceProfile: mocks.activateVoiceProfile,
  createVoiceBlindTrial: mocks.createVoiceBlindTrial,
  enrollSpeakerProfiles: mocks.enrollSpeakerProfiles,
  enrollVoiceProfile: mocks.enrollVoiceProfile,
  evaluateVoiceProfile: mocks.evaluateVoiceProfile,
  exportAccountArchive: mocks.exportAccountArchive,
  getPersonaStatus: mocks.getPersonaStatus,
  getPersonaTraits: mocks.getPersonaTraits,
  getPersonaVersions: mocks.getPersonaVersions,
  getSpeakerProfiles: mocks.getSpeakerProfiles,
  getVoiceProfiles: mocks.getVoiceProfiles,
  grantPersonaConsent: mocks.grantPersonaConsent,
  grantVoiceConsent: mocks.grantVoiceConsent,
  previewVoiceBlindTrial: mocks.previewVoiceBlindTrial,
  reviewPersonaTrait: mocks.reviewPersonaTrait,
  revokePersonaConsent: mocks.revokePersonaConsent,
  revokeSpeakerProfile: mocks.revokeSpeakerProfile,
  revokeVoiceConsent: mocks.revokeVoiceConsent,
  revokeVoiceProfile: mocks.revokeVoiceProfile,
  rollbackPersonaVersion: mocks.rollbackPersonaVersion,
  deleteAccountData: mocks.deleteAccountData,
}));

vi.mock("../lib/audioEnrollment.js", () => ({
  prepareSpeakerEnrollment: mocks.prepareSpeakerEnrollment,
  prepareVoiceCloneSample: mocks.prepareVoiceCloneSample,
}));

import { DigitalSelfPanel } from "./DigitalSelfPanel.jsx";

let personaAllowed;
let personaTraits;
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
    target_model: "cosyvoice-v3.5-plus",
    created_at: "2026-07-19T00:00:00Z",
    ...overrides,
  };
}

describe("DigitalSelfPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    personaAllowed = false;
    personaTraits = [];
    speakerProfiles = [];
    voiceConsent = null;
    voiceProfiles = [];
    mocks.getPersonaStatus.mockImplementation(async () => ({
      learning_allowed: personaAllowed,
    }));
    mocks.getPersonaTraits.mockImplementation(async () => ({ items: personaTraits }));
    mocks.getPersonaVersions.mockResolvedValue({ items: [] });
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
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(
      await screen.findByRole("heading", { name: "数字心智与声音" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "人格学习" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "声纹识别" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "声音复刻" })).toBeInTheDocument();
    expect(screen.getByText(/声纹只用于区分主人、访客或不确定/)).toBeInTheDocument();
    expect(screen.getByText(/不能单独授权删除、导出或其他敏感操作/)).toBeInTheDocument();
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
        status: "candidate",
      },
      {
        trait_id: "emotion-style-1",
        category: "emotional_expression",
        description: "安慰家人时语气更柔和",
        confidence: 0.8,
        observation_count: 2,
        status: "candidate",
      },
    ];

    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("重音方式")).toBeInTheDocument();
    expect(screen.getByText("情绪表达")).toBeInTheDocument();
  });

  it("grants and revokes persona learning and lets the owner review a candidate trait", async () => {
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
    mocks.reviewPersonaTrait.mockImplementation(async () => {
      personaTraits = [{ ...personaTraits[0], status: "confirmed" }];
    });
    mocks.revokePersonaConsent.mockImplementation(async () => {
      personaAllowed = false;
    });
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    const consent = await screen.findByLabelText("我同意 Memoria 学习我的表达与思维偏好");
    fireEvent.click(consent);
    fireEvent.click(screen.getByRole("button", { name: "开启人格学习" }));
    await waitFor(() => expect(mocks.grantPersonaConsent).toHaveBeenCalledOnce());

    fireEvent.click(await screen.findByRole("button", { name: "确认这条特征" }));
    await waitFor(() => {
      expect(mocks.reviewPersonaTrait).toHaveBeenCalledWith("trait-1", "confirm");
    });

    fireEvent.click(screen.getByRole("button", { name: "撤销人格学习授权" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销人格学习" }));
    await waitFor(() => expect(mocks.revokePersonaConsent).toHaveBeenCalledOnce());
  });

  it("requires an owner-supplied counterexample before confirming a decision trait", async () => {
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

    const confirmation = await screen.findByRole("button", { name: "确认这条特征" });
    expect(confirmation).toBeDisabled();
    fireEvent.change(screen.getByLabelText("例外或反例（确认前必填）"), {
      target: { value: "紧急安全风险出现时会立即行动。" },
    });
    fireEvent.click(confirmation);

    await waitFor(() => {
      expect(mocks.reviewPersonaTrait).toHaveBeenCalledWith(
        "decision-1",
        "confirm",
        { counterexample: "紧急安全风险出现时会立即行动。" },
      );
    });
  });

  it("shows an existing counterexample for a value trait without blocking confirmation", async () => {
    personaAllowed = true;
    personaTraits = [
      {
        trait_id: "value-1",
        category: "value_priority",
        description: "通常优先兑现承诺",
        counterexample: "承诺会伤害家人安全时会重新协商。",
        confidence: 0.9,
        observation_count: 9,
        status: "candidate",
      },
    ];
    render(<DigitalSelfPanel onBack={vi.fn()} />);

    expect(await screen.findByText("承诺会伤害家人安全时会重新协商。"))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认这条特征" })).toBeEnabled();
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

  it("only enables voice activation after subjective and server quality gates pass", async () => {
    voiceConsent = { policy_version: "voice-clone-v1", granted_at: "2026-07-19" };
    voiceProfiles = [
      candidateVoice({ evaluation_status: "passed", quality_status: "passed" }),
    ];
    mocks.activateVoiceProfile.mockImplementation(async () => {
      voiceProfiles = [
        candidateVoice({
          status: "active",
          evaluation_status: "passed",
          quality_status: "passed",
        }),
      ];
    });

    render(<DigitalSelfPanel onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "激活这个声音" }));

    await waitFor(() => {
      expect(mocks.activateVoiceProfile).toHaveBeenCalledWith("voice-1");
    });
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
