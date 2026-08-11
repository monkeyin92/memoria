"use strict";

const { FrameReassembler, fragmentFrame } = require("./packet-framer");
const { SensitiveBuffer } = require("./sensitive-buffer");

const ENDPOINTS = Object.freeze(["proto-ver", "proto-session", "prov-scan", "prov-config", "prov-status", "memoria-bootstrap"]);

function protocolError(message, code = "PROTOCOL_UNSUPPORTED") {
  const error = new Error(message);
  error.code = code;
  return error;
}

function encodeUtf8(value) {
  const encoded = unescape(encodeURIComponent(value));
  const bytes = new Uint8Array(encoded.length);
  for (let index = 0; index < encoded.length; index += 1) bytes[index] = encoded.charCodeAt(index);
  return bytes;
}

/*
 * Transport interface:
 *   establishSecureSession() -> authenticated session
 *   request(endpoint, payload) -> decoded response
 *   writeWifiCredentials({ ssid, password })
 *   dispose()
 *
 * The default implementation intentionally has no securityAdapter or
 * protobuf codec.  This means the production path stops before the first
 * credential write until an audited Protocomm Security 1/2 implementation
 * and generated protocol codec are injected.
 */
class BleProvisioningTransport {
  constructor({
    adapter,
    deviceId,
    serviceId,
    writeCharacteristicId,
    notifyCharacteristicId,
    epoch,
    mtu = 20,
    securityAdapter = null,
    codec = null,
  } = {}) {
    this.adapter = adapter;
    this.deviceId = deviceId;
    this.serviceId = serviceId;
    this.writeCharacteristicId = writeCharacteristicId;
    this.notifyCharacteristicId = notifyCharacteristicId;
    this.epoch = epoch;
    this.mtu = mtu;
    this.securityAdapter = securityAdapter;
    this.codec = codec;
    this._secureSession = null;
    this._writeQueue = Promise.resolve();
    this._unsubscribeValue = null;
    this._reassembler = new FrameReassembler();
  }

  _requireAdapter() {
    if (!this.adapter || typeof this.adapter.write !== "function") {
      throw protocolError("BLE transport 不完整，已停止配网");
    }
  }

  _requireSecureSession() {
    if (!this._secureSession?.authenticated) {
      throw protocolError("Protocomm 安全会话尚未建立，已停止发送网络信息");
    }
  }

  async establishSecureSession() {
    if (
      !this.securityAdapter ||
      this.securityAdapter.auditStatus !== "audited" ||
      typeof this.securityAdapter.establishSession !== "function"
    ) {
      throw protocolError(
        "当前小程序没有经审计的 Protocomm crypto，不能假称 BLE 安全会话已完成",
      );
    }
    const session = await this.securityAdapter.establishSession({
      adapter: this.adapter,
      deviceId: this.deviceId,
      serviceId: this.serviceId,
      writeCharacteristicId: this.writeCharacteristicId,
      notifyCharacteristicId: this.notifyCharacteristicId,
      epoch: this.epoch,
    });
    if (!session || session.authenticated !== true) {
      throw protocolError("Protocomm 安全会话验证失败", "BLE_SESSION_REJECTED");
    }
    this._secureSession = session;
    return { authenticated: true, security: session.security || "protocomm" };
  }

  _encode(endpoint, payload) {
    if (!ENDPOINTS.includes(endpoint)) throw protocolError(`未知的配网端点 ${endpoint}`);
    if (!this.codec || typeof this.codec.encode !== "function") {
      throw protocolError("缺少经审计的 Protocomm protobuf codec，已停止 BLE 写入");
    }
    const encoded = this.codec.encode(endpoint, payload, this._secureSession);
    if (!(encoded instanceof Uint8Array) && !(encoded instanceof ArrayBuffer)) {
      throw protocolError("Protocomm codec 返回的帧无效");
    }
    return encoded instanceof Uint8Array ? encoded : new Uint8Array(encoded);
  }

  async _writeFrameBytes(bytes) {
    this._requireAdapter();
    const frames = fragmentFrame(bytes, { mtu: this.mtu });
    for (const frame of frames) {
      this._writeQueue = this._writeQueue.then(() =>
        this.adapter.write({
          deviceId: this.deviceId,
          serviceId: this.serviceId,
          characteristicId: this.writeCharacteristicId,
          value: frame,
          epoch: this.epoch,
        }),
      );
      await this._writeQueue;
    }
  }

  async send(endpoint, payload) {
    this._requireSecureSession();
    const encoded = this._encode(endpoint, payload);
    await this._writeFrameBytes(encoded);
  }

  async request(endpoint, payload) {
    this._requireSecureSession();
    if (!this.codec || typeof this.codec.decode !== "function") {
      throw protocolError("缺少经审计的 Protocomm protobuf codec，已停止读取 BLE 响应");
    }
    await this.send(endpoint, payload);
    // A real codec/adapter implementation owns response correlation and
    // notification decoding.  The default path never reaches this branch.
    throw protocolError("Protocomm response correlation 尚未接入", "PROTOCOL_UNSUPPORTED");
  }

  async writeWifiCredentials({ ssid, password } = {}) {
    if (typeof ssid !== "string" || !ssid || ssid.length > 128) {
      throw protocolError("Wi‑Fi 名称无效", "WIFI_INVALID");
    }
    if (typeof password !== "string" || password.length > 128) {
      throw protocolError("Wi‑Fi 密码无效", "WIFI_INVALID");
    }
    const sensitive = new SensitiveBuffer();
    sensitive.setText(password);
    try {
      await this.send("prov-config", {
        ssid,
        // The codec must consume this only inside the protected session. It
        // is never sent through api.js or serialized into a log/error.
        password: sensitive.getText(),
      });
    } finally {
      sensitive.clear();
    }
  }

  dispose() {
    this._secureSession?.dispose?.();
    this._secureSession = null;
    this._reassembler.reset();
    this._unsubscribeValue?.();
    this._unsubscribeValue = null;
    this._writeQueue = Promise.resolve();
  }
}

function createProvisioningTransport(options) {
  return new BleProvisioningTransport(options);
}

module.exports = {
  ENDPOINTS,
  BleProvisioningTransport,
  createProvisioningTransport,
  protocolError,
  encodeUtf8,
};
