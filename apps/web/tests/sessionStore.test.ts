import { beforeEach, describe, expect, it } from "vitest";

import { useSessionStore } from "../src/state/sessionStore";

const session = {
  session_id: "s",
  livekit_url: "wss://example.livekit.cloud",
  room_name: "voice-x",
  participant_token: "jwt-token",
  expires_in: 300,
  agent_name: "duplex-zh-agent",
  config: { locale: "zh-CN", allow_text_fallback: true },
};

describe("sessionStore", () => {
  beforeEach(() => {
    useSessionStore.getState().reset();
  });

  it("maps server states and drops older state events", () => {
    const store = useSessionStore.getState();
    store.applyAssistantState(5, "speaking", 2);
    store.applyAssistantState(4, "thinking", 1);
    expect(useSessionStore.getState()).toMatchObject({
      generationId: 5,
      uiState: "speaking",
    });

    store.applyAssistantState(5, "interruption_pending", 2);
    expect(useSessionStore.getState().uiState).toBe("interrupted");
    store.applyAssistantState(5, "recovering", 2);
    expect(useSessionStore.getState().uiState).toBe("reconnecting");
    store.applyAssistantState(5, "closed", 2);
    expect(useSessionStore.getState().uiState).toBe("closed");
  });

  it.each([
    ["thinking", "thinking"],
    ["listening", "listening"],
    ["user_speaking", "listening"],
    ["eot_pending", "listening"],
    ["tool_waiting", "tool_waiting"],
    ["connecting", "ready"],
  ] as const)("maps %s to %s", (serverState, expected) => {
    useSessionStore.getState().applyAssistantState(1, serverState, 1);
    expect(useSessionStore.getState().uiState).toBe(expected);
  });

  it("drops older transcript events for both speakers", () => {
    const store = useSessionStore.getState();
    store.applyAssistantState(5, "speaking", 2);
    store.applyTranscript({
      speaker: "assistant",
      text: "新内容",
      final: false,
      turn_id: 2,
      generation_id: 5,
    });
    store.applyTranscript({
      speaker: "user",
      text: "旧内容",
      final: true,
      turn_id: 1,
      generation_id: 3,
    });
    const lines = useSessionStore.getState().transcripts;
    expect(lines).toHaveLength(1);
    expect(lines[0].text).toBe("新内容");
  });

  it("replaces interim text and accepts only server-authoritative heard truncation", () => {
    const store = useSessionStore.getState();
    store.applyTranscript({
      speaker: "assistant",
      text: "用户还没有全部听到这段话",
      final: false,
      heard: false,
      turn_id: 1,
      generation_id: 1,
    });
    store.applyTranscript({
      speaker: "assistant",
      text: "用户听到了这段",
      final: true,
      heard: true,
      turn_id: 1,
      generation_id: 1,
    });
    expect(useSessionStore.getState().transcripts).toEqual([
      expect.objectContaining({
        text: "用户听到了这段",
        final: true,
        heard: true,
      }),
    ]);
  });

  it("advances the generation on RTC recovery", () => {
    const store = useSessionStore.getState();
    store.applyAssistantState(3, "speaking", 1);
    store.advanceGeneration();
    expect(useSessionStore.getState()).toMatchObject({
      generationId: 4,
      awaitingServerGeneration: true,
    });
  });

  it("ends locally while preserving the transcript", () => {
    const store = useSessionStore.getState();
    store.setSession(session);
    store.applyTranscript({
      speaker: "user",
      text: "保留的内容",
      final: true,
      turn_id: 1,
      generation_id: 1,
    });
    store.endSession();
    expect(useSessionStore.getState()).toMatchObject({
      session: null,
      uiState: "closed",
      error: null,
    });
    expect(useSessionStore.getState().transcripts).toHaveLength(1);
  });

  it("starts a new session cleanly and never stores provider secrets", () => {
    const store = useSessionStore.getState();
    store.setError("old error");
    store.applyAssistantState(7, "speaking", 3);
    store.setSession(session);
    expect(useSessionStore.getState()).toMatchObject({
      generationId: 0,
      transcripts: [],
      error: null,
    });
    expect(JSON.stringify(useSessionStore.getState().session)).not.toMatch(
      /API_SECRET|DASHSCOPE|DEEPSEEK_API/,
    );
  });

  it("applies synchronized heard progress only after server generation alignment", () => {
    const store = useSessionStore.getState();
    store.applyAssistantState(3, "speaking", 7);
    store.applySynchronizedAssistantTranscript([
      {
        id: "sync-1",
        text: "实际已播放",
        language: "zh-CN",
        startTime: 0,
        endTime: 1,
        final: false,
        firstReceivedTime: 0,
        lastReceivedTime: 1,
      },
    ]);
    expect(useSessionStore.getState().transcripts[0]).toMatchObject({
      text: "实际已播放",
      heard: true,
      turn_id: 7,
      generation_id: 3,
    });

    store.advanceGeneration();
    store.applySynchronizedAssistantTranscript([
      {
        id: "stale-sync",
        text: "重连前积压",
        language: "zh-CN",
        startTime: 0,
        endTime: 1,
        final: true,
        firstReceivedTime: 0,
        lastReceivedTime: 1,
      },
    ]);
    expect(useSessionStore.getState().transcripts).toHaveLength(1);
  });
});
