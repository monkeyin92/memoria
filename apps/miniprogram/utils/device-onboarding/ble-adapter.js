"use strict";

/*
 * The only module allowed to call wx.* Bluetooth APIs for device onboarding.
 * Business pages see a small transport-shaped interface and never touch GATT
 * callbacks directly.  Every callback captures an attempt epoch; after a new
 * attempt or dispose, late events become no-ops.
 */

function bleError(message, code = "BLE_CONNECTION_FAILED") {
  const error = new Error(message);
  error.code = code;
  return error;
}

function callWx(wxApi, method, options = {}, code = "BLE_CONNECTION_FAILED") {
  if (!wxApi || typeof wxApi[method] !== "function") {
    return Promise.reject(bleError(`当前微信运行时不支持 ${method}`, "BLUETOOTH_UNSUPPORTED"));
  }
  return new Promise((resolve, reject) => {
    wxApi[method]({
      ...options,
      success: resolve,
      fail: (error) => reject(Object.assign(bleError(error?.errMsg || `${method} 调用失败`, code), { cause: error })),
    });
  });
}

function normalizeName(value) {
  return String(value || "").trim().toUpperCase();
}

function matchesDevice(device, { bleName = "", displayTail = "" } = {}) {
  const name = normalizeName(device?.name || device?.localName);
  const expectedName = normalizeName(bleName);
  const expectedTail = normalizeName(displayTail).replace(/[^A-Z0-9]/g, "");
  if (expectedName && name !== expectedName) return false;
  if (expectedTail && !name.replace(/[^A-Z0-9]/g, "").includes(expectedTail)) return false;
  return Boolean(device?.deviceId);
}

function characteristicIsWritable(characteristic) {
  return characteristic?.properties?.write === true || characteristic?.properties?.writeNoResponse === true;
}

function characteristicIsNotifiable(characteristic) {
  return characteristic?.properties?.notify === true || characteristic?.properties?.indicate === true;
}

function initialAttPayload(wxApi) {
  try {
    // iOS/CoreBluetooth negotiates ATT MTU automatically and commonly exposes
    // a 185-byte MTU (182-byte value payload). WeChat does not provide
    // setBLEMTU on iOS, so defaulting to 20 locally rejects Security 1's
    // 43-byte command before it can reach an otherwise compatible robot.
    if (wxApi?.getSystemInfoSync?.()?.platform === "ios") return 182;
  } catch {
    // Unknown runtimes retain the BLE 4.0-safe 20-byte payload below.
  }
  return 20;
}

function normalizeUuid(value) {
  return String(value || "").replace(/-/g, "").toLowerCase();
}

function toArrayBuffer(value) {
  if (value instanceof ArrayBuffer) return value;
  if (ArrayBuffer.isView(value)) {
    return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
  }
  return null;
}

function hasBleValue(value) {
  const buffer = toArrayBuffer(value);
  return buffer && buffer.byteLength > 0 ? buffer : null;
}

function endpointUuid(serviceUuid, endpointId) {
  const normalized = normalizeUuid(serviceUuid);
  if (normalized.length !== 32 || !Number.isInteger(endpointId)) return "";
  const low = (endpointId & 0xff).toString(16).padStart(2, "0");
  const high = ((endpointId >>> 8) & 0xff).toString(16).padStart(2, "0");
  const bytes = `${normalized.slice(0, 24)}${low}${high}${normalized.slice(28)}`;
  return `${bytes.slice(0, 8)}-${bytes.slice(8, 12)}-${bytes.slice(12, 16)}-${bytes.slice(16, 20)}-${bytes.slice(20)}`;
}

function reversedEndpointUuid(serviceUuid, endpointId) {
  const normalized = normalizeUuid(serviceUuid);
  if (normalized.length !== 32 || !Number.isInteger(endpointId)) return "";
  const low = (endpointId & 0xff).toString(16).padStart(2, "0");
  const high = ((endpointId >>> 8) & 0xff).toString(16).padStart(2, "0");
  // CoreBluetooth exposes the NimBLE UUID bytes in reverse order.  The
  // endpoint's little-endian uint16 therefore appears in bytes 2..3 of the
  // textual UUID, rather than at the same offset as the service UUID.
  const bytes = `${normalized.slice(0, 4)}${high}${low}${normalized.slice(8)}`;
  return `${bytes.slice(0, 8)}-${bytes.slice(8, 12)}-${bytes.slice(12, 16)}-${bytes.slice(16, 20)}-${bytes.slice(20)}`;
}

function endpointUuidCandidates(serviceUuid, endpointId) {
  return [endpointUuid(serviceUuid, endpointId), reversedEndpointUuid(serviceUuid, endpointId)]
    .filter(Boolean);
}

