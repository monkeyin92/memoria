class PcmJitterPlayer {
  constructor({
    sampleRate = 24000,
    minLeadSeconds = 0.08,
    maxLeadSeconds = 0.45,
    bufferMilliseconds = 80,
    frameMilliseconds = 20,
    maxConcealFrames = 3,
    onTrace = null,
  } = {}) {
    if (
      sampleRate <= 0 ||
      frameMilliseconds <= 0 ||
      (sampleRate * frameMilliseconds) % 1000 !== 0
    ) {
      throw new RangeError("invalid PCM player audio contract");
    }
    this.sampleRate = sampleRate;
    this.minLeadSeconds = minLeadSeconds;
    this.maxLeadSeconds = maxLeadSeconds;
    this.frameSamples = (sampleRate * frameMilliseconds) / 1000;
    this.frameBytes = this.frameSamples * 2;
    this.bufferSamples = Math.round((sampleRate * bufferMilliseconds) / 1000);
    this.maxConcealFrames = maxConcealFrames;
    this.onTrace = onTrace;
    this.context = null;
    this.nextStartAt = 0;
    this.sources = new Set();
    this.pending = [];
    this.pendingSamples = 0;
    this.flushTimer = null;
    this.generationId = null;
    this.expectedSequence = null;
    this.fadeInPending = false;
    this.lastSample = 0;
    this.gain = 1;
    this.gainNode = null;
    this.firstPlaybackGenerationId = null;
  }

  async resume() {
    if (!this.context) {
      this.context = wx.createWebAudioContext();
      if (typeof this.context.createGain === "function") {
        this.gainNode = this.context.createGain();
        this.gainNode.gain.value = this.gain;
        this.gainNode.connect(this.context.destination);
      }
    }
    if (this.context.state !== "running") await this.context.resume();
  }

  enqueue(pcm, { sequence, generationId } = {}) {
    if (!this.context || this.context.state !== "running") return;
    if (!pcm || pcm.byteLength !== this.frameBytes) {
      throw new RangeError(
        `PCM frame must be one ${this.frameBytes}-byte PCM16/20ms frame`,
      );
    }
    let input = new Int16Array(pcm);
    if (!input.length) return;
    const hasGeneration = Number.isInteger(generationId) && generationId >= 0;
    if (this.generationId !== null && (!hasGeneration || generationId !== this.generationId)) {
      return;
    }
    if (this.generationId === null && hasGeneration) this.generationId = generationId;
    if (Number.isInteger(sequence) && sequence >= 0) {
      if (this.expectedSequence !== null && sequence !== this.expectedSequence) {
        const distance = (sequence - this.expectedSequence) >>> 0;
        if (distance >= 0x80000000) return;
        if (distance <= this.maxConcealFrames) {
          this._trace("miniprogram_gap_concealed", {
            missing_frames: distance,
          });
          input = this._concealGap(distance, input);
        } else {
          this._trace("miniprogram_playback_hard_reset", {
            missing_frames: distance,
          });
          this._clearPlayback();
        }
      }
      this.expectedSequence = (sequence + 1) >>> 0;
    }
    this._appendPending(input);
    this.lastSample = input[input.length - 1] || 0;
  }

  _appendPending(input) {
    this.pending.push(input);
    this.pendingSamples += input.length;
    clearTimeout(this.flushTimer);
    this.flushTimer = null;
    if (this.pendingSamples < this.bufferSamples) {
      this.flushTimer = setTimeout(() => this._flushPending(), 40);
      return;
    }
    this._flushPending();
  }

  _concealGap(missingFrames, input) {
    const fadeSamples = Math.min(Math.round(this.sampleRate * 0.005), input.length);
    for (let frameIndex = 0; frameIndex < missingFrames; frameIndex += 1) {
      const concealed = new Int16Array(this.frameSamples);
      if (frameIndex === 0 && this.lastSample && fadeSamples) {
        for (let index = 0; index < fadeSamples; index += 1) {
          concealed[index] = Math.round(
            this.lastSample * (1 - (index + 1) / fadeSamples),
          );
        }
      }
      this._appendPending(concealed);
    }
    if (!fadeSamples) return input;
    const faded = input.slice();
    for (let index = 0; index < fadeSamples; index += 1) {
      faded[index] = Math.round(faded[index] * ((index + 1) / fadeSamples));
    }
    return faded;
  }

  _flushPending() {
    if (!this.pendingSamples || !this.context || this.context.state !== "running") return;
    clearTimeout(this.flushTimer);
    this.flushTimer = null;
    const input = new Int16Array(this.pendingSamples);
    let offset = 0;
    for (const chunk of this.pending) {
      input.set(chunk, offset);
      offset += chunk.length;
    }
    this.pending = [];
    this.pendingSamples = 0;
    this.lastSample = 0;
    const buffer = this.context.createBuffer(1, input.length, this.sampleRate);
    const samples = buffer.getChannelData(0);
    for (let index = 0; index < input.length; index += 1) {
      samples[index] = input[index] / 32768;
    }
    const now = this.context.currentTime;
    const pendingAudioMs = Math.round((input.length / this.sampleRate) * 1000);
    if (this.nextStartAt <= now) {
      if (this.nextStartAt > 0) {
        this._trace("miniprogram_playback_underrun", {
          pending_audio_ms: pendingAudioMs,
        });
      }
      this.nextStartAt = now + this.minLeadSeconds;
      this.fadeInPending = true;
    } else if (this.nextStartAt > now + this.maxLeadSeconds) {
      this._trace("miniprogram_playback_hard_reset", {
        clock_ahead_ms: Math.round((this.nextStartAt - now) * 1000),
      });
      this._clearPlayback();
    }
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gainNode || this.context.destination);
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    this._scheduleFadeIn(this.nextStartAt);
    source.start(this.nextStartAt);
    if (
      Number.isInteger(this.generationId) &&
      this.firstPlaybackGenerationId !== this.generationId
    ) {
      this.firstPlaybackGenerationId = this.generationId;
      this._trace("first_playback", {
        pending_audio_ms: pendingAudioMs,
      });
    }
    this.nextStartAt += buffer.duration;
  }

  _trace(name, detail = {}) {
    if (typeof this.onTrace !== "function") return;
    const now = this.context?.currentTime || 0;
    const metrics = {
      queue_lead_ms: Math.round(Math.max(0, this.nextStartAt - now) * 1000),
      pending_audio_ms: Math.round((this.pendingSamples / this.sampleRate) * 1000),
      scheduled_sources: this.sources.size,
      ...detail,
    };
    try {
      this.onTrace({
        name,
        generationId: this.generationId,
        detail: metrics,
      });
    } catch {
      // Diagnostics must never interrupt playout.
    }
  }

  setGain(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return;
    const target = Math.min(1, Math.max(0, value));
    this.gain = target;
    const gainParam = this.gainNode?.gain;
    if (gainParam) {
      const now = this.context?.currentTime || 0;
      if (
        typeof gainParam.cancelScheduledValues === "function" &&
        typeof gainParam.setValueAtTime === "function" &&
        typeof gainParam.linearRampToValueAtTime === "function"
      ) {
        try {
          gainParam.cancelScheduledValues(now);
          gainParam.setValueAtTime(
            typeof gainParam.value === "number" ? gainParam.value : target,
            now,
          );
          gainParam.linearRampToValueAtTime(target, now + 0.01);
          return;
        } catch {
          // Direct value assignment below remains the compatibility fallback.
        }
      }
      gainParam.value = target;
    }
  }

  _scheduleFadeIn(startAt) {
    if (!this.fadeInPending) return;
    const gainParam = this.gainNode?.gain;
    this.fadeInPending = false;
    if (
      !gainParam ||
      typeof gainParam.cancelScheduledValues !== "function" ||
      typeof gainParam.setValueAtTime !== "function" ||
      typeof gainParam.linearRampToValueAtTime !== "function"
    ) {
      return;
    }
    try {
      gainParam.cancelScheduledValues(startAt);
      gainParam.setValueAtTime(0, startAt);
      gainParam.linearRampToValueAtTime(this.gain, startAt + 0.01);
    } catch {
      gainParam.value = this.gain;
    }
  }

  _clearPlayback() {
    clearTimeout(this.flushTimer);
    this.flushTimer = null;
    this.pending = [];
    this.pendingSamples = 0;
    const now = this.context?.currentTime || 0;
    const gainParam = this.gainNode?.gain;
    let stopAt = null;
    if (
      gainParam &&
      typeof gainParam.cancelScheduledValues === "function" &&
      typeof gainParam.setValueAtTime === "function" &&
      typeof gainParam.linearRampToValueAtTime === "function"
    ) {
      try {
        stopAt = now + 0.005;
        gainParam.cancelScheduledValues(now);
        gainParam.setValueAtTime(
          typeof gainParam.value === "number" ? gainParam.value : this.gain,
          now,
        );
        gainParam.linearRampToValueAtTime(0, stopAt);
        gainParam.setValueAtTime(0, stopAt);
        this.fadeInPending = true;
      } catch {
        stopAt = null;
        this.fadeInPending = false;
      }
    }
    for (const source of this.sources) {
      try {
        if (stopAt === null) source.stop();
        else source.stop(stopAt);
      } catch {
        // A source can already have ended while a gateway reset arrives.
      }
    }
    this.sources.clear();
    this.nextStartAt = this.context ? now + this.minLeadSeconds : 0;
  }

  reset(generationId, barrierSequence = null) {
    this._clearPlayback();
    this.generationId = Number.isInteger(generationId) && generationId >= 0 ? generationId : null;
    this.expectedSequence =
      Number.isInteger(barrierSequence) && barrierSequence >= 0
        ? barrierSequence >>> 0
        : null;
  }

  async close() {
    this.reset();
    if (this.context) {
      await this.context.close();
      this.context = null;
      this.gainNode = null;
    }
  }
}

module.exports = {
  PcmJitterPlayer,
};
