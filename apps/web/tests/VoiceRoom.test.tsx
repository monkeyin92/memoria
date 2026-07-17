import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VoiceRoom } from "../src/components/VoiceRoom";

const livekit = vi.hoisted(() => {
  type Handler = (...args: unknown[]) => void;

  class Room {
    static getLocalDevices = vi.fn();
    handlers = new Map<string, Set<Handler>>();
    remoteParticipants = new Map<
      string,
      { audioTrackPublications: Map<string, { track?: unknown }> }
    >();
    connect = vi.fn().mockResolvedValue(undefined);
    disconnect = vi.fn().mockResolvedValue(undefined);
    startAudio = vi.fn().mockResolvedValue(undefined);
    switchActiveDevice = vi.fn().mockResolvedValue(true);
    getActiveDevice = vi.fn().mockReturnValue("mic-1");
    localParticipant = {
      setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
    };

    constructor() {
      rooms.push(this);
    }

    on(event: string, handler: Handler) {
      const handlers = this.handlers.get(event) ?? new Set<Handler>();
      handlers.add(handler);
      this.handlers.set(event, handlers);
      return this;
    }

    emit(event: string, ...args: unknown[]) {
      this.handlers.get(event)?.forEach((handler) => handler(...args));
    }
  }

  const rooms: Room[] = [];
  return { Room, rooms, getLocalDevices: Room.getLocalDevices };
});

vi.mock("livekit-client", () => {
  return {
    Room: livekit.Room,
    RoomEvent: {
      TrackSubscribed: "trackSubscribed",
      TrackUnsubscribed: "trackUnsubscribed",
      DataReceived: "dataReceived",
      TranscriptionReceived: "transcriptionReceived",
      Reconnecting: "reconnecting",
      SignalReconnecting: "signalReconnecting",
      Reconnected: "reconnected",
      Disconnected: "disconnected",
      MediaDevicesChanged: "mediaDevicesChanged",
      MediaDevicesError: "mediaDevicesError",
    },
    Track: { Kind: { Audio: "audio" } },
  };
});

const session = {
  session_id: "s",
  livekit_url: "wss://example.livekit.cloud",
  room_name: "voice-x",
  participant_token: "token",
  expires_in: 300,
  agent_name: "duplex-zh-agent",
  config: { locale: "zh-CN", allow_text_fallback: true },
};

const devices = [
  {
    deviceId: "mic-1",
    groupId: "group-1",
    kind: "audioinput",
    label: "内置麦克风",
    toJSON: () => ({}),
  },
  {
    deviceId: "mic-2",
    groupId: "group-2",
    kind: "audioinput",
    label: "USB 麦克风",
    toJSON: () => ({}),
  },
] satisfies MediaDeviceInfo[];

const callbacks = () => ({
  onData: vi.fn(),
  onSynchronizedTranscript: vi.fn(),
  onConnectionState: vi.fn(),
  onReconnected: vi.fn().mockResolvedValue(true),
  onEnded: vi.fn(),
});

describe("VoiceRoom", () => {
  beforeEach(() => {
    livekit.rooms.length = 0;
    livekit.getLocalDevices.mockReset().mockResolvedValue(devices);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("checks permission before connecting and supports microphone switching", async () => {
    const props = callbacks();
    render(<VoiceRoom {...props} session={session} micEnabled />);
    const room = livekit.rooms[0];

    await waitFor(() => expect(room.connect).toHaveBeenCalled());
    expect(livekit.getLocalDevices).toHaveBeenCalledWith("audioinput", true);
    expect(room.switchActiveDevice).toHaveBeenCalledWith("audioinput", "mic-1");
    expect(screen.getByText("麦克风权限：已授权")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("输入设备"), {
      target: { value: "mic-2" },
    });
    await waitFor(() =>
      expect(room.switchActiveDevice).toHaveBeenCalledWith("audioinput", "mic-2"),
    );
  });

  it("attaches and detaches subscribed audio tracks", async () => {
    const props = callbacks();
    const { container } = render(
      <VoiceRoom {...props} session={session} micEnabled />,
    );
    const room = livekit.rooms[0];
    await waitFor(() => expect(room.connect).toHaveBeenCalled());

    const audio = document.createElement("audio");
    const track = {
      sid: "track-1",
      kind: "audio",
      attach: vi.fn().mockReturnValue(audio),
      detach: vi.fn().mockReturnValue([audio]),
    };
    act(() => room.emit("trackSubscribed", track));
    expect(track.attach).toHaveBeenCalledOnce();
    expect(container.querySelector('audio[data-track-sid="track-1"]')).toBe(audio);

    act(() => room.emit("trackUnsubscribed", track));
    expect(track.detach).toHaveBeenCalled();
    expect(container.querySelector("audio")).toBeNull();
  });

  it("forwards data and refreshes tracks after reconnect", async () => {
    const props = callbacks();
    render(<VoiceRoom {...props} session={session} micEnabled />);
    const room = livekit.rooms[0];
    await waitFor(() => expect(room.connect).toHaveBeenCalled());

    const payload = new Uint8Array([1, 2, 3]);
    act(() => room.emit("dataReceived", payload));
    expect(props.onData).toHaveBeenCalledWith(payload);

    act(() => room.emit("reconnecting"));
    expect(props.onConnectionState).toHaveBeenCalledWith("reconnecting");
    await act(async () => {
      room.emit("reconnected");
      await Promise.resolve();
    });
    expect(props.onReconnected).toHaveBeenCalledOnce();
    expect(props.onConnectionState).toHaveBeenCalledWith("ready");
    expect(room.startAudio).toHaveBeenCalled();
  });

  it("forwards only synchronized transcriptions from the agent", async () => {
    const props = callbacks();
    render(<VoiceRoom {...props} session={session} micEnabled />);
    const room = livekit.rooms[0];
    await waitFor(() => expect(room.connect).toHaveBeenCalled());
    const segments = [{ text: "实际已播放", final: false }];

    act(() => room.emit("transcriptionReceived", segments, { isAgent: false }));
    expect(props.onSynchronizedTranscript).not.toHaveBeenCalled();
    act(() => room.emit("transcriptionReceived", segments, { isAgent: true }));
    expect(props.onSynchronizedTranscript).toHaveBeenCalledWith(segments);
  });

  it("revalidates microphone state after page restoration", async () => {
    const props = callbacks();
    render(<VoiceRoom {...props} session={session} micEnabled={false} />);
    const room = livekit.rooms[0];
    await waitFor(() => expect(room.connect).toHaveBeenCalled());
    room.localParticipant.setMicrophoneEnabled.mockClear();

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() =>
      expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(false),
    );
    expect(livekit.getLocalDevices).toHaveBeenCalledWith("audioinput", false);
  });

  it("ends a session after the reconnect timeout", async () => {
    vi.useFakeTimers();
    const props = callbacks();
    render(<VoiceRoom {...props} session={session} micEnabled />);
    const room = livekit.rooms[0];
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => room.emit("reconnecting"));
    act(() => vi.advanceTimersByTime(10_000));
    expect(props.onEnded).toHaveBeenCalledWith("连接超时，请重新连接");
    expect(room.disconnect).toHaveBeenCalled();
  });
});
