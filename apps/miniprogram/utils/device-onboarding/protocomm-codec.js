"use strict";

const { Aes256Ctr, cryptoError, nacl, randomBytes, sha256 } = require("./crypto");

const ENDPOINT_UUIDS = Object.freeze({
  "proto-ver": 0xff50,
  "proto-session": 0xff51,
  "prov-scan": 0xff52,
  "prov-config": 0xff53,
  "prov-status": 0xff54,
  "memoria-bootstrap": 0xff55,
});

function utf8(value) {
  if (value instanceof Uint8Array) return value;
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(String(value));
  const encoded = unescape(encodeURIComponent(String(value)));
  const bytes = new Uint8Array(encoded.length);
  for (let index = 0; index < encoded.length; index += 1) bytes[index] = encoded.charCodeAt(index);
  return bytes;
}

function concat(...parts) {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const output = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function varint(value) {
  const output = [];
  let current = Number(value);
  do {
    const byte = current & 0x7f;
    current = Math.floor(current / 128);
    output.push(byte | (current ? 0x80 : 0));
  } while (current);
  return Uint8Array.from(output);
}

function fieldBytes(field, bytes) {
  return concat(varint((field << 3) | 2), varint(bytes.length), bytes);
}

function fieldVarint(field, value) {
  return concat(varint(field << 3), varint(value));
}

function readVarint(bytes, state) {
  let value = 0;
  let multiplier = 1;
  while (state.offset < bytes.length && multiplier <= 0x10000000000000) {
    const byte = bytes[state.offset++];
    value += (byte & 0x7f) * multiplier;
    if (!(byte & 0x80)) return value;
    multiplier *= 128;
  }
  throw cryptoError("Protocomm protobuf varint 无效");
}

function readFields(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const state = { offset: 0 };
  const fields = new Map();
  while (state.offset < bytes.length) {
    const key = readVarint(bytes, state);
    const field = Math.floor(key / 8);
    const wire = key & 7;
    let value;
    if (wire === 0) value = readVarint(bytes, state);
    else if (wire === 2) {
      const length = readVarint(bytes, state);
      if (length > bytes.length - state.offset) throw cryptoError("Protocomm protobuf 长度无效");
      value = bytes.slice(state.offset, state.offset + length);
      state.offset += length;
    } else throw cryptoError("Protocomm protobuf wire type 不受支持");
    if (!fields.has(field)) fields.set(field, []);
    fields.get(field).push({ wire, value });
  }
  return fields;
}

function onlyField(fields, number, wire) {
  const values = fields.get(number) || [];
  if (values.length !== 1 || values[0].wire !== wire) throw cryptoError("Protocomm protobuf 字段不符合契约");
  return values[0].value;
}

function sessionData(secPayload) {
  return concat(fieldVarint(2, 1), fieldBytes(11, secPayload));
}

function sec1Payload(message, field, payload) {
  return concat(fieldVarint(1, message), fieldBytes(field, payload));
}

function sessionCmd0(publicKey) {
  return sessionData(sec1Payload(0, 20, fieldBytes(1, publicKey)));
}

function sessionCmd1(proof) {
  return sessionData(sec1Payload(2, 22, fieldBytes(2, proof)));
}

function parseSessionResponse0(bytes) {
  const outer = readFields(bytes);
  const sec = readFields(onlyField(outer, 11, 2));
  if (onlyField(sec, 1, 0) !== 1) throw cryptoError("ESP32 未选择 Security 1");
  const response = readFields(onlyField(sec, 21, 2));
  const status = response.get(1);
  if (status && (status.length !== 1 || status[0].wire !== 0 || status[0].value !== 0)) {
    throw cryptoError("ESP32 Security 1 会话被拒绝");
  }
  return {
    devicePublicKey: onlyField(response, 2, 2),
    deviceRandom: onlyField(response, 3, 2),
  };
}

function parseSessionResponse1(bytes, clientPublicKey, cipher) {
  const outer = readFields(bytes);
  const sec = readFields(onlyField(outer, 11, 2));
  // Sec1MsgType.Session_Response1 is enum value 3. Value 1 is only
  // Session_Response0; accepting it here made the JS mock pass while every
  // real ESP-IDF response was rejected before decrypting the device proof.
  if (onlyField(sec, 1, 0) !== 3) throw cryptoError("ESP32 未完成 Security 1 会话");
  const response = readFields(onlyField(sec, 23, 2));
  const status = response.get(1);
  if (status && (status.length !== 1 || status[0].wire !== 0 || status[0].value !== 0)) {
    throw cryptoError("ESP32 Security 1 会话被拒绝");
  }
  const verified = cipher.update(onlyField(response, 3, 2));
  if (verified.length !== clientPublicKey.length || verified.some((value, index) => value !== clientPublicKey[index])) {
    throw cryptoError("ESP32 Security 1 设备证明不匹配");
  }
}

function jsonEnvelope(payload) {
  return fieldBytes(1, utf8(JSON.stringify(payload || {})));
}

function parseJsonEnvelope(bytes) {
  const fields = readFields(bytes);
  const value = onlyField(fields, 1, 2);
  let text;
  if (typeof TextDecoder !== "undefined") text = new TextDecoder().decode(value);
  else {
    let binary = "";
    value.forEach((byte) => { binary += String.fromCharCode(byte); });
    text = decodeURIComponent(escape(binary));
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw Object.assign(new Error("Protocomm 业务响应不是合法 JSON"), { code: "PROTOCOL_UNSUPPORTED", cause: error });
  }
}

function xor(left, right) {
  const output = new Uint8Array(left.length);
  for (let index = 0; index < left.length; index += 1) output[index] = left[index] ^ right[index];
  return output;
}

class Security1Adapter {
  constructor({ pop } = {}) {
    this.pop = utf8(pop || "");
    this.auditStatus = "audited";
  }

  async establishSession({ adapter, deviceId, serviceId, endpointCharacteristics, epoch }) {
    const endpoint = endpointCharacteristics?.["proto-session"];
    if (!endpoint?.writeCharacteristicId) throw cryptoError("缺少 Protocomm proto-session 特征");
    const clientSecret = await randomBytes(32);
    const keyPair = nacl.box.keyPair.fromSecretKey(clientSecret);
    const write = async (bytes) => {
      await adapter.write({
        deviceId,
        serviceId,
        characteristicId: endpoint.writeCharacteristicId,
        value: bytes.buffer,
        epoch,
      });
      return adapter.read({
        deviceId,
        serviceId,
        characteristicId: endpoint.writeCharacteristicId,
        epoch,
      });
    };
    const response0 = parseSessionResponse0(await write(sessionCmd0(keyPair.publicKey)));
    if (response0.devicePublicKey.length !== 32 || response0.deviceRandom.length !== 16) {
      throw cryptoError("ESP32 Security 1 响应长度无效");
    }
    // ESP-IDF derives the AES key from the raw X25519 shared secret.  TweetNaCl
    // box.before() returns the XSalsa20 precomputed box key, which is a
    // different value and makes ESP-IDF reject the client proof.
    const shared = nacl.scalarMult(clientSecret, response0.devicePublicKey);
    const updatedKey = this.pop.length ? xor(shared, sha256(this.pop)) : shared;
    const cipher = new Aes256Ctr(updatedKey, response0.deviceRandom);
    const response1 = await write(sessionCmd1(cipher.update(response0.devicePublicKey)));
    parseSessionResponse1(response1, keyPair.publicKey, cipher);
    clientSecret.fill(0);
    shared.fill(0);
    updatedKey.fill(0);
    return {
      authenticated: true,
      security: "protocomm-security1",
      encrypt: (bytes) => cipher.update(bytes),
      decrypt: (bytes) => cipher.update(bytes),
      dispose: () => cipher.clear(),
    };
  }
}

const codec = {
  endpointUuids: ENDPOINT_UUIDS,
  encode(endpoint, payload, session) {
    if (endpoint === "proto-ver") return new Uint8Array(0);
    const plain = jsonEnvelope(payload);
    return session?.encrypt ? session.encrypt(plain) : plain;
  },
  decode(endpoint, payload, session) {
    if (endpoint === "proto-ver") {
      let text = "";
      const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
      for (const byte of bytes) text += String.fromCharCode(byte);
      return text;
    }
    const plain = session?.decrypt ? session.decrypt(payload) : payload;
    return parseJsonEnvelope(plain);
  },
};

module.exports = { ENDPOINT_UUIDS, Security1Adapter, codec, parseJsonEnvelope, readFields };
