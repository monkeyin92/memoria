import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useVoiceSession } from "../src/hooks/useVoiceSession";
import { useSessionStore } from "../src/state/sessionStore";

const api = vi.hoisted(() => ({
  createSession: vi.fn(),
  notifyRtcRecovered: vi.fn(),
  stopResponse: vi.fn(),
}));

vi.mock("../src/api/controlApi", () => api);

const session = {
  session_id: "session-1",
  livekit_url: "wss://example.livekit.cloud",
  room_name: "voice-1",
  participant_token: "short-lived-token",
  expires_in: 300,
  agent_name: "duplex-zh-agent",
  config: { locale: "zh-CN", allow_text_fallback: true },
};

const encode = (value: unknown) =>
  new TextEncoder().encode(JSON.stringify(value));

describe("useVoiceSession", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSessionStore.getState().reset();
    api.createSession.mockResolvedValue(session);
    api.notifyRtcRecovered.mockResolvedValue(undefined);
    api.stopResponse.mockResolvedValue(undefined);
  });

  it("starts a session and applies validated data events", async () => {
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => {
      await result.current.start();
    });
    act(() => {
      result.current.handleData(
        encode({
          type: "assistant_state",
          session_id: session.session_id,
          state: "speaking",
          phase: "speaking",
          turn_id: 1,
          generation_id: 2,
          at: "2026-07-15T00:00:00Z",
        }),
      );
      result.current.handleData(
        encode({
          type: "transcript_delta",
          session_id: session.session_id,
          speaker: "assistant",
          text: "服务端已听文本",
          final: true,
          heard: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 2,
          tool_epoch: 0,
        }),
      );
    });
    expect(result.current.uiState).toBe("speaking");
    expect(result.current.transcripts[0]).toMatchObject({
      text: "服务端已听文本",
      heard: true,
    });
  });

  it("ignores invalid, mismatched-session, and stale data", async () => {
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => void (await result.current.start()));
    act(() => {
      result.current.handleData(new Uint8Array([1, 2, 3]));
      result.current.handleData(
        encode({
          type: "assistant_state",
          session_id: "another-session",
          state: "speaking",
          phase: "speaking",
          turn_id: 1,
          generation_id: 9,
          at: "2026-07-15T00:00:00Z",
        }),
      );
      result.current.handleData(
        encode({
          type: "transcript_delta",
          session_id: "another-session",
          speaker: "assistant",
          text: "不应跨会话显示",
          final: true,
          heard: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 9,
          tool_epoch: 0,
        }),
      );
    });
    expect(result.current.uiState).toBe("ready");
    expect(useSessionStore.getState().generationId).toBe(0);
    expect(result.current.transcripts).toEqual([]);
  });

  it("does not locally truncate assistant text after stop", async () => {
    useSessionStore.getState().setSession(session);
    useSessionStore.getState().applyTranscript({
      speaker: "assistant",
      text: "仍等待服务端 heard 文本",
      final: false,
      heard: false,
      turn_id: 1,
      generation_id: 1,
    });
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => {
      await result.current.stopAssistant();
    });
    expect(api.stopResponse).toHaveBeenCalledWith(session.session_id);
    expect(result.current.transcripts[0]).toMatchObject({
      text: "仍等待服务端 heard 文本",
      final: false,
      heard: false,
    });
  });

  it("uses synchronized agent transcription as heard progress", async () => {
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => void (await result.current.start()));
    act(() => {
      result.current.handleData(
        encode({
          type: "assistant_state",
          session_id: session.session_id,
          state: "speaking",
          phase: "speaking",
          turn_id: 3,
          generation_id: 4,
          at: "2026-07-15T00:00:00Z",
        }),
      );
      result.current.handleSynchronizedTranscript([
        {
          id: "sync-1",
          text: "用户已经听到",
          language: "zh-CN",
          startTime: 0,
          endTime: 1,
          final: false,
          firstReceivedTime: 0,
          lastReceivedTime: 1,
        },
      ]);
    });
    expect(result.current.transcripts[0]).toMatchObject({
      text: "用户已经听到",
      final: false,
      heard: true,
      turn_id: 3,
      generation_id: 4,
    });
  });

  it("surfaces stop failures without changing the transcript", async () => {
    useSessionStore.getState().setSession(session);
    api.stopResponse.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => {
      await result.current.stopAssistant();
    });
    expect(result.current.error).toBe("network down");
  });

  it("advances generation only after the server accepts RTC recovery", async () => {
    useSessionStore.getState().setSession(session);
    const { result } = renderHook(() => useVoiceSession());
    await act(async () => {
      expect(await result.current.handleReconnected()).toBe(true);
    });
    expect(api.notifyRtcRecovered).toHaveBeenCalledWith(session.session_id);
    expect(useSessionStore.getState()).toMatchObject({
      generationId: 1,
      awaitingServerGeneration: true,
    });

    api.notifyRtcRecovered.mockRejectedValueOnce(new Error("recovery failed"));
    await act(async () => {
      expect(await result.current.handleReconnected()).toBe(false);
    });
    expect(useSessionStore.getState().generationId).toBe(1);
    expect(result.current.error).toBe("recovery failed");
  });
});
