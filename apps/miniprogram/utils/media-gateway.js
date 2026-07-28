const { FRAME_TYPE, decodePcmFrame, encodePcmFrame } = require("./media-protocol");
const { PcmJitterPlayer } = require("./pcm-player");

const RECORDER_START_TIMEOUT_MS = 2000;
const FIRST_UPLINK_FRAME_TIMEOUT_MS = 3000;
const RECORDER_RESTART_DELAY_MS = 120;
// WeChat can emit a generic onError just before a close that carries the useful
// gateway code, so let the close callback settle the initial connection first.
const SOCKET_CLOSE_PRIORITY_DELAY_MS = 100;
const GATEWAY_HEADER_HANDSHAKE = Object.freeze({
  protocol: "X-Memoria-Gateway-Protocol",
  ticket: "X-Memoria-Gateway-Ticket",
  downlinkGeneration: "X-Memoria-Downlink-Generation",
});
const ASSISTANT_INPUT_BLOCKING_STATES = new Set([
  "thinking",
  "thinking_silent",
  "tool_waiting",
  "speaking",
  "interruption_pending",
  "recovering",
]);
const ASSISTANT_INPUT_RELEASE_STATES = new Set([
  "ready",
  "listening",
  "user_speaking",
  "eot_pending",
  "interrupted",
]);

function socketConnectionErrorKind(detail) {
  if (/domain list|合法域名/i.test(detail)) {
    return "domain";
  }
  if (/certificate|ssl|tls|handshake/i.test(detail)) {
    return "certificate";
  }
  if (/timeout|timed out/i.test(detail)) {
    return "timeout";
  }
  if (/connection refused/i.test(detail)) {
    return "connection_refused";
  }
  return "unknown";
}

function socketConnectionErrorMessage(error) {
  const detail = typeof error?.errMsg === "string" ? error.errMsg.trim() : "";
  const kind = socketConnectionErrorKind(detail);
  if (kind === "domain") {
    return "小程序 Socket 合法域名未生效，请检查 wss 域名配置。";
  }
  if (kind === "certificate") {
    return "语音网络证书校验失败，请检查 Socket 域名证书。";
  }
  if (kind === "timeout") {
    return "语音网络连接超时，请检查网络后重试。";
  }
  if (kind === "connection_refused") {
    return "语音网络连接被当前网络拒绝（connection refused），请关闭 VPN/代理后重试，或切换 Wi‑Fi/移动网络。";
  }
  return detail ? `语音网络连接失败：${detail.slice(0, 120)}` : "语音网络连接失败。";
}

function socketConnectionError(error) {
  const detail = typeof error?.errMsg === "string" ? error.errMsg.trim() : "";
  const connectionError = new Error(socketConnectionErrorMessage(error));
  if (socketConnectionErrorKind(detail) === "connection_refused") {
    connectionError.code = "socket_connection_refused";
  }
  return connectionError;
}

function socketCloseError(event) {
  const code = Number.isInteger(event?.code) ? event.code : null;
  const details = {
    4400: ["gateway_protocol_error", "语音网关协议不兼容，请更新后重试。"],
    4401: ["gateway_ticket_rejected", "语音网关凭据已失效，请重新开始语音。"],
    1011: ["gateway_unavailable", "语音服务暂时不可用，请稍后重试。"],
  }[code];
  if (!details) return null;
  const error = new Error(details[1]);
  error.code = details[0];
  return error;
}

function gatewayEventGenerationId(event) {
  const generationId =
    event?.type === "ui_event"
      ? event.event?.generation_id
      : event?.generation_id;
  return Number.isInteger(generationId) ? generationId : null;
}

