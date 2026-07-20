import { describe, expect, it, vi } from "vitest";

import {
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

describe("audio enrollment preparation", () => {
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
