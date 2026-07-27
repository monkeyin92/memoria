const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  GENERATION_HEADER_SIZE,
  GENERATION_PROTOCOL_VERSION,
  HEADER_SIZE,
  MAX_AUDIO_PAYLOAD_BYTES,
  PROTOCOL_VERSION,
  FRAME_TYPE,
  decodePcmFrame,
  encodePcmFrame,
} = require("../utils/media-protocol");

const contract = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "../../../packages/contracts/miniprogram-media.json"),
    "utf8",
  ),
);

test("PCM media constants match the shared contract", () => {
  assert.equal(PROTOCOL_VERSION, contract.binary.protocol_version);
  assert.equal(
    GENERATION_PROTOCOL_VERSION,
    contract.binary.generation_protocol_version,
  );
  assert.equal(HEADER_SIZE, contract.binary.header_size);
  assert.equal(GENERATION_HEADER_SIZE, contract.binary.generation_header_size);
  assert.equal(
    MAX_AUDIO_PAYLOAD_BYTES,
    contract.binary.max_audio_payload_bytes,
  );
  assert.deepEqual(FRAME_TYPE, {
    UPLINK_AUDIO: contract.binary.frame_types.uplink_audio,
    DOWNLINK_AUDIO: contract.binary.frame_types.downlink_audio,
  });
});

for (const vector of contract.golden_frames) {
  test(`PCM media golden frame stays compatible: ${vector.name}`, () => {
    const type = FRAME_TYPE[vector.frame_type.toUpperCase()];
    const encoded =
      vector.generation_id === null
        ? encodePcmFrame(
            type,
            vector.sequence,
            vector.timestamp_ms,
            Buffer.from(vector.payload_hex, "hex"),
          )
        : Buffer.from(vector.frame_hex, "hex");

    assert.equal(Buffer.from(encoded).toString("hex"), vector.frame_hex);
    const decoded = decodePcmFrame(
      Buffer.from(vector.frame_hex, "hex"),
      type,
    );
    assert.equal(decoded.sequence, vector.sequence);
    assert.equal(decoded.timestampMs, vector.timestamp_ms);
    assert.equal(decoded.generationId, vector.generation_id);
    assert.equal(
      Buffer.from(decoded.payload).toString("hex"),
      vector.payload_hex,
    );
  });
}

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
