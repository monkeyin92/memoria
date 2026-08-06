import { describe, expect, it, vi } from "vitest";

import {
  AudioWorkletPlaybackTracker,
  AUDIO_WORKLET_PLAYBACK_PROCESSOR,
} from "./AudioWorkletPlaybackTracker.js";

class FakeAudioContext {
  static instances = [];

  constructor() {
    this.state = "running";
    this.sampleRate = 48_000;
    this.destination = { id: "destination" };
    this.audioWorklet = { addModule: vi.fn(async () => undefined) };
    this.sources = [];
    this.closed = false;
    FakeAudioContext.instances.push(this);
  }

  createMediaElementSource(element) {
    const source = {
      element,
      connect: vi.fn(),
      disconnect: vi.fn(),
    };
    this.sources.push(source);
    return source;
  }

  resume() {
    this.state = "running";
    return Promise.resolve();
  }

  close() {
    this.closed = true;
    this.state = "closed";
    return Promise.resolve();
  }
}

class FakeAudioWorkletNode {
  static instances = [];

  constructor(context, processorName, options) {
    this.context = context;
    this.processorName = processorName;
    this.options = options;
    this.port = { onmessage: null };
    this.connect = vi.fn();
    this.disconnect = vi.fn();
    FakeAudioWorkletNode.instances.push(this);
  }
}

describe("AudioWorkletPlaybackTracker", () => {
  it("routes a media element through the rendered-sample counter", async () => {
    FakeAudioContext.instances.length = 0;
    FakeAudioWorkletNode.instances.length = 0;
    const onRendered = vi.fn();
    const onDiagnostic = vi.fn();
    const tracker = new AudioWorkletPlaybackTracker({
      AudioContextImpl: FakeAudioContext,
      AudioWorkletNodeImpl: FakeAudioWorkletNode,
      workletUrl: "/worklets/test-counter.js",
      onRendered,
      onDiagnostic,
    });
    const element = document.createElement("audio");

    await expect(tracker.attach(element)).resolves.toBe(true);
    const context = FakeAudioContext.instances[0];
    const node = FakeAudioWorkletNode.instances[0];
    expect(context.audioWorklet.addModule).toHaveBeenCalledWith(
      "/worklets/test-counter.js",
    );
    expect(node.processorName).toBe(AUDIO_WORKLET_PLAYBACK_PROCESSOR);
    expect(node.options.processorOptions).toEqual({ notifyEveryFrames: 960 });
    expect(context.sources[0].element).toBe(element);
    expect(context.sources[0].connect).toHaveBeenCalledWith(node);
    expect(node.connect).toHaveBeenCalledWith(context.destination);

    node.port.onmessage({ data: { type: "ready", rendered_frames: 0 } });
    expect(tracker.getSnapshot()).toEqual({ renderedFrames: 0, sampleRate: 48_000 });
    node.port.onmessage({ data: { type: "rendered", rendered_frames: 960 } });
    expect(tracker.getSnapshot()).toEqual({ renderedFrames: 960, sampleRate: 48_000 });
    expect(onRendered).toHaveBeenCalledWith({
      renderedFrames: 960,
      sampleRate: 48_000,
    });
    expect(onDiagnostic).toHaveBeenCalledWith(
      "audio_worklet_playback",
      "ready",
      { sample_rate: 48_000 },
    );

    await tracker.close();
    expect(node.disconnect).toHaveBeenCalled();
    expect(context.sources[0].disconnect).toHaveBeenCalled();
    expect(context.closed).toBe(true);
    expect(tracker.getSnapshot()).toBeNull();
    expect(node.port.onmessage).toBeNull();
  });

  it("reports unsupported setup without preventing fallback playback", async () => {
    const onDiagnostic = vi.fn();
    const tracker = new AudioWorkletPlaybackTracker({
      AudioContextImpl: null,
      AudioWorkletNodeImpl: null,
      onDiagnostic,
    });

    await expect(tracker.attach(document.createElement("audio"))).resolves.toBe(
      false,
    );
    expect(onDiagnostic).toHaveBeenCalledWith(
      "audio_worklet_playback",
      "unsupported",
    );
    expect(tracker.getSnapshot()).toBeNull();
  });

  it("leaves the media element untouched when autoplay keeps the graph suspended", async () => {
    class SuspendedAudioContext extends FakeAudioContext {
      constructor() {
        super();
        this.state = "suspended";
        this.resume = vi.fn(async () => undefined);
      }
    }

    const onDiagnostic = vi.fn();
    const tracker = new AudioWorkletPlaybackTracker({
      AudioContextImpl: SuspendedAudioContext,
      AudioWorkletNodeImpl: FakeAudioWorkletNode,
      onDiagnostic,
    });

    await expect(tracker.attach(document.createElement("audio"))).resolves.toBe(false);
    const context = FakeAudioContext.instances.at(-1);
    expect(context.resume).toHaveBeenCalledOnce();
    expect(context.sources).toHaveLength(0);
    expect(context.closed).toBe(true);
    expect(onDiagnostic).toHaveBeenCalledWith(
      "audio_worklet_playback",
      "fallback",
      expect.objectContaining({ error: "Error" }),
    );
  });
});
