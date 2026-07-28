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
});
