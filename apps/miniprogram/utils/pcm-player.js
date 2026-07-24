class PcmJitterPlayer {
  constructor({ sampleRate = 24000, minLeadSeconds = 0.08, maxLeadSeconds = 0.45 } = {}) {
    this.sampleRate = sampleRate;
    this.minLeadSeconds = minLeadSeconds;
    this.maxLeadSeconds = maxLeadSeconds;
    this.context = null;
    this.nextStartAt = 0;
    this.sources = new Set();
  }

  async resume() {
    if (!this.context) this.context = wx.createWebAudioContext();
    if (this.context.state !== "running") await this.context.resume();
  }

  enqueue(pcm) {
    if (!this.context || this.context.state !== "running") return;
    const input = new Int16Array(pcm);
    if (!input.length) return;
    const buffer = this.context.createBuffer(1, input.length, this.sampleRate);
    const samples = buffer.getChannelData(0);
    for (let index = 0; index < input.length; index += 1) {
      samples[index] = input[index] / 32768;
    }
    const now = this.context.currentTime;
    if (
      this.nextStartAt < now - this.minLeadSeconds ||
      this.nextStartAt > now + this.maxLeadSeconds
    ) {
      this.nextStartAt = now + this.minLeadSeconds;
    }
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    source.start(this.nextStartAt);
    this.nextStartAt += buffer.duration;
  }

  reset() {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // A source can already have ended while a gateway reset arrives.
      }
    }
    this.sources.clear();
    this.nextStartAt = this.context ? this.context.currentTime + this.minLeadSeconds : 0;
  }

  async close() {
    this.reset();
    if (this.context) {
      await this.context.close();
      this.context = null;
    }
  }
}

module.exports = {
  PcmJitterPlayer,
};
