const cloneMediaTypes = new Set([
  "audio/wav",
  "audio/x-wav",
  "audio/mpeg",
  "audio/mp4",
  "audio/aac",
  "audio/ogg",
  "audio/flac",
]);

function defaultAudioContextFactory() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) throw new Error("当前浏览器无法读取录音，请换用较新的浏览器");
  return new AudioContext();
}

function mediaTypeFor(file) {
  if (cloneMediaTypes.has(file.type)) return file.type;
  const extension = file.name?.split(".").pop()?.toLowerCase();
  const byExtension = {
    wav: "audio/wav",
    mp3: "audio/mpeg",
    m4a: "audio/mp4",
    mp4: "audio/mp4",
    aac: "audio/aac",
    ogg: "audio/ogg",
    flac: "audio/flac",
  };
  return byExtension[extension] || "";
}

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return window.btoa(binary);
}

async function decodeFile(file, audioContextFactory) {
  if (!file || typeof file.arrayBuffer !== "function") {
    throw new Error("请选择有效的录音文件");
  }
  const data = await file.arrayBuffer();
  const context = audioContextFactory();
  try {
    const audio = await context.decodeAudioData(data.slice(0));
    return { audio, data };
  } catch {
    throw new Error("无法读取这段录音，请换用 WAV、MP3、M4A 或 FLAC")
  } finally {
    await context.close?.();
  }
}

export async function prepareVoiceCloneSample(
  file,
  { audioContextFactory = defaultAudioContextFactory } = {},
) {
  if (file?.size > 15 * 1024 * 1024) {
    throw new Error("录音不能超过 15 MB");
  }
  const mediaType = mediaTypeFor(file || {});
  if (!mediaType) throw new Error("请使用 WAV、MP3、M4A、AAC、OGG 或 FLAC 录音");
  const { audio, data } = await decodeFile(file, audioContextFactory);
  const durationMs = Math.round(audio.duration * 1000);
  if (durationMs < 10_000 || durationMs > 20_000) {
    throw new Error("请提供 10–20 秒、环境安静且只有本人说话的录音");
  }
  return {
    audio_base64: bytesToBase64(data),
    media_type: mediaType,
    duration_ms: durationMs,
    sample_rate: Math.round(audio.sampleRate),
  };
}

function pcm16Mono(audio, targetRate = 16_000) {
  const outputLength = Math.max(1, Math.round(audio.duration * targetRate));
  const output = new Int16Array(outputLength);
  const channels = Array.from(
    { length: audio.numberOfChannels },
    (_, index) => audio.getChannelData(index),
  );
  const ratio = audio.sampleRate / targetRate;
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * ratio;
    const left = Math.min(audio.length - 1, Math.floor(position));
    const right = Math.min(audio.length - 1, left + 1);
    const fraction = position - left;
    let value = 0;
    for (const channel of channels) {
      value += channel[left] + (channel[right] - channel[left]) * fraction;
    }
    value = Math.max(-1, Math.min(1, value / channels.length));
    output[index] = value < 0 ? Math.round(value * 32_768) : Math.round(value * 32_767);
  }
  return output.buffer;
}

export async function prepareSpeakerEnrollment(
  files,
  { audioContextFactory = defaultAudioContextFactory } = {},
) {
  const selected = Array.from(files || []);
  if (selected.length < 3 || selected.length > 10) {
    throw new Error("声纹登记需要 3–10 段不同内容的本人录音");
  }
  const samples = [];
  for (const file of selected) {
    const { audio } = await decodeFile(file, audioContextFactory);
    if (audio.duration < 1.5 || audio.duration > 15) {
      throw new Error("每段声纹录音请保持在 1.5–15 秒");
    }
    samples.push({
      audio_base64: bytesToBase64(pcm16Mono(audio)),
      sample_rate: 16_000,
      device: "h5-web-audio",
      scene: "owner-enrollment",
    });
  }
  return samples;
}
