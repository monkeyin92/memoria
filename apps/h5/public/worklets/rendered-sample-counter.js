class RenderedSampleCounterProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.renderedFrames = 0;
    this.framesSinceNotification = 0;
    this.notifyEveryFrames = Math.max(
      128,
      Number(options?.processorOptions?.notifyEveryFrames) || 960,
    );
    this.port.postMessage({ type: "ready", rendered_frames: 0 });
  }

  process(inputs, outputs) {
    const input = inputs[0] || [];
    const output = outputs[0] || [];
    const frameCount = output[0]?.length || input[0]?.length || 128;
    for (let channel = 0; channel < output.length; channel += 1) {
      const target = output[channel];
      const source = input[channel] || input[0];
      target.fill(0);
      if (source) target.set(source.subarray(0, target.length));
    }
    this.renderedFrames += frameCount;
    this.framesSinceNotification += frameCount;
    if (this.framesSinceNotification >= this.notifyEveryFrames) {
      this.framesSinceNotification = 0;
      this.port.postMessage({
        type: "rendered",
        rendered_frames: this.renderedFrames,
      });
    }
    return true;
  }
}

registerProcessor(
  "memoria-rendered-sample-counter-v1",
  RenderedSampleCounterProcessor,
);
