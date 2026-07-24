const PROTOCOL_VERSION = 1;
const HEADER_SIZE = 20;
const FRAME_TYPE = Object.freeze({
  UPLINK_AUDIO: 1,
  DOWNLINK_AUDIO: 2,
});

function asArrayBuffer(value) {
  if (value instanceof ArrayBuffer) return value;
  if (ArrayBuffer.isView(value)) {
    return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
  }
  throw new TypeError("PCM payload must be an ArrayBuffer");
}

function writeUint64(view, offset, value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError("timestamp must be a non-negative safe integer");
  }
  const high = Math.floor(value / 0x1_0000_0000);
  const low = value % 0x1_0000_0000;
  view.setUint32(offset, high);
  view.setUint32(offset + 4, low);
}

function readUint64(view, offset) {
  const high = view.getUint32(offset);
  const low = view.getUint32(offset + 4);
  const value = high * 0x1_0000_0000 + low;
  if (!Number.isSafeInteger(value)) {
    throw new RangeError("timestamp exceeds JavaScript safe integer range");
  }
  return value;
}

function encodePcmFrame(type, sequence, timestampMs, payload) {
  if (!Object.values(FRAME_TYPE).includes(type)) throw new TypeError("unknown PCM frame type");
  if (!Number.isInteger(sequence) || sequence < 0 || sequence > 0xffffffff) {
    throw new RangeError("invalid PCM sequence");
  }
  const pcm = asArrayBuffer(payload);
  if (!pcm.byteLength || pcm.byteLength > 64 * 1024) {
    throw new RangeError("invalid PCM payload length");
  }
  const frame = new ArrayBuffer(HEADER_SIZE + pcm.byteLength);
  const view = new DataView(frame);
  view.setUint8(0, type);
  view.setUint8(1, PROTOCOL_VERSION);
  view.setUint16(2, 0);
  view.setUint32(4, sequence);
  writeUint64(view, 8, timestampMs);
  view.setUint32(16, pcm.byteLength);
  new Uint8Array(frame, HEADER_SIZE).set(new Uint8Array(pcm));
  return frame;
}

function decodePcmFrame(frame, expectedType) {
  const data = asArrayBuffer(frame);
  if (data.byteLength < HEADER_SIZE) throw new Error("PCM frame header is incomplete");
  const view = new DataView(data);
  const type = view.getUint8(0);
  const version = view.getUint8(1);
  const flags = view.getUint16(2);
  const sequence = view.getUint32(4);
  const timestampMs = readUint64(view, 8);
  const payloadLength = view.getUint32(16);
  if (version !== PROTOCOL_VERSION || flags !== 0) throw new Error("PCM frame header is invalid");
  if (!Object.values(FRAME_TYPE).includes(type)) throw new Error("PCM frame type is invalid");
  if (expectedType !== undefined && type !== expectedType) throw new Error("PCM frame direction is invalid");
  if (!payloadLength || payloadLength !== data.byteLength - HEADER_SIZE) {
    throw new Error("PCM frame payload length is invalid");
  }
  return {
    type,
    sequence,
    timestampMs,
    payload: data.slice(HEADER_SIZE),
  };
}

module.exports = {
  PROTOCOL_VERSION,
  HEADER_SIZE,
  FRAME_TYPE,
  encodePcmFrame,
  decodePcmFrame,
};