class WxBleAdapter {
  constructor(wxApi = globalThis.wx) {
    this.wx = wxApi;
    this._epoch = 0;
    this._deviceId = "";
    this._listeners = [];
    this._discoveryTimer = null;
    this._cancelDiscovery = null;
    this._disposed = false;
  }

  beginAttempt() {
    this._cancelDiscovery?.();
    this._disposed = false;
    this._epoch += 1;
    return this._epoch;
  }

  currentEpoch() {
    return this._epoch;
  }

  isCurrent(epoch) {
    return !this._disposed && epoch === this._epoch;
  }

  _assertCurrent(epoch) {
    if (!this.isCurrent(epoch)) throw bleError("BLE 操作已过期", "BLE_ATTEMPT_STALE");
  }

  _register(eventMethod, offMethod, callback) {
    if (typeof this.wx?.[eventMethod] !== "function") {
      throw bleError(`当前微信运行时不支持 ${eventMethod}`, "BLUETOOTH_UNSUPPORTED");
    }
    this.wx[eventMethod](callback);
    this._listeners.push({ eventMethod, offMethod, callback });
    return () => this._removeListener(eventMethod, offMethod, callback);
  }

  _removeListener(eventMethod, offMethod, callback) {
    const index = this._listeners.findIndex(
      (entry) => entry.eventMethod === eventMethod && entry.callback === callback,
    );
    if (index >= 0) this._listeners.splice(index, 1);
    if (typeof this.wx?.[offMethod] === "function") {
      try {
        this.wx[offMethod](callback);
      } catch {
        // Listener removal is best effort on older base libraries.
      }
    }
  }

  async open() {
    const epoch = this.beginAttempt();
    try {
      await callWx(this.wx, "openBluetoothAdapter", {}, "BLUETOOTH_DISABLED");
      this._assertCurrent(epoch);
      return epoch;
    } catch (error) {
      if (error?.cause?.errCode === 10001 || /not available|not init|蓝牙/i.test(error?.message || "")) {
        error.code = "BLUETOOTH_DISABLED";
      }
      throw error;
    }
  }