class MiniProgramMediaSession {
  constructor(session, callbacks = {}) {
    this.session = session;
    this.callbacks = callbacks;
    this.socket = null;
    this.recorder = wx.getRecorderManager();
    this.player = new PcmJitterPlayer({
      sampleRate: session.media_gateway?.audio?.sample_rate || 24000,
      frameMilliseconds: session.media_gateway?.audio?.frame_ms || 20,
      onTrace: (trace) => this._sendClientAudioTrace(trace),
      onPlaybackStateChange: (active) => {
        this.playbackActive = active;
        this._syncRecordingState();
      },
    });
    this.sequence = 0;
    this.playbackGenerationId = null;
    this.ready = false;
    this.recording = false;
    this.recorderStarted = false;
    this.recorderStarting = false;
    this.microphoneEnabled = true;
    this.assistantResponseActive = false;
    this.playbackActive = false;
    this.systemInterrupted = false;
    this._uplinkDiscontinuityPending = false;
    this.intentionalClose = false;
    this._firstUplinkFrame = false;
    this._recoveryAttempted = false;
    this._uplinkFailed = false;
    this._startTimer = null;
    this._firstFrameTimer = null;
    this._restartTimer = null;
    this._readyResolve = null;
    this._readyReject = null;
    this._pendingSocketErrorTimer = null;
    this._pendingSocketError = null;
    this._closeNotified = false;
    this._bindRecorder();
  }

  async connect() {
    if (!this.session?.media_gateway?.websocket_url || !this.session?.media_gateway?.ticket) {
      throw new Error("缺少小程序媒体网关会话。");
    }
    await this.player.resume();
    const ready = new Promise((resolve, reject) => {
      this._readyResolve = resolve;
      this._readyReject = reject;
    });
    this.socket = wx.connectSocket({
      url: this.session.media_gateway.websocket_url,
      header: {
        [GATEWAY_HEADER_HANDSHAKE.protocol]: "1",
        [GATEWAY_HEADER_HANDSHAKE.ticket]: this.session.media_gateway.ticket,
        [GATEWAY_HEADER_HANDSHAKE.downlinkGeneration]: "2",
      },
      timeout: 10000,
    });
    this.socket.onOpen?.(() => {
      this._sendLegacyHello();
    });
    this.socket.onMessage((message) => this._onMessage(message));
    this.socket.onError((error) => {
      this._deferSocketError(error);
    });
    this.socket.onClose((event) => {
      this._handleSocketClose(event);
    });
    await ready;
  }

  async setMicrophoneEnabled(enabled) {
    this.microphoneEnabled = Boolean(enabled);
    this._syncRecordingState();
  }

  _syncRecordingState() {
    if (!this.ready || this.systemInterrupted) return;
    const captureEnabled = this._microphoneCaptureEnabled();
    if (captureEnabled && this._uplinkDiscontinuityPending) {
      this._resumeAfterSystemInterruption();
      return;
    }
    if (captureEnabled && this.recorderStarted && !this.recording) {
      this._recoveryAttempted = false;
      this._firstUplinkFrame = false;
      this.recorder.resume();
      this.recording = true;
      this._armFirstFrameTimeout();
    } else if (captureEnabled && !this.recording) {
      this._recoveryAttempted = false;
      this._startRecording();
    } else if (!captureEnabled && this.recorderStarting) {
      this._clearRecorderTimers();
      this.recorderStarting = false;
      try {
        this.recorder.stop();
      } catch {
        // The delayed onStart handler is fenced by recorderStarting.
      }
    } else if (!captureEnabled && this.recording) {
      this.recorder.pause();
      this.recording = false;
      this._clearRecorderTimers();
    }
  }

  _microphoneCaptureEnabled() {
    return (
      this.microphoneEnabled &&
      !this.assistantResponseActive &&
      !this.playbackActive
    );
  }

  async close() {
    this.intentionalClose = true;
    this._clearPendingSocketError();
    this.ready = false;
    this._uplinkFailed = true;
    this._stopRecorder();
    const socket = this.socket;
    this.socket = null;
    socket?.close({ code: 1000 });
    await this.player.close();
  }

