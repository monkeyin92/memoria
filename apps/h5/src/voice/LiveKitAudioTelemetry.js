import {
  addInboundAudioDeltas,
  addOutboundAudioDeltas,
  extractInboundAudioStats,
  extractMicrophoneSettings,
  extractOutboundAudioStats,
} from "./webrtcStats.js";

const SAMPLE_INTERVAL_MS = 5_000;

export class LiveKitAudioTelemetry {
  constructor({ onDiagnostic, sampleIntervalMs = SAMPLE_INTERVAL_MS }) {
    this.onDiagnostic = onDiagnostic;
    this.sampleIntervalMs = sampleIntervalMs;
    this.inbound = null;
    this.microphone = null;
  }

  observeInboundTrack(track, isCurrent = () => true) {
    this.stopInbound();
    this.inbound = this.#sampleTrack({
      track,
      isCurrent,
      name: "webrtc_inbound_audio",
      extract: extractInboundAudioStats,
      addDeltas: addInboundAudioDeltas,
    });
  }

  observeMicrophoneTrack(track, isCurrent = () => true) {
    this.stopMicrophone();
    if (!track || !isCurrent()) return;
    const settings = extractMicrophoneSettings(
      track.getSourceTrackSettings?.() ||
        track.mediaStreamTrack?.getSettings?.(),
    );
    if (settings) {
      this.onDiagnostic("webrtc_microphone_settings", "ok", settings);
    }
    this.microphone = this.#sampleTrack({
      track,
      isCurrent,
      name: "webrtc_outbound_audio",
      extract: extractOutboundAudioStats,
      addDeltas: addOutboundAudioDeltas,
    });
  }

  #sampleTrack({ track, isCurrent, name, extract, addDeltas }) {
    if (typeof track?.getRTCStatsReport !== "function") return null;
    const state = { track, timer: null, baseline: null };
    const sample = async () => {
      if (!isCurrent() || state.track !== track) return;
      try {
        const metrics = extract(await track.getRTCStatsReport());
        if (!isCurrent() || state.track !== track || !metrics) return;
        state.baseline ||= metrics;
        this.onDiagnostic(name, "ok", addDeltas(metrics, state.baseline));
      } catch {
        // Diagnostics must never disturb capture or playback.
      }
    };
    void sample();
    state.timer = globalThis.setInterval(
      () => void sample(),
      this.sampleIntervalMs,
    );
    return state;
  }

  stopInbound() {
    this.#stop(this.inbound);
    this.inbound = null;
  }

  stopInboundTrack(track) {
    if (this.inbound?.track === track) this.stopInbound();
  }

  stopMicrophone() {
    this.#stop(this.microphone);
    this.microphone = null;
  }

  #stop(state) {
    if (!state) return;
    state.track = null;
    if (state.timer !== null) globalThis.clearInterval(state.timer);
  }

  stop() {
    this.stopInbound();
    this.stopMicrophone();
  }
}
