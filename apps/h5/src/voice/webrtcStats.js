const inboundAudioFields = {
  jitter: "jitter",
  packetsLost: "packets_lost",
  packetsReceived: "packets_received",
  packetsDiscarded: "packets_discarded",
  bytesReceived: "bytes_received",
  nackCount: "nack_count",
  concealedSamples: "concealed_samples",
  silentConcealedSamples: "silent_concealed_samples",
  totalSamplesReceived: "total_samples_received",
  concealmentEvents: "concealment_events",
  jitterBufferDelay: "jitter_buffer_delay",
  jitterBufferTargetDelay: "jitter_buffer_target_delay",
  jitterBufferMinimumDelay: "jitter_buffer_minimum_delay",
  jitterBufferEmittedCount: "jitter_buffer_emitted_count",
  totalSamplesDuration: "total_samples_duration",
  insertedSamplesForDeceleration: "inserted_samples_for_deceleration",
  removedSamplesForAcceleration: "removed_samples_for_acceleration",
};

export function extractInboundAudioStats(report) {
  const rows = report?.values ? report.values() : Array.isArray(report) ? report : [];
  for (const row of rows) {
    if (
      row?.type !== "inbound-rtp" ||
      ![row.kind, row.mediaType].includes("audio")
    ) {
      continue;
    }
    const metrics = {};
    for (const [source, target] of Object.entries(inboundAudioFields)) {
      if (typeof row[source] === "number" && Number.isFinite(row[source])) {
        metrics[target] = row[source];
      }
    }
    const totalSamples = metrics.total_samples_received;
    if (totalSamples > 0) {
      metrics.concealment_ratio = Number(
        ((metrics.concealed_samples || 0) / totalSamples).toFixed(6),
      );
      metrics.non_silent_concealment_ratio = Number(
        (
          Math.max(
            0,
            (metrics.concealed_samples || 0) -
              (metrics.silent_concealed_samples || 0),
          ) / totalSamples
        ).toFixed(6),
      );
    }
    const emitted = metrics.jitter_buffer_emitted_count;
    if (emitted > 0) {
      metrics.average_jitter_buffer_delay_ms = Number(
        (((metrics.jitter_buffer_delay || 0) / emitted) * 1000).toFixed(3),
      );
      if (typeof metrics.jitter_buffer_target_delay === "number") {
        metrics.average_jitter_buffer_target_delay_ms = Number(
          ((metrics.jitter_buffer_target_delay / emitted) * 1000).toFixed(3),
        );
      }
    }
    if (metrics.bytes_received > 0 && metrics.total_samples_duration > 0) {
      metrics.encoded_audio_bitrate_kbps = Number(
        (
          (metrics.bytes_received * 8) /
          metrics.total_samples_duration /
          1000
        ).toFixed(3),
      );
    }
    return Object.keys(metrics).length ? metrics : null;
  }
  return null;
}