  _bindRecorder() {
    const supportsInterruptionResume =
      typeof this.recorder.onInterruptionEnd === "function";
    this.recorder.onStart(() => {
      if (this.intentionalClose || this.systemInterrupted || !this.recorderStarting) return;
      if (!this._microphoneCaptureEnabled()) {
        this.recorderStarting = false;
        try {
          this.recorder.stop();
        } catch {
          // The disabled microphone intent wins even if onStart arrives late.
        }
        return;
      }
      this.recorderStarting = false;
      this.recorderStarted = true;
      this.recording = true;
      this._clearStartTimer();
      this._armFirstFrameTimeout();
    });
    this.recorder.onFrameRecorded((frame) => {
      if (
        !this.ready ||
        !this.recording ||
        !this._microphoneCaptureEnabled() ||
        !this.socket ||
        this._uplinkFailed
      ) {
        return;
      }
      const sequence = this.sequence;
      let data;
      try {
        data = encodePcmFrame(
          FRAME_TYPE.UPLINK_AUDIO,
          sequence,
          Date.now(),
          frame.frameBuffer,
        );
      } catch {
        this._failUplink("麦克风音频帧不可用，请轻触恢复语音。");
        return;
      }
      try {
        this.socket.send({
          data,
          fail: () => this._failUplink("麦克风音频发送失败，请轻触恢复语音。"),
        });
        this.sequence = (sequence + 1) >>> 0;
        this._firstUplinkFrame = true;
        this._clearFirstFrameTimer();
      } catch {
        this._failUplink("麦克风音频发送失败，请轻触恢复语音。");
      }
    });
    this.recorder.onError(() => {
      if (this.systemInterrupted) return;
      this._failUplink("麦克风录音失败，请轻触恢复语音。");
    });
    this.recorder.onInterruptionBegin(() => {
      if (!supportsInterruptionResume) {
        this._failUplink("录音被系统中断，请轻触恢复语音。");
        return;
      }
      if (this.intentionalClose || this._uplinkFailed || this.systemInterrupted) return;
      this.systemInterrupted = true;
      this._uplinkDiscontinuityPending = true;
      this._stopRecorder();
      this.callbacks.onEvent?.({ type: "recorder_state", state: "interrupted" });
    });
    if (supportsInterruptionResume) {
      this.recorder.onInterruptionEnd(() => {
        if (!this.systemInterrupted) return;
        this.systemInterrupted = false;
        this._resumeAfterSystemInterruption();
      });
    }
  }

  _onMessage(message) {
    if (typeof message.data === "string") {
      try {
        const event = JSON.parse(message.data);
        const eventGenerationId = gatewayEventGenerationId(event);
        if (
          this.playbackGenerationId !== null &&
          eventGenerationId !== null &&
          eventGenerationId < this.playbackGenerationId
        ) {
          return;
        }
        if (event.type === "handshake_ack") {
          if (
            event.protocol_version !== 1 ||
            !["header", "hello"].includes(event.transport)
          ) {
            this._rejectReady(new Error("语音握手响应无效，请重新开始语音。"));
            return;
          }
          this.callbacks.onEvent?.(event);
          return;
        }
        if (event.type === "ready") {
          if (!this._acceptReadyAudioContract(event)) return;
          this._clearPendingSocketError();
          this.ready = true;
          this._startRecording();
          const resolve = this._readyResolve;
          this._readyResolve = null;
          this._readyReject = null;
          resolve?.();
        }
        if (event.type === "audio_reset") {
          const barrierSequence = Number.isInteger(event.barrier_sequence)
            ? event.barrier_sequence
            : null;
          const generationId = Number.isInteger(event.generation_id)
            ? event.generation_id
            : null;
          const staleReset =
            this.playbackGenerationId !== null &&
            generationId !== null &&
            generationId < this.playbackGenerationId;
          if (!staleReset) {
            this.playbackGenerationId = generationId;
            this.player.setGain(1);
            this.player.reset(
              this.playbackGenerationId,
              barrierSequence,
            );
          }
          if (!staleReset && this.playbackGenerationId !== null && barrierSequence !== null) {
            this._sendTransportEvent({
              type: "playout_reset",
              generation_id: this.playbackGenerationId,
              barrier_sequence: barrierSequence,
              client_timestamp_ms: Date.now(),
            });
          }
        }
        if (
          event.type === "ui_event" &&
          event.event?.type === "assistant_state"
        ) {
          this._observeAssistantState(event.event.state);
        }
        if (
          event.type === "ui_event" &&
          event.event?.type === "assistant_audio" &&
          (event.event.action === "duck" || event.event.action === "restore") &&
          typeof event.event.gain === "number"
        ) {
          this.player.setGain(event.event.gain);
        }
        this.callbacks.onEvent?.(event);
      } catch {
        this.callbacks.onError?.("语音服务返回了无效控制消息。");
      }
      return;
    }
    try {
      const frame = decodePcmFrame(message.data, FRAME_TYPE.DOWNLINK_AUDIO);
      this.player.enqueue(frame.payload, {
        sequence: frame.sequence,
        generationId: Number.isInteger(frame.generationId)
          ? frame.generationId
          : this.playbackGenerationId,
      });
    } catch {
      this.callbacks.onError?.("收到的语音播放帧无效。");
    }
  }

