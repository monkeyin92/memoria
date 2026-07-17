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
  flushPendingMessages: vi.fn().mockResolvedValue(undefined),
  getMemoryDays: vi.fn().mockResolvedValue({ items: [] }),
  getProfile: vi.fn(),
  saveMessage: vi.fn().mockResolvedValue(undefined),
  summarizeDay: vi.fn().mockResolvedValue(undefined),
  updateProfile: vi.fn().mockResolvedValue(undefined),
  useVoiceSession: vi.fn(),
  resumeAudio: vi.fn().mockResolvedValue(true),
}));

vi.mock("./api.js", () => ({
  bootstrapIdentity: mocks.bootstrapIdentity,
  cachePendingMessage: mocks.cachePendingMessage,
  flushPendingMessages: mocks.flushPendingMessages,
  getMemoryDays: mocks.getMemoryDays,
  getProfile: mocks.getProfile,
  saveMessage: mocks.saveMessage,
  summarizeDay: mocks.summarizeDay,
  updateProfile: mocks.updateProfile,
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
    error: "",
    audioBlocked: false,
    audioContainerRef: { current: null },
    start: vi.fn(),
    resumeAudio: mocks.resumeAudio,
    toggleMic: vi.fn(),
    stopAssistant: vi.fn(),
    end: vi.fn().mockResolvedValue(undefined),
  };
}

describe("App identity and profile preferences", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mocks.flushPendingMessages.mockResolvedValue(undefined);
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
    expect(mocks.resumeAudio).toHaveBeenCalledWith(true);
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

  it("defaults to cascade and persists Omni Flash / Plus backends", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    const first = render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    const cascade = screen.getByRole("radio", { name: "级联" });
    const omniFlash = screen.getByRole("radio", { name: "Omni Flash" });
    const omniPlus = screen.getByRole("radio", { name: "Omni Plus" });
    expect(cascade).toHaveAttribute("aria-checked", "true");
    expect(omniFlash).toHaveAttribute("aria-checked", "false");
    expect(omniPlus).toHaveAttribute("aria-checked", "false");

    fireEvent.click(omniFlash);
    expect(omniFlash).toHaveAttribute("aria-checked", "true");
    expect(window.localStorage.getItem("memoria:voice-backend")).toBe(
      "qwen_omni",
    );
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ voiceBackend: "qwen_omni" }),
    );

    fireEvent.click(omniPlus);
    expect(omniPlus).toHaveAttribute("aria-checked", "true");
    expect(window.localStorage.getItem("memoria:voice-backend")).toBe(
      "qwen_omni_plus",
    );
    expect(mocks.useVoiceSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ voiceBackend: "qwen_omni_plus" }),
    );

    first.unmount();
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });
    expect(screen.getByRole("radio", { name: "Omni Plus" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("locks the backend selector while a voice session is active", async () => {
    mocks.bootstrapIdentity.mockResolvedValue({
      user_id: "anonymous-user",
      access_token: "token",
    });
    mocks.useVoiceSession.mockImplementation(() => ({
      ...voiceState(),
      session: { session_id: "active-session" },
      uiState: "ready",
      statusLabel: "我在这里",
    }));
    render(<App />);
    await screen.findByRole("heading", { name: /小忆/ });

    expect(screen.getByRole("radio", { name: "级联" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Omni Flash" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Omni Plus" })).toBeDisabled();
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
