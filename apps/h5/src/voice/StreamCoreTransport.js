import { VoiceTransport } from "./VoiceTransport.js";

const EVENTS_LABEL = "memoria.events.v1";
const DEFAULT_CONNECT_TIMEOUT_MS = 10_000;

function randomEventId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `evt-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function asSdpResponse(response) {
  if (typeof response === "string") return response;
  if (!response?.ok) {
    throw new Error(`媒体协商失败（${response?.status || "unknown"}）`);
  }
  return response.text();
}

function waitForIceGathering(pc, timeoutMs) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    let timer = null;
    const done = () => {
      if (timer !== null) globalThis.clearTimeout(timer);
      pc.removeEventListener?.("icegatheringstatechange", onChange);
      resolve();
    };
    const onChange = () => {
      if (pc.iceGatheringState === "complete") done();
    };
    pc.addEventListener?.("icegatheringstatechange", onChange);
    timer = globalThis.setTimeout(done, timeoutMs);
  });
}

function waitForPeerConnected(pc, timeoutMs) {
  const state = pc.connectionState;
  if (state === "connected") return Promise.resolve();
  if (state === "failed" || state === "closed") {
    return Promise.reject(new Error(`WebRTC 连接失败（${state}）`));
  }
  return new Promise((resolve, reject) => {
    let timer = null;
    let settled = false;
    const finish = (error = null) => {
      if (settled) return;
      settled = true;
      if (timer !== null) globalThis.clearTimeout(timer);
      pc.removeEventListener?.("connectionstatechange", onChange);
      if (error) reject(error);
      else resolve();
    };
    const onChange = () => {
      const next = pc.connectionState;
      if (next === "connected") finish();
      else if (next === "failed" || next === "closed") {
        finish(new Error(`WebRTC 连接失败（${next}）`));
      }
    };
    pc.addEventListener?.("connectionstatechange", onChange);
    timer = globalThis.setTimeout(
      () => finish(new Error("WebRTC 连接超时")),
      timeoutMs,
    );
    // A mocked or non-event-target implementation may already have changed
    // state between the first read and listener installation.
    onChange();
  });
}

function runtimeConfig(session) {
  const nested = session?.streamcore || session?.media || {};
  return {
    whipUrl: nested.whip_url || session?.whip_url,
    token: nested.token || session?.token,
    streamEpoch:
      Number.isInteger(nested.stream_epoch) && nested.stream_epoch > 0
        ? nested.stream_epoch
        : Number.isInteger(session?.stream_epoch) && session.stream_epoch > 0
          ? session.stream_epoch
          : 1,
  };
}

/**
 * Browser-native WHIP transport for the Memoria media contract.
 *
 * It intentionally contains no StreamCore/third-party runtime code: the
 * server owns ICE, RTP and the Voice Core bridge, while this adapter only
 * performs browser WebRTC signalling and validates the event fence.
 */
export class StreamCoreTransport extends VoiceTransport {
  constructor({
    RTCPeerConnectionImpl = globalThis.RTCPeerConnection,
    getUserMedia = (...args) => globalThis.navigator?.mediaDevices?.getUserMedia(...args),
    fetchImpl = (...args) => globalThis.fetch(...args),
    exchangeSdp,
    stopResponse,
    reconnectSession,
    heartbeatSession,
    onMicrophoneTrack = () => undefined,
    onState = () => undefined,
    onTranscript = () => undefined,
    onRemoteStream = () => undefined,
    onDataReceived = () => undefined,
    onPlaybackFlush = () => undefined,
    onDiagnostic = () => undefined,
    onError = () => undefined,
    onDisconnected = () => undefined,
    onLocalDuck = () => undefined,
    dataChannelLabel = EVENTS_LABEL,
    connectTimeoutMs = DEFAULT_CONNECT_TIMEOUT_MS,
    heartbeatIntervalMs = 60_000,
  } = {}) {
    super();
    this.mediaRuntime = "streamcore";
    this.RTCPeerConnectionImpl = RTCPeerConnectionImpl;
    this.getUserMedia = getUserMedia;
    this.fetchImpl = fetchImpl;
    this.exchangeSdp = exchangeSdp;
    this.stopResponse = stopResponse;
    this.reconnectSession = reconnectSession;
    this.heartbeatSession = heartbeatSession;
    this.onMicrophoneTrack = onMicrophoneTrack;
    this.onState = onState;
    this.onTranscript = onTranscript;
    this.onRemoteStream = onRemoteStream;
    this.onDataReceived = onDataReceived;
    this.onPlaybackFlush = onPlaybackFlush;
    this.onDiagnostic = onDiagnostic;
    this.onError = onError;
    this.onDisconnected = onDisconnected;
    this.onLocalDuck = onLocalDuck;
    this.dataChannelLabel = dataChannelLabel;
    this.connectTimeoutMs = connectTimeoutMs;
    this.heartbeatIntervalMs = heartbeatIntervalMs;
    this.pc = null;
    this.channel = null;
    this.mediaStream = null;
    this.session = null;
    this.streamEpoch = 0;
    this.lastEventSequence = -1;
    this.nextClientSequence = 0;
    this.closed = false;
    this._connectionHandler = null;
    this._trackHandler = null;
    this._dataChannelHandler = null;
    this._connectOptions = null;
    this._heartbeatTimer = null;
    this._heartbeatInFlight = false;
    this._connectHandshakeComplete = false;
    this._disconnectNotified = false;
  }

  async connect(
    session,
    { getMicrophoneEnabled = () => true, isCurrent = () => true } = {},
  ) {
    const config = runtimeConfig(session);
    if (!session?.session_id || !config.whipUrl || !config.token) {
      throw new Error("服务端没有返回可用的 Media Runtime 会话");
    }
    if (typeof this.RTCPeerConnectionImpl !== "function") {
      throw new Error("当前浏览器不支持 WebRTC");
    }
    await this.close();
    this.closed = false;
    this.session = session;
    this._connectOptions = { getMicrophoneEnabled, isCurrent };
    this.streamEpoch = config.streamEpoch;
    this.lastEventSequence = -1;
    this.nextClientSequence = 0;
    this._connectHandshakeComplete = false;
    this._disconnectNotified = false;
    this.onState("connecting");

    const pc = new this.RTCPeerConnectionImpl({
      iceServers: session.ice_servers || session.iceServers || [],
    });
    this.pc = pc;
    this._installPeerHandlers(pc, isCurrent);
    const microphoneEnabled = Boolean(getMicrophoneEnabled());
    if (microphoneEnabled) {
      this.mediaStream = await this.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
      if (!isCurrent()) return;
      for (const track of this.mediaStream?.getAudioTracks?.() || []) {
        pc.addTrack(track, this.mediaStream);
      }
      this.onMicrophoneTrack(this.mediaStream?.getAudioTracks?.()[0] || null);
    }
    const channel = pc.createDataChannel(this.dataChannelLabel);
    this.channel = channel;
    this._installDataChannel(channel, isCurrent);
    const offer = await pc.createOffer();
    if (!isCurrent()) return;
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc, Math.min(this.connectTimeoutMs, 3_000));
    const localSdp = pc.localDescription?.sdp || offer.sdp;
    const answerSdp = await this._exchangeSdp({
      session,
      whipUrl: config.whipUrl,
      token: config.token,
      offerSdp: localSdp,
    });
    if (!isCurrent()) return;
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    await waitForPeerConnected(pc, this.connectTimeoutMs);
    if (!isCurrent() || this.pc !== pc) return;
    this._connectHandshakeComplete = true;
    this._startHeartbeat();
    this.onState("ready");
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    if (
      typeof this.heartbeatSession !== "function" ||
      !Number.isFinite(this.heartbeatIntervalMs) ||
      this.heartbeatIntervalMs <= 0
    ) {
      return;
    }
    this._heartbeatTimer = globalThis.setInterval(() => {
      void this._renewHeartbeat();
    }, this.heartbeatIntervalMs);
    this._heartbeatTimer?.unref?.();
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer !== null) {
      globalThis.clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
    this._heartbeatInFlight = false;
  }

  async _renewHeartbeat() {
    if (
      this.closed ||
      this._heartbeatInFlight ||
      typeof this.heartbeatSession !== "function" ||
      !this.session?.session_id
    ) {
      return;
    }
    this._heartbeatInFlight = true;
    try {
      const response = await this.heartbeatSession(
        this.session.session_id,
        this.streamEpoch,
      );
      const epoch = Number(response?.stream_epoch);
      if (!Number.isInteger(epoch) || epoch !== this.streamEpoch) {
        throw new Error("媒体 heartbeat 返回了错误的 stream epoch");
      }
      this.session = {
        ...this.session,
        stream_epoch: epoch,
        ...(this.session.streamcore
          ? {
              streamcore: {
                ...this.session.streamcore,
                stream_epoch: epoch,
                ...(response?.expires_at
                  ? { expires_at: response.expires_at }
                  : {}),
              },
            }
          : {}),
      };
      this.onDiagnostic("media_heartbeat", "ok");
    } catch (error) {
      this.onDiagnostic("media_heartbeat", "error", {
        error: error?.message || "heartbeat_failed",
      });
      this._stopHeartbeat();
      if (!this.closed && !this._disconnectNotified) {
        this._disconnectNotified = true;
        this.onDisconnected("media_heartbeat_failed");
      }
    } finally {
      this._heartbeatInFlight = false;
    }
  }

  async _exchangeSdp({ session, whipUrl, token, offerSdp }) {
    if (typeof this.exchangeSdp === "function") {
      return asSdpResponse(
        await this.exchangeSdp(session.session_id, offerSdp, {
          token,
          whipUrl,
        }),
      );
    }
    const response = await this.fetchImpl(whipUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/sdp",
        Accept: "application/sdp",
      },
      body: offerSdp,
    });
    return asSdpResponse(response);
  }

  _installPeerHandlers(pc, isCurrent) {
    this._connectionHandler = () => {
      if (!isCurrent() || this.pc !== pc) return;
      const state = pc.connectionState;
      if (state === "connected") {
        if (this._connectHandshakeComplete) this.onState("ready");
        this._disconnectNotified = false;
      }
      if (state === "connecting") this.onState("connecting");
      if (state === "disconnected" || state === "failed") {
        this._connectHandshakeComplete = false;
        this.onState("reconnecting");
        if (!this._disconnectNotified) {
          this._disconnectNotified = true;
          this.onDisconnected(state);
        }
      }
    };
    this._trackHandler = (event) => {
      if (!isCurrent() || this.pc !== pc) return;
      const stream = event?.streams?.[0];
      if (stream) this.onRemoteStream(stream);
    };
    pc.addEventListener?.("connectionstatechange", this._connectionHandler);
    pc.addEventListener?.("track", this._trackHandler);
    pc.onconnectionstatechange = this._connectionHandler;
    pc.ontrack = this._trackHandler;
  }

  _installDataChannel(channel, isCurrent) {
    channel.onopen = () => {
      if (isCurrent()) this.onDiagnostic("media_events_open", "ok");
    };
    channel.onclose = () => {
      if (isCurrent()) this.onDiagnostic("media_events_closed", "ok");
    };
    channel.onerror = () => {
      if (isCurrent()) this.onError("媒体事件通道不可用");
    };
    channel.onmessage = (event) => {
      if (!isCurrent()) return;
      this._handleEvent(event?.data);
    };
  }

  _handleEvent(raw) {
    let event;
    try {
      event = typeof raw === "string" ? JSON.parse(raw) : raw;
    } catch {
      return;
    }
    if (!event || typeof event !== "object" || typeof event.type !== "string") return;
    if (event.v !== undefined && event.v !== 1) return;
    if (
      event.session_id !== undefined &&
      event.session_id !== this.session?.session_id
    ) {
      return;
    }
    if (event.stream_epoch !== undefined) {
      if (!Number.isInteger(event.stream_epoch) || event.stream_epoch !== this.streamEpoch) {
        return;
      }
    }
    if (
      event.payload &&
      typeof event.payload === "object" &&
      event.payload.session_id !== undefined &&
      event.payload.session_id !== this.session?.session_id
    ) {
      return;
    }
    if (Number.isInteger(event.sequence)) {
      if (event.sequence <= this.lastEventSequence) return;
      this.lastEventSequence = event.sequence;
    }
    this.onDataReceived(event);
    const payload = event.payload && typeof event.payload === "object" ? event.payload : event;
    if (event.type === "assistant.state" || event.type === "assistant_state") {
      if (typeof payload.state === "string") this.onState(payload.state, event);
    } else if (
      event.type === "user.transcript.partial" ||
      event.type === "user.transcript.final" ||
      event.type === "assistant.text.delta" ||
      event.type === "assistant.text.final" ||
      event.type === "transcript_delta"
    ) {
      this.onTranscript({
        ...payload,
        session_id: payload.session_id || event.session_id,
        turn_id: payload.turn_id ?? event.turn_id,
        generation_id: payload.generation_id ?? event.generation_id,
        speaker: payload.speaker || (event.type.startsWith("user.") ? "user" : "assistant"),
        final:
          event.type === "user.transcript.final" ||
          event.type === "assistant.text.final" ||
          payload.final === true,
      });
    } else if (event.type === "playback.duck") {
      this.onLocalDuck(true, payload);
    } else if (event.type === "playback.restore") {
      this.onLocalDuck(false, payload);
    } else if (event.type === "playback.flush") {
      this.onPlaybackFlush(payload, event);
    } else if (event.type === "session.reconnecting") {
      this.onState("reconnecting", event);
    } else if (event.type === "session.ready") {
      this.onState("ready", event);
    } else if (event.type === "session.closed") {
      this.onState("closed", event);
    } else if (event.type === "assistant.audio.started") {
      this.onState("speaking", event);
    } else if (event.type === "assistant.audio.stopped") {
      this.onState("listening", event);
    } else if (event.type === "error") {
      const message = payload.message || payload.code || "媒体服务错误";
      this.onError(String(message));
    } else if (event.type === "ping") {
      void this.publishData({
        type: "pong",
        payload: { ping_sequence: event.sequence },
      }).catch(() => undefined);
    }
  }

  async prepare() {
    return undefined;
  }

  async setMicrophoneEnabled(enabled) {
    const active = Boolean(enabled);
    if (!this.mediaStream && active) {
      this.mediaStream = await this.getUserMedia({ audio: true, video: false });
      for (const track of this.mediaStream?.getAudioTracks?.() || []) {
        this.pc?.addTrack(track, this.mediaStream);
      }
    }
    for (const track of this.mediaStream?.getAudioTracks?.() || []) {
      track.enabled = active;
    }
    this.onMicrophoneTrack(active ? this.mediaStream?.getAudioTracks?.()[0] || null : null);
    return this.mediaStream;
  }

  async reconnect() {
    if (!this.session) throw new Error("媒体会话尚未连接");
    const current = this.session;
    let nextSession;
    if (typeof this.reconnectSession === "function") {
      nextSession = await this.reconnectSession(
        current.session_id,
        current,
        this.streamEpoch,
      );
    } else {
      const nextEpoch = Math.max(this.streamEpoch + 1, 1);
      const nested = current.streamcore || current.media || {};
      nextSession = {
        ...current,
        stream_epoch: nextEpoch,
        streamcore: { ...nested, stream_epoch: nextEpoch },
      };
    }
    const nextEpoch = Number(nextSession?.stream_epoch);
    if (!Number.isInteger(nextEpoch) || nextEpoch <= this.streamEpoch) {
      throw new Error("服务端没有推进媒体 stream epoch");
    }
    this.onState("reconnecting");
    await this.connect(nextSession, this._connectOptions || {});
    return nextEpoch;
  }

  resumeAudio() {
    const elements = globalThis.document?.querySelectorAll?.("audio") || [];
    return Promise.all(
      [...elements].map((element) =>
        element.muted ? Promise.resolve() : element.play(),
      ),
    );
  }

  stopAssistant() {
    this.onLocalDuck(true, { reason: "user_stop" });
    const idempotencyKey = randomEventId();
    const stopEvent = this.publishData({
      type: "client.stop_assistant",
      event_id: idempotencyKey,
      payload: { reason: "user_button", idempotency_key: idempotencyKey },
    });
    if (!this.session?.session_id || typeof this.stopResponse !== "function") {
      return stopEvent.catch(() => undefined);
    }
    // DataChannel is the low-latency path. HTTP is a true fallback when the
    // channel is unavailable or send fails; using both as independent cancel
    // commands would bump generation twice.
    return stopEvent.catch(() =>
      this.stopResponse(this.session.session_id, idempotencyKey),
    );
  }

  publishData(payload) {
    if (!this.channel || this.channel.readyState !== "open") {
      return Promise.reject(new Error("媒体事件通道尚未连接"));
    }
    const body =
      payload && typeof payload === "object" && typeof payload.type === "string"
        ? payload
        : { type: "client.event", payload };
    const explicitSequence = Number.isInteger(body.sequence) ? body.sequence : null;
    if (explicitSequence !== null && explicitSequence >= this.nextClientSequence) {
      this.nextClientSequence = explicitSequence + 1;
    }
    const envelope = {
      v: 1,
      type: body.type,
      event_id: body.event_id || randomEventId(),
      session_id: this.session?.session_id,
      stream_epoch: this.streamEpoch,
      sequence: explicitSequence === null ? this.nextClientSequence++ : explicitSequence,
      turn_id: body.turn_id,
      generation_id: body.generation_id,
      server_monotonic_ms: body.server_monotonic_ms,
      payload: body.payload || {},
    };
    for (const [key, value] of Object.entries(envelope)) {
      if (value === undefined) delete envelope[key];
    }
    this.channel.send(JSON.stringify(envelope));
    return Promise.resolve();
  }

  sendText(text) {
    const normalized = typeof text === "string" ? text.trim() : "";
    if (!normalized || normalized.length > 500) {
      return Promise.reject(new Error("文字消息需要在 1 到 500 个字符之间"));
    }
    return this.publishData({
      type: "client.text",
      payload: { text: normalized },
    });
  }

  getStats() {
    return this.pc?.getStats?.() || Promise.resolve(new Map());
  }

  async close() {
    if (this.closed && !this.pc && !this.mediaStream) return;
    this.closed = true;
    this._stopHeartbeat();
    for (const track of this.mediaStream?.getTracks?.() || []) track.stop();
    this.onMicrophoneTrack(null);
    this.mediaStream = null;
    this.channel?.close?.();
    if (this.pc) {
      if (this._connectionHandler) {
        this.pc.removeEventListener?.("connectionstatechange", this._connectionHandler);
      }
      if (this._trackHandler) this.pc.removeEventListener?.("track", this._trackHandler);
      this.pc.close?.();
    }
    this.channel = null;
    this.pc = null;
    this.session = null;
  }
}

export const STREAMCORE_EVENTS_LABEL = EVENTS_LABEL;
