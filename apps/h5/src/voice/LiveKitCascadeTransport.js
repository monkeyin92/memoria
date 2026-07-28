import { Room, RoomEvent } from "livekit-client";

import { VoiceTransport } from "./VoiceTransport.js";

const roomOptions = {
  adaptiveStream: true,
  dynacast: true,
  audioCaptureDefaults: {
    autoGainControl: true,
    echoCancellation: true,
    noiseSuppression: true,
    channelCount: 1,
  },
};

export class LiveKitCascadeTransport extends VoiceTransport {
  constructor({
    room = new Room(roomOptions),
    getLocalDevices = () => Room.getLocalDevices("audioinput", true),
    stopResponse,
    onTrackSubscribed = () => undefined,
    onTrackUnsubscribed = () => undefined,
    onDataReceived = () => undefined,
    onTranscriptionReceived = () => undefined,
    onReconnecting = () => undefined,
    onReconnected = () => undefined,
    onDisconnected = () => undefined,
  } = {}) {
    super();
    this.room = room;
    this.getLocalDevices = getLocalDevices;
    this.stopResponse = stopResponse;
    this.session = null;
    this.closed = false;
    this.handlers = [
      [RoomEvent.TrackSubscribed, onTrackSubscribed],
      [RoomEvent.TrackUnsubscribed, onTrackUnsubscribed],
      [RoomEvent.DataReceived, onDataReceived],
      [RoomEvent.TranscriptionReceived, onTranscriptionReceived],
      [RoomEvent.Reconnecting, onReconnecting],
      [RoomEvent.Reconnected, onReconnected],
      [RoomEvent.Disconnected, onDisconnected],
    ];
    this.handlers.forEach(([event, handler]) => this.room.on(event, handler));
  }

  prepare({ unlockAudio = false } = {}) {
    if (!unlockAudio) return Promise.resolve();
    return this.room.startAudio();
  }

  async connect(
    session,
    {
      getMicrophoneEnabled = () => true,
      isCurrent = () => true,
    } = {},
  ) {
    if (
      !session?.session_id ||
      !session.livekit_url ||
      !session.participant_token
    ) {
      throw new Error("服务端没有返回可用的 LiveKit 会话");
    }
    const requestedMicrophoneState = Boolean(getMicrophoneEnabled());
    const devices = await this.getLocalDevices();
    if (!isCurrent()) return;
    if (!devices.length) throw new Error("没有找到可用的麦克风");
    this.session = session;
    await this.room.connect(session.livekit_url, session.participant_token);
    if (!isCurrent()) return;
    await this.room.localParticipant.setMicrophoneEnabled(
      requestedMicrophoneState,
    );
    if (!isCurrent()) return;
    const latestMicrophoneState = Boolean(getMicrophoneEnabled());
    if (latestMicrophoneState !== requestedMicrophoneState) {
      await this.room.localParticipant.setMicrophoneEnabled(
        latestMicrophoneState,
      );
    }
  }

  setMicrophoneEnabled(enabled) {
    return this.room.localParticipant.setMicrophoneEnabled(Boolean(enabled));
  }

  resumeAudio() {
    return this.room.startAudio();
  }

  stopAssistant() {
    if (!this.session?.session_id || typeof this.stopResponse !== "function") {
      return Promise.resolve();
    }
    return this.stopResponse(this.session.session_id);
  }

  publishData(payload, options) {
    return this.room.localParticipant.publishData(payload, options);
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    this.handlers.forEach(([event, handler]) => this.room.off(event, handler));
    this.handlers = [];
    await this.room.disconnect();
  }
}