  async discover({ serviceUuid, bleName, displayTail, timeoutMs = 12_000, epoch = this._epoch } = {}) {
    this._assertCurrent(epoch);
    if (!serviceUuid && !bleName && !displayTail) {
      throw bleError("缺少 BLE 设备筛选条件", "BLE_DEVICE_NOT_FOUND");
    }
    const devices = await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        if (this._discoveryTimer) clearTimeout(this._discoveryTimer);
        this._discoveryTimer = null;
        if (this._cancelDiscovery === cancel) this._cancelDiscovery = null;
        this._removeListener("onBluetoothDeviceFound", "offBluetoothDeviceFound", onFound);
        try {
          this.wx?.stopBluetoothDevicesDiscovery?.({ complete() {} });
        } catch {
          // The connection cleanup path also stops discovery.
        }
        callback(value);
      };
      const cancel = () => finish(reject, bleError("BLE 操作已过期", "BLE_ATTEMPT_STALE"));
      this._cancelDiscovery = cancel;
      const onFound = (event) => {
        if (!this.isCurrent(epoch)) return;
        const candidates = Array.isArray(event?.devices) ? event.devices : [event];
        const device = candidates.find((item) => matchesDevice(item, { bleName, displayTail }));
        if (device) finish(resolve, device);
      };
      try {
        this._register("onBluetoothDeviceFound", "offBluetoothDeviceFound", onFound);
      } catch (error) {
        finish(reject, error);
        return;
      }
      this._discoveryTimer = setTimeout(() => {
        finish(reject, bleError("没有找到目标机器人", "BLE_DEVICE_NOT_FOUND"));
      }, Math.max(100, timeoutMs));
      if (typeof this.wx?.startBluetoothDevicesDiscovery !== "function") {
        finish(reject, bleError("当前微信运行时不支持蓝牙搜索", "BLUETOOTH_UNSUPPORTED"));
        return;
      }
      this.wx.startBluetoothDevicesDiscovery({
        // Do not rely on the platform-level service filter here.  iOS/CoreBluetooth
        // can expose ESP-IDF's little-endian UUID bytes in reversed textual order,
        // which makes a valid Protocomm advertisement invisible to discovery.
        // The device name is filtered above; connect() still validates the exact
        // service and every required characteristic before any protocol request.
        services: [],
        allowDuplicatesKey: false,
        success: () => {
          if (!this.isCurrent(epoch)) finish(reject, bleError("BLE 搜索已过期", "BLE_ATTEMPT_STALE"));
        },
        fail: (error) => finish(reject, Object.assign(bleError(error?.errMsg || "BLE 搜索失败", "BLE_DEVICE_NOT_FOUND"), { cause: error })),
      });
    });
    this._assertCurrent(epoch);
    return devices;
  }

  subscribeAdapterState(callback, { epoch = this._epoch } = {}) {
    if (typeof callback !== "function") throw new TypeError("蓝牙适配器状态回调必须是函数");
    const wrapped = (event) => {
      if (!this.isCurrent(epoch)) return;
      callback(event);
    };
    return this._register("onBluetoothAdapterStateChange", "offBluetoothAdapterStateChange", wrapped);
  }

  async connect(deviceId, { serviceUuid, epoch = this._epoch } = {}) {
    this._assertCurrent(epoch);
    if (typeof deviceId !== "string" || !deviceId) throw bleError("BLE deviceId 无效");
    this._deviceId = deviceId;
    await callWx(this.wx, "createBLEConnection", { deviceId, timeout: 10_000 }, "BLE_CONNECTION_FAILED");
    this._assertCurrent(epoch);

    let removeDisconnectedListener = () => {};
    const disconnected = new Promise((_, reject) => {
      const onConnectionStateChange = (event) => {
        if (!this.isCurrent(epoch) || event?.deviceId !== deviceId) return;
        if (event?.connected === false) reject(bleError("BLE 连接中断", "BLE_DISCONNECTED"));
      };
      try {
        removeDisconnectedListener = this._register(
          "onBLEConnectionStateChange",
          "offBLEConnectionStateChange",
          onConnectionStateChange,
        );
      } catch {
        // Older base libraries may not expose this optional event. The active
        // operation is still fenced by the attempt epoch.
      }
    });
    try {
      const servicesResult = await Promise.race([
        callWx(this.wx, "getBLEDeviceServices", { deviceId }, "BLE_CONNECTION_FAILED"),
        disconnected,
      ]);
      this._assertCurrent(epoch);
      const services = Array.isArray(servicesResult?.services) ? servicesResult.services : [];
      const normalizedExpected = normalizeName(serviceUuid);
      const service = services.find((item) => normalizeName(item.uuid) === normalizedExpected) ||
        (services.length === 1 ? services[0] : null);
      if (!service?.uuid) throw bleError("机器人缺少兼容的 BLE 服务", "PROTOCOL_UNSUPPORTED");

      const characteristicsResult = await Promise.race([
        callWx(this.wx, "getBLEDeviceCharacteristics", { deviceId, serviceId: service.uuid }, "BLE_CONNECTION_FAILED"),
        disconnected,
      ]);
      this._assertCurrent(epoch);
      const characteristics = Array.isArray(characteristicsResult?.characteristics)
        ? characteristicsResult.characteristics
        : [];
      const endpointIds = {
        "proto-ver": 0xff50,
        "proto-session": 0xff51,
        "prov-scan": 0xff52,
        "prov-config": 0xff53,
        "prov-status": 0xff54,
        "memoria-bootstrap": 0xff55,
      };
      const endpointCharacteristics = {};
      for (const [name, id] of Object.entries(endpointIds)) {
        const expectedUuids = new Set(endpointUuidCandidates(service.uuid, id).map(normalizeUuid));
        const characteristic = characteristics.find((item) => expectedUuids.has(normalizeUuid(item.uuid)));
        if (!characteristic || !characteristicIsWritable(characteristic)) {
          throw bleError(`机器人缺少 Protocomm 端点 ${name}`, "PROTOCOL_UNSUPPORTED");
        }
        endpointCharacteristics[name] = {
          writeCharacteristicId: characteristic.uuid,
          readCharacteristicId: characteristic.uuid,
        };
      }
      let mtu = initialAttPayload(this.wx);
      if (typeof this.wx?.setBLEMTU === "function") {
        try {
          const mtuResult = await callWx(this.wx, "setBLEMTU", { deviceId, mtu: 247 }, "BLE_CONNECTION_FAILED");
          mtu = Math.max(20, Number(mtuResult?.mtu || 247) - 3);
        } catch {
          // The default ATT payload remains valid for small version/session reads.
        }
      }
      if (typeof this.wx?.getBLEMTU === "function") {
        try {
          const mtuResult = await callWx(this.wx, "getBLEMTU", { deviceId, writeType: "write" }, "BLE_CONNECTION_FAILED");
          if (Number.isFinite(Number(mtuResult?.mtu))) {
            mtu = Math.max(20, Number(mtuResult.mtu) - 3);
          }
        } catch {
          // Older base libraries and iOS may not expose a readable MTU.
        }
      }
      return {
        deviceId,
        serviceId: service.uuid,
        endpointCharacteristics,
        mtu,
        epoch,
      };
    } finally {
      removeDisconnectedListener();
    }
  }

  async notify({ deviceId = this._deviceId, serviceId, characteristicId, epoch = this._epoch } = {}) {
    this._assertCurrent(epoch);
    await callWx(this.wx, "notifyBLECharacteristicValueChanged", {
      deviceId,
      serviceId,
      characteristicId,
      state: true,
    }, "BLE_CONNECTION_FAILED");
    this._assertCurrent(epoch);
  }

  subscribeValue(callback, { epoch = this._epoch } = {}) {
    if (typeof callback !== "function") throw new TypeError("BLE notify callback 必须是函数");
    const wrapped = (event) => {
      if (!this.isCurrent(epoch)) return;
      callback(event);
    };
    return this._register("onBLECharacteristicValueChange", "offBLECharacteristicValueChange", wrapped);
  }

  async write({ deviceId = this._deviceId, serviceId, characteristicId, value, epoch = this._epoch } = {}) {
    this._assertCurrent(epoch);
    if (!(value instanceof ArrayBuffer)) throw new TypeError("BLE write 必须使用 ArrayBuffer");
    await callWx(this.wx, "writeBLECharacteristicValue", {
      deviceId,
      serviceId,
      characteristicId,
      value,
    }, "BLE_CONNECTION_FAILED");
    this._assertCurrent(epoch);
  }

  async read({ deviceId = this._deviceId, serviceId, characteristicId, epoch = this._epoch, timeoutMs = 5000 } = {}) {
    this._assertCurrent(epoch);
    if (typeof this.wx?.readBLECharacteristicValue !== "function") {
      throw bleError("当前微信运行时不支持 BLE 读取", "BLUETOOTH_UNSUPPORTED");
    }
    const value = await new Promise((resolve, reject) => {
      let timer = null;
      let settled = false;
      const finish = (callback, result) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        removeListener?.();
        callback(result);
      };
      const onValue = (event) => {
        if (!this.isCurrent(epoch) || event?.deviceId !== deviceId || event?.characteristicId !== characteristicId) return;
        // WeChat's read success callback may be emitted before the actual GATT
        // value-change event, and some iOS base libraries also emit an empty
        // value-change event first. Never let an empty/stale value terminate a
        // Protocomm response read; the response itself is always non-empty.
        const value = hasBleValue(event?.value);
        if (value) finish(resolve, value);
      };
      let removeListener = () => {};
      try {
        removeListener = this._register("onBLECharacteristicValueChange", "offBLECharacteristicValueChange", onValue);
      } catch (error) {
        finish(reject, error);
        return;
      }
      timer = setTimeout(() => finish(reject, bleError("读取 Protocomm 响应超时", "BLE_RESPONSE_TIMEOUT")), timeoutMs);
      this.wx.readBLECharacteristicValue({
        deviceId,
        serviceId,
        characteristicId,
        success: (result) => {
          // The documented success result normally contains only errMsg. If a
          // platform supplies a value here, accept it only when it is a real,
          // non-empty ArrayBuffer; otherwise wait for onBLECharacteristicValueChange.
          const value = hasBleValue(result?.value);
          if (value) finish(resolve, value);
        },
        fail: (error) => finish(reject, Object.assign(bleError(error?.errMsg || "BLE 读取失败"), { cause: error })),
      });
    });
    this._assertCurrent(epoch);
    return toArrayBuffer(value) || value;
  }

  async disconnect() {
    const deviceId = this._deviceId;
    if (!deviceId) return;
    try {
      await callWx(this.wx, "closeBLEConnection", { deviceId }, "BLE_CONNECTION_FAILED");
    } catch {
      // dispose still removes callbacks and invalidates the epoch.
    }
    this._deviceId = "";
  }

  dispose() {
    this._disposed = true;
    this._cancelDiscovery?.();
    this._cancelDiscovery = null;
    this._epoch += 1;
    if (this._discoveryTimer) clearTimeout(this._discoveryTimer);
    this._discoveryTimer = null;
    for (const entry of [...this._listeners]) {
      this._removeListener(entry.eventMethod, entry.offMethod, entry.callback);
    }
    try {
      this.wx?.stopBluetoothDevicesDiscovery?.({ complete() {} });
    } catch {
      // Best effort.
    }
    const deviceId = this._deviceId;
    this._deviceId = "";
    if (deviceId) {
      try {
        this.wx?.closeBLEConnection?.({ deviceId, complete() {} });
      } catch {
        // Best effort.
      }
    }
    try {
      this.wx?.closeBluetoothAdapter?.({ complete() {} });
    } catch {
      // Best effort.
    }
  }
}

module.exports = {
  WxBleAdapter,
  bleError,
  endpointUuid,
  endpointUuidCandidates,
  matchesDevice,
};
