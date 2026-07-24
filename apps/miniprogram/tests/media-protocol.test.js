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
