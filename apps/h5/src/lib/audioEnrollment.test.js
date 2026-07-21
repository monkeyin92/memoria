import { describe, expect, it, vi } from "vitest";

import {
  createSpeakerPcmRecorder,
  prepareSpeakerEnrollment,
  prepareVoiceCloneSample,
} from "./audioEnrollment.js";

function audioFile(name = "sample.wav") {
  return {
    name,
    type: "audio/wav",
    size: 8,
    arrayBuffer: vi.fn().mockResolvedValue(
      Uint8Array.from([82, 73, 70, 70, 1, 2, 3, 4]).buffer,
    ),
  };
}

function contextFactory({ duration = 12, sampleRate = 24_000 } = {}) {
  const samples = Math.round(duration * sampleRate);
  const buffer = {
    duration,
    sampleRate,
    numberOfChannels: 1,
    length: samples,
    getChannelData: () => new Float32Array(samples).fill(0.25),
  };
  return () => ({
    decodeAudioData: vi.fn().mockResolvedValue(buffer),
    close: vi.fn().mockResolvedValue(undefined),
  });
}

function pcmRecorderFixture({ sampleRate = 48_000 } = {}) {
  let processor;
  const context = {
    sampleRate,
    destination: {},
    decodeAudioData: vi.fn().mockRejectedValue(new Error("container cannot be decoded")),
    createMediaStreamSource: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() })),
    createScriptProcessor: vi.fn(() => {
      processor = {
        connect: vi.fn(),
        disconnect: vi.fn(),
        onaudioprocess: null,
      };
      return processor;
    }),
    createGain: vi.fn(() => ({
      gain: { value: 1 },
      connect: vi.fn(),
      disconnect: vi.fn(),
    })),
    resume: vi.fn().mockResolvedValue(undefined),
    close: vi.fn().mockResolvedValue(undefined),
  };
  return {
    factory: () => context,
    emit: (samples) => processor.onaudioprocess({
      inputBuffer: { getChannelData: () => samples },
      outputBuffer: { getChannelData: () => new Float32Array(samples.length) },
    }),
    context,
  };
}

describe("audio enrollment preparation", () => {
  it("captures live enrollment as PCM without decoding a MediaRecorder container", async () => {
    const fixture = pcmRecorderFixture();
    const recorder = createSpeakerPcmRecorder(
      { id: "microphone" },
      { audioContextFactory: fixture.factory },
    );

    await recorder.start();
    fixture.emit(new Float32Array(96_000).fill(0.25));
    const sample = await recorder.stop();

    expect(fixture.context.resume).toHaveBeenCalledOnce();
    expect(fixture.context.close).toHaveBeenCalledOnce();
    expect(fixture.context.decodeAudioData).not.toHaveBeenCalled();
    expect(sample.sample_rate).toBe(16_000);
    expect(sample.audio_base64.length).toBeGreaterThan(80_000);
    expect(sample.device).toBe("h5-web-audio");
  });

  it("resamples a 44.1 kHz microphone stream to even-sized raw PCM", async () => {
    const fixture = pcmRecorderFixture({ sampleRate: 44_100 });
    const recorder = createSpeakerPcmRecorder(
      { id: "microphone" },
      { audioContextFactory: fixture.factory },
    );

    await recorder.start();
    fixture.emit(new Float32Array(88_200).fill(0.125));
    const sample = await recorder.stop();
    const bytes = Uint8Array.from(atob(sample.audio_base64), (character) => character.charCodeAt(0));

    expect(bytes.byteLength).toBe(64_000);
    expect(bytes.byteLength % 2).toBe(0);
    expect(new DataView(bytes.buffer).getInt16(0, true)).toBeGreaterThan(4_000);
  });

  it("keeps the original 10-20 second voice sample and reports trusted metadata", async () => {
    const result = await prepareVoiceCloneSample(audioFile(), {
      audioContextFactory: contextFactory(),
    });

    expect(result.media_type).toBe("audio/wav");
    expect(result.duration_ms).toBe(12_000);
    expect(result.sample_rate).toBe(24_000);
    expect(result.audio_base64).toMatch(/^UklGRg/);
  });

  it("rejects clone recordings outside the guided duration", async () => {
    await expect(
      prepareVoiceCloneSample(audioFile(), {
        audioContextFactory: contextFactory({ duration: 7 }),
      }),
    ).rejects.toThrow("10–20 秒");
  });

  it("converts three speaker clips into 16 kHz mono PCM samples", async () => {
    const result = await prepareSpeakerEnrollment(
      [audioFile("one.wav"), audioFile("two.wav"), audioFile("three.wav")],
      { audioContextFactory: contextFactory({ duration: 2, sampleRate: 24_000 }) },
    );

    expect(result).toHaveLength(3);
    expect(result.every((sample) => sample.sample_rate === 16_000)).toBe(true);
    expect(result.every((sample) => sample.audio_base64.length > 100)).toBe(true);
  });
});