  _sendTransportEvent(event) {
    if (!this.socket) return false;
    try {
      this.socket.send({
        data: JSON.stringify(event),
        fail:
          event.type === "uplink_discontinuity"
            ? () => this._failUplink("录音恢复同步失败，请轻触恢复语音。")
            : undefined,
      });
      return true;
    } catch {
      // Telemetry must never interrupt the media path.
      return false;
    }
  }

  _sendLegacyHello() {
    if (!this.socket || this.ready) return;
    try {
      this.socket.send({
        data: JSON.stringify({
          type: "hello",
          protocol_version: 1,
          ticket: this.session.media_gateway.ticket,
          capabilities: { downlink_generation: 2 },
        }),
        fail: (error) => this._deferSocketError(error),
      });
    } catch {
      this._deferSocketError({ errMsg: "sendSocketMessage:fail" });
    }
  }

  _observeAssistantState(state) {
    if (ASSISTANT_INPUT_BLOCKING_STATES.has(state)) {
      this.assistantResponseActive = true;
    } else if (ASSISTANT_INPUT_RELEASE_STATES.has(state)) {
      this.assistantResponseActive = false;
    } else {
      return;
    }
    this._syncRecordingState();
  }

  _sendClientAudioTrace(trace) {
    if (
      !this.ready ||
      !this.socket ||
      typeof trace?.name !== "string" ||
      !Number.isInteger(trace.generationId) ||
      trace.generationId < 0 ||
      !trace.detail ||
      typeof trace.detail !== "object"
    ) {
      return false;
    }
    const detail = {};
    for (const [key, value] of Object.entries(trace.detail)) {
      if (Number.isFinite(value) && value >= 0) detail[key] = Math.round(value);
    }
    if (!Object.keys(detail).length) return false;
    return this._sendTransportEvent({
      type: "client_audio_trace",
      name: trace.name,
      generation_id: trace.generationId,
      client_timestamp_ms: Date.now(),
      detail,
    });
  }

  _acceptReadyAudioContract(event) {
    const audio = event?.audio;
    const supported =
      event?.protocol_version === 1 &&
      audio?.sample_rate === this.player.sampleRate &&
      audio?.channels === 1 &&
      audio?.sample_format === "s16le" &&
      audio?.frame_ms === 20 &&
      [1, 2].includes(audio?.frame_protocol_version);
    if (supported) return true;
    this.ready = false;
    this._uplinkFailed = true;
    this._rejectReady(new Error("语音服务音频格式不兼容，请更新后重试。"));
    const socket = this.socket;
    this.socket = null;
    socket?.close({ code: 1002 });
    return false;
  }

  _startRecording() {
    if (
      !this._microphoneCaptureEnabled() ||
      this.systemInterrupted ||
      this.recording ||
      this.recorderStarting ||
      this._uplinkFailed
    ) {
      return;
    }
    let platform = "";
    try {
      platform = typeof wx.getDeviceInfo === "function" ? wx.getDeviceInfo().platform : "";
    } catch {
      platform = "";
    }
    this._firstUplinkFrame = false;
    this.recorderStarting = true;
    try {
      this.recorder.start({
        duration: 600000,
        sampleRate: 16000,
        numberOfChannels: 1,
        encodeBitRate: 24000,
        format: "PCM",
        frameSize: 1,
        audioSource: platform === "android" ? "voice_communication" : "auto",
      });
      this._clearStartTimer();
      if (this.recorderStarting) {
        this._startTimer = setTimeout(() => {
          if (this.recorderStarting) this._recoverRecorder();
        }, RECORDER_START_TIMEOUT_MS);
      }
    } catch {
      this._recoverRecorder();
    }
  }

  _armFirstFrameTimeout() {
    this._clearFirstFrameTimer();
    this._firstFrameTimer = setTimeout(() => {
      if (this.recording && !this._firstUplinkFrame) this._recoverRecorder();
    }, FIRST_UPLINK_FRAME_TIMEOUT_MS);
  }

