import { describe, expect, it, vi } from "vitest";

import { LiveKitCascadeTransport } from "./LiveKitCascadeTransport.js";
import { VoiceTransport } from "./VoiceTransport.js";

function fakeRoom() {
  const handlers = new Map();
  return {
    handlers,
    startAudio: vi.fn(async () => undefined),
    connect: vi.fn(async () => undefined),
    disconnect: vi.fn(async () => undefined),
    on: vi.fn((event, handler) => handlers.set(event, handler)),
    off: vi.fn((event) => handlers.delete(event)),
    localParticipant: {
      setMicrophoneEnabled: vi.fn(async () => undefined),
      publishData: vi.fn(async () => undefined),
      sendText: vi.fn(async () => undefined),
    },
  };
}

describe("LiveKitCascadeTransport", () => {
  it("owns LiveKit lifecycle behind the shared transport contract", async () => {
    const room = fakeRoom();
    const stopResponse = vi.fn(async () => undefined);
    let microphoneEnabled = false;
    const transport = new LiveKitCascadeTransport({
      room,
      getLocalDevices: vi.fn(async () => [{ deviceId: "mic-1" }]),
      stopResponse,
    });

    expect(transport).toBeInstanceOf(VoiceTransport);
    await transport.prepare({ unlockAudio: true });
    const connecting = transport.connect(
      {
        session_id: "session-1",
        livekit_url: "wss://livekit.example.com",
        participant_token: "token",
      },
      { getMicrophoneEnabled: () => microphoneEnabled },
    );
    microphoneEnabled = true;
    await connecting;
    await transport.stopAssistant();
    await transport.close();

    expect(room.startAudio).toHaveBeenCalledTimes(1);
    expect(room.connect).toHaveBeenCalledWith(
      "wss://livekit.example.com",
      "token",
    );
    expect(room.localParticipant.setMicrophoneEnabled.mock.calls).toEqual([
      [false],
      [true],
    ]);
    expect(stopResponse).toHaveBeenCalledWith("session-1");
    expect(room.disconnect).toHaveBeenCalledTimes(1);
  });

  it("fails before connecting when no microphone exists", async () => {
    const transport = new LiveKitCascadeTransport({
      room: fakeRoom(),
      getLocalDevices: vi.fn(async () => []),
      stopResponse: vi.fn(),
    });

    await expect(
      transport.connect({
        session_id: "session-1",
        livekit_url: "wss://livekit.example.com",
        participant_token: "token",
      }),
    ).rejects.toThrow(/麦克风/);
  });

  it("exposes the published microphone track for diagnostics", async () => {
    const room = fakeRoom();
    const microphoneTrack = { kind: "audio" };
    room.localParticipant.setMicrophoneEnabled.mockResolvedValue({
      track: microphoneTrack,
    });
    const onMicrophoneTrack = vi.fn();
    const transport = new LiveKitCascadeTransport({
      room,
      getLocalDevices: vi.fn(async () => [{ deviceId: "mic-1" }]),
      onMicrophoneTrack,
      stopResponse: vi.fn(),
    });

    await transport.connect({
      session_id: "session-1",
      livekit_url: "wss://livekit.example.com",
      participant_token: "token",
    });
    await transport.setMicrophoneEnabled(false);

    expect(onMicrophoneTrack).toHaveBeenNthCalledWith(1, microphoneTrack);
    expect(onMicrophoneTrack).toHaveBeenNthCalledWith(2, null);
  });

  it("opens text-only sessions without microphone discovery and sends lk.chat text", async () => {
    const room = fakeRoom();
    const getLocalDevices = vi.fn(async () => []);
    const transport = new LiveKitCascadeTransport({
      room,
      getLocalDevices,
      stopResponse: vi.fn(),
    });

    await transport.connect(
      {
        session_id: "session-text",
        livekit_url: "wss://livekit.example.com",
        participant_token: "token",
      },
      { getMicrophoneEnabled: () => false },
    );
    await transport.sendText("今天星期几？");

    expect(getLocalDevices).not.toHaveBeenCalled();
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(false);
    expect(room.localParticipant.sendText).toHaveBeenCalledWith(
      "今天星期几？",
      { topic: "lk.chat" },
    );
  });
});
