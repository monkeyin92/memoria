"use strict";

const defaultApi = require("../api");
const { WxBleAdapter } = require("./ble-adapter");
const { createProvisioningTransport } = require("./provisioning-transport");
const { parseDeviceQr } = require("./qr-code");
const { SensitiveBuffer } = require("./sensitive-buffer");
const { normalizeWifiNetworks, getConnectedWifi } = require("./wifi-model");
const { isActivationReady, normalizeActivationResponse } = require("./contracts");
const {
  transition,
  clientStateForServerState,
  isExpired,
  errorMessage,
  errorCode,
  progressIndex,
  activationIsLateOrReady,
  PROGRESS_STEPS,
} = require("./state");
const {
  saveOnboardingSessionId,
  clearOnboardingSessionId,
} = require("./session-store");

function operationId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function sameId(left, right) {
  return typeof left === "string" && left.length > 0 && left === right;
}

class OnboardingController {
  constructor({
    apiClient = defaultApi,
    bleAdapterFactory = () => new WxBleAdapter(),
    transportFactory = (options) => createProvisioningTransport(options),
    clientOnboardingId = operationId("client_onb"),
    now = () => Date.now(),
    onChange = null,
  } = {}) {
    this.api = apiClient;
    this.bleAdapterFactory = bleAdapterFactory;
    this.transportFactory = transportFactory;
    this.clientOnboardingId = clientOnboardingId;
    this.now = now;
    this.onChange = onChange;

    this._state = "prepare";
    this._attemptEpoch = 0;
    this._busy = false;
    this._error = "";
    this._errorCode = "";
    this._session = null;
    this._claim = null;
    this._binding = null;
    this._activation = null;
    this._wifiNetworks = [];
    this._adapter = null;
    this._transport = null;
    this._wifiPassword = new SensitiveBuffer();
    this._wifiSsid = "";
    this._activationTimer = null;
    this._disposed = false;
    this._emit();
  }

  get state() {
    return this._state;
  }

  get session() {
    return this._session;
  }

  get claim() {
    return this._claim;
  }

  snapshot() {
    return {
      state: this._state,
      stateLabel: require("./state").STATE_LABELS[this._state],
      busy: this._busy,
      error: this._error,
      errorCode: this._errorCode,
      session: this._session,
      claim: this._claim,
      binding: this._binding,
      activation: this._activation,
      wifiNetworks: this._wifiNetworks,
      device: this._session?.device || this._claim?.device || null,
      provisioning: this._session?.provisioning || null,
      progressSteps: PROGRESS_STEPS,
      progressIndex: progressIndex({
        state: this._session?.state,
        networkStatus: this._session?.network_status,
        activationStatus: this._activation?.status || this._session?.activation_status,
      }),
      activationReady: Boolean(this._activation && isActivationReady(this._activation.status)),
    };
  }

  _emit() {
    try {
      this.onChange?.(this.snapshot());
    } catch {
      // A destroyed page must not break the controller's fences.
    }
  }

  _setState(next, { force = false } = {}) {
    if (next === this._state) {
      this._emit();
      return;
    }
    this._state = force ? next : transition(this._state, next);
    this._error = "";
    this._errorCode = "";
    this._emit();
  }

  _setError(error, { keepState = true } = {}) {
    this._errorCode = errorCode(error);
    this._error = errorMessage(error);
    if (!keepState) this._state = "scan";
    this._emit();
  }

  _beginAttempt() {
    this._attemptEpoch += 1;
    return this._attemptEpoch;
  }

  _isCurrent(epoch) {
    return !this._disposed && epoch === this._attemptEpoch;
  }

  _setBusy(value) {
    this._busy = value;
    this._emit();
  }

  startScan() {
    if (this._state === "prepare" || this._state === "complete") {
      this._setState("scan", { force: this._state === "complete" });
      return;
    }
    if (this._state !== "scan") this._setState("scan", { force: true });
  }

