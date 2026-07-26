const assert = require("node:assert/strict");
const test = require("node:test");

const {
  FRAME_TYPE,
  decodePcmFrame,
  encodePcmFrame,
} = require("../utils/media-protocol");

test("PCM media frame round-trips with a versioned binary header", () => {
  const payload = new Uint8Array([0, 1, 2, 3]).buffer;
  const encoded = encodePcmFrame(FRAME_TYPE.UPLINK_AUDIO, 7, 123456, payload);
  const decoded = decodePcmFrame(encoded, FRAME_TYPE.UPLINK_AUDIO);

  assert.equal(decoded.sequence, 7);
  assert.equal(decoded.timestampMs, 123456);
  assert.deepEqual([...new Uint8Array(decoded.payload)], [0, 1, 2, 3]);
  assert.equal(decoded.generationId, null);
});

test("PCM media frame decodes a generation-bound downlink header", () => {
  const payload = new Uint8Array([4, 5]).buffer;
  const encoded = new ArrayBuffer(24 + payload.byteLength);
  const view = new DataView(encoded);
  view.setUint8(0, FRAME_TYPE.DOWNLINK_AUDIO);
  view.setUint8(1, 2);
  view.setUint16(2, 0);
  view.setUint32(4, 8);
  view.setUint32(8, 3);
  view.setUint32(12, 0);
  view.setUint32(16, 123456);
  view.setUint32(20, payload.byteLength);
  new Uint8Array(encoded, 24).set(new Uint8Array(payload));

  const decoded = decodePcmFrame(encoded, FRAME_TYPE.DOWNLINK_AUDIO);

  assert.equal(decoded.sequence, 8);
  assert.equal(decoded.generationId, 3);
  assert.equal(decoded.timestampMs, 123456);
  assert.deepEqual([...new Uint8Array(decoded.payload)], [4, 5]);
});

test("PCM media frame rejects a wrong direction or mismatched payload length", () => {
  const encoded = encodePcmFrame(
    FRAME_TYPE.DOWNLINK_AUDIO,
    1,
    2,
    new Uint8Array([0, 0]).buffer,
  );
  assert.throws(() => decodePcmFrame(encoded, FRAME_TYPE.UPLINK_AUDIO), /direction/);

  const tampered = encoded.slice(0);
  new DataView(tampered).setUint32(16, 3);
  assert.throws(() => decodePcmFrame(tampered), /payload length/);
});
