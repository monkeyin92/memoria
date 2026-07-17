/**
 * Lightweight log-mel mean/std speaker embedding (browser port of
 * services/agent/src/orchestration/speaker_verify.py). Not a commercial
 * voiceprint product — good enough to reject a clearly different nearby talker.
 */

const EPS = 1e-8;

function hzToMel(hz) {
  return 2595 * Math.log10(1 + hz / 700);
}

function melToHz(mel) {
  return 700 * (10 ** (mel / 2595) - 1);
}

function melFilterbank({ nFft, nMels, sampleRate, fmin = 80, fmax = null }) {
  const maxF = fmax ?? sampleRate / 2;
  const nFreqs = Math.floor(nFft / 2) + 1;
  const mels = [];
  const melMin = hzToMel(fmin);
  const melMax = hzToMel(maxF);
  for (let i = 0; i < nMels + 2; i += 1) {
    mels.push(melMin + ((melMax - melMin) * i) / (nMels + 1));
  }
  const bins = mels.map((m) =>
    Math.min(
      nFreqs - 1,
      Math.max(0, Math.floor(((nFft + 1) * melToHz(m)) / sampleRate)),
    ),
  );
  const fb = Array.from({ length: nMels }, () => new Float32Array(nFreqs));
  for (let i = 0; i < nMels; i += 1) {
    let left = bins[i];
    let center = bins[i + 1];
    let right = bins[i + 2];
    if (center <= left) center = left + 1;
    if (right <= center) right = Math.min(nFreqs - 1, center + 1);
    for (let j = left; j < center; j += 1) {
      fb[i][j] = (j - left) / Math.max(1, center - left);
    }
    for (let j = center; j < right; j += 1) {
      fb[i][j] = (right - j) / Math.max(1, right - center);
    }
  }
  return fb;
}

function floatFromPcm16le(pcm) {
  const view = new DataView(
    pcm.buffer,
    pcm.byteOffset,
    pcm.byteLength - (pcm.byteLength % 2),
  );
  const out = new Float32Array(view.byteLength / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

export function speechMsFromPcm(
  pcm,
  { sampleRate = 16000, energyThreshold = 0.012, frameMs = 20 } = {},
) {
  const x = floatFromPcm16le(pcm);
  if (!x.length) return 0;
  const frame = Math.max(1, Math.floor((sampleRate * frameMs) / 1000));
  let voiced = 0;
  for (let start = 0; start + frame <= x.length; start += frame) {
    let energy = 0;
    for (let i = 0; i < frame; i += 1) energy += x[start + i] * x[start + i];
    if (Math.sqrt(energy / frame + EPS) >= energyThreshold) voiced += frameMs;
  }
  return voiced;
}

function nextPow2(n) {
  let p = 1;
  while (p < n) p <<= 1;
  return p;
}

/** Radix-2 real FFT power spectrum length n/2+1. */
function rfftPower(frame, nFft) {
  const n = nFft;
  const re = new Float64Array(n);
  const im = new Float64Array(n);
  for (let i = 0; i < frame.length && i < n; i += 1) re[i] = frame[i];
  // Bit-reversal permutation + iterative Cooley–Tukey
  for (let i = 1, j = 0; i < n; i += 1) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      const tr = re[i];
      re[i] = re[j];
      re[j] = tr;
      const ti = im[i];
      im[i] = im[j];
      im[j] = ti;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wlenRe = Math.cos(ang);
    const wlenIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let wRe = 1;
      let wIm = 0;
      for (let j = 0; j < len / 2; j += 1) {
        const uRe = re[i + j];
        const uIm = im[i + j];
        const vRe = re[i + j + len / 2] * wRe - im[i + j + len / 2] * wIm;
        const vIm = re[i + j + len / 2] * wIm + im[i + j + len / 2] * wRe;
        re[i + j] = uRe + vRe;
        im[i + j] = uIm + vIm;
        re[i + j + len / 2] = uRe - vRe;
        im[i + j + len / 2] = uIm - vIm;
        const nextWRe = wRe * wlenRe - wIm * wlenIm;
        wIm = wRe * wlenIm + wIm * wlenRe;
        wRe = nextWRe;
      }
    }
  }
  const out = new Float32Array(n / 2 + 1);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = re[i] * re[i] + im[i] * im[i];
  }
  return out;
}

export function embedPcm(
  pcm,
  {
    sampleRate = 16000,
    nMels = 40,
    nFft = 512,
    hopMs = 10,
    winMs = 25,
    minSpeechMs = 600,
  } = {},
) {
  const speechMs = speechMsFromPcm(pcm, { sampleRate });
  if (speechMs < minSpeechMs) return null;
  const x = floatFromPcm16le(pcm);
  if (x.length < nFft) return null;
  const hop = Math.max(1, Math.floor((sampleRate * hopMs) / 1000));
  const win = Math.max(nFft, Math.floor((sampleRate * winMs) / 1000));
  const window = new Float32Array(win);
  for (let i = 0; i < win; i += 1) {
    window[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / Math.max(1, win - 1)));
  }
  const fb = melFilterbank({ nFft, nMels, sampleRate });
  const frames = [];
  const frameBuf = new Float32Array(win);
  for (let start = 0; start + win <= x.length; start += hop) {
    for (let i = 0; i < win; i += 1) frameBuf[i] = x[start + i] * window[i];
    const power = rfftPower(frameBuf, nFft);
    const mel = new Float32Array(nMels);
    for (let m = 0; m < nMels; m += 1) {
      let sum = 0;
      const row = fb[m];
      for (let k = 0; k < row.length; k += 1) sum += row[k] * power[k];
      mel[m] = Math.log(sum + 1e-6);
    }
    frames.push(mel);
  }
  if (frames.length < 8) return null;
  const mean = new Float32Array(nMels);
  const std = new Float32Array(nMels);
  for (const f of frames) {
    for (let i = 0; i < nMels; i += 1) mean[i] += f[i];
  }
  for (let i = 0; i < nMels; i += 1) mean[i] /= frames.length;
  for (const f of frames) {
    for (let i = 0; i < nMels; i += 1) {
      const d = f[i] - mean[i];
      std[i] += d * d;
    }
  }
  for (let i = 0; i < nMels; i += 1) std[i] = Math.sqrt(std[i] / frames.length);
  const vec = new Float32Array(nMels * 2);
  for (let i = 0; i < nMels; i += 1) {
    vec[i] = mean[i];
    vec[nMels + i] = std[i];
  }
  let norm = 0;
  for (let i = 0; i < vec.length; i += 1) norm += vec[i] * vec[i];
  norm = Math.sqrt(norm) + EPS;
  for (let i = 0; i < vec.length; i += 1) vec[i] /= norm;
  return vec;
}

export function cosineSimilarity(a, b) {
  if (!a?.length || !b?.length || a.length !== b.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) + EPS);
}

export function float32ToPcm16le(float32) {
  const out = new Uint8Array(float32.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < float32.length; i += 1) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return out;
}

export function downsampleTo16k(float32, srcRate) {
  if (srcRate === 16000) return float32;
  const ratio = srcRate / 16000;
  const outLen = Math.floor(float32.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i += 1) {
    const src = i * ratio;
    const i0 = Math.floor(src);
    const i1 = Math.min(float32.length - 1, i0 + 1);
    const t = src - i0;
    out[i] = float32[i0] * (1 - t) + float32[i1] * t;
  }
  return out;
}

// Keep tree-shaking happy for optional consumers.
void nextPow2;
