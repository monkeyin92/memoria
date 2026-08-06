const WORKLET_PROCESSOR_NAME = "memoria-rendered-sample-counter-v1";

function defaultWorkletUrl() {
  const baseUrl = import.meta.env?.BASE_URL || "/";
  return `${baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`}worklets/rendered-sample-counter.js`;
}

function supportedAudioContext(AudioContextImpl) {
  return typeof AudioContextImpl === "function";
}

function snapshotFromMessage(message, sampleRate) {
  if (
    !message ||
    typeof message !== "object" ||
    !["ready", "rendered"].includes(message.type) ||
    !Number.isSafeInteger(message.rendered_frames) ||
    message.rendered_frames < 0 ||
    !Number.isFinite(sampleRate) ||
    sampleRate <= 0
  ) {
    return null;
  }
  return {
    renderedFrames: message.rendered_frames,
    sampleRate,
  };
}

/**
 * Counts samples that the Web Audio rendering graph has pulled from a remote
 * media element. It intentionally does not infer physical DAC completion:
 * browsers do not expose that watermark. Callers must retain that distinction
 * in diagnostics and product claims.
 */
export class AudioWorkletPlaybackTracker {
  constructor({
    AudioContextImpl = globalThis.AudioContext || globalThis.webkitAudioContext,
    AudioWorkletNodeImpl = globalThis.AudioWorkletNode,
    workletUrl = defaultWorkletUrl(),
    onDiagnostic = () => undefined,
    onRendered = () => undefined,
  } = {}) {
    this.AudioContextImpl = AudioContextImpl;
    this.AudioWorkletNodeImpl = AudioWorkletNodeImpl;
    this.workletUrl = workletUrl;
    this.onDiagnostic = onDiagnostic;
    this.onRendered = onRendered;
    this.context = null;
    this.source = null;
    this.node = null;
    this.element = null;
    this.snapshot = null;
    this._attachPromise = null;
    this._generation = 0;
  }

  setOnDiagnostic(onDiagnostic = () => undefined) {
    this.onDiagnostic = onDiagnostic;
  }

  setOnRendered(onRendered = () => undefined) {
    this.onRendered = onRendered;
  }

  getSnapshot() {
    if (this.context?.state !== "running") return null;
    return this.snapshot ? { ...this.snapshot } : null;
  }

  async attach(element) {
    if (!element || typeof element !== "object") return false;
    if (this.element === element && this._attachPromise) {
      return this._attachPromise;
    }
    if (this.element === element && this.node) return true;
    if (this.element && this.element !== element) await this.close();

    const generation = ++this._generation;
    const attach = this._attach(element, generation);
    this._attachPromise = attach;
    try {
      return await attach;
    } finally {
      if (this._generation === generation) this._attachPromise = null;
    }
  }

  async _attach(element, generation) {
    if (
      !supportedAudioContext(this.AudioContextImpl) ||
      typeof this.AudioWorkletNodeImpl !== "function"
    ) {
      this.onDiagnostic("audio_worklet_playback", "unsupported");
      return false;
    }

    let context = null;
    let source = null;
    let node = null;
    try {
      context = new this.AudioContextImpl({ latencyHint: "interactive" });
      if (
        !context?.audioWorklet?.addModule ||
        typeof context.createMediaElementSource !== "function" ||
        !context.destination
      ) {
        throw new Error("AudioWorklet API unavailable");
      }
      // A suspended graph would reroute the media element into silence after
      // ``createMediaElementSource``. Resume before claiming the element so a
      // rejected autoplay policy leaves the native element untouched.
      if (context.state !== "running") {
        if (typeof context.resume !== "function") {
          throw new Error("AudioContext is not running");
        }
        await context.resume();
      }
      if (context.state !== "running") {
        throw new Error("AudioContext is not running");
      }
      await context.audioWorklet.addModule(this.workletUrl);
      if (generation !== this._generation) {
        await context.close?.();
        return false;
      }
      node = new this.AudioWorkletNodeImpl(context, WORKLET_PROCESSOR_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [2],
        processorOptions: { notifyEveryFrames: 960 },
      });
      source = context.createMediaElementSource(element);
      source.connect(node);
      node.connect(context.destination);
      node.port.onmessage = ({ data }) => {
        if (generation !== this._generation || this.node !== node) return;
        const snapshot = snapshotFromMessage(data, context.sampleRate);
        if (!snapshot) return;
        this.snapshot = snapshot;
        if (context.state === "running") this.onRendered({ ...snapshot });
      };
      this.context = context;
      this.source = source;
      this.node = node;
      this.element = element;
      this.snapshot = null;
      this.onDiagnostic("audio_worklet_playback", "ready", {
        sample_rate: context.sampleRate,
      });
      return true;
    } catch (error) {
      node?.disconnect?.();
      source?.disconnect?.();
      try {
        await context?.close?.();
      } catch {
        // Setup failure must preserve the normal media-element fallback.
      }
      if (generation === this._generation) {
        this.onDiagnostic("audio_worklet_playback", "fallback", {
          error: error?.name || "setup_failed",
        });
      }
      return false;
    }
  }

  async resume() {
    if (!this.context || this.context.state === "running") return true;
    if (typeof this.context.resume !== "function") return false;
    try {
      await this.context.resume();
      return this.context.state === "running";
    } catch {
      return false;
    }
  }

  async close() {
    this._generation += 1;
    this._attachPromise = null;
    const node = this.node;
    const source = this.source;
    const context = this.context;
    this.node = null;
    this.source = null;
    this.context = null;
    this.element = null;
    this.snapshot = null;
    if (node?.port) node.port.onmessage = null;
    node?.disconnect?.();
    source?.disconnect?.();
    try {
      await context?.close?.();
    } catch {
      // Closing a browser-owned graph is best-effort cleanup.
    }
  }
}

export const AUDIO_WORKLET_PLAYBACK_PROCESSOR = WORKLET_PROCESSOR_NAME;
