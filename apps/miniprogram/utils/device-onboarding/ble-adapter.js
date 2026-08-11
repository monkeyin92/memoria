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
        services: serviceUuid ? [serviceUuid] : [],
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
      const writable = characteristics.filter(characteristicIsWritable);
      const notifiable = characteristics.filter(characteristicIsNotifiable);
      if (writable.length !== 1 || notifiable.length !== 1) {
        throw bleError("机器人 BLE 特征不明确，已停止兼容性尝试", "PROTOCOL_UNSUPPORTED");
      }
      await this.notify({
        deviceId,
        serviceId: service.uuid,
        characteristicId: notifiable[0].uuid,
        epoch,
      });
      return {
        deviceId,
        serviceId: service.uuid,
        writeCharacteristicId: writable[0].uuid,
        notifyCharacteristicId: notifiable[0].uuid,
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
  matchesDevice,
};
