import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const liveKit = vi.hoisted(() => ({
  instances: [],
  rejectStartAudioCount: 0,
  Room: null,
  RoomEvent: {
    TrackSubscribed: "track-subscribed",
    TrackUnsubscribed: "track-unsubscribed",
    DataReceived: "data-received",
    TranscriptionReceived: "transcription-received",
    Reconnecting: "reconnecting",
    Reconnected: "reconnected",
    Disconnected: "disconnected",
  },
}));

const api = vi.hoisted(() => ({
  createSession: vi.fn(),
  exchangeOmniSdp: vi.fn(),
  publishOmniTelemetry: vi.fn(),
  notifyRtcRecovered: vi.fn(),
  stopResponse: vi.fn(),
}));

const omni = vi.hoisted(() => ({ instances: [] }));

vi.mock("livekit-client", () => {
  class Room {
    static getLocalDevices = vi.fn();

    constructor() {
      this.handlers = new Map();
      this.connect = vi.fn().mockResolvedValue(undefined);
      this.disconnect = vi.fn().mockResolvedValue(undefined);
      this.startAudio = vi.fn(() => {
        if (liveKit.rejectStartAudioCount > 0) {
          liveKit.rejectStartAudioCount -= 1;
          return Promise.reject(new Error("autoplay blocked"));
        }
        return Promise.resolve();
      });
      this.localParticipant = {
        setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
      };
      liveKit.instances.push(this);
    }

    on(event, handler) {
      this.handlers.set(event, handler);
      return this;
    }

    off(event, handler) {
      if (this.handlers.get(event) === handler) this.handlers.delete(event);
      return this;
    }

    emit(event, ...args) {
      this.handlers.get(event)?.(...args);
    }
  }

  liveKit.Room = Room;
  return { Room, RoomEvent: liveKit.RoomEvent };
});

vi.mock("../api.js", () => ({
  createSession: api.createSession,
  exchangeOmniSdp: api.exchangeOmniSdp,
  publishOmniTelemetry: api.publishOmniTelemetry,
  notifyRtcRecovered: api.notifyRtcRecovered,
  stopResponse: api.stopResponse,
}));

vi.mock("../voice/QwenOmniWebRTCTransport.js", () => ({
  QwenOmniWebRTCTransport: class {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this.prepare = vi.fn().mockResolvedValue(undefined);
      this.connect = vi.fn(async () => callbacks.onState("ready"));
      this.setMicrophoneEnabled = vi.fn().mockResolvedValue(undefined);
      this.cancelResponse = vi.fn(() => callbacks.onState("interrupted"));
      this.close = vi.fn();
      omni.instances.push(this);
    }
  },
}));

import { useVoiceSession } from "./useVoiceSession.js";

function encodeEvent(event) {
  return new TextEncoder().encode(JSON.stringify(event));
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

async function renderStartedHook({ voiceReplyEnabled = true } = {}) {
  const onFinalTranscript = vi.fn();
  const rendered = renderHook(
    ({ enabled }) =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript,
        voiceReplyEnabled: enabled,
      }),
    { initialProps: { enabled: voiceReplyEnabled } },
  );
  rendered.result.current.audioContainerRef.current = document.createElement("div");
  await act(async () => {
    await rendered.result.current.start();
  });
  const room = liveKit.instances.at(-1);
  act(() => {
    room.emit(
      liveKit.RoomEvent.DataReceived,
      encodeEvent({
        type: "assistant_state",
        session_id: "session-1",
        state: "ready",
        turn_id: 0,
        generation_id: 0,
      }),
      { isAgent: true },
      null,
      "voice-agent.ui",
    );
  });
  await waitFor(() => expect(rendered.result.current.uiState).toBe("ready"));
  return {
    ...rendered,
    onFinalTranscript,
    room,
  };
}

