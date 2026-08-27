"use strict";

// The ESP-IDF Security 1 contract is X25519 + SHA-256(PoP) XOR + AES-256-CTR.
// This module is deliberately self-contained so the Mini Program does not
// depend on Node's crypto APIs at runtime.
const nacl = require("./vendor/nacl-fast");

function cryptoError(message) {
  const error = new Error(message);
  error.code = "BLE_SESSION_REJECTED";
  return error;
}

function utf8(value) {
  if (value instanceof Uint8Array) return value;
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(String(value));
  const encoded = unescape(encodeURIComponent(String(value)));
  const bytes = new Uint8Array(encoded.length);
  for (let index = 0; index < encoded.length; index += 1) bytes[index] = encoded.charCodeAt(index);
  return bytes;
}

async function randomBytes(length) {
  if (!Number.isInteger(length) || length <= 0) throw new RangeError("随机长度无效");
  const bytes = new Uint8Array(length);
  const cryptoApi = typeof globalThis !== "undefined" ? globalThis.crypto : null;
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
    return bytes;
  }
  const wxApi = typeof globalThis !== "undefined" ? globalThis.wx : null;
  if (wxApi && typeof wxApi.getRandomValues === "function") {
    return new Promise((resolve, reject) => {
      wxApi.getRandomValues({
        length,
        success: (result) => {
          const value = result?.randomValues || result?.value || result;
          if (!(value instanceof ArrayBuffer) || value.byteLength !== length) {
            reject(cryptoError("微信安全随机源返回长度无效"));
            return;
          }
          resolve(new Uint8Array(value));
        },
        fail: (error) => reject(Object.assign(cryptoError("微信安全随机源不可用"), { cause: error })),
      });
    });
  }
  throw cryptoError("当前微信运行时没有可用的安全随机源");
}

function sha256(input) {
  const bytes = utf8(input);
  const words = new Uint32Array(64);
  const bitLength = bytes.length * 8;
  const paddedLength = ((bytes.length + 9 + 63) >> 6) << 6;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 4, bitLength >>> 0, false);
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const rotr = (value, bits) => (value >>> bits) | (value << (32 - bits));
  for (let block = 0; block < padded.length; block += 64) {
    for (let index = 0; index < 16; index += 1) words[index] = view.getUint32(block + index * 4, false);
    for (let index = 16; index < 64; index += 1) {
      const value = words[index - 15];
      const s0 = rotr(value, 7) ^ rotr(value, 18) ^ (value >>> 3);
      const previous = words[index - 2];
      const s1 = rotr(previous, 17) ^ rotr(previous, 19) ^ (previous >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choose + constants[index] + words[index]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    state[0] = (state[0] + a) >>> 0;
    state[1] = (state[1] + b) >>> 0;
    state[2] = (state[2] + c) >>> 0;
    state[3] = (state[3] + d) >>> 0;
    state[4] = (state[4] + e) >>> 0;
    state[5] = (state[5] + f) >>> 0;
    state[6] = (state[6] + g) >>> 0;
    state[7] = (state[7] + h) >>> 0;
  }
  const output = new Uint8Array(32);
  const outputView = new DataView(output.buffer);
  state.forEach((value, index) => outputView.setUint32(index * 4, value, false));
  return output;
}

const SBOX = [
  0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
  0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
  0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
  0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
  0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
  0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
  0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
  0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
  0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
  0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
  0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
  0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
  0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
  0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
  0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
  0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
];

function xtime(value) {
  return ((value << 1) ^ ((value & 0x80) ? 0x1b : 0)) & 0xff;
}

function expandKey(key) {
  const expanded = new Uint8Array(240);
  expanded.set(key);
  let bytes = 32;
  let rcon = 1;
  const temp = new Uint8Array(4);
  while (bytes < 240) {
    temp.set(expanded.subarray(bytes - 4, bytes));
    if (bytes % 32 === 0) {
      const first = temp[0];
      temp[0] = SBOX[temp[1]] ^ rcon;
      temp[1] = SBOX[temp[2]];
      temp[2] = SBOX[temp[3]];
      temp[3] = SBOX[first];
      rcon = xtime(rcon);
    } else if (bytes % 32 === 16) {
      temp[0] = SBOX[temp[0]];
      temp[1] = SBOX[temp[1]];
      temp[2] = SBOX[temp[2]];
      temp[3] = SBOX[temp[3]];
    }
    for (let index = 0; index < 4 && bytes < 240; index += 1) {
      expanded[bytes] = expanded[bytes - 32] ^ temp[index];
      bytes += 1;
    }
  }
  return expanded;
}

function encryptBlock(input, key) {
  const state = new Uint8Array(input);
  const addRoundKey = (round) => {
    const offset = round * 16;
    for (let index = 0; index < 16; index += 1) state[index] ^= key[offset + index];
  };
  const subBytes = () => state.forEach((value, index) => { state[index] = SBOX[value]; });
  const shiftRows = () => {
    const copy = new Uint8Array(state);
    for (let column = 0; column < 4; column += 1) {
      for (let row = 0; row < 4; row += 1) state[column * 4 + row] = copy[((column + row) % 4) * 4 + row];
    }
  };
  const mixColumns = () => {
    for (let column = 0; column < 4; column += 1) {
      const offset = column * 4;
      const a = state[offset];
      const b = state[offset + 1];
      const c = state[offset + 2];
      const d = state[offset + 3];
      const e = a ^ b ^ c ^ d;
      state[offset] ^= e ^ xtime(a ^ b);
      state[offset + 1] ^= e ^ xtime(b ^ c);
      state[offset + 2] ^= e ^ xtime(c ^ d);
      state[offset + 3] ^= e ^ xtime(d ^ a);
    }
  };
  addRoundKey(0);
  for (let round = 1; round < 14; round += 1) {
    subBytes();
    shiftRows();
    mixColumns();
    addRoundKey(round);
  }
  subBytes();
  shiftRows();
  addRoundKey(14);
  return state;
}

class Aes256Ctr {
  constructor(key, iv) {
    if (key.length !== 32 || iv.length !== 16) throw cryptoError("AES-CTR 参数长度无效");
    this.key = expandKey(key);
    this.counter = new Uint8Array(iv);
    this.stream = new Uint8Array(0);
    this.offset = 0;
  }

  _nextStream() {
    this.stream = encryptBlock(this.counter, this.key);
    this.offset = 0;
    for (let index = 15; index >= 0; index -= 1) {
      this.counter[index] = (this.counter[index] + 1) & 0xff;
      if (this.counter[index] !== 0) break;
    }
  }

  update(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
    const output = new Uint8Array(bytes.length);
    for (let index = 0; index < bytes.length; index += 1) {
      if (this.offset >= this.stream.length) this._nextStream();
      output[index] = bytes[index] ^ this.stream[this.offset++];
    }
    return output;
  }

  clear() {
    this.key.fill(0);
    this.counter.fill(0);
    this.stream.fill(0);
  }
}

module.exports = { Aes256Ctr, cryptoError, nacl, randomBytes, sha256 };
