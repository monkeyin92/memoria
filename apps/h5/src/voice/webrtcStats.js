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

const inboundDeltaFields = [
  "packets_lost",
  "packets_received",
  "packets_discarded",
  "concealed_samples",
  "total_samples_received",
];

const outboundAudioFields = {
  packetsSent: "packets_sent",
  bytesSent: "bytes_sent",
  retransmittedPacketsSent: "retransmitted_packets_sent",
  retransmittedBytesSent: "retransmitted_bytes_sent",
  nackCount: "nack_count",
  totalPacketSendDelay: "total_packet_send_delay",
};

const remoteInboundAudioFields = {
  packetsLost: "packets_lost",
  packetsReceived: "packets_received",
  jitter: "jitter",
  roundTripTime: "round_trip_time",
  fractionLost: "fraction_lost",
};

const microphoneSourceFields = {
  audioLevel: "audio_level",
  totalAudioEnergy: "total_audio_energy",
  totalSamplesDuration: "total_samples_duration",
  echoReturnLoss: "echo_return_loss",
  echoReturnLossEnhancement: "echo_return_loss_enhancement",
};

const outboundDeltaFields = [
  "packets_sent",
  "bytes_sent",
  "retransmitted_packets_sent",
  "retransmitted_bytes_sent",
];

function audioRows(report) {
  return report?.values
    ? report.values()
    : Array.isArray(report)
      ? report
      : [];
}

function copyFiniteNumbers(target, row, fields) {
  for (const [source, name] of Object.entries(fields)) {
    const value = row?.[source];
    if (
      typeof value === "number" &&
      Number.isFinite(value) &&
      value >= 0
    ) {
      target[name] = value;
    }
  }
}

export function addInboundAudioDeltas(metrics, baseline = metrics) {
  return addAudioDeltas(metrics, baseline, inboundDeltaFields);
}

function addAudioDeltas(metrics, baseline, fields) {
  const result = { ...metrics };
  for (const field of fields) {
    if (
      typeof metrics?.[field] === "number" &&
      typeof baseline?.[field] === "number"
    ) {
      result[`${field}_delta`] = Math.max(
        0,
        metrics[field] - baseline[field],
      );
    }
  }
  return result;
}

export function extractInboundAudioStats(report) {
  for (const row of audioRows(report)) {
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

export function addOutboundAudioDeltas(metrics, baseline = metrics) {
  return addAudioDeltas(metrics, baseline, outboundDeltaFields);
}

export function extractOutboundAudioStats(report) {
  const metrics = {};
  for (const row of audioRows(report)) {
    if (![row?.kind, row?.mediaType].includes("audio")) continue;
    if (row.type === "outbound-rtp") {
      copyFiniteNumbers(metrics, row, outboundAudioFields);
    } else if (row.type === "remote-inbound-rtp") {
      copyFiniteNumbers(metrics, row, remoteInboundAudioFields);
    } else if (row.type === "media-source") {
      copyFiniteNumbers(metrics, row, microphoneSourceFields);
    }
  }
  return Object.keys(metrics).length ? metrics : null;
}

export function extractMicrophoneSettings(settings) {
  if (!settings || typeof settings !== "object") return null;
  const result = {};
  const numericFields = {
    channelCount: "channel_count",
    sampleRate: "sample_rate",
    sampleSize: "sample_size",
  };
  const booleanFields = {
    autoGainControl: "auto_gain_control",
    echoCancellation: "echo_cancellation",
    noiseSuppression: "noise_suppression",
  };
  copyFiniteNumbers(result, settings, numericFields);
  for (const [source, name] of Object.entries(booleanFields)) {
    if (typeof settings[source] === "boolean") {
      result[name] = settings[source];
    }
  }
  if (
    typeof settings.latency === "number" &&
    Number.isFinite(settings.latency) &&
    settings.latency >= 0
  ) {
    result.latency_ms = Number((settings.latency * 1000).toFixed(3));
  }
  return Object.keys(result).length ? result : null;
}