  async introspectQr(rawPayload) {
    const epoch = this._beginAttempt();
    this.startScan();
    this._setBusy(true);
    try {
      // Local parsing is only a shape/version gate.  The exact raw payload is
      // sent unchanged to the server for signature and revocation checks.
      parseDeviceQr(rawPayload);
      const session = await this.api.introspectDeviceQr({
        qrPayload: rawPayload,
        clientOnboardingId: this.clientOnboardingId,
      });
      if (!this._isCurrent(epoch)) return null;
      this._session = session;
      this._claim = null;
      this._binding = null;
      this._activation = null;
      this._qrPayload = "";
      saveOnboardingSessionId(session.onboarding_session_id);
      this._setState("device_verified", { force: true });
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) {
        this._qrPayload = "";
        this._setError(error, { keepState: false });
      }
      return null;
    } finally {
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  async connectBle() {
    if (!this._session?.provisioning || !this._session?.device) {
      const error = new Error("请先完成设备识别");
      error.code = "QR_INVALID";
      this._setError(error);
      return null;
    }
    const epoch = this._beginAttempt();
    this._setState("ble", { force: true });
    this._setBusy(true);
    this._disposeBle();
    const adapter = this.bleAdapterFactory();
    this._adapter = adapter;
    try {
      const adapterEpoch = await adapter.open();
      if (!this._isCurrent(epoch)) return null;
      const device = await adapter.discover({
        serviceUuid: this._session.provisioning.service_uuid,
        bleName: this._session.provisioning.ble_name,
        displayTail: this._session.device.display_tail,
        epoch: adapterEpoch,
      });
      if (!this._isCurrent(epoch)) return null;
      const connection = await adapter.connect(device.deviceId, {
        serviceUuid: this._session.provisioning.service_uuid,
        epoch: adapterEpoch,
      });
      if (!this._isCurrent(epoch)) return null;
      const transport = this.transportFactory({
        adapter,
        ...connection,
        protocolVersion: this._session.provisioning.protocol_version,
      });
      this._transport = transport;
      await transport.establishSecureSession();
      if (!this._isCurrent(epoch)) return null;
      this._setState("wifi", { force: true });
      await this.loadWifiNetworks();
      return connection;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      this._disposeBle();
      return null;
    } finally {
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  async loadWifiNetworks() {
    if (!this._transport) return [];
    try {
      const response = await this._transport.request("prov-scan", {});
      const networks = normalizeWifiNetworks(response?.networks || response);
      this._wifiNetworks = networks;
      this._emit();
      return networks;
    } catch (error) {
      this._setError(error);
      return [];
    }
  }

  async phoneWifi() {
    return getConnectedWifi();
  }

  setWifiCredentials({ ssid, password } = {}) {
    if (typeof ssid !== "string" || !ssid || ssid.length > 128) {
      const error = new Error("请先选择或输入 Wi‑Fi 名称");
      error.code = "WIFI_INVALID";
      throw error;
    }
    if (typeof password !== "string" || password.length > 128) {
      const error = new Error("请填写 Wi‑Fi 密码");
      error.code = "WIFI_INVALID";
      throw error;
    }
    this._wifiSsid = ssid;
    this._wifiPassword.setText(password);
  }

  async provisionWifi({ ssid, password } = {}) {
    const epoch = this._beginAttempt();
    this._setState("wifi", { force: true });
    this._setBusy(true);
    try {
      this.setWifiCredentials({ ssid, password });
      if (!this._transport) {
        const error = new Error("BLE 安全配网会话不可用");
        error.code = "PROTOCOL_UNSUPPORTED";
        throw error;
      }
      await this._transport.writeWifiCredentials({
        ssid: this._wifiSsid,
        password: this._wifiPassword.getText(),
      });
      if (!this._isCurrent(epoch)) return false;
      this._setState("progress", { force: true });
      this._setBusy(false);
      await this.refreshSession({ preserveProgress: true });
      return true;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return false;
    } finally {
      // Passwords are cleared even on failure; a retry re-enters them in the
      // page instance and never reuses a stale sensitive value.
      this.clearSensitiveInput();
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  async refreshSession({ preserveProgress = false } = {}) {
    const sessionId = this._session?.onboarding_session_id;
    if (!sessionId) return null;
    const epoch = this._beginAttempt();
    try {
      const session = await this.api.getOnboardingSession(sessionId);
      if (!this._isCurrent(epoch)) return null;
      if (!sameId(session.onboarding_session_id, sessionId)) {
        throw new Error("服务端返回了不属于当前启用会话的数据");
      }
      this._session = session;
      const nextState = clientStateForServerState(session.state);
      if (nextState && !(preserveProgress && this._state === "progress" && nextState === "wifi")) {
        this._setState(nextState, { force: true });
      } else {
        this._emit();
      }
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    }
  }

  async reserveClaim() {
    const session = this._session;
    if (!session) return null;
    if (isExpired(session.expires_at, this.now())) {
      const error = new Error("本次认领已超时");
      error.code = "CLAIM_EXPIRED";
      this._setError(error);
      return null;
    }
    const epoch = this._beginAttempt();
    this._setState("claim", { force: true });
    this._setBusy(true);
    try {
      const claim = await this.api.reserveDeviceClaim({
        onboardingSessionId: session.onboarding_session_id,
        deviceId: session.device.device_id,
        // The key must survive a lost response and page reconstruction.  It is
        // an operation identifier, not an authorization secret.
        idempotencyKey: `claim-${session.onboarding_session_id}`,
        expectedStateVersion: session.state_version || undefined,
      });
      if (!this._isCurrent(epoch)) return null;
      if (
        !sameId(claim.onboarding_session_id, session.onboarding_session_id) ||
        !sameId(claim.device_id, session.device.device_id)
      ) {
        throw new Error("服务端返回的认领不属于当前设备");
      }
      if (isExpired(claim.expires_at, this.now())) {
        const error = new Error("认领保留已过期");
        error.code = "CLAIM_EXPIRED";
        throw error;
      }
      this._claim = claim;
      this._setState("claim", { force: true });
      return claim;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    } finally {
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  beginInitialize() {
    if (!this._claim) {
      const error = new Error("请先完成设备认领");
      error.code = "CLAIM_CONFLICT";
      this._setError(error);
      return false;
    }
    this._setState("initialize", { force: true });
    return true;
  }

  onBindingCreated(manifest) {
    if (!manifest || typeof manifest.device_id !== "string" || typeof manifest.binding_id !== "string") {
      const error = new Error("绑定清单无效，无法等待激活");
      error.code = "BINDING_FAILED";
      this._setError(error);
      return false;
    }
    this._binding = manifest;
    this._setState("activation", { force: true });
    this.refreshActivation();
    return true;
  }

  async refreshActivation() {
    const deviceId = this._binding?.device_id || this._session?.device?.device_id || this._claim?.device_id;
    if (!deviceId || typeof this.api.getActivationStatus !== "function") {
      const error = new Error("激活状态接口不可用，完成页保持等待");
      error.code = "ACTIVATION_ACK_TIMEOUT";
      this._setError(error);
      return null;
    }
    const epoch = this._beginAttempt();
    try {
      const result = await this.api.getActivationStatus(deviceId);
      if (!this._isCurrent(epoch)) return null;
      const status = normalizeActivationResponse(result);
      if (activationIsLateOrReady(this._activation, status)) return this._activation;
      this._activation = status;
      if (isActivationReady(status.status)) {
        this._setState("complete", { force: true });
        clearOnboardingSessionId();
      } else {
        this._setState("activation", { force: true });
      }
      return status;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    }
  }

  startActivationPolling(intervalMs = 3000) {
    this.stopActivationPolling();
    this._activationTimer = setInterval(() => {
      if (this._state !== "activation") {
        this.stopActivationPolling();
        return;
      }
      this.refreshActivation();
    }, Math.max(1000, intervalMs));
  }

  stopActivationPolling() {
    if (this._activationTimer) clearInterval(this._activationTimer);
    this._activationTimer = null;
  }


  clearSensitiveInput() {
    this._wifiPassword.clear();
    this._wifiSsid = "";
  }

  pause() {
    this._attemptEpoch += 1;
    this.stopActivationPolling();
    this.clearSensitiveInput();
    this._disposeBle();
  }

  async cancel() {
    const sessionId = this._session?.onboarding_session_id;
    const epoch = this._beginAttempt();
    this._setBusy(true);
    try {
      if (sessionId) await this.api.cancelOnboardingSession(sessionId);
      if (!this._isCurrent(epoch)) return false;
      clearOnboardingSessionId();
      this._session = null;
      this._claim = null;
      this._binding = null;
      this._activation = null;
      this._setState("prepare", { force: true });
      return true;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return false;
    } finally {
      this.clearSensitiveInput();
      this._disposeBle();
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  async resume(sessionId) {
    if (!sessionId) return null;
    const epoch = this._beginAttempt();
    this._setBusy(true);
    try {
      const session = await this.api.getOnboardingSession(sessionId);
      if (!this._isCurrent(epoch)) return null;
      if (!sameId(session.onboarding_session_id, sessionId)) {
        const error = new Error("服务端返回了不属于当前启用会话的数据");
        error.code = "ONBOARDING_SESSION_MISMATCH";
        throw error;
      }
      this._session = session;
      saveOnboardingSessionId(session.onboarding_session_id);
      const nextState = clientStateForServerState(session.state);
      if (nextState) this._setState(nextState, { force: true });
      if (session.claim_id && typeof this.api.getDeviceClaim === "function") {
        try {
          const claim = await this.api.getDeviceClaim(session.claim_id);
          // The claim lookup is a second await under the same attempt fence.
          // A page pause/dispose or a newer resume must be able to discard it
          // before it mutates the controller.
          if (!this._isCurrent(epoch)) return null;
          if (
            !sameId(claim.onboarding_session_id, session.onboarding_session_id) ||
            !sameId(claim.device_id, session.device.device_id)
          ) {
            const error = new Error("服务端返回的认领不属于当前设备或启用会话");
            error.code = "CLAIM_CONFLICT";
            throw error;
          }
          this._claim = claim;
          this._emit();
        } catch {
          // The session remains resumable; claim expiry is surfaced on reserve.
        }
      }
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    } finally {
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  _disposeBle() {
    this._transport?.dispose?.();
    this._transport = null;
    this._adapter?.dispose?.();
    this._adapter = null;
  }

  dispose() {
    this._disposed = true;
    this._attemptEpoch += 1;
    this.stopActivationPolling();
    this.clearSensitiveInput();
    this._disposeBle();
    this.onChange = null;
  }
}

module.exports = {
  OnboardingController,
};
