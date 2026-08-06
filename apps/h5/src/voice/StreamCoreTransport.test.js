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
    this.channels = new Map();
    this.tracks = [];
    this.transceivers = [];
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

  addTransceiver(kind, init) {
    const sender = { replaceTrack: vi.fn(async () => undefined) };
    const transceiver = { kind, init, sender };
    this.transceivers.push(transceiver);
    return transceiver;
  }

  createDataChannel(label, options) {
    this.channel = new FakeChannel(label);
    this.channel.options = options;
    this.channels.set(label, this.channel);
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

class GatheringPeerConnection extends FakePeerConnection {
  constructor() {
    super();
    this.iceGatheringState = "gathering";
  }
}

class RetryGatheringPeerConnection extends FakePeerConnection {
  static attempts = 0;

  constructor() {
    super();
    RetryGatheringPeerConnection.attempts += 1;
    if (RetryGatheringPeerConnection.attempts === 1) {
      this.iceGatheringState = "gathering";
    }
  }
}

function stream(trackOverrides = {}) {
  const track = { enabled: true, stop: vi.fn(), ...trackOverrides };
  return {
    track,
    getAudioTracks: () => [track],
    getTracks: () => [track],
  };
}

class FakePlaybackTracker {
  constructor(sampleRate = 48_000) {
    this.sampleRate = sampleRate;
    this.renderedFrames = 0;
    this.onRendered = () => undefined;
    this.onDiagnostic = () => undefined;
    this.closed = false;
  }

  setOnRendered(callback) {
    this.onRendered = callback;
  }

  setOnDiagnostic(callback) {
    this.onDiagnostic = callback;
  }

  getSnapshot() {
    return this.closed
      ? null
      : { renderedFrames: this.renderedFrames, sampleRate: this.sampleRate };
  }

  resume() {
    return Promise.resolve(true);
  }

  emit(renderedFrames) {
    this.renderedFrames = renderedFrames;
    this.onRendered(this.getSnapshot());
  }

  close() {
    this.closed = true;
    this.onRendered = () => undefined;
    this.onDiagnostic = () => undefined;
    return Promise.resolve();
  }
}

let nextServerEventId = 0;

function mediaEvent({
  type,
  session_id,
  stream_epoch,
  sequence,
  payload = {},
  turn_id = payload.turn_id ?? 0,
  generation_id = payload.generation_id ?? 0,
  tool_epoch = payload.tool_epoch ?? 0,
  ...rest
}) {
  nextServerEventId += 1;
  return {
    v: 1,
    protocol: "media-v1",
    type,
    event_id: `server-event-${nextServerEventId}`,
    session_id,
    stream_epoch,
    sequence,
    turn_id,
    generation_id,
    tool_epoch,
    server_monotonic_ms: sequence,
    payload,
    ...rest,
  };
}

describe("StreamCoreTransport", () => {
  it("uses identical constraints and sanitized diagnostics for every microphone capture", async () => {
    const onDiagnostic = vi.fn();
    const microphoneTrack = (sampleRate) => ({
      getSettings: vi.fn(() => ({
        deviceId: "private-device-id",
        echoCancellation: true,
        groupId: "private-group-id",
        label: "Private microphone label",
        noiseSuppression: true,
        sampleRate,
      })),
      getCapabilities: vi.fn(() => ({
        deviceId: "private-device-id",
        echoCancellation: [true, false],
        groupId: "private-group-id",
        latency: { min: 0.01, max: 0.1 },
        noiseSuppression: [true],
        sampleRate: { min: 8_000, max: 48_000 },
      })),
    });
    const captures = [
      stream(microphoneTrack(48_000)),
      stream(microphoneTrack(44_100)),
    ];
    const getUserMedia = vi.fn(async () => captures.shift());
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia,
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onDiagnostic,
    });
    const session = {
      session_id: "microphone-capture-session",
      whip_url: "https://media.example/whip",
      token: "token",
    };

    await transport.connect(session);
    await transport.close();
    await transport.connect(session, { getMicrophoneEnabled: () => false });
    await transport.setMicrophoneEnabled(true);

    const expectedConstraints = {
      audio: {
        channelCount: { ideal: 1 },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    };
    expect(getUserMedia).toHaveBeenNthCalledWith(1, expectedConstraints);
    expect(getUserMedia).toHaveBeenNthCalledWith(2, expectedConstraints);
    expect(onDiagnostic).toHaveBeenCalledWith(
      "webrtc_microphone_settings",
      "ok",
      expect.objectContaining({
        echo_cancellation: true,
        noise_suppression: true,
        sample_rate: 48_000,
      }),
    );
    expect(onDiagnostic).toHaveBeenCalledWith(
      "webrtc_microphone_capabilities",
      "ok",
      {
        echo_cancellation: [true, false],
        latency_ms: { min: 10, max: 100 },
        noise_suppression: [true],
        sample_rate: { min: 8_000, max: 48_000 },
      },
    );
    expect(
      onDiagnostic.mock.calls.filter(
        ([name]) => name === "webrtc_microphone_settings",
      ),
    ).toHaveLength(2);
    expect(
      onDiagnostic.mock.calls.filter(
        ([name]) => name === "webrtc_microphone_capabilities",
      ),
    ).toHaveLength(2);
    expect(JSON.stringify(onDiagnostic.mock.calls)).not.toMatch(
      /private-|deviceId|groupId|label/i,
    );
    await transport.close();
  });

  it("rejects incomplete ICE gathering before exchanging SDP", async () => {
    const exchangeSdp = vi.fn(async () => "v=0\\no=answer");
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: GatheringPeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp,
      connectTimeoutMs: 5,
    });

    await expect(
      transport.connect({
        session_id: "ice-timeout-session",
        whip_url: "https://media.example/whip",
        token: "token",
      }),
    ).rejects.toThrow("ICE 候选收集超时");
    expect(exchangeSdp).not.toHaveBeenCalled();
    expect(transport.pc.listeners.has("icegatheringstatechange")).toBe(false);
    await transport.close();
  });

  it("retries a bounded ICE gathering timeout before surfacing failure", async () => {
    RetryGatheringPeerConnection.attempts = 0;
    const exchangeSdp = vi.fn(async () => "v=0\\no=answer");
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: RetryGatheringPeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp,
      connectTimeoutMs: 5,
      iceRetryDelayMs: 0,
    });

    await transport.connect({
      session_id: "ice-retry-session",
      whip_url: "https://media.example/whip",
      token: "token",
    });

    expect(RetryGatheringPeerConnection.attempts).toBe(2);
    expect(exchangeSdp).toHaveBeenCalledTimes(1);
    await transport.close();
  });

  it("reserves an audio transceiver when starting muted and later replaces its track", async () => {
    const captured = stream();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => captured),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
    });

    await transport.connect(
      {
        session_id: "late-microphone-session",
        whip_url: "https://media.example/whip",
        token: "token",
      },
      { getMicrophoneEnabled: () => false },
    );

    expect(transport.pc.transceivers).toHaveLength(1);
    expect(transport.pc.transceivers[0]).toMatchObject({
      kind: "audio",
      init: { direction: "sendrecv" },
    });

    await transport.setMicrophoneEnabled(true);

    expect(transport.pc.transceivers[0].sender.replaceTrack).toHaveBeenCalledWith(
      captured.track,
    );
    expect(transport.pc.tracks).toEqual([]);
    await transport.close();
  });

  it("exposes the server-selected interaction authority from session.ready", async () => {
    const onState = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onState,
    });
    await transport.connect({
      session_id: "authority-session",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "session.ready",
        session_id: "authority-session",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          state: "ready",
          interaction_authority: "go_shadow",
        },
      })),
    });

    expect(transport.interactionAuthority).toBe("go_shadow");
    expect(onState).toHaveBeenCalledWith(
      "ready",
      expect.objectContaining({ interaction_authority: "go_shadow" }),
    );
  });

  it("uses independent control, conversation, and ephemeral DataChannel sequences", async () => {
    const onState = vi.fn();
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onState,
      onTranscript,
    });
    await transport.connect({
      session_id: "multi-channel-session",
      whip_url: "https://media.example/whip",
      token: "token",
    });

    expect([...transport.channels.keys()]).toEqual([
      "control",
      "conversation",
      "ephemeral",
    ]);
    expect(transport.channels.get("ephemeral").options).toEqual({
      ordered: false,
      maxRetransmits: 1,
    });

    transport.channels.get("ephemeral").onmessage({
      data: JSON.stringify(
        mediaEvent({
          type: "user.transcript.partial",
          session_id: "multi-channel-session",
          stream_epoch: 1,
          sequence: 0,
          payload: { text: "南" },
        }),
      ),
    });
    transport.channels.get("control").onmessage({
      data: JSON.stringify(
        mediaEvent({
          type: "session.ready",
          session_id: "multi-channel-session",
          stream_epoch: 1,
          sequence: 0,
          payload: { state: "ready", interaction_authority: "python_authoritative" },
        }),
      ),
    });

    expect(onTranscript).toHaveBeenCalledTimes(1);
    expect(onState).toHaveBeenCalledWith(
      "ready",
      expect.objectContaining({ sequence: 0 }),
    );
    await transport.stopAssistant();
    expect(transport.channels.get("control").sent.at(-1)).toEqual(
      expect.objectContaining({ type: "client.stop_assistant" }),
    );
    await transport.close();
  });

  it("consumes only monotonic typed floor effects for the current media stream", async () => {
    const onFloor = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onFloor,
    });
    await transport.connect({
      session_id: "floor-session",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "floor.state",
        session_id: "floor-session",
        stream_epoch: 1,
        sequence: 0,
        payload: { floor_state: "user_holds_floor", floor_epoch: 1 },
      })),
    });
    expect(onFloor).toHaveBeenCalledWith(
      "user_holds_floor",
      expect.objectContaining({ sequence: 0, turn_id: 0, generation_id: 0 }),
    );

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "floor.state",
        session_id: "floor-session",
        stream_epoch: 1,
        sequence: 1,
        payload: { floor_state: "silence", floor_epoch: 1 },
      })),
    });
    expect(onFloor).toHaveBeenCalledTimes(1);

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "floor.state",
        session_id: "floor-session",
        stream_epoch: 1,
        sequence: 2,
        payload: { floor_state: "invalid", floor_epoch: 2 },
      })),
    });
    expect(transport.lastEventSequence).toBe(0);

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "floor.state",
        session_id: "floor-session",
        stream_epoch: 1,
        sequence: 3,
        payload: { floor_state: "silence", floor_epoch: 2 },
      })),
    });
    expect(onFloor).toHaveBeenLastCalledWith(
      "silence",
      expect.objectContaining({ sequence: 3 }),
    );
  });

  it("routes projection events separately from legacy transcript fallback", async () => {
    const onProjection = vi.fn();
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onProjection,
      onTranscript,
    });
    await transport.connect({
      session_id: "projection-session",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "turn.provisional.started",
        session_id: "projection-session",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          provisional_id: "p-1",
          stream_epoch: 1,
          projection_revision: 1,
        },
      })),
    });
    expect(onProjection).toHaveBeenCalledWith(expect.objectContaining({
      type: "turn.provisional.started",
      provisional_id: "p-1",
      envelope_stream_epoch: 1,
    }));

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "user.transcript.final",
        session_id: "projection-session",
        stream_epoch: 1,
        sequence: 1,
        payload: { speaker: "user", text: "legacy", final: true },
      })),
    });
    expect(onTranscript).toHaveBeenCalledWith(expect.objectContaining({
      event_type: "user.transcript.final",
      text: "legacy",
    }));
  });

  it("rejects a projection payload that forges the envelope stream epoch", async () => {
    const onProjection = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onProjection,
    });
    await transport.connect({
      session_id: "projection-epoch-session",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 3,
    });

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "turn.provisional.patch",
        session_id: "projection-epoch-session",
        stream_epoch: 3,
        sequence: 0,
        payload: {
          provisional_id: "p-forged",
          stream_epoch: 99,
          projection_revision: 1,
        },
      })),
    });

    expect(onProjection).not.toHaveBeenCalled();
    expect(transport.lastEventSequence).toBe(-1);
  });

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
      data: JSON.stringify(mediaEvent({
        type: "user.transcript.final",
        session_id: "session-1",
        stream_epoch: 3,
        sequence: 2,
        payload: { text: "你好", turn_id: 1, generation_id: 1, tool_epoch: 0 },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "user.transcript.final",
        session_id: "session-1",
        stream_epoch: 3,
        sequence: 1,
        payload: { text: "迟到", turn_id: 1, generation_id: 1, tool_epoch: 0 },
      })),
    });

    expect(onState).toHaveBeenCalledWith("connecting");
    expect(onState).toHaveBeenCalledWith("ready");
    expect(onTranscript).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ text: "你好", final: true, speaker: "user" }),
    );
  });

  it("propagates task/context versions and rejects stale same-fence events", async () => {
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onTranscript,
    });
    await transport.connect({
      session_id: "session-version-fence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.text.final",
        session_id: "session-version-fence",
        stream_epoch: 1,
        sequence: 0,
        turn_id: 1,
        generation_id: 1,
        tool_epoch: 0,
        task_epoch: 3,
        context_version: 7,
        payload: { text: "最新结果" },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-version-fence",
        stream_epoch: 1,
        sequence: 1,
        turn_id: 1,
        generation_id: 1,
        tool_epoch: 0,
        task_epoch: 3,
        context_version: 7,
        payload: {
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
          final: false,
        },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-version-fence",
        stream_epoch: 1,
        sequence: 2,
        turn_id: 1,
        generation_id: 1,
        tool_epoch: 0,
        task_epoch: 2,
        context_version: 6,
        payload: {
          sequence: 1,
          source_start_sample: 480,
          frame_samples: 480,
          final: true,
        },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.text.final",
        session_id: "session-version-fence",
        stream_epoch: 1,
        sequence: 3,
        turn_id: 1,
        generation_id: 1,
        tool_epoch: 0,
        task_epoch: 2,
        context_version: 6,
        payload: { text: "迟到结果" },
      })),
    });

    expect(onTranscript).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenLastCalledWith(
      expect.objectContaining({ task_epoch: 3, context_version: 7, text: "最新结果" }),
    );
    expect(transport.lastAudioSequence).toBe(0);
    expect(transport.lastAudioSampleEnd).toBe(480);
    await transport.publishData({ type: "client.trace", payload: {} });
    expect(transport.channel.sent.at(-1)).toEqual(
      expect.objectContaining({ task_epoch: 3, context_version: 7 }),
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
        v: 1,
        protocol: "media-v1",
        type: "client.stop_assistant",
        event_id: expect.any(String),
        session_id: "session-2",
        stream_epoch: 1,
        sequence: 0,
        turn_id: 0,
        generation_id: 0,
        tool_epoch: 0,
        client_monotonic_ms: expect.any(Number),
        payload: expect.any(Object),
      }),
    );
    expect(transport.channel.sent[0]).not.toHaveProperty("server_monotonic_ms");
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

  it("drops invalid, stale, and cross-session events while ignoring future fields", async () => {
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
    const valid = mediaEvent({
      type: "transcript_delta",
      session_id: "session-event-fence",
      stream_epoch: 4,
      sequence: 0,
      payload: { text: "invalid", turn_id: 0, generation_id: 0, tool_epoch: 0 },
    });
    const without = (field) => {
      const event = { ...valid };
      delete event[field];
      return event;
    };
    for (const data of [
      without("v"),
      without("protocol"),
      without("event_id"),
      without("session_id"),
      without("stream_epoch"),
      without("sequence"),
      without("turn_id"),
      without("generation_id"),
      without("tool_epoch"),
      without("server_monotonic_ms"),
      without("payload"),
      { ...valid, v: 2 },
      { ...valid, event_id: "" },
      { ...valid, session_id: "other" },
      { ...valid, stream_epoch: 3 },
      { ...valid, stream_epoch: 5 },
      { ...valid, payload: null },
      {
        ...valid,
        payload: { ...valid.payload, generation_id: 1 },
      },
    ]) {
      transport.channel.onmessage({ data: JSON.stringify(data) });
    }
    expect(onTranscript).not.toHaveBeenCalled();
    transport.channel.onmessage({
      data: JSON.stringify({ ...valid, future_extension: true }),
    });
    expect(onTranscript).toHaveBeenCalledTimes(1);
  });

  it("drops events that omit the complete media fence", async () => {
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onTranscript,
    });
    await transport.connect({
      session_id: "session-missing-fence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    const missingFence = mediaEvent({
      type: "user.transcript.final",
      session_id: "session-missing-fence",
      stream_epoch: 1,
      sequence: 0,
      payload: { text: "没有 fence", turn_id: 1, generation_id: 1 },
    });
    delete missingFence.tool_epoch;
    transport.channel.onmessage({ data: JSON.stringify(missingFence) });
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("does not let an invalid high-sequence event poison the next valid event", async () => {
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onTranscript,
    });
    await transport.connect({
      session_id: "session-sequence-fence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    const invalid = mediaEvent({
      type: "user.transcript.final",
      session_id: "session-sequence-fence",
      stream_epoch: 1,
      sequence: 7,
      payload: { text: "invalid", turn_id: 1, generation_id: 1 },
    });
    delete invalid.tool_epoch;
    transport.channel.onmessage({ data: JSON.stringify(invalid) });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "user.transcript.final",
        event_id: "server-event-valid-after-invalid",
        session_id: "session-sequence-fence",
        stream_epoch: 1,
        sequence: 7,
        payload: { ...invalid.payload, tool_epoch: 0, text: "valid" },
      })),
    });
    expect(onTranscript).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ text: "valid", final: true }),
    );
  });

  it("keeps tool epochs monotonic within one turn and generation", async () => {
    const onTranscript = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      onTranscript,
    });
    await transport.connect({
      session_id: "session-tool-fence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    const emit = (sequence, toolEpoch, text) =>
      transport.channel.onmessage({
        data: JSON.stringify(mediaEvent({
          type: "assistant.text.delta",
          session_id: "session-tool-fence",
          stream_epoch: 1,
          sequence,
          payload: {
            text,
            turn_id: 1,
            generation_id: 1,
            tool_epoch: toolEpoch,
          },
        })),
      });

    emit(10, 2, "current");
    emit(20, 1, "stale");
    emit(11, 3, "newer");
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.text.delta",
        session_id: "session-tool-fence",
        stream_epoch: 1,
        sequence: 30,
        payload: {
          text: "old turn",
          turn_id: 0,
          generation_id: 99,
          tool_epoch: 99,
        },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.text.delta",
        session_id: "session-tool-fence",
        stream_epoch: 1,
        sequence: 12,
        payload: {
          text: "next turn",
          turn_id: 2,
          generation_id: 2,
          tool_epoch: 0,
        },
      })),
    });

    expect(onTranscript.mock.calls.map(([event]) => event.text)).toEqual([
      "current",
      "newer",
      "next turn",
    ]);
    expect(transport.currentTurnId).toBe(2);
    expect(transport.currentGenerationId).toBe(2);
    expect(transport.currentToolEpoch).toBe(0);
    expect(transport.lastEventSequence).toBe(12);
  });

  it("publishes monotonic playback progress bounded by received audio", async () => {
    let playbackTime = 12;
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      getPlaybackTime: () => playbackTime,
    });
    await transport.connect({
      session_id: "session-progress",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    expect(await transport.publishPlaybackProgressFromTime(12)).toBe(false);
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-progress",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    expect(await transport.publishPlaybackProgressFromTime(12.03)).toBe(true);
    expect(await transport.publishPlaybackProgressFromTime(12.03)).toBe(false);
    expect(transport.channel.sent.at(-1)).toEqual(
      expect.objectContaining({
        type: "client.playback.progress",
        turn_id: 1,
        generation_id: 1,
        payload: expect.objectContaining({
          received_sequence: 0,
          rendered_sample_end: 480,
          approximate: true,
          tool_epoch: 0,
        }),
      }),
    );

    playbackTime = 18;
    expect(await transport.publishPlaybackProgressFromTime(playbackTime)).toBe(false);
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-progress",
        stream_epoch: 1,
        sequence: 1,
        payload: {
          turn_id: 2,
          generation_id: 2,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    expect(await transport.publishPlaybackProgressFromTime(18.01)).toBe(true);
    expect(transport.channel.sent.at(-1).payload).toEqual(
      expect.objectContaining({
        rendered_sample_end: 240,
        generation_id: 2,
      }),
    );
  });

  it("publishes rendered-sample ACKs from an AudioWorklet tracker and fences generations", async () => {
    const getPlaybackTime = vi.fn(() => 12);
    const tracker = new FakePlaybackTracker();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      getPlaybackTime,
    });
    await transport.connect({
      session_id: "session-worklet-progress",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.setPlaybackTracker(tracker);
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-worklet-progress",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 960,
        },
      })),
    });

    tracker.emit(960);
    await Promise.resolve();
    expect(transport.channel.sent.at(-1)).toEqual(
      expect.objectContaining({
        type: "client.playback.progress",
        payload: expect.objectContaining({
          rendered_sample_end: 480,
          approximate: false,
          generation_id: 1,
        }),
      }),
    );
    expect(getPlaybackTime).not.toHaveBeenCalled();

    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-worklet-progress",
        stream_epoch: 1,
        sequence: 1,
        payload: {
          turn_id: 2,
          generation_id: 2,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 960,
        },
      })),
    });
    tracker.emit(960);
    expect(
      transport.channel.sent.filter(
        (event) => event.type === "client.playback.progress",
      ),
    ).toHaveLength(1);
    tracker.emit(1_920);
    await Promise.resolve();
    expect(transport.channel.sent.at(-1)).toEqual(
      expect.objectContaining({
        type: "client.playback.progress",
        generation_id: 2,
        payload: expect.objectContaining({
          rendered_sample_end: 480,
          approximate: false,
        }),
      }),
    );
  });

  it("detaches worklet ACK callbacks across reconnect and close", async () => {
    const tracker = new FakePlaybackTracker();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      reconnectSession: vi.fn(async () => ({
        session_id: "session-worklet-reconnect",
        stream_epoch: 2,
        streamcore: {
          whip_url: "https://media.example/whip",
          token: "rotated-token",
          stream_epoch: 2,
        },
      })),
    });
    await transport.connect({
      session_id: "session-worklet-reconnect",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.setPlaybackTracker(tracker);
    const oldChannel = transport.channel;
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-worklet-reconnect",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    tracker.emit(960);
    const beforeReconnect = oldChannel.sent.length;

    await transport.reconnect();
    tracker.emit(1_920);
    expect(oldChannel.sent).toHaveLength(beforeReconnect);
    // Reattachment is performed by the hook after the new remote track event.
    transport.setPlaybackTracker(tracker);
    const newChannel = transport.channel;
    expect(newChannel.sent).toHaveLength(0);
    newChannel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-worklet-reconnect",
        stream_epoch: 2,
        sequence: 0,
        payload: {
          turn_id: 2,
          generation_id: 2,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    tracker.emit(2_880);
    await Promise.resolve();
    expect(newChannel.sent.at(-1)).toEqual(
      expect.objectContaining({
        type: "client.playback.progress",
        stream_epoch: 2,
        payload: expect.objectContaining({ approximate: false }),
      }),
    );
    await transport.close();
    tracker.emit(3_840);
    expect(oldChannel.sent).toHaveLength(beforeReconnect);
    expect(newChannel.sent.filter((event) => event.type === "client.playback.progress")).toHaveLength(1);
  });

  it("keeps an exact watermark suppressed after mute and seek until a new fence", async () => {
    let audible = true;
    const tracker = new FakePlaybackTracker();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      isPlaybackAudible: () => audible,
    });
    await transport.connect({
      session_id: "session-worklet-muted",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.setPlaybackTracker(tracker);
    const emitFrame = (eventSequence, turnId, generationId, sequence, sourceStart) =>
      transport.channel.onmessage({
        data: JSON.stringify(mediaEvent({
          type: "assistant.audio.frame",
          session_id: "session-worklet-muted",
          stream_epoch: 1,
          sequence: eventSequence,
          payload: {
            turn_id: turnId,
            generation_id: generationId,
            tool_epoch: 0,
            sequence,
            source_start_sample: sourceStart,
            frame_samples: 960,
          },
        })),
      });

    emitFrame(0, 1, 1, 0, 0);
    tracker.emit(960);
    expect(transport.channel.sent.at(-1).payload).toEqual(
      expect.objectContaining({ rendered_sample_end: 480, approximate: false }),
    );
    const ackCount = transport.channel.sent.filter(
      (event) => event.type === "client.playback.progress",
    ).length;

    audible = false;
    tracker.emit(1_920);
    transport.resetPlaybackTimelineFromTime(0);
    audible = true;
    emitFrame(1, 1, 1, 1, 960);
    tracker.emit(2_880);
    expect(transport.channel.sent.filter(
      (event) => event.type === "client.playback.progress",
    )).toHaveLength(ackCount);

    emitFrame(2, 2, 2, 0, 0);
    tracker.emit(3_840);
    expect(transport.channel.sent.at(-1).payload).toEqual(
      expect.objectContaining({
        rendered_sample_end: 480,
        approximate: false,
        generation_id: 2,
      }),
    );
  });

  it("rebuilds the playback baseline after mute without resetting audio sequence", async () => {
    let playbackTime = 2;
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\o=answer"),
      getPlaybackTime: () => playbackTime,
      isPlaybackAudible: () => true,
    });
    await transport.connect({
      session_id: "session-progress-baseline",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-progress-baseline",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    expect(await transport.publishPlaybackProgressFromTime(2.02)).toBe(true);
    transport.resetPlaybackTimelineFromTime(2.02);
    playbackTime = 2.02;
    expect(await transport.publishPlaybackProgressFromTime(2.02)).toBe(false);
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-progress-baseline",
        stream_epoch: 1,
        sequence: 1,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 1,
          source_start_sample: 480,
          frame_samples: 480,
        },
      })),
    });
    playbackTime = 2.04;
    expect(await transport.publishPlaybackProgressFromTime(2.04)).toBe(true);
  });

  it("rejects discontinuous audio metadata without poisoning the event sequence", async () => {
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      getPlaybackTime: () => 0,
    });
    await transport.connect({
      session_id: "session-audio-sequence",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    const frame = (eventSequence, audioSequence, sourceStart) => mediaEvent({
      type: "assistant.audio.frame",
      session_id: "session-audio-sequence",
      stream_epoch: 1,
      sequence: eventSequence,
      payload: {
        turn_id: 1,
        generation_id: 1,
        tool_epoch: 0,
        sequence: audioSequence,
        source_start_sample: sourceStart,
        frame_samples: 480,
      },
    });

    transport.channel.onmessage({ data: JSON.stringify(frame(9, 2, 960)) });
    transport.channel.onmessage({ data: JSON.stringify(frame(1, 0, 0)) });
    transport.channel.onmessage({ data: JSON.stringify(frame(2, 1, 480)) });

    expect(transport.lastEventSequence).toBe(2);
    expect(transport.lastAudioSequence).toBe(1);
    expect(transport.lastAudioSampleEnd).toBe(960);
  });

  it("releases a playback watermark when DataChannel send fails", async () => {
    const playbackTime = 4;
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      getPlaybackTime: () => playbackTime,
    });
    await transport.connect({
      session_id: "session-progress-retry",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });
    expect(await transport.publishPlaybackProgressFromTime(4)).toBe(false);
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "assistant.audio.frame",
        session_id: "session-progress-retry",
        stream_epoch: 1,
        sequence: 0,
        payload: {
          turn_id: 1,
          generation_id: 1,
          tool_epoch: 0,
          sequence: 0,
          source_start_sample: 0,
          frame_samples: 480,
        },
      })),
    });
    transport.channel.send = () => {
      throw new Error("channel closed");
    };
    await expect(transport.publishPlaybackProgressFromTime(4.03)).rejects.toThrow(
      "channel closed",
    );
    expect(transport.lastPlaybackProgressSample).toBe(-1);
  });

  it("times out a hung WHIP exchange", async () => {
    let exchangeSignal = null;
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn((_sessionId, _offer, options) => {
        exchangeSignal = options.signal;
        return new Promise(() => undefined);
      }),
      connectTimeoutMs: 5,
    });
    await expect(
      transport.connect({
        session_id: "session-timeout",
        whip_url: "https://media.example/whip",
        token: "token",
        stream_epoch: 1,
      }),
    ).rejects.toThrow("媒体协商超时");
    expect(exchangeSignal).toBeInstanceOf(AbortSignal);
    expect(exchangeSignal.aborted).toBe(true);
  });

  it("aborts hung heartbeat and reconnect requests", async () => {
    let heartbeatSignal = null;
    let reconnectSignal = null;
    const onDisconnected = vi.fn();
    const transport = new StreamCoreTransport({
      RTCPeerConnectionImpl: FakePeerConnection,
      getUserMedia: vi.fn(async () => stream()),
      exchangeSdp: vi.fn(async () => "v=0\\no=answer"),
      heartbeatSession: vi.fn((_sessionId, _epoch, options) => {
        heartbeatSignal = options.signal;
        return new Promise(() => undefined);
      }),
      reconnectSession: vi.fn((_sessionId, _current, _epoch, options) => {
        reconnectSignal = options.signal;
        return new Promise(() => undefined);
      }),
      onDisconnected,
      connectTimeoutMs: 5,
      heartbeatIntervalMs: 0,
    });
    await transport.connect({
      session_id: "session-request-timeouts",
      whip_url: "https://media.example/whip",
      token: "token",
      stream_epoch: 1,
    });

    await transport._renewHeartbeat();
    expect(heartbeatSignal.aborted).toBe(true);
    expect(onDisconnected).toHaveBeenCalledWith("media_heartbeat_failed");
    transport._disconnectNotified = false;
    await expect(transport.reconnect()).rejects.toThrow("媒体重连超时");
    expect(reconnectSignal.aborted).toBe(true);
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
      data: JSON.stringify(mediaEvent({
        v: 1,
        type: "playback.flush",
        session_id: "session-events",
        stream_epoch: 2,
        sequence: 1,
        turn_id: 0,
        generation_id: 0,
        tool_epoch: 0,
        payload: { reason: "generation_cancel", turn_id: 0, generation_id: 0, tool_epoch: 0 },
      })),
    });
    transport.channel.onmessage({
      data: JSON.stringify(mediaEvent({
        type: "ping",
        session_id: "session-events",
        stream_epoch: 2,
        sequence: 2,
        turn_id: 0,
        generation_id: 0,
        tool_epoch: 0,
        server_monotonic_ms: 2,
        payload: {},
      })),
    });
    expect(onPlaybackFlush).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "generation_cancel" }),
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
      expect.objectContaining({
        expectedFence: {
          stream_epoch: 1,
          turn_id: 0,
          generation_id: 0,
          tool_epoch: 0,
        },
      }),
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
    expect(reconnectSession).toHaveBeenCalledWith(
      "session-epoch",
      expect.any(Object),
      4,
      expect.objectContaining({ signal: expect.any(AbortSignal), timeoutMs: 10_000 }),
    );
    expect(transport.streamEpoch).toBe(5);
    expect(transport.session.stream_epoch).toBe(5);
  });

  it("renews the authoritative session TTL without changing the media epoch", async () => {
    const heartbeatSession = vi.fn(async () => ({
      stream_epoch: 4,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      token: "renewed-token",
      token_expires_at: new Date(Date.now() + 30_000).toISOString(),
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
    expect(heartbeatSession).toHaveBeenCalledWith(
      "session-heartbeat",
      4,
      expect.objectContaining({ signal: expect.any(AbortSignal), timeoutMs: 10_000 }),
    );
    expect(transport.session.streamcore.expires_at).not.toBe("old");
    expect(transport.session.streamcore.token).toBe("renewed-token");
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