describe("useVoiceSession production edges", () => {
  beforeEach(async () => {
    await import("livekit-client");
    vi.clearAllMocks();
    liveKit.instances.length = 0;
    omni.instances.length = 0;
    liveKit.rejectStartAudioCount = 0;
    liveKit.Room.getLocalDevices.mockResolvedValue([
      { deviceId: "microphone" },
    ]);
    api.createSession.mockResolvedValue({
      session_id: "session-1",
      livekit_url: "wss://livekit.example",
      participant_token: "participant-token",
    });
    api.notifyRtcRecovered.mockResolvedValue(undefined);
    api.exchangeOmniSdp.mockResolvedValue("answer-sdp");
    api.publishOmniTelemetry.mockResolvedValue(null);
    api.stopResponse.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("relies on LiveKit startAudio before the session request", async () => {
    const AudioContextConstructor = vi.fn();
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      writable: true,
      value: AudioContextConstructor,
    });
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );

    let startPromise;
    act(() => {
      startPromise = result.current.start();
      expect(AudioContextConstructor).not.toHaveBeenCalled();
      expect(liveKit.instances).toHaveLength(1);
      expect(liveKit.instances[0].startAudio).toHaveBeenCalledTimes(1);
      expect(liveKit.instances[0].startAudio.mock.invocationCallOrder[0]).toBeLessThan(
        api.createSession.mock.invocationCallOrder[0],
      );
    });
    await act(async () => {
      await startPromise;
    });
  });

  it("routes Qwen Omni through its WebRTC transport without calling LiveKit control APIs", async () => {
    api.createSession.mockResolvedValueOnce({
      session_id: "omni-session",
      voice_backend: "qwen_omni",
      config: { voice: "Tina" },
    });
    const onFinalTranscript = vi.fn();
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript,
        voiceReplyEnabled: true,
        voiceBackend: "qwen_omni",
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");

    await act(async () => {
      await result.current.start();
    });

    const transport = omni.instances[0];
    expect(liveKit.instances).toHaveLength(0);
    expect(api.createSession).toHaveBeenCalledWith(
      "anonymous-user",
      "qwen_omni",
      null,
    );
    expect(transport.prepare).toHaveBeenCalledTimes(1);
    expect(transport.callbacks.speakerVerifyEnabled).toBe(false);
    expect(transport.connect).toHaveBeenCalledWith(
      expect.objectContaining({ session_id: "omni-session" }),
    );
    expect(result.current.uiState).toBe("ready");

    act(() => {
      transport.callbacks.onTranscript({
        speaker: "assistant",
        text: "我在这里。",
        final: true,
        heard: false,
        turn_id: 1,
        generation_id: 1,
      });
      transport.callbacks.onTranscript({
        speaker: "user",
        text: "你好。",
        final: true,
        heard: false,
        history_eligible: true,
        turn_id: 1,
        generation_id: 1,
      });
    });
    expect(onFinalTranscript).toHaveBeenCalledTimes(1);
    expect(onFinalTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ speaker: "user", text: "你好。" }),
    );

    await act(async () => {
      await result.current.toggleMic();
      await result.current.stopAssistant();
    });
    expect(transport.setMicrophoneEnabled).toHaveBeenCalledWith(false);
    expect(transport.cancelResponse).toHaveBeenCalledTimes(1);
    expect(api.stopResponse).not.toHaveBeenCalled();
    expect(result.current.uiState).toBe("interrupted");

    await act(async () => {
      await result.current.end();
    });
    expect(transport.close).toHaveBeenCalled();
  });

  it("keeps a single audio element when Omni repeats the remote stream", async () => {
    api.createSession.mockResolvedValueOnce({
      session_id: "omni-session",
      voice_backend: "qwen_omni",
      config: { voice: "Tina" },
    });
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
        voiceBackend: "qwen_omni",
      }),
    );
    const container = document.createElement("div");
    result.current.audioContainerRef.current = container;
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);

    await act(async () => {
      await result.current.start();
    });
    const stream = { id: "remote-audio" };
    act(() => {
      omni.instances[0].callbacks.onRemoteStream(stream);
      omni.instances[0].callbacks.onRemoteStream(stream);
    });

    expect(container.querySelectorAll("audio")).toHaveLength(1);
    expect(container.querySelector("audio").srcObject).toBe(stream);

    act(() => {
      omni.instances[0].callbacks.onDiagnostic("omni_speech_started");
    });
    expect(container.querySelector("audio").muted).toBe(true);

    act(() => {
      omni.instances[0].callbacks.onDiagnostic("omni_response_created");
    });
    expect(container.querySelector("audio").muted).toBe(false);
  });

  it("waits for explicit agent readiness and remains retryable after a 45 second timeout", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");

    await act(async () => {
      await result.current.start();
    });
    const readyRoom = liveKit.instances[0];
    expect(readyRoom.connect).toHaveBeenCalledTimes(1);
    expect(result.current.uiState).toBe("connecting");

    act(() => {
      for (const state of ["connecting", "listening", "unknown-state"]) {
        readyRoom.emit(
          liveKit.RoomEvent.DataReceived,
          encodeEvent({
            type: "assistant_state",
            session_id: "session-1",
            state,
            turn_id: 0,
            generation_id: 0,
          }),
          { isAgent: true },
          null,
          "voice-agent.ui",
        );
      }
    });
    expect(result.current.uiState).toBe("connecting");

    act(() => {
      readyRoom.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.uiState).toBe("ready");

    await act(async () => {
      await result.current.end();
      await result.current.start();
    });
    const timedOutRoom = liveKit.instances[1];
    expect(result.current.uiState).toBe("connecting");
    expect(result.current.session).toEqual(
      expect.objectContaining({ session_id: "session-1" }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(44_999);
    });
    expect(timedOutRoom.disconnect).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await Promise.resolve();
    });
    expect(timedOutRoom.disconnect).toHaveBeenCalledTimes(1);
    expect(result.current.session).toBeNull();
    expect(result.current.uiState).toBe("closed");
    expect(result.current.error).toMatch(/超时.*再试/);
  });

  it("does not arm a late timeout when ready arrives before microphone setup finishes", async () => {
    vi.useFakeTimers();
    const micSetup = deferred();
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");

    let startPromise;
    let room;
    act(() => {
      startPromise = result.current.start();
      room = liveKit.instances[0];
      room.localParticipant.setMicrophoneEnabled.mockImplementationOnce(
        () => micSetup.promise,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalled();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      micSetup.resolve();
    });
    await act(async () => {
      await startPromise;
      await vi.advanceTimersByTimeAsync(45_000);
    });

    expect(result.current.uiState).toBe("ready");
    expect(room.disconnect).not.toHaveBeenCalled();
  });

  it("does not revive a cancelled start or let it affect a newer room", async () => {
    const firstSession = deferred();
    api.createSession
      .mockImplementationOnce(() => firstSession.promise)
      .mockResolvedValueOnce({
        session_id: "session-2",
        livekit_url: "wss://livekit.example",
        participant_token: "participant-token-2",
      });
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");

    let oldStart;
    act(() => {
      oldStart = result.current.start();
    });
    const oldRoom = liveKit.instances[0];
    await act(async () => {
      await result.current.end();
      await result.current.start();
    });
    const currentRoom = liveKit.instances[1];
    act(() => {
      currentRoom.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-2",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      firstSession.resolve({
        session_id: "session-old",
        livekit_url: "wss://livekit.example",
        participant_token: "participant-token-old",
      });
    });
    await act(async () => {
      await oldStart;
    });

    expect(oldRoom.connect).not.toHaveBeenCalled();
    expect(oldRoom.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    expect(currentRoom.disconnect).not.toHaveBeenCalled();
    expect(result.current.session.session_id).toBe("session-2");
    expect(result.current.uiState).toBe("ready");
  });

  it("cleans handlers and ignores stale room events after a new session starts", async () => {
    const first = await renderStartedHook();
    const oldDisconnected = first.room.handlers.get(
      liveKit.RoomEvent.Disconnected,
    );
    const oldReconnecting = first.room.handlers.get(
      liveKit.RoomEvent.Reconnecting,
    );
    const oldData = first.room.handlers.get(liveKit.RoomEvent.DataReceived);
    await act(async () => {
      await first.result.current.end();
    });
    expect(first.room.handlers.size).toBe(0);

    api.createSession.mockResolvedValueOnce({
      session_id: "session-2",
      livekit_url: "wss://livekit.example",
      participant_token: "participant-token-2",
    });
    await act(async () => {
      await first.result.current.start();
    });
    const currentRoom = liveKit.instances[1];
    act(() => {
      currentRoom.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-2",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      oldDisconnected();
      oldReconnecting();
      oldData(
        encodeEvent({
          type: "transcript_delta",
          speaker: "assistant",
          text: "旧会话字幕",
          final: true,
          heard: true,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    expect(currentRoom.disconnect).not.toHaveBeenCalled();
    expect(first.result.current.session.session_id).toBe("session-2");
    expect(first.result.current.uiState).toBe("ready");
    expect(first.result.current.transcripts).toEqual([]);
  });

  it("accepts the recovered generation without double incrementing it", async () => {
    const recovery = deferred();
    api.notifyRtcRecovered.mockImplementationOnce(() => recovery.promise);
    const { result, room } = await renderStartedHook();

    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
      room.emit(liveKit.RoomEvent.Reconnected);
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "listening",
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      recovery.resolve();
    });
    await act(async () => {
      await recovery.promise;
      await Promise.resolve();
    });
    expect(api.notifyRtcRecovered).toHaveBeenCalledTimes(1);

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "speaking",
          turn_id: 1,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "assistant",
          text: "恢复后的回答",
          final: true,
          heard: true,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.uiState).toBe("listening");
    expect(result.current.latestTranscript.text).toBe("恢复后的回答");
  });

  it("ignores an older recovery failure after a newer recovery cycle succeeds", async () => {
    const firstRecovery = deferred();
    const secondRecovery = deferred();
    api.notifyRtcRecovered
      .mockImplementationOnce(() => firstRecovery.promise)
      .mockImplementationOnce(() => secondRecovery.promise);
    const { result, room } = await renderStartedHook();

    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
    });
    await waitFor(() =>
      expect(api.notifyRtcRecovered).toHaveBeenCalledTimes(1),
    );
    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
    });
    await waitFor(() =>
      expect(api.notifyRtcRecovered).toHaveBeenCalledTimes(2),
    );

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "listening",
          turn_id: 2,
          generation_id: 2,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      secondRecovery.resolve();
      firstRecovery.reject(new Error("stale recovery failed"));
    });
    await act(async () => {
      await Promise.allSettled([
        firstRecovery.promise,
        secondRecovery.promise,
      ]);
      await Promise.resolve();
    });

    expect(room.disconnect).not.toHaveBeenCalled();
    expect(result.current.session.session_id).toBe("session-1");
    expect(result.current.uiState).toBe("listening");
    expect(result.current.error).toBe("");
  });

  it("persists only authoritative transcript_delta events", async () => {
    const { result, room, onFinalTranscript } = await renderStartedHook();
    const agent = { isAgent: true };

    act(() => {
      room.emit(
        liveKit.RoomEvent.TranscriptionReceived,
        [{ text: "仅供显示", final: true }],
        agent,
      );
    });
    expect(result.current.latestTranscript.text).toBe("仅供显示");
    expect(onFinalTranscript).not.toHaveBeenCalled();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "user",
          text: "主人问题",
          final: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });
    expect(onFinalTranscript).toHaveBeenCalledTimes(1);

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "assistant",
          text: "权威回答",
          final: true,
          heard: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });
    expect(onFinalTranscript).toHaveBeenCalledTimes(2);
    expect(onFinalTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ text: "权威回答" }),
    );
    expect(result.current.latestTranscript.text).toBe("权威回答");

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "assistant",
          text: "同一代修订后的最终稿",
          final: true,
          heard: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.latestTranscript.text).toBe("同一代修订后的最终稿");
    expect(onFinalTranscript).toHaveBeenCalledTimes(2);

    act(() => {
      room.emit(
        liveKit.RoomEvent.TranscriptionReceived,
        [{ text: "迟到的回退文本", final: true }],
        agent,
      );
    });
    expect(result.current.latestTranscript.text).toBe("同一代修订后的最终稿");
    expect(onFinalTranscript).toHaveBeenCalledTimes(2);
  });

  it("shows history-ineligible transcripts but never persists them", async () => {
    const { result, room, onFinalTranscript } = await renderStartedHook();
    const agent = { isAgent: true };

    act(() => {
      for (const event of [
        {
          speaker: "user",
          text: "访客问题",
          final: true,
          history_eligible: false,
          turn_id: 2,
          generation_id: 2,
        },
        {
          speaker: "assistant",
          text: "访客对应回答",
          final: true,
          heard: true,
          history_eligible: false,
          turn_id: 2,
          generation_id: 2,
        },
        {
          speaker: "user",
          text: "旧版缺少资格字段",
          final: true,
          turn_id: 3,
          generation_id: 3,
        },
        {
          speaker: "assistant",
          text: "非法资格字段",
          final: true,
          heard: true,
          history_eligible: "true",
          turn_id: 4,
          generation_id: 4,
        },
      ]) {
        room.emit(
          liveKit.RoomEvent.DataReceived,
          encodeEvent({
            type: "transcript_delta",
            ...event,
          }),
          agent,
          null,
          "voice-agent.ui",
        );
      }
    });

    expect(result.current.transcripts.map((line) => line.text)).toEqual([
      "访客问题",
      "访客对应回答",
      "旧版缺少资格字段",
      "非法资格字段",
    ]);
    expect(onFinalTranscript).not.toHaveBeenCalled();
  });

  it("clears account-scoped voice state when the session is reset", async () => {
    const { result, room } = await renderStartedHook();
    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "assistant",
          text: "旧账号最后一句",
          final: true,
          heard: true,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.latestTranscript.text).toBe("旧账号最后一句");

    await act(async () => {
      await result.current.reset();
    });

    expect(result.current.transcripts).toEqual([]);
    expect(result.current.latestTranscript).toBeNull();
    expect(result.current.audioDiagnostics).toEqual([]);
    expect(result.current.error).toBe("");
    expect(result.current.micEnabled).toBe(true);
    expect(result.current.uiState).toBe("idle");
  });

  it("activates a short-lived voice emotion only after the user turn is accepted", async () => {
    const { result, room } = await renderStartedHook();
    const agent = { isAgent: true };
    vi.useFakeTimers();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "emotion_observation",
          session_id: "session-1",
          label: "happy",
          persist: false,
          turn_id: 1,
          generation_id: 1,
          expires_after_ms: 500,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.emotionHint).toBeNull();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "user",
          text: "我今天真的很开心",
          final: true,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.emotionHint).toEqual({
      label: "happy",
      turnId: 1,
      generationId: 1,
    });

    act(() => vi.advanceTimersByTime(500));
    expect(result.current.emotionHint).toBeNull();
  });

  it("never applies a pending emotion from an older generation to a newer turn", async () => {
    const { result, room } = await renderStartedHook();
    const agent = { isAgent: true };
    const emit = (event) =>
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent(event),
        agent,
        null,
        "voice-agent.ui",
      );

    act(() => {
      emit({
        type: "emotion_observation",
        session_id: "session-1",
        label: "sad",
        persist: false,
        turn_id: 1,
        generation_id: 0,
        expires_after_ms: 30_000,
      });
      emit({
        type: "transcript_delta",
        speaker: "user",
        text: "这是新一代话轮",
        final: true,
        turn_id: 1,
        generation_id: 1,
      });
    });

    expect(result.current.emotionHint).toBeNull();
  });

  it("clears the prior emotion on a new accepted turn and ignores late old observations", async () => {
    const { result, room } = await renderStartedHook();
    const agent = { isAgent: true };
    const emit = (event) =>
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent(event),
        agent,
        null,
        "voice-agent.ui",
      );

    act(() => {
      emit({
        type: "emotion_observation",
        session_id: "session-1",
        label: "sad",
        persist: false,
        turn_id: 1,
        generation_id: 1,
        expires_after_ms: 30_000,
      });
      emit({
        type: "transcript_delta",
        speaker: "user",
        text: "最近有点累",
        final: true,
        turn_id: 1,
        generation_id: 1,
      });
    });
    expect(result.current.emotionHint?.label).toBe("sad");

    act(() => {
      emit({
        type: "transcript_delta",
        speaker: "user",
        text: "我们聊点别的",
        final: true,
        turn_id: 2,
        generation_id: 2,
      });
      emit({
        type: "emotion_observation",
        session_id: "session-1",
        label: "angry",
        persist: false,
        turn_id: 1,
        generation_id: 1,
        expires_after_ms: 30_000,
      });
    });
    expect(result.current.emotionHint).toBeNull();

    await act(async () => {
      await result.current.end();
    });
    expect(result.current.emotionHint).toBeNull();
  });

  it("does not show unstable user interim transcripts", async () => {
    const { result, room, onFinalTranscript } = await renderStartedHook();
    const agent = { isAgent: true };

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "user",
          text: "It's",
          final: false,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.transcripts).toEqual([]);
    expect(onFinalTranscript).not.toHaveBeenCalled();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          speaker: "user",
          text: "你好。",
          final: true,
          history_eligible: true,
          turn_id: 1,
          generation_id: 1,
        }),
        agent,
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.latestTranscript.text).toBe("你好。");
    expect(onFinalTranscript).toHaveBeenCalledTimes(1);
  });

  it("mutes attached remote audio when voice replies are disabled", async () => {
    const { result, room, rerender } = await renderStartedHook();
    const element = document.createElement("audio");
    const play = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(element, "play", { configurable: true, value: play });
    const track = {
      kind: "audio",
      attach: vi.fn(() => element),
    };

    act(() => {
      room.emit(liveKit.RoomEvent.TrackSubscribed, track);
    });
    expect(element.muted).toBe(false);
    expect(play).toHaveBeenCalled();

    rerender({ enabled: false });
    await waitFor(() => expect(element.muted).toBe(true));

    await act(async () => {
      await result.current.resumeAudio(true);
    });
    expect(element.muted).toBe(false);
    expect(room.startAudio).toHaveBeenCalled();
  });

  it("deduplicates cascade audio tracks and reattaches after unsubscribe", async () => {
    const { result, room } = await renderStartedHook();
    const container = result.current.audioContainerRef.current;
    const first = document.createElement("audio");
    const second = document.createElement("audio");
    Object.defineProperty(first, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    Object.defineProperty(second, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    const track = {
      kind: "audio",
      sid: "agent-audio-1",
      attach: vi.fn()
        .mockReturnValueOnce(first)
        .mockReturnValueOnce(second),
      detach: vi.fn(() => [first]),
    };
    const repeatedTrack = {
      kind: "audio",
      sid: "agent-audio-1",
      attach: vi.fn(),
      detach: vi.fn(() => [first]),
    };

    act(() => {
      room.emit(liveKit.RoomEvent.TrackSubscribed, track);
      room.emit(liveKit.RoomEvent.TrackSubscribed, repeatedTrack);
    });
    expect(track.attach).toHaveBeenCalledTimes(1);
    expect(repeatedTrack.attach).not.toHaveBeenCalled();
    expect(container.querySelectorAll("audio")).toHaveLength(1);

    act(() => room.emit(liveKit.RoomEvent.TrackUnsubscribed, repeatedTrack));
    expect(repeatedTrack.detach).toHaveBeenCalledTimes(1);
    expect(container.querySelectorAll("audio")).toHaveLength(0);

    act(() => room.emit(liveKit.RoomEvent.TrackSubscribed, track));
    expect(track.attach).toHaveBeenCalledTimes(2);
    expect(container.querySelectorAll("audio")).toHaveLength(1);
  });

  it("clears cascade audio ownership when a session ends", async () => {
    const { result, room } = await renderStartedHook();
    const container = result.current.audioContainerRef.current;
    const first = document.createElement("audio");
    const second = document.createElement("audio");
    Object.defineProperty(first, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    Object.defineProperty(second, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    const track = {
      kind: "audio",
      sid: "agent-audio-1",
      attach: vi.fn()
        .mockReturnValueOnce(first)
        .mockReturnValueOnce(second),
    };

    act(() => room.emit(liveKit.RoomEvent.TrackSubscribed, track));
    await act(async () => {
      await result.current.end();
      await result.current.start();
    });
    const retryRoom = liveKit.instances.at(-1);
    act(() => retryRoom.emit(liveKit.RoomEvent.TrackSubscribed, track));

    expect(track.attach).toHaveBeenCalledTimes(2);
    expect(container.querySelectorAll("audio")).toHaveLength(1);
  });

  it("records the complete first-audio path through actual media progress", async () => {
    const { result, room } = await renderStartedHook();
    const element = document.createElement("audio");
    const play = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(element, "play", { configurable: true, value: play });
    Object.defineProperty(element, "currentTime", {
      configurable: true,
      writable: true,
      value: 0,
    });
    const track = {
      kind: "audio",
      attach: vi.fn(() => element),
    };

    act(() => {
      room.emit(liveKit.RoomEvent.TrackSubscribed, track);
    });
    await act(async () => {
      await Promise.resolve();
      element.dispatchEvent(new Event("playing"));
      element.currentTime = 0.02;
      element.dispatchEvent(new Event("timeupdate"));
    });

    expect(result.current.audioDiagnostics.map((event) => event.name)).toEqual(
      expect.arrayContaining([
        "audio_unlock",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "playing",
        "first_playback",
      ]),
    );
    expect(
      result.current.audioDiagnostics.find(
        (event) => event.name === "first_playback",
      ),
    ).toEqual(expect.objectContaining({ status: "ok" }));
  });

  it("records allowlisted LiveKit inbound audio quality metrics", async () => {
    const { result, room } = await renderStartedHook();
    const element = document.createElement("audio");
    Object.defineProperty(element, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    const track = {
      kind: "audio",
      attach: vi.fn(() => element),
      getRTCStatsReport: vi.fn().mockResolvedValue(
        new Map([
          [
            "audio",
            {
              type: "inbound-rtp",
              kind: "audio",
              jitter: 0.005,
              packetsLost: 1,
              packetsReceived: 80,
              concealedSamples: 240,
              concealmentEvents: 1,
              jitterBufferDelay: 0.1,
            },
          ],
        ]),
      ),
    };

    act(() => room.emit(liveKit.RoomEvent.TrackSubscribed, track));

    await waitFor(() =>
      expect(
        result.current.audioDiagnostics.find(
          ({ name }) => name === "webrtc_inbound_audio",
        ),
      ).toEqual(
        expect.objectContaining({
          detail: expect.objectContaining({
            jitter: 0.005,
            packets_lost: 1,
            concealed_samples: 240,
          }),
        }),
      ),
    );
  });

  it("explains a strict speaker rejection and clears it after an accepted turn", async () => {
    const { result, room } = await renderStartedHook();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "audio_trace",
          source: "agent",
          session_id: "session-1",
          name: "target_speaker_rejected",
          status: "ok",
          turn_id: 0,
          generation_id: 0,
          detail: { reason: "target_non_owner" },
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.error).toMatch(/没有确认到主人声音/);

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "transcript_delta",
          session_id: "session-1",
          speaker: "user",
          text: "这句已经放行",
          final: true,
          heard: false,
          history_eligible: false,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.error).toBe("");
  });

  it("drops a stale target-speaker rejection after a newer generation is active", async () => {
    const { result, room } = await renderStartedHook();

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "listening",
          turn_id: 2,
          generation_id: 2,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "audio_trace",
          source: "agent",
          session_id: "session-1",
          name: "target_speaker_rejected",
          status: "ok",
          turn_id: 1,
          generation_id: 1,
          detail: { reason: "target_non_owner" },
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    expect(result.current.error).toBe("");
    expect(result.current.audioDiagnostics).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ name: "target_speaker_rejected" })]),
    );
  });

  it("ducks playback during a candidate interruption and restores it smoothly", async () => {
    const { room } = await renderStartedHook();
    const element = document.createElement("audio");
    Object.defineProperty(element, "play", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    const track = { kind: "audio", attach: vi.fn(() => element) };
    act(() => room.emit(liveKit.RoomEvent.TrackSubscribed, track));

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_audio",
          session_id: "session-1",
          action: "duck",
          gain: 0.25,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(element.volume).toBe(0.25);

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_audio",
          session_id: "session-1",
          action: "restore",
          gain: 1,
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(element.volume).toBe(1);
  });

  it("surfaces blocked playback and can recover from a later user gesture", async () => {
    liveKit.rejectStartAudioCount = 2;
    const rendered = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    rendered.result.current.audioContainerRef.current = document.createElement("div");

    await act(async () => {
      await rendered.result.current.start();
    });
    const room = liveKit.instances[0];
    expect(rendered.result.current.audioBlocked).toBe(true);

    room.startAudio.mockResolvedValue(undefined);
    await act(async () => {
      expect(await rendered.result.current.resumeAudio(true)).toBe(true);
    });
    expect(rendered.result.current.audioBlocked).toBe(false);
  });

  it("keeps the agent state authoritative when muting or stopping", async () => {
    const { result, room } = await renderStartedHook();
    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "speaking",
          turn_id: 1,
          generation_id: 1,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });

    await act(async () => {
      await result.current.toggleMic();
      await result.current.stopAssistant();
    });
    expect(result.current.micEnabled).toBe(false);
    expect(result.current.uiState).toBe("speaking");

    act(() => {
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "interrupted",
          turn_id: 1,
          generation_id: 2,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.uiState).toBe("interrupted");
  });

  it("preserves a connecting-stage mute when the microphone is published", async () => {
    const sessionRequest = deferred();
    api.createSession.mockImplementationOnce(() => sessionRequest.promise);
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");

    let startPromise;
    act(() => {
      startPromise = result.current.start();
    });
    await waitFor(() => expect(result.current.uiState).toBe("connecting"));
    await act(async () => {
      await result.current.toggleMic();
    });
    expect(result.current.uiState).toBe("connecting");
    expect(result.current.micEnabled).toBe(false);

    sessionRequest.resolve({
      session_id: "session-1",
      livekit_url: "wss://livekit.example",
      participant_token: "participant-token",
    });
    await act(async () => {
      await startPromise;
    });
    const room = liveKit.instances[0];
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(
      false,
    );
    expect(result.current.uiState).toBe("connecting");
  });

  it("applies mute after transport connects while explicit ready is pending", async () => {
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");
    await act(async () => {
      await result.current.start();
    });
    const room = liveKit.instances[0];
    room.localParticipant.setMicrophoneEnabled.mockClear();
    expect(result.current.uiState).toBe("connecting");

    await act(async () => {
      await result.current.toggleMic();
    });

    expect(result.current.micEnabled).toBe(false);
    expect(result.current.uiState).toBe("connecting");
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(
      false,
    );
  });

  it("restores microphone control after a pre-ready transport reconnect", async () => {
    const { result } = renderHook(() =>
      useVoiceSession({
        userId: "anonymous-user",
        onFinalTranscript: vi.fn(),
        voiceReplyEnabled: true,
      }),
    );
    result.current.audioContainerRef.current = document.createElement("div");
    await act(async () => {
      await result.current.start();
    });
    const room = liveKit.instances[0];
    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
      room.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    room.localParticipant.setMicrophoneEnabled.mockClear();

    await act(async () => {
      await result.current.toggleMic();
    });

    expect(result.current.uiState).toBe("ready");
    expect(result.current.micEnabled).toBe(false);
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(
      false,
    );
  });

  it("ignores a stale stop failure after a newer session is ready", async () => {
    const stopRequest = deferred();
    api.stopResponse.mockImplementationOnce(() => stopRequest.promise);
    const first = await renderStartedHook();
    let oldStop;
    act(() => {
      oldStop = first.result.current.stopAssistant();
    });
    await act(async () => {
      await first.result.current.end();
    });

    api.createSession.mockResolvedValueOnce({
      session_id: "session-2",
      livekit_url: "wss://livekit.example",
      participant_token: "participant-token-2",
    });
    await act(async () => {
      await first.result.current.start();
    });
    const currentRoom = liveKit.instances[1];
    act(() => {
      currentRoom.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-2",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
      stopRequest.reject(new Error("old stop failed"));
    });
    await act(async () => {
      await oldStop;
    });

    expect(first.result.current.session.session_id).toBe("session-2");
    expect(first.result.current.uiState).toBe("ready");
    expect(first.result.current.error).toBe("");
  });

  it("reconnects with the latest microphone state", async () => {
    const { result, room } = await renderStartedHook();
    await act(async () => {
      await result.current.toggleMic();
    });
    expect(result.current.micEnabled).toBe(false);

    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
    });
    await waitFor(() => {
      expect(api.notifyRtcRecovered).toHaveBeenCalledWith("session-1");
      expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(
        false,
      );
    });
  });

  it("reapplies a mute changed while reconnect microphone setup is pending", async () => {
    const { result, room } = await renderStartedHook();
    const firstMicApply = deferred();
    room.localParticipant.setMicrophoneEnabled.mockClear();
    room.localParticipant.setMicrophoneEnabled
      .mockImplementationOnce(() => firstMicApply.promise)
      .mockResolvedValue(undefined);

    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      room.emit(liveKit.RoomEvent.Reconnected);
    });
    await waitFor(() =>
      expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(
        true,
      ),
    );
    await act(async () => {
      await result.current.toggleMic();
    });
    expect(result.current.uiState).toBe("reconnecting");
    expect(result.current.micEnabled).toBe(false);

    firstMicApply.resolve();
    await act(async () => {
      await firstMicApply.promise;
      await Promise.resolve();
    });
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(
      false,
    );
  });

  it("disconnects after a 10 second reconnect timeout and remains retryable", async () => {
    const { result, room } = await renderStartedHook();
    room.disconnect.mockRejectedValueOnce(new Error("transport already gone"));
    vi.useFakeTimers();

    act(() => {
      room.emit(liveKit.RoomEvent.Reconnecting);
      vi.advanceTimersByTime(9_999);
    });
    expect(room.disconnect).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(room.disconnect).toHaveBeenCalledTimes(1);
    expect(result.current.session).toBeNull();
    expect(result.current.uiState).toBe("closed");
    expect(result.current.error).toMatch(/重新连接超时/);

    vi.useRealTimers();
    await act(async () => {
      await result.current.start();
    });
    expect(liveKit.instances).toHaveLength(2);
    expect(result.current.uiState).toBe("connecting");
    const retryRoom = liveKit.instances[1];
    act(() => {
      retryRoom.emit(
        liveKit.RoomEvent.DataReceived,
        encodeEvent({
          type: "assistant_state",
          session_id: "session-1",
          state: "ready",
          turn_id: 0,
          generation_id: 0,
        }),
        { isAgent: true },
        null,
        "voice-agent.ui",
      );
    });
    expect(result.current.uiState).toBe("ready");
  });
});
