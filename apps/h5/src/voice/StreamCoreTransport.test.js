import { describe, expect, it, vi } from "vitest";

import { StreamCoreTransport, STREAMCORE_EVENTS_LABEL } from "./StreamCoreTransport.js";
import { VoiceTransport } from "./VoiceTransport.js";
import { createVoiceTransport } from "./voiceTransportFactory.js";

class FakeChannel {
  constructor(label) {
    this.label = label;
    this.readyState = "open";
    this.sent = [];
    this.onmessage = null;
  }

  send(payload) {
    this.sent.push(JSON.parse(payload));
  }

  close() {
    this.readyState = "closed";
  }
}

class FakePeerConnection {
  constructor() {
    this.iceGatheringState = "complete";
    this.connectionState = "connected";
    this.localDescription = null;
    this.listeners = new Map();
    this.channel = null;
    this.tracks = [];
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  removeEventListener(name, listener) {
    if (this.listeners.get(name) === listener) this.listeners.delete(name);
  }

  addTrack(track) {
    this.tracks.push(track);
  }

  createDataChannel(label) {
    this.channel = new FakeChannel(label);
    return this.channel;
  }

  createOffer() {
    return Promise.resolve({ type: "offer", sdp: "v=0\no=offer" });
  }

  setLocalDescription(description) {
    this.localDescription = description;
    return Promise.resolve();
  }

  setRemoteDescription(description) {
    this.remoteDescription = description;
    return Promise.resolve();
  }

  close() {
    this.connectionState = "closed";
  }
}

class ConnectingPeerConnection extends FakePeerConnection {
  constructor() {
    super();
    this.connectionState = "connecting";
  }