  _recoverRecorder() {
    if (
      this.intentionalClose ||
      this._uplinkFailed ||
      !this.ready ||
      !this._microphoneCaptureEnabled() ||
      this.systemInterrupted
    ) {
      return;
    }
    this._clearRecorderTimers();
    this.recording = false;
    this.recorderStarted = false;
    this.recorderStarting = false;
    if (this._recoveryAttempted) {
      this._failUplink("未收到麦克风音频，请轻触恢复语音。");
      return;
    }
    this._recoveryAttempted = true;
    try {
      this.recorder.stop();
    } catch {
      // A failed stale-stop must not prevent the one controlled restart.
    }
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      if (!this.intentionalClose && this.ready && !this._uplinkFailed) this._startRecording();
    }, RECORDER_RESTART_DELAY_MS);
  }

  _failUplink(message) {
    if (this.intentionalClose || this._uplinkFailed) return;
    this._uplinkFailed = true;
    this._uplinkDiscontinuityPending = false;
    this._stopRecorder();
    this.callbacks.onInterrupted?.(message);
  }

  _resumeAfterSystemInterruption() {
    if (
      !this._uplinkDiscontinuityPending ||
      this.systemInterrupted ||
      !this._microphoneCaptureEnabled() ||
      this.intentionalClose ||
      this._uplinkFailed ||
      !this.ready ||
      !this.socket
    ) {
      return;
    }
    const sent = this._sendTransportEvent({
      type: "uplink_discontinuity",
      next_sequence: this.sequence,
      client_timestamp_ms: Date.now(),
    });
    if (!sent) {
      this._failUplink("录音恢复同步失败，请轻触恢复语音。");
      return;
    }
    this._uplinkDiscontinuityPending = false;
    this._recoveryAttempted = false;
    this.callbacks.onEvent?.({ type: "recorder_state", state: "resumed" });
    this._startRecording();
  }

  _handleSocketClose(event) {
    const pendingSocketError = this._takePendingSocketError();
    this.playbackGenerationId = null;
    this.ready = false;
    this.player.reset();
    this._uplinkFailed = true;
    this.systemInterrupted = false;
    this._uplinkDiscontinuityPending = false;
    this._stopRecorder();
    this.socket = null;
    this._rejectReady(
      socketCloseError(event) || pendingSocketError || new Error("语音连接已关闭。"),
    );
    if (!this.intentionalClose && !this._closeNotified) {
      this._closeNotified = true;
      this.callbacks.onClose?.();
    }
  }

  _stopRecorder() {
    const shouldStop =
      this.recording || this.recorderStarted || this.recorderStarting;
    this._clearRecorderTimers();
    this.recording = false;
    this.recorderStarted = false;
    this.recorderStarting = false;
    this._firstUplinkFrame = false;
    if (!shouldStop) return;
    try {
      this.recorder.stop();
    } catch {
      // RecorderManager may already have stopped during a platform interruption.
    }
  }

  _clearRecorderTimers() {
    this._clearStartTimer();
    this._clearFirstFrameTimer();
    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }
  }

  _clearStartTimer() {
    if (this._startTimer) {
      clearTimeout(this._startTimer);
      this._startTimer = null;
    }
  }

  _clearFirstFrameTimer() {
    if (this._firstFrameTimer) {
      clearTimeout(this._firstFrameTimer);
      this._firstFrameTimer = null;
    }
  }

  _deferSocketError(error) {
    if (!this._readyReject || this._pendingSocketError !== null) return;
    this._pendingSocketError = socketConnectionError(error);
    this._pendingSocketErrorTimer = setTimeout(() => {
      const pendingSocketError = this._pendingSocketError;
      this._pendingSocketErrorTimer = null;
      this._pendingSocketError = null;
      if (pendingSocketError) this._rejectReady(pendingSocketError);
    }, SOCKET_CLOSE_PRIORITY_DELAY_MS);
  }

  _clearPendingSocketError() {
    if (this._pendingSocketErrorTimer !== null) {
      clearTimeout(this._pendingSocketErrorTimer);
    }
    this._pendingSocketErrorTimer = null;
    this._pendingSocketError = null;
  }

  _takePendingSocketError() {
    const pendingSocketError = this._pendingSocketError;
    this._clearPendingSocketError();
    return pendingSocketError;
  }

  _rejectReady(error) {
    this._clearPendingSocketError();
    if (!this._readyReject) return;
    const reject = this._readyReject;
    this._readyReject = null;
    this._readyResolve = null;
    reject(error);
  }
}

module.exports = {
  MiniProgramMediaSession,
};
