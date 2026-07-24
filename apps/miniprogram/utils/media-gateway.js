const { FRAME_TYPE, decodePcmFrame, encodePcmFrame } = require("./media-protocol");
const { PcmJitterPlayer } = require("./pcm-player");

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
    this.intentionalClose = false;
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
    this.socket.onError(() => this._rejectReady(new Error("语音网络连接失败。")));
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
      this.recorder.resume();
      this.recording = true;
    } else if (enabled && !this.recording) {
      this._startRecording();
    } else if (!enabled && this.recording) {
      this.recorder.pause();
      this.recording = false;
    }
  }

  async close() {
    this.intentionalClose = true;
    if (this.recorderStarted) {
      this.recorder.stop();
      this.recording = false;
      this.recorderStarted = false;
    }
    this.socket?.close({ code: 1000 });
    this.socket = null;
    await this.player.close();
  }

  _bindRecorder() {
    this.recorder.onFrameRecorded((frame) => {
      if (!this.ready || !this.recording || !this.socket) return;
      try {
        this.socket.send({
          data: encodePcmFrame(
            FRAME_TYPE.UPLINK_AUDIO,
            this.sequence,
            Date.now(),
            frame.frameBuffer,
          ),
        });
        this.sequence = (this.sequence + 1) >>> 0;
      } catch {
        this.callbacks.onError?.("麦克风音频帧不可用。");
      }
    });
    this.recorder.onError(() => {
      this.recording = false;
      this.recorderStarted = false;
      this.callbacks.onInterrupted?.("麦克风录音失败，请轻触恢复语音。");
    });
    this.recorder.onInterruptionBegin(() => {
      this.recording = false;
      this.recorderStarted = false;
      this.callbacks.onInterrupted?.("录音被系统中断，请轻触恢复语音。");
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
        if (event.type === "audio_reset") this.player.reset();
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
    if (this.recording) return;
    let platform = "";
    try {
      platform = typeof wx.getDeviceInfo === "function" ? wx.getDeviceInfo().platform : "";
    } catch {
      platform = "";
    }
    this.recorder.start({
      duration: 600000,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 24000,
      format: "PCM",
      frameSize: 1,
      audioSource: platform === "android" ? "voice_communication" : "auto",
    });
    this.recording = true;
    this.recorderStarted = true;
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
