"use strict";

/*
 * Memoria provisioning transport frame.
 *
 * This is only the byte transport envelope.  It does not replace Protocomm
 * Security 1/2 or the generated protobuf contract.  Keeping this layer
 * independently testable lets the BLE path handle real MTU fragmentation
 * without pretending that an unaudited security handshake is complete.
 */

const MAGIC_0 = 0x4d;
const MAGIC_1 = 0x50;
const VERSION = 1;
const HEADER_SIZE = 14;
const FLAG_FIRST = 1;
const FLAG_LAST = 2;
let nextMessageId = 1;

function toBytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (typeof value === "string") {
    const encoded = unescape(encodeURIComponent(value));
    const bytes = new Uint8Array(encoded.length);
    for (let index = 0; index < encoded.length; index += 1) bytes[index] = encoded.charCodeAt(index);
    return bytes;
  }
  throw new TypeError("帧载荷必须是字符串或字节数组");
}

function crc16(bytes) {
  let crc = 0xffff;
  for (const byte of bytes) {
    crc ^= byte << 8;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
      crc &= 0xffff;
    }
  }
  return crc;
}

function writeUint16(target, offset, value) {
  target[offset] = (value >>> 8) & 0xff;
  target[offset + 1] = value & 0xff;
}

function readUint16(source, offset) {
  return (source[offset] << 8) | source[offset + 1];
}

function fragmentFrame(payload, { mtu = 20, messageId = null } = {}) {
  const bytes = toBytes(payload);
  if (!Number.isInteger(mtu) || mtu <= HEADER_SIZE) {
    throw new RangeError(`BLE MTU 必须大于 ${HEADER_SIZE}`);
  }
  const chunkSize = mtu - HEADER_SIZE;
  const total = Math.max(1, Math.ceil(bytes.byteLength / chunkSize));
  if (total > 0xffff) throw new RangeError("BLE 帧过大");
  const id = messageId === null ? nextMessageId++ : messageId;
  if (!Number.isInteger(id) || id < 0 || id > 0xffff) throw new RangeError("messageId 无效");
  const frames = [];
  for (let sequence = 0; sequence < total; sequence += 1) {
    const start = sequence * chunkSize;
    const chunk = bytes.slice(start, Math.min(bytes.byteLength, start + chunkSize));
    const frame = new Uint8Array(HEADER_SIZE + chunk.byteLength);
    frame[0] = MAGIC_0;
    frame[1] = MAGIC_1;
    frame[2] = VERSION;
    frame[3] = (sequence === 0 ? FLAG_FIRST : 0) | (sequence === total - 1 ? FLAG_LAST : 0);
    writeUint16(frame, 4, id);
    writeUint16(frame, 6, sequence);
    writeUint16(frame, 8, total);
    writeUint16(frame, 10, chunk.byteLength);
    writeUint16(frame, 12, crc16(chunk));
    frame.set(chunk, HEADER_SIZE);
    frames.push(frame.buffer);
  }
  return frames;
}

class FrameReassembler {
  constructor() {
    this.reset();
  }

  reset() {
    this.messageId = null;
    this.total = 0;
    this.parts = [];
  }

  push(frameValue) {
    const frame = toBytes(frameValue);
    if (frame.byteLength < HEADER_SIZE) throw new Error("BLE 帧头不完整");
    if (frame[0] !== MAGIC_0 || frame[1] !== MAGIC_1 || frame[2] !== VERSION) {
      throw new Error("BLE 帧版本不受支持");
    }
    const flags = frame[3];
    const messageId = readUint16(frame, 4);
    const sequence = readUint16(frame, 6);
    const total = readUint16(frame, 8);
    const length = readUint16(frame, 10);
    const checksum = readUint16(frame, 12);
    if (!total || sequence >= total || length !== frame.byteLength - HEADER_SIZE) {
      throw new Error("BLE 帧序号或长度无效");
    }
    const chunk = frame.slice(HEADER_SIZE);
    if (crc16(chunk) !== checksum) throw new Error("BLE 帧完整性校验失败");
    if (sequence === 0 && !(flags & FLAG_FIRST)) throw new Error("BLE 帧缺少首片标记");
    if (sequence === total - 1 && !(flags & FLAG_LAST)) throw new Error("BLE 帧缺少末片标记");
    if (this.messageId === null) {
      if (sequence !== 0) throw new Error("BLE 帧未从首片开始");
      this.messageId = messageId;
      this.total = total;
      this.parts = new Array(total);
    }
    if (messageId !== this.messageId || total !== this.total) {
      throw new Error("BLE 帧消息不一致");
    }
    if (this.parts[sequence]) return null;
    this.parts[sequence] = chunk;
    for (let index = 0; index < this.parts.length; index += 1) {
      if (this.parts[index] === undefined) return null;
    }
    const lengthTotal = this.parts.reduce((sum, part) => sum + part.byteLength, 0);
    const merged = new Uint8Array(lengthTotal);
    let offset = 0;
    for (const part of this.parts) {
      merged.set(part, offset);
      offset += part.byteLength;
    }
    this.reset();
    return merged;
  }
}

module.exports = {
  HEADER_SIZE,
  FLAG_FIRST,
  FLAG_LAST,
  crc16,
  fragmentFrame,
  FrameReassembler,
};
