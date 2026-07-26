const { FRAME_TYPE, decodePcmFrame, encodePcmFrame } = require("./media-protocol");
const { PcmJitterPlayer } = require("./pcm-player");

const RECORDER_START_TIMEOUT_MS = 2000;
const FIRST_UPLINK_FRAME_TIMEOUT_MS = 3000;
const RECORDER_RESTART_DELAY_MS = 120;

function socketConnectionErrorMessage(error) {
  const detail = typeof error?.errMsg === "string" ? error.errMsg.trim() : "";
  if (/domain list|合法域名/i.test(detail)) {
    return "小程序 Socket 合法域名未生效，请检查 wss 域名配置。";
  }
  if (/certificate|ssl|tls|handshake/i.test(detail)) {
    return "语音网络证书校验失败，请检查 Socket 域名证书。";
  }
  if (/timeout|timed out/i.test(detail)) {
    return "语音网络连接超时，请检查网络后重试。";
  }
  return detail ? `语音网络连接失败：${detail.slice(0, 120)}` : "语音网络连接失败。";
}

class MiniProgramMediaSession {
  constructor(session, callbacks = {}) {
    this.session = session;
    this.callbacks = callbacks;
    this.socket = null;
    this.recorder = wx.getRecorderManager();
    this.player = new PcmJitterPlayer({
      sampleRate: session.media_gateway?.audio?.sample_rate || 24000,
    });
    this.sequence = 0;
    this.ready = false;
    this.recording = false;
    this.recorderStarted = false;
    this.recorderStarting = false;
    this.intentionalClose = false;
    this._firstUplinkFrame = false;
    this._recoveryAttempted = false;
    this._uplinkFailed = false;
    this._startTimer = null;
    this._firstFrameTimer = null;
    this._restartTimer = null;
    this._readyResolve = null;
    this._readyReject = null;
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
      timeout: 10000,
    });
    this.socket.onOpen(() => {
      this.socket.send({
        data: JSON.stringify({
          type: "hello",
          protocol_version: 1,
          ticket: this.session.media_gateway.ticket,
        }),
      });
    });
    this.socket.onMessage((message) => this._onMessage(message));
    this.socket.onError((error) => {
      this._rejectReady(new Error(socketConnectionErrorMessage(error)));
    });
    this.socket.onClose(() => {
      this.player.reset();
      this._rejectReady(new Error("语音连接已关闭。"));
      if (!this.intentionalClose) this.callbacks.onClose?.();
    });
    await ready;
  }

  async setMicrophoneEnabled(enabled) {
    if (!this.ready) return;
    if (enabled && this.recorderStarted && !this.recording) {
      this._recoveryAttempted = false;
      this._firstUplinkFrame = false;
      this.recorder.resume();
      this.recording = true;
      this._armFirstFrameTimeout();
    } else if (enabled && !this.recording) {
      this._recoveryAttempted = false;
      this._startRecording();
    } else if (!enabled && this.recording) {
      this.recorder.pause();
      this.recording = false;
      this._clearRecorderTimers();
    }
  }

  async close() {
    this.intentionalClose = true;
    this._clearRecorderTimers();
    if (this.recorderStarted) {
      this.recorder.stop();
      this.recording = false;
      this.recorderStarted = false;
    }
    this.recorderStarting = false;
    this.socket?.close({ code: 1000 });
    this.socket = null;
    await this.player.close();
  }

  _bindRecorder() {
    this.recorder.onStart(() => {
      if (this.intentionalClose || !this.recorderStarting) return;
      this.recorderStarting = false;
      this.recorderStarted = true;
      this.recording = true;
      this._clearStartTimer();
      this._armFirstFrameTimeout();
    });
    this.recorder.onFrameRecorded((frame) => {
      if (!this.ready || !this.recording || !this.socket || this._uplinkFailed) return;
      try {
        const data = encodePcmFrame(
          FRAME_TYPE.UPLINK_AUDIO,
          this.sequence,
          Date.now(),
          frame.frameBuffer,
        );
        this.sequence = (this.sequence + 1) >>> 0;
        this._firstUplinkFrame = true;
        this._clearFirstFrameTimer();
        this.socket.send({
          data,
          fail: () => this._failUplink("麦克风音频发送失败，请轻触恢复语音。"),
        });
      } catch {
        this._failUplink("麦克风音频帧不可用，请轻触恢复语音。");
      }
    });
    this.recorder.onError(() => {
      this._failUplink("麦克风录音失败，请轻触恢复语音。");
    });
    this.recorder.onInterruptionBegin(() => {
      this._failUplink("录音被系统中断，请轻触恢复语音。");
    });
  }

  _onMessage(message) {
    if (typeof message.data === "string") {
      try {
        const event = JSON.parse(message.data);
        if (event.type === "ready") {
          this.ready = true;
          this._startRecording();
          const resolve = this._readyResolve;
          this._readyResolve = null;
          this._readyReject = null;
          resolve?.();
        }
        if (event.type === "audio_reset") {
          this.player.setGain(1);
          this.player.reset();
        }
        if (
          event.type === "ui_event" &&
          event.event?.type === "assistant_audio" &&
          (event.event.action === "duck" || event.event.action === "restore") &&
          typeof event.event.gain === "number"
        ) {
          this.player.setGain(event.event.action === "duck" ? 0 : event.event.gain);
        }
        this.callbacks.onEvent?.(event);
      } catch {
        this.callbacks.onError?.("语音服务返回了无效控制消息。");
      }
      return;
    }
    try {
      const frame = decodePcmFrame(message.data, FRAME_TYPE.DOWNLINK_AUDIO);
      this.player.enqueue(frame.payload);
    } catch {
      this.callbacks.onError?.("收到的语音播放帧无效。");
    }
  }

  _startRecording() {
    if (this.recording || this.recorderStarting || this._uplinkFailed) return;
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
    if (this.intentionalClose || this._uplinkFailed || !this.ready) return;
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
    this._clearRecorderTimers();
    this.recording = false;
    this.recorderStarted = false;
    this.recorderStarting = false;
    this.callbacks.onInterrupted?.(message);
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

  _rejectReady(error) {
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
