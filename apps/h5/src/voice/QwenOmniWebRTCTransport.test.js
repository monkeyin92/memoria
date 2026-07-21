import { beforeEach, describe, expect, it, vi } from "vitest";

import { QwenOmniWebRTCTransport } from "./QwenOmniWebRTCTransport.js";
import { extractInboundAudioStats } from "./webrtcStats.js";

class FakeDataChannel {
  constructor(label) {
    this.label = label;
    this.readyState = "open";
    this.sent = [];
    this.onmessage = null;
  }

  send(payload) {
    this.sent.push(JSON.parse(payload));
  }

  emit(event) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  close() {
    this.readyState = "closed";
  }
}

class FakePeerConnection {
  constructor(configuration) {
    this.configuration = configuration;
    this.iceGatheringState = "complete";
    this.connectionState = "new";
    this.localDescription = null;
    this.remoteDescription = null;
    this.senders = [];
    this.channels = [];
    this.listeners = new Map();
    this.ondatachannel = null;
    this.ontrack = null;
    this.onconnectionstatechange = null;
    peerConnections.push(this);
  }

  addTrack(track) {
    this.senders.push({ track });
    return { track };
  }

  createDataChannel(label) {
    const channel = new FakeDataChannel(label);
    this.channels.push(channel);
    return channel;
  }

  createOffer() {
    return Promise.resolve({ type: "offer", sdp: "offer-sdp" });
  }

  setLocalDescription(description) {
    this.localDescription = description;
    return Promise.resolve();
  }

  setRemoteDescription(description) {
    this.remoteDescription = description;
    return Promise.resolve();
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  removeEventListener(name, listener) {
    if (this.listeners.get(name) === listener) this.listeners.delete(name);
  }

  emitDataChannel(channel) {
    this.ondatachannel?.({ channel });
  }

  emitTrack(stream, receiver = undefined) {
    this.ontrack?.({
      track: { id: "remote-audio", kind: "audio" },
      streams: [stream],
      receiver,
    });
  }

  close() {
    this.connectionState = "closed";
  }
}

const peerConnections = [];

function callbacks() {
  return {
    exchangeSdp: vi.fn().mockResolvedValue("answer-sdp"),
    onState: vi.fn(),
    onTranscript: vi.fn(),
    onRemoteStream: vi.fn(),
    onDiagnostic: vi.fn(),
    onError: vi.fn(),
    onDisconnected: vi.fn(),
  };
}

describe("QwenOmniWebRTCTransport", () => {
  beforeEach(() => {
    peerConnections.length = 0;
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
  });

  it("keeps only allowlisted numeric inbound audio metrics", () => {
    const report = new Map([
      [
        "audio",
        {
          type: "inbound-rtp",
          kind: "audio",
          jitter: 0.004,
          packetsLost: 2,
          packetsReceived: 100,
          packetsDiscarded: 1,
          bytesReceived: 12000,
          nackCount: 3,
          concealedSamples: 480,
          silentConcealedSamples: 240,
          totalSamplesReceived: 48000,
          concealmentEvents: 1,
          jitterBufferDelay: 0.12,
          jitterBufferTargetDelay: 0.18,
          jitterBufferMinimumDelay: 0.06,
          jitterBufferEmittedCount: 4800,
          totalSamplesDuration: 1,
          codecId: "must-not-leave-browser",
          transcript: "must-not-leave-browser",
        },
      ],
    ]);

    expect(extractInboundAudioStats(report)).toEqual({
      jitter: 0.004,
      packets_lost: 2,
      packets_received: 100,
      packets_discarded: 1,
      bytes_received: 12000,
      nack_count: 3,
      concealed_samples: 480,
      silent_concealed_samples: 240,
      total_samples_received: 48000,
      concealment_events: 1,
      jitter_buffer_delay: 0.12,
      jitter_buffer_target_delay: 0.18,
      jitter_buffer_minimum_delay: 0.06,
      jitter_buffer_emitted_count: 4800,
      total_samples_duration: 1,
      concealment_ratio: 0.01,
      non_silent_concealment_ratio: 0.005,
      average_jitter_buffer_delay_ms: 0.025,
      average_jitter_buffer_target_delay_ms: 0.037,
      encoded_audio_bitrate_kbps: 96,
    });
  });

  it("samples inbound audio quality after WebRTC connects", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const pc = peerConnections[0];
    pc.getStats = vi.fn().mockResolvedValue(
      new Map([
        [
          "audio",
          {
            type: "inbound-rtp",
            kind: "audio",
            jitter: 0.006,
            packetsLost: 3,
            packetsReceived: 120,
            concealedSamples: 960,
            concealmentEvents: 2,
            jitterBufferDelay: 0.2,
          },
        ],
      ]),
    );
    pc.connectionState = "connected";
    pc.onconnectionstatechange();

    await vi.waitFor(() =>
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "webrtc_inbound_audio",
        "ok",
        expect.objectContaining({
          jitter: 0.006,
          packets_lost: 3,
          concealed_samples: 960,
        }),
      ),
    );
    transport.close();
  });

