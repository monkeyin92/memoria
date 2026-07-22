import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  bootstrapIdentity: vi.fn(),
  cachePendingMessage: vi.fn(),
  deleteAccountData: vi.fn(),
  endVoice: vi.fn().mockResolvedValue(undefined),
  resetVoice: vi.fn().mockResolvedValue(undefined),
  exportAccountArchive: vi.fn(),
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
  getRawVoiceConsent: vi.fn().mockResolvedValue({ consent: null }),
  getProfile: vi.fn(),
  loginAccount: vi.fn(),
  logoutAllDevices: vi.fn(),
  logoutCurrentDevice: vi.fn(),
  registerAccount: vi.fn(),
  reviewMemoryClaim: vi.fn(),
  saveMessage: vi.fn().mockResolvedValue(undefined),
  searchLifeArchive: vi.fn().mockResolvedValue({ items: [] }),
  summarizeDay: vi.fn().mockResolvedValue(undefined),
  updateProfile: vi.fn().mockResolvedValue(undefined),
  getPersonaStatus: vi.fn().mockResolvedValue({ learning_allowed: false }),
  getPersonaTraits: vi.fn().mockResolvedValue({ items: [] }),
  getPersonaVersions: vi.fn().mockResolvedValue({ items: [] }),
  getSpeakerProfiles: vi.fn().mockResolvedValue({ items: [] }),
  getVoiceProfiles: vi.fn().mockResolvedValue({ consent: null, items: [] }),
  grantPersonaConsent: vi.fn(),
  grantRawVoiceConsent: vi.fn(),
  revokePersonaConsent: vi.fn(),
  revokeRawVoiceConsent: vi.fn(),
  reviewPersonaTrait: vi.fn(),
  rollbackPersonaVersion: vi.fn(),
  enrollSpeakerProfiles: vi.fn(),
  revokeSpeakerProfile: vi.fn(),
  grantVoiceConsent: vi.fn(),
  revokeVoiceConsent: vi.fn(),
  enrollVoiceProfile: vi.fn(),
  previewVoiceProfile: vi.fn(),
  evaluateVoiceProfile: vi.fn(),
  activateVoiceProfile: vi.fn(),
  revokeVoiceProfile: vi.fn(),
  useVoiceSession: vi.fn(),
  resumeAudio: vi.fn().mockResolvedValue(true),
}));

vi.mock("./api.js", () => ({
  bootstrapIdentity: mocks.bootstrapIdentity,
  cachePendingMessage: mocks.cachePendingMessage,
  deleteAccountData: mocks.deleteAccountData,
  exportAccountArchive: mocks.exportAccountArchive,
  flushPendingMessages: mocks.flushPendingMessages,
  getMemoryDays: mocks.getMemoryDays,
  getLifeTimeline: mocks.getLifeTimeline,
  getMemoryReviewQueue: mocks.getMemoryReviewQueue,
  getInteractionCapabilities: mocks.getInteractionCapabilities,
  getRawVoiceConsent: mocks.getRawVoiceConsent,
  getProfile: mocks.getProfile,
  loginAccount: mocks.loginAccount,
  logoutAllDevices: mocks.logoutAllDevices,
  logoutCurrentDevice: mocks.logoutCurrentDevice,
  registerAccount: mocks.registerAccount,
  reviewMemoryClaim: mocks.reviewMemoryClaim,
  saveMessage: mocks.saveMessage,
  searchLifeArchive: mocks.searchLifeArchive,
  summarizeDay: mocks.summarizeDay,
  updateProfile: mocks.updateProfile,
  getPersonaStatus: mocks.getPersonaStatus,
  getPersonaTraits: mocks.getPersonaTraits,
  getPersonaVersions: mocks.getPersonaVersions,
  getSpeakerProfiles: mocks.getSpeakerProfiles,
  getVoiceProfiles: mocks.getVoiceProfiles,
  grantPersonaConsent: mocks.grantPersonaConsent,
  grantRawVoiceConsent: mocks.grantRawVoiceConsent,
  revokePersonaConsent: mocks.revokePersonaConsent,
  revokeRawVoiceConsent: mocks.revokeRawVoiceConsent,
  reviewPersonaTrait: mocks.reviewPersonaTrait,
  rollbackPersonaVersion: mocks.rollbackPersonaVersion,
  enrollSpeakerProfiles: mocks.enrollSpeakerProfiles,
  revokeSpeakerProfile: mocks.revokeSpeakerProfile,
  grantVoiceConsent: mocks.grantVoiceConsent,
  revokeVoiceConsent: mocks.revokeVoiceConsent,
  enrollVoiceProfile: mocks.enrollVoiceProfile,
  previewVoiceProfile: mocks.previewVoiceProfile,
  evaluateVoiceProfile: mocks.evaluateVoiceProfile,
  activateVoiceProfile: mocks.activateVoiceProfile,
  revokeVoiceProfile: mocks.revokeVoiceProfile,
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
    emotionHint: null,
    error: "",
    audioBlocked: false,
    audioContainerRef: { current: null },
    start: vi.fn(),
    resumeAudio: mocks.resumeAudio,
    toggleMic: vi.fn(),
    stopAssistant: vi.fn(),
    end: mocks.endVoice,
    reset: mocks.resetVoice,
  };
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
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "safe-passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    await waitFor(() => {
      expect(mocks.registerAccount).toHaveBeenCalledWith(
        "memorykeeper",
        "safe-passphrase",
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
    expect(screen.getByText("专属凭证保护你的对话")).toBeInTheDocument();
    expect(screen.queryByText("你的对话只属于你")).not.toBeInTheDocument();

    fireEvent.click(voiceReply);
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

  it("opens companion switching from interaction mode and saves only the next-session style", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      account_type: "registered",
      access_token: "token",
    });
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
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });
    fireEvent.click(screen.getByRole("button", { name: "我的" }));

    fireEvent.click(await screen.findByRole("button", { name: /退出当前设备/ }));

    await waitFor(() => {
      expect(mocks.logoutCurrentDevice).toHaveBeenCalledOnce();
      expect(mocks.resetVoice).toHaveBeenCalledOnce();
    });
    expect(await screen.findByRole("heading", { name: "创建你的 Memoria 账号" }))
      .toBeInTheDocument();
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
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "token",
    });
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

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
    });
    expect(
      await screen.findByRole("heading", { name: "创建你的 Memoria 账号" }),
    ).toBeInTheDocument();
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
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.getMemoryDays.mockResolvedValue({
      items: [
        {
          day: "2026-07-15",
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
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    fireEvent.click(screen.getByRole("button", { name: "回顾" }));
    expect(await screen.findByText("由 LLM 从今日对话中整理"))
      .toBeInTheDocument();
  });
});
