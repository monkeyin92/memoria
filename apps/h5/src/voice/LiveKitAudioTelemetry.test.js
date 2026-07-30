import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveKitAudioTelemetry } from "./LiveKitAudioTelemetry.js";

describe("LiveKitAudioTelemetry", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reports only allowlisted microphone settings and outbound audio metrics", async () => {
    vi.useFakeTimers();
    const onDiagnostic = vi.fn();
    const track = {
      getSourceTrackSettings: vi.fn(() => ({
        autoGainControl: true,
        channelCount: 1,
        deviceId: "private-device-id",
        echoCancellation: true,
        groupId: "private-group-id",
        label: "Private microphone label",
        latency: 0.02,
        noiseSuppression: true,
        sampleRate: 48_000,
        sampleSize: 16,
      })),
      getRTCStatsReport: vi.fn().mockResolvedValue(
        new Map([
          [
            "outbound",
            {
              type: "outbound-rtp",
              kind: "audio",
              packetsSent: 120,
              bytesSent: 24_000,
              retransmittedPacketsSent: 2,
              trackIdentifier: "private-track-id",
            },
          ],
          [
            "remote",
            {
              type: "remote-inbound-rtp",
              kind: "audio",
              packetsLost: 3,
              jitter: 0.006,
              roundTripTime: 0.08,
            },
          ],
          [
            "source",
            {
              type: "media-source",
              kind: "audio",
              audioLevel: 0.12,
              totalAudioEnergy: 4.2,
              totalSamplesDuration: 12.5,
            },
          ],
        ]),
      ),
    };
    const telemetry = new LiveKitAudioTelemetry({ onDiagnostic });

    telemetry.observeMicrophoneTrack(track, () => true);
    await vi.runOnlyPendingTimersAsync();

    expect(onDiagnostic).toHaveBeenCalledWith(
      "webrtc_microphone_settings",
      "ok",
      {
        auto_gain_control: true,
        channel_count: 1,
        echo_cancellation: true,
        latency_ms: 20,
        noise_suppression: true,
        sample_rate: 48_000,
        sample_size: 16,
      },
    );
    expect(onDiagnostic).toHaveBeenCalledWith(
      "webrtc_outbound_audio",
      "ok",
      expect.objectContaining({
        audio_level: 0.12,
        bytes_sent: 24_000,
        jitter: 0.006,
        packets_lost: 3,
        packets_sent: 120,
        retransmitted_packets_sent: 2,
        round_trip_time: 0.08,
        total_audio_energy: 4.2,
        total_samples_duration: 12.5,
      }),
    );
    expect(JSON.stringify(onDiagnostic.mock.calls)).not.toMatch(
      /device|group|label|trackIdentifier|private-/i,
    );

    telemetry.stop();
  });
});