  it("keeps microphone media gated until session.updated and uses semantic VAD", async () => {
    vi.useFakeTimers();
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);

    const preparing = transport.prepare();
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith(
      expect.objectContaining({ audio: expect.any(Object) }),
    );
    await preparing;
    expect(transport.speakerGate.state()).toBe("disabled");
    await transport.connect({
      session_id: "omni-session",
      config: { voice: "Cherry" },
    });

    const pc = peerConnections[0];
    expect(pc.configuration).toEqual({ iceServers: [] });
    expect(pc.channels[0].label).toBe("oai-events");
    expect(track.enabled).toBe(false);
    expect(events.exchangeSdp).toHaveBeenCalledWith(
      "omni-session",
      "offer-sdp",
    );
    expect(pc.remoteDescription).toEqual({ type: "answer", sdp: "answer-sdp" });

    const txt = new FakeDataChannel("txt");
    pc.emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    expect(txt.sent).toHaveLength(1);
    expect(txt.sent[0]).toEqual(
      expect.objectContaining({
        type: "session.update",
        session: expect.objectContaining({
          voice: "Cherry",
          instructions: expect.stringContaining("一到两句"),
          input_audio_transcription: {
            model: "qwen3-asr-flash-realtime",
          },
          turn_detection: expect.objectContaining({
            type: "semantic_vad",
            threshold: 0.5,
            silence_duration_ms: 800,
          }),
        }),
      }),
    );
    expect(txt.sent[0].session.instructions).toContain("多步骤");
    expect(txt.sent[0].session.instructions).toContain("直接问题");
    expect(txt.sent[0].session.instructions).toContain("不要连续两轮使用同一个开场");
    expect(txt.sent[0].session.instructions).toContain("听见用户自然笑出声");
    expect(txt.sent[0].session.instructions).toContain("严肃风险");
    expect(txt.sent[0].session.instructions).toContain("口语训练");
    expect(txt.sent[0].session.instructions).toContain("今天星期几");
    expect(txt.sent[0].session.instructions).toContain("不要把“哈哈”逐字念出来");
    expect(txt.sent[0].session.instructions).toContain("接受上一轮的提议");
    expect(txt.sent[0].session.instructions).toContain("正文恢复正常速度");
    expect(txt.sent[0].session.instructions).not.toContain("用户长段表达时可以偶尔自然附和");
    expect(txt.sent[0].session).not.toHaveProperty("smooth_output");
    expect(track.enabled).toBe(false);

    try {
      txt.emit({ type: "session.updated" });
      expect(track.enabled).toBe(true);
      expect(events.onState).toHaveBeenLastCalledWith("ready");
      expect(
        txt.sent.filter(({ type }) => type === "response.create"),
      ).toHaveLength(0);

      await vi.advanceTimersByTimeAsync(400);
      expect(
        txt.sent.filter(({ type }) => type === "response.create"),
      ).toHaveLength(1);

      txt.emit({ type: "session.updated" });
      await vi.advanceTimersByTimeAsync(400);
      expect(
        txt.sent.filter(({ type }) => type === "response.create"),
      ).toHaveLength(1);
    } finally {
      transport.close();
      vi.useRealTimers();
    }
  });

  it("requests short yield ack for wait intent variants (not exact enum)", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      // Disable client speaker gate so we do not sit in enroll pending.
      transport.speakerGate = {
        state: () => "open",
        close: () => undefined,
        forceOpen: () => undefined,
      };
      await transport.connect({
        session_id: "omni-session",
        config: { voice: "Liora Mira" },
      });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });
      // Truncated prod ASR must still force yield ack.
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "wait-0",
      });
      txt.emit({
        type: "conversation.item.input_audio_transcription.completed",
        item_id: "wait-0",
        transcript: "哎，等。",
      });
      expect(
        txt.sent.filter(({ type }) => type === "response.create").at(-1)
          ?.response?.instructions,
      ).toContain("嗯，你说");
      // Plus often mangles「等一下」to English garbage.
      txt.emit({ type: "response.created", response: { id: "ans-1" } });
      const createsAfterFirst = txt.sent.filter(
        ({ type }) => type === "response.create",
      ).length;
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "wait-egg",
      });
      // Barge cancels ans-1; controlled create must wait for response.done.
      txt.emit({
        type: "conversation.item.input_audio_transcription.completed",
        item_id: "wait-egg",
        transcript: "Uh, need egg.",
      });
      expect(
        txt.sent.filter(({ type }) => type === "response.create"),
      ).toHaveLength(createsAfterFirst);
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_controlled_create_queued",
        "ok",
        expect.objectContaining({ kind: "interrupt_ack" }),
      );
      txt.emit({ type: "response.done", response: { id: "ans-1" } });
      expect(
        txt.sent.filter(({ type }) => type === "response.create").at(-1)
          ?.response?.instructions,
      ).toContain("嗯，你说");
      txt.emit({ type: "response.created", response: { id: "ans-egg" } });
      txt.emit({ type: "response.done", response: { id: "ans-egg" } });
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "wait-1",
      });
      txt.emit({
        type: "conversation.item.input_audio_transcription.completed",
        item_id: "wait-1",
        transcript: "你等一下啊",
      });
      const creates = txt.sent.filter(({ type }) => type === "response.create");
      expect(creates.length).toBeGreaterThanOrEqual(1);
      const ack = creates.at(-1);
      expect(ack.response.instructions).toContain("嗯，你说");
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_interrupt_ack_requested",
        "ok",
        expect.objectContaining({ kind: "interrupt_ack" }),
      );
    } finally {
      transport.close();
    }
  });

  it("serializes interrupt_ack create after cancel settles (no active-response race)", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      transport.speakerGate = {
        state: () => "open",
        close: () => undefined,
        forceOpen: () => undefined,
      };
      await transport.connect({
        session_id: "omni-session",
        config: { voice: "Liora Mira" },
      });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });

      // AI is speaking.
      txt.emit({ type: "response.created", response: { id: "ai-talk" } });
      txt.emit({
        type: "response.audio_transcript.delta",
        response_id: "ai-talk",
        item_id: "ai-item",
        delta: "我继续说很长一段",
      });

      // User barges with wait intent.
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "barge-wait",
      });
      expect(txt.sent.some(({ type }) => type === "response.cancel")).toBe(true);
      const createsBeforeAck = txt.sent.filter(
        ({ type }) => type === "response.create",
      ).length;

      txt.emit({
        type: "conversation.item.input_audio_transcription.completed",
        item_id: "barge-wait",
        transcript: "等一下",
      });
      // Must NOT create while server still has active response.
      expect(
        txt.sent.filter(({ type }) => type === "response.create"),
      ).toHaveLength(createsBeforeAck);
      expect(events.onError).not.toHaveBeenCalled();

      // Simulate the race error if a create had been sent early — soft recover.
      txt.emit({
        type: "error",
        error: {
          type: "invalid_request_error",
          code: "invalid_request_error",
          message: "Conversation already has an active response",
        },
      });
      expect(events.onError).not.toHaveBeenCalled();
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_active_response_conflict",
        "error",
        expect.objectContaining({
          message: expect.stringContaining("active response"),
        }),
      );

      // Server settles → flush interrupt ack create.
      txt.emit({ type: "response.done", response: { id: "ai-talk" } });
      const ackCreate = txt.sent
        .filter(({ type }) => type === "response.create")
        .at(-1);
      expect(ackCreate?.response?.instructions).toContain("嗯，你说");
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_interrupt_ack_requested",
        "ok",
        expect.objectContaining({ kind: "interrupt_ack" }),
      );
    } finally {
      transport.close();
    }
  });

  it("yields short ack when wait lands in post-response feedback guard", async () => {
    vi.useFakeTimers();
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      transport.speakerGate = {
        state: () => "open",
        close: () => undefined,
        forceOpen: () => undefined,
      };
      await transport.connect({
        session_id: "omni-session",
        config: { voice: "Liora Mira" },
      });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });

      // AI finishes a free-form turn → feedback guard arms.
      txt.emit({ type: "response.created", response: { id: "ai-end" } });
      txt.emit({
        type: "response.audio_transcript.delta",
        response_id: "ai-end",
        item_id: "ai-item",
        delta: "好的我继续讲",
      });
      txt.emit({ type: "response.done", response: { id: "ai-end" } });
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_feedback_guard_started",
        "ok",
        expect.anything(),
      );

      // User says 等一下 during the feedback mute window.
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "wait-in-feedback",
      });
      expect(events.onDiagnostic).toHaveBeenCalledWith("omni_feedback_suppressed");
      txt.emit({
        type: "input_audio_buffer.speech_stopped",
        item_id: "wait-in-feedback",
      });
      // Auto VAD reply is cancelled as echo; must not leave dead air.
      txt.emit({ type: "response.created", response: { id: "echo-reply" } });
      expect(txt.sent.some(({ type }) => type === "response.cancel")).toBe(true);
      txt.emit({ type: "response.done", response: { id: "echo-reply" } });

      // 700ms barge fallback forces 嗯你说 even if ASR empty/garbled.
      await vi.advanceTimersByTimeAsync(750);
      const ack = txt.sent
        .filter(({ type }) => type === "response.create")
        .at(-1);
      expect(ack?.response?.instructions).toContain("嗯，你说");
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_interrupt_ack_requested",
        "ok",
        expect.objectContaining({ kind: "interrupt_ack" }),
      );
    } finally {
      transport.close();
      vi.useRealTimers();
    }
  });

  it("suppresses welcome without cancelling an unknown response when user speaks first", async () => {
    vi.useFakeTimers();
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      await transport.connect({ session_id: "omni-session", config: {} });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "user-first",
      });

      await vi.advanceTimersByTimeAsync(500);
      expect(txt.sent.filter(({ type }) => type === "response.create")).toHaveLength(0);
      expect(txt.sent.filter(({ type }) => type === "response.cancel")).toHaveLength(0);
      expect(events.onDiagnostic).toHaveBeenCalledWith("omni_welcome_suppressed");
    } finally {
      transport.close();
      vi.useRealTimers();
    }
  });

  it("waits for response.created before cancelling a superseded welcome", async () => {
    vi.useFakeTimers();
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      await transport.connect({ session_id: "omni-session", config: {} });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });
      await vi.advanceTimersByTimeAsync(400);
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "user-after-request",
      });

      expect(txt.sent.filter(({ type }) => type === "response.cancel")).toHaveLength(0);
      txt.emit({ type: "response.created", response: { id: "late-welcome" } });
      txt.emit({ type: "response.created", response: { id: "late-welcome" } });
      expect(txt.sent.filter(({ type }) => type === "response.cancel")).toHaveLength(1);
    } finally {
      transport.close();
      vi.useRealTimers();
    }
  });

  it("attaches each remote audio track only once", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const localStream = {
      getAudioTracks: () => [track],
      getTracks: () => [track],
    };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(localStream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });

    const remoteStream = { id: "remote" };
    peerConnections[0].emitTrack(remoteStream);
    peerConnections[0].emitTrack(remoteStream);

    expect(events.onRemoteStream).toHaveBeenCalledTimes(1);
    expect(events.onRemoteStream).toHaveBeenCalledWith(remoteStream);
  });

  it("computes a shorter feedback guard on clean RTP and longer on dirty path", async () => {
    const { computeFeedbackGuardMs } = await import("./QwenOmniWebRTCTransport.js");
    expect(
      computeFeedbackGuardMs({
        non_silent_concealment_ratio: 0,
        average_jitter_buffer_delay_ms: 20,
        packets_discarded: 0,
        packets_received: 1000,
      }),
    ).toBeLessThanOrEqual(250);
    expect(
      computeFeedbackGuardMs({
        non_silent_concealment_ratio: 0.03,
        average_jitter_buffer_delay_ms: 120,
        packets_discarded: 200,
        packets_received: 1000,
      }),
    ).toBeGreaterThanOrEqual(500);
  });

  it("sets a small receiver jitter buffer target when the browser supports it", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const localStream = {
      getAudioTracks: () => [track],
      getTracks: () => [track],
    };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(localStream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });

    const receiver = { jitterBufferTarget: null };
    peerConnections[0].emitTrack({ id: "remote" }, receiver);

    expect(receiver.jitterBufferTarget).toBe(120);
    expect(events.onDiagnostic).toHaveBeenCalledWith(
      "omni_playout_buffer_configured",
    );
  });

  it("suppresses the automatic response caused by post-playback microphone feedback", async () => {
    vi.useFakeTimers();
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    try {
      await transport.prepare();
      await transport.connect({ session_id: "omni-session", config: {} });
      const txt = new FakeDataChannel("txt");
      peerConnections[0].emitDataChannel(txt);
      txt.emit({ type: "session.created" });
      txt.emit({ type: "session.updated" });
      txt.emit({ type: "response.created", response: { id: "welcome" } });
      txt.emit({ type: "response.done", response: { id: "welcome" } });

      expect(track.enabled).toBe(false);
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "assistant-tail-echo",
      });
      txt.emit({
        type: "input_audio_buffer.speech_stopped",
        item_id: "assistant-tail-echo",
      });
      txt.emit({ type: "response.created", response: { id: "echo-response" } });

      expect(txt.sent.at(-1)).toEqual({ type: "response.cancel" });
      expect(events.onDiagnostic).toHaveBeenCalledWith(
        "omni_feedback_suppressed",
      );
      // Still track speech for control phrases (等一下) during feedback window.
      expect(events.onState).toHaveBeenCalledWith("listening");

      await vi.advanceTimersByTimeAsync(800);
      expect(track.enabled).toBe(true);
      txt.emit({
        type: "input_audio_buffer.speech_started",
        item_id: "real-user",
      });
      expect(events.onState).toHaveBeenLastCalledWith("listening");
    } finally {
      transport.close();
      vi.useRealTimers();
    }
  });

  it("cancels a response that starts after the user has begun speaking", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const txt = new FakeDataChannel("txt");
    peerConnections[0].emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });
    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-item",
    });
    txt.sent.length = 0;

    txt.emit({ type: "response.created", response: { id: "late-response" } });

    expect(txt.sent).toContainEqual({ type: "response.cancel" });
    expect(events.onState).toHaveBeenLastCalledWith("listening");
  });

  it("maps realtime transcripts, receives RTP audio, and cancels on the txt channel", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const pc = peerConnections[0];
    const txt = new FakeDataChannel("txt");
    pc.emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });

    const remoteStream = { id: "remote" };
    pc.emitTrack(remoteStream);
    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-item-1",
    });
    txt.emit({
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "user-item-1",
      transcript: "我今天有点累。",
    });
    txt.emit({ type: "response.created", response: { id: "response-1" } });
    txt.emit({
      type: "response.output_item.added",
      response_id: "response-1",
      item: { id: "item-1" },
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-1",
      item_id: "item-1",
      delta: "那就",
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-1",
      item_id: "item-1",
      delta: "先休息一下。",
    });
    txt.emit({
      type: "response.text.done",
      response_id: "response-1",
      item_id: "item-1",
      text: "那就先休息一下。",
    });
    txt.emit({ type: "response.done", response: { id: "response-1" } });

    expect(events.onRemoteStream).toHaveBeenCalledWith(remoteStream);
    expect(events.onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({
        speaker: "user",
        text: "我今天有点累。",
        final: true,
      }),
    );
    expect(events.onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({
        speaker: "assistant",
        text: "那就先休息一下。",
        final: true,
        heard: false,
      }),
    );
    expect(
      events.onTranscript.mock.calls.filter(
        ([event]) => event.speaker === "assistant" && event.final === false,
      ),
    ).toHaveLength(2);
    expect(events.onState.mock.calls.map(([state]) => state)).toEqual(
      expect.arrayContaining(["listening", "thinking", "speaking", "ready"]),
    );

    const cancelCount = txt.sent.filter(
      ({ type }) => type === "response.cancel",
    ).length;
    transport.cancelResponse();
    expect(
      txt.sent.filter(({ type }) => type === "response.cancel"),
    ).toHaveLength(cancelCount);
  });

  it("stops local media and ignores late events after close", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const pc = peerConnections[0];
    const txt = new FakeDataChannel("txt");
    pc.emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });

    transport.close();
    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-item-2",
    });

    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(pc.connectionState).toBe("closed");
    expect(events.onState).not.toHaveBeenCalledWith("listening");
  });

  it("does not let a cancelled response overwrite a newer user speech state", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const txt = new FakeDataChannel("txt");
    peerConnections[0].emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });
    txt.emit({ type: "response.created", response: { id: "response-old" } });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-old",
      item_id: "item-old",
      delta: "旧回答",
    });
    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-item-3",
    });
    txt.emit({
      type: "response.text.done",
      response_id: "response-old",
      item_id: "item-old",
      text: "旧回答不应覆盖",
    });
    txt.emit({ type: "response.done", response: { id: "response-old" } });

    expect(events.onState).toHaveBeenLastCalledWith("listening");
    expect(events.onTranscript).not.toHaveBeenCalledWith(
      expect.objectContaining({ text: "旧回答不应覆盖" }),
    );
  });

  it("fences interleaved late events by provider response and item ids", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const txt = new FakeDataChannel("txt");
    peerConnections[0].emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });

    txt.emit({ type: "response.created", response: { id: "response-a" } });
    txt.emit({
      type: "response.output_item.added",
      response_id: "response-a",
      item: { id: "item-a" },
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-a",
      item_id: "item-a",
      delta: "旧回答",
    });
    txt.emit({ type: "response.created", response: { id: "response-b" } });
    txt.emit({
      type: "response.output_item.added",
      response_id: "response-b",
      item: { id: "item-b" },
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-a",
      item_id: "item-a",
      delta: "不应污染",
    });
    txt.emit({ type: "response.done", response: { id: "response-a" } });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-b",
      item_id: "item-b",
      delta: "新回答",
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "response-b",
      item_id: "wrong-item",
      delta: "错误分段",
    });
    txt.emit({
      type: "response.text.done",
      response_id: "response-b",
      item_id: "item-b",
      text: "新回答完成。",
    });
    txt.emit({ type: "response.done", response: { id: "response-b" } });

    const assistantTexts = events.onTranscript.mock.calls
      .map(([event]) => event)
      .filter(({ speaker }) => speaker === "assistant")
      .map(({ text }) => text);
    expect(assistantTexts).toContain("新回答完成。");
    expect(assistantTexts).not.toContain("不应污染");
    expect(assistantTexts).not.toContain("错误分段");
    expect(events.onState).toHaveBeenLastCalledWith("ready");
  });

  it("binds interleaved user transcripts to their speech-start item ids", async () => {
    const track = { kind: "audio", enabled: true, stop: vi.fn() };
    const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const events = callbacks();
    const transport = new QwenOmniWebRTCTransport(events);
    await transport.prepare();
    await transport.connect({ session_id: "omni-session", config: {} });
    const txt = new FakeDataChannel("txt");
    peerConnections[0].emitDataChannel(txt);
    txt.emit({ type: "session.created" });
    txt.emit({ type: "session.updated" });

    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-a",
    });
    txt.emit({
      type: "input_audio_buffer.speech_stopped",
      item_id: "user-a",
    });
    txt.emit({ type: "response.created", response: { id: "response-a" } });
    txt.emit({
      type: "input_audio_buffer.speech_started",
      item_id: "user-b",
    });
    txt.emit({
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "unknown-user",
      stash: "不应出现",
    });
    txt.emit({
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "user-b",
      stash: "B 还在说",
    });
    txt.emit({
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "user-a",
      transcript: "A 完成",
    });
    txt.emit({
      type: "response.created",
      response: { id: "premature-response" },
    });
    txt.emit({
      type: "response.text.delta",
      response_id: "premature-response",
      item_id: "premature-item",
      delta: "不应抢答",
    });

    expect(events.onState).toHaveBeenLastCalledWith("listening");

    txt.emit({
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "user-b",
      transcript: "B 完成",
    });
    txt.emit({ type: "response.created", response: { id: "response-b" } });
    txt.emit({
      type: "response.text.done",
      response_id: "response-b",
      item_id: "response-item-b",
      text: "现在回答 B。",
    });

    const transcriptEvents = events.onTranscript.mock.calls.map(
      ([event]) => event,
    );
    expect(
      transcriptEvents.filter(({ speaker }) => speaker === "user"),
    ).toEqual([
      expect.objectContaining({
        text: "B 还在说",
        final: false,
        turn_id: 2,
        generation_id: 1,
      }),
      expect.objectContaining({
        text: "A 完成",
        final: true,
        turn_id: 1,
        generation_id: 0,
      }),
      expect.objectContaining({
        text: "B 完成",
        final: true,
        turn_id: 2,
        generation_id: 1,
      }),
    ]);
    expect(transcriptEvents).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: "不应出现" }),
        expect.objectContaining({ text: "不应抢答" }),
      ]),
    );
    expect(transcriptEvents).toContainEqual(
      expect.objectContaining({
        speaker: "assistant",
        text: "现在回答 B。",
        generation_id: 2,
      }),
    );
  });
});