  setRemoteDescription(description) {
    this.remoteDescription = description;
    queueMicrotask(() => {
      this.connectionState = "connected";
      this.listeners.get("connectionstatechange")?.();
    });
    return Promise.resolve();
  }
}

function stream() {
  const track = { enabled: true, stop: vi.fn() };
  return {
    track,
    getAudioTracks: () => [track],
    getTracks: () => [track],
  };
}

describe("StreamCoreTransport", () => {
  it("negotiates a server-issued WHIP session and fences events", async () => {
    const media = stream();
    const onState = vi.fn();
    const onTranscript = vi.fn();
    const exchangeSdp = vi.fn(async (_sessionId, offer) => {
      expect(offer).toContain("v=0");
      return "v=0\no=answer";
    });
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => media),
      exchangeSdp,
      onState,
      onTranscript,
    });

    expect(transport).toBeInstanceOf(VoiceTransport);
    await transport.connect({
      session_id: "session-1",
      whip_url: "https://media.example/whip",
      token: "short-lived-token",
      stream_epoch: 3,
    });

    expect(exchangeSdp).toHaveBeenCalledTimes(1);
    expect(transport.channel.label).toBe(STREAMCORE_EVENTS_LABEL);
    transport.channel.onmessage({
      data: JSON.stringify({
        type: "user.transcript.final",
        stream_epoch: 3,
        sequence: 2,
        payload: { text: "你好", turn_id: 1, generation_id: 1 },
      }),
    });
    transport.channel.onmessage({
      data: JSON.stringify({
        type: "user.transcript.final",
        stream_epoch: 3,
        sequence: 1,
        payload: { text: "迟到", turn_id: 1, generation_id: 1 },
      }),
    });

    expect(onState).toHaveBeenCalledWith("connecting");
    expect(onState).toHaveBeenCalledWith("ready");
    expect(onTranscript).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ text: "你好", final: true, speaker: "user" }),
    );
  });

  it("ducks locally and sends a fenced stop command before HTTP fallback", async () => {
    const onLocalDuck = vi.fn();
    const stopResponse = vi.fn(async () => undefined);
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      stopResponse,
      exchangeSdp: vi.fn(async () => "v=0\no=answer"),
      onLocalDuck,
    });
    await transport.connect({
      session_id: "session-2",
      whip_url: "https://media.example/whip",
      token: "token",
    });

    await transport.stopAssistant();

    expect(onLocalDuck).toHaveBeenCalledWith(true, { reason: "user_stop" });
    expect(transport.channel.sent[0]).toEqual(
      expect.objectContaining({
        type: "client.stop_assistant",
        session_id: "session-2",
        stream_epoch: 1,
        sequence: 0,
      }),
    );
    expect(stopResponse).not.toHaveBeenCalled();
  });

  it("waits for the peer connection to become connected before reporting ready", async () => {
    const onState = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: ConnectingPeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onState,
      connectTimeoutMs: 100,
    });
    await transport.connect({
      session_id: "session-connect-wait",
      whip_url: "https://media.example/whip",
      token: "token",
    });
    expect(onState).toHaveBeenLastCalledWith("ready");
  });

  it("notifies only once for repeated peer failure events", async () => {
    const onDisconnected = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onDisconnected,
    });
    await transport.connect({
      session_id: "session-disconnect-fence",
      whip_url: "https://media.example/whip",
      token: "token",
    });
    transport.pc.connectionState = "failed";
    transport.pc.listeners.get("connectionstatechange")();
    transport.pc.listeners.get("connectionstatechange")();
    expect(onDisconnected).toHaveBeenCalledTimes(1);
  });

  it("drops invalid, stale, future, and cross-session events", async () => {
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onTranscript,
    });
    await transport.connect({
      session_id: "session-event-fence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 4,
    });
    for (const data of [
      { v: 2, session_id: "session-event-fence", stream_epoch: 4, type: "transcript_delta" },
      { v: 1, session_id: "other", stream_epoch: 4, type: "transcript_delta" },
      { v: 1, session_id: "session-event-fence", stream_epoch: 3, type: "transcript_delta" },
      { v: 1, session_id: "session-event-fence", stream_epoch: 5, type: "transcript_delta" },
    ]) {
      transport.channel.onmessage({ data: JSON.stringify(data) });
    }
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("handles playback flush and responds to server pings", async () => {
    const onPlaybackFlush = vi.fn();
    const onState = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onPlaybackFlush,
      onState,
    });
    await transport.connect({
      session_id: "session-events",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 2,
    });
    transport.channel.onmessage({
      data: JSON.stringify({
        v: 1,
        type: "playback.flush",
        session_id: "session-events",
        stream_epoch: 2,
        sequence: 1,
        payload: { reason: "generation_cancel" },
      }),
    });
    transport.channel.onmessage({
      data: JSON.stringify({
        v: 1,
        type: "ping",
        session_id: "session-events",
        stream_epoch: 2,
        sequence: 2,
      }),
    });
    expect(onPlaybackFlush).toHaveBeenCalledWith(
      { reason: "generation_cancel" },
      expect.objectContaining({ type: "playback.flush" }),
    );
    expect(transport.channel.sent.at(-1)).toEqual(
      expect.objectContaining({ type: "pong", stream_epoch: 2 }),
    );
    expect(onState).toHaveBeenCalledWith("ready");
  });

  it("uses HTTP stop only when the DataChannel is unavailable", async () => {
    const stopResponse = vi.fn(async () => undefined);
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      stopResponse,
      exchangeSdp: vi.fn(async () => "v=0\no=answer"),
    });
    await transport.connect({
      session_id: "session-stop-fallback",
      whip_url: "https://media.example/whip",
      token: "token",
    });
    transport.channel.close();
    await transport.stopAssistant();
    expect(stopResponse).toHaveBeenCalledWith(
      "session-stop-fallback",
      expect.any(String),
    );
  });

  it("increments stream epoch when a media session is rebuilt", async () => {
    const reconnectSession = vi.fn(async () => ({
      session_id: "session-epoch",
      stream_epoch: 5,
      streamcore: {
        whip_url: "https://media.example/whip",
        token: "rotated-token",
        stream_epoch: 5,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      },
    }));
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\no=answer"),
      reconnectSession,
    });
    await transport.connect({
      session_id: "session-epoch",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 4,
    });

    await expect(transport.reconnect()).resolves.toBe(5);
    expect(reconnectSession).toHaveBeenCalledWith("session-epoch", expect.any(Object), 4);
    expect(transport.streamEpoch).toBe(5);
    expect(transport.session.stream_epoch).toBe(5);
  });

  it("renews the authoritative session TTL without changing the media epoch", async () => {
    const heartbeatSession = vi.fn(async () => ({
      stream_epoch: 4,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    }));
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      heartbeatSession,
    });
    await transport.connect({
      session_id: "session-heartbeat",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 4,
      streamcore: { stream_epoch: 4, expires_at: "old" },
    });
    await transport._renewHeartbeat();
    expect(heartbeatSession).toHaveBeenCalledWith("session-heartbeat", 4);
    expect(transport.session.streamcore.expires_at).not.toBe("old");
    expect(transport.streamEpoch).toBe(4);
    await transport.close();
  });

  it("keeps LiveKit as the default and exposes lazy fallback construction", () => {
    class FakeLiveKit extends VoiceTransport {
      async connect() {}
      async setMicrophoneEnabled() {}
      stopAssistant() {}
      close() {}
    }
    const transport = createVoiceTransport({
      liveKitTransport: FakeLiveKit,
      streamCoreOptions: { RTCPeerConnectionImpl: FakePeerConnection },
      mediaRuntime: "livekit",
    });
    expect(transport).toBeInstanceOf(FakeLiveKit);
  });

  it("constructs StreamCore only when the server runtime flag opts in", () => {
    class FakeStreamCore extends VoiceTransport {
      async connect() {}
      async setMicrophoneEnabled() {}
      stopAssistant() {}
      close() {}
    }
    class FakeLiveKit extends VoiceTransport {
      async connect() {}
      async setMicrophoneEnabled() {}
      stopAssistant() {}
      close() {}
    }
    const transport = createVoiceTransport({
      mediaRuntime: "streamcore",
      streamCoreTransport: FakeStreamCore,
      liveKitTransport: FakeLiveKit,
    });
    expect(transport).toBeInstanceOf(FakeStreamCore);
    expect(transport.createFallback()).toBeInstanceOf(FakeLiveKit);
  });
});
