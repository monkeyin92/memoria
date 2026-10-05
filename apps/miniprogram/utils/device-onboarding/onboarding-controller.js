"use strict";

const defaultApi = require("../api");
const { WxBleAdapter } = require("./ble-adapter");
const { createProvisioningTransport } = require("./provisioning-transport");
const { Security1Adapter, codec } = require("./protocomm-codec");
const { parseDeviceQr } = require("./qr-code");
const { SensitiveBuffer } = require("./sensitive-buffer");
const { normalizeWifiNetworks, getConnectedWifi } = require("./wifi-model");
const { isActivationReady, normalizeActivationResponse } = require("./contracts");
const {
  transition,
  clientStateForSession,
  isReprovisionSession,
  isExpired,
  errorMessage,
  errorCode,
  progressIndex,
  activationIsLateOrReady,
  PROGRESS_STEPS,
  STATE_LABELS,
  REPROVISION_STATE_LABELS,
} = require("./state");
const {
  saveOnboardingSessionId,
  clearOnboardingSessionId,
} = require("./session-store");

// After Wi-Fi is written the page watches the robot come online: over BLE
// (prov-status: joined the network yet?) and on the server (online proof).
const NETWORK_POLL_MS = 2000;
const WIFI_JOIN_TIMEOUT_MS = 45000;
const ONLINE_TIMEOUT_MS = 90000;
// The robot normally takes its settings within seconds of the binding; past
// this the activation step says what usually helps.
const ACTIVATION_SLOW_MS = 60000;
const ONLINE_SERVER_STATES = new Set([
  "device_online",
  "claim_reserved",
  "binding_committing",
  "bound",
  "activating",
  "activated",
]);

function isDeviceOnline(session) {
  return (
    ONLINE_SERVER_STATES.has(session?.state) ||
    session?.network_status?.status === "device_proof_accepted"
  );
}

// The server stops answering for a session whose robot was unbound meanwhile:
// 403 ACTOR_MISMATCH on the activation lookup, and 403/404/409/410 on cancel
// (409 = it is past binding and can no longer be cancelled).  None of these
// can be fixed by retrying, so the stored session is dropped instead.
const CANCEL_NOTHING_LEFT_STATUSES = new Set([403, 404, 409, 410]);

function activationNoLongerYours(error) {
  return error?.status === 403 || error?.code === "ACTOR_MISMATCH";
}

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
    transportFactory = null,
    clientOnboardingId = operationId("client_onb"),
    now = () => Date.now(),
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    onChange = null,
    mode = "add",
  } = {}) {
    this.api = apiClient;
    this.bleAdapterFactory = bleAdapterFactory;
    this.transportFactory = transportFactory || ((options) => createProvisioningTransport({
      ...options,
      securityAdapter: new Security1Adapter({ pop: options.pop }),
      codec,
    }));
    this.clientOnboardingId = clientOnboardingId;
    this.now = now;
    this.sleep = sleep;
    this.onChange = onChange;
    // The entry point's intent.  Once a session exists, the server's purpose
    // decides the flow: an owner scanning their own bound robot from "add"
    // only updates Wi-Fi as well.
    this.mode = mode === "reprovision" ? "reprovision" : "add";

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
    // { phase: joining|cloud|online|failed, ssid, joined, elapsedS, failure: ""|wifi_join|cloud }
    this._network = null;
    this._adapter = null;
    this._transport = null;
    this._wifiPassword = new SensitiveBuffer();
    this._wifiSsid = "";
    this._activationTimer = null;
    this._activationSince = null;
    this._bootstrapPop = "";
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

  get reprovision() {
    return this._session ? isReprovisionSession(this._session) : this.mode === "reprovision";
  }

  snapshot() {
    const reprovision = this.reprovision;
    return {
      state: this._state,
      stateLabel: (reprovision ? REPROVISION_STATE_LABELS : STATE_LABELS)[this._state],
      reprovision,
      busy: this._busy,
      error: this._error,
      errorCode: this._errorCode,
      session: this._session,
      claim: this._claim,
      binding: this._binding,
      activation: this._activation,
      wifiNetworks: this._wifiNetworks,
      network: this._network,
      device: this._session?.device || this._claim?.device || null,
      provisioning: this._session?.provisioning || null,
      progressSteps: PROGRESS_STEPS,
      progressIndex: progressIndex({
        state: this._session?.state,
        networkStatus: this._session?.network_status,
        // A reprovision has no activation of its own to report.
        activationStatus: reprovision
          ? undefined
          : this._activation?.status || this._session?.activation_status,
      }),
      activationReady: Boolean(this._activation && isActivationReady(this._activation.status)),
      activationSlow:
        this._state === "activation" &&
        this._activationSince !== null &&
        this.now() - this._activationSince >= ACTIVATION_SLOW_MS,
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
    this._activationSince = next === "activation" ? this.now() : null;
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
      // Local parsing is only a shape/version gate.  The exact device payload
      // (unwrapped from a WeChat bind link when the board shows one) is sent
      // unchanged to the server for signature and revocation checks.
      let parsedQr;
      try {
        parsedQr = parseDeviceQr(rawPayload);
      } catch (error) {
        // Keep the public message generic by default, but retain the local
        // protocol reason so the device onboarding screen can distinguish a
        // malformed scanner result from a camera failure without exposing QR
        // contents, the PoP, or the device signature.
        if (error?.code === "QR_INVALID" && !error.clientDetail) {
          error.clientDetail = error.message;
        }
        throw error;
      }
      const session = await this.api.introspectDeviceQr({
        qrPayload: parsedQr.raw_payload,
        clientOnboardingId: this.clientOnboardingId,
      });
      if (!this._isCurrent(epoch)) return null;
      if (this.mode === "reprovision" && !isReprovisionSession(session)) {
        // Not bound to this account (released, or someone else's robot).
        // Stop before BLE instead of silently turning "only update Wi-Fi"
        // into claiming a device.  The session is left to expire so the same
        // QR still works from the add-device entry.
        const error = new Error("这台机器人没有绑定在你的账号上");
        error.code = "REPROVISION_DEVICE_NOT_BOUND";
        throw error;
      }
      this._session = session;
      this._claim = null;
      this._binding = null;
      this._activation = null;
      this._bootstrapPop = parsedQr.payload.pop;
      saveOnboardingSessionId(session.onboarding_session_id);
      if (!isReprovisionSession(session) && isDeviceOnline(session)) {
        // Rescanning a QR that is still on screen returns the same session.
        // Once the robot has proved itself online the server no longer issues
        // it a challenge, so BLE and Wi-Fi again could only time out; pick up
        // from the step the session has reached.
        return this._continueOnlineSession(session, epoch);
      }
      this._setState("device_verified", { force: true });
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) {
        this._bootstrapPop = "";
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
        pop: this._bootstrapPop,
      });
      this._transport = transport;
      await transport.establishSecureSession();
      await transport.request("memoria-bootstrap", {
        onboarding_session_id: this._session.onboarding_session_id,
        mobile_nonce: this._session.mobile_nonce,
      });
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
        const error = new Error("BLE 安全会话已失效，需要重新扫描二维码");
        error.code = "BLE_REAUTH_REQUIRED";
        throw error;
      }
      await this._transport.writeWifiCredentials({
        ssid: this._wifiSsid,
        password: this._wifiPassword.getText(),
      });
      // The password has reached the robot; do not hold it while waiting.
      const sentSsid = this._wifiSsid;
      this.clearSensitiveInput();
      if (!this._isCurrent(epoch)) return false;
      this._setState("progress", { force: true });
      this._setBusy(false);
      return this._watchNetwork(epoch, sentSsid);
    } catch (error) {
      if (this._isCurrent(epoch)) {
        this._setError(error, { keepState: error?.code !== "BLE_REAUTH_REQUIRED" });
      }
      return false;
    } finally {
      // Passwords are cleared even on failure; a retry re-enters them in the
      // page instance and never reuses a stale sensitive value.
      this.clearSensitiveInput();
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  /** Keep waiting after a timeout, or pick the watch up again after a resume. */
  watchNetwork() {
    if (!this._session) return Promise.resolve(false);
    return this._watchNetwork(this._beginAttempt());
  }

  /** Back to the Wi-Fi form; BLE is reused when it is still connected. */
  retryWifi() {
    this._network = null;
    if (!this._transport) return this.connectBle();
    this._setState("wifi", { force: true });
    return this.loadWifiNetworks();
  }

  async _watchNetwork(epoch, ssidHint = "") {
    const sessionId = this._session?.onboarding_session_id;
    if (!sessionId) return false;
    const startedAt = this.now();
    const ssid = ssidHint || this._network?.ssid || "";
    let joined = false;
    this._network = { phase: "joining", ssid, joined, elapsedS: 0, failure: "" };
    this._emit();
    while (this._isCurrent(epoch)) {
      await this.sleep(NETWORK_POLL_MS);
      if (!this._isCurrent(epoch)) return false;
      if (!joined && this._transport) {
        try {
          const status = await this._transport.request("prov-status", {});
          joined = status?.connected === true;
        } catch {
          // BLE can drop while the radio joins the new network; the server
          // still says when the robot comes online.
        }
      }
      let session = null;
      try {
        session = await this.api.getOnboardingSession(sessionId);
      } catch {
        session = null;
      }
      if (!this._isCurrent(epoch)) return false;
      if (session && sameId(session.onboarding_session_id, sessionId)) this._session = session;
      const elapsed = this.now() - startedAt;
      const elapsedS = Math.round(elapsed / 1000);
      if (session && isDeviceOnline(session)) {
        this._network = { phase: "online", ssid, joined: true, elapsedS, failure: "" };
        if (clientStateForSession(session) === "complete") {
          this._setState("complete", { force: true });
          clearOnboardingSessionId();
          return true;
        }
        this._emit();
        if (!this.reprovision) await this.reserveClaim();
        return true;
      }
      let failure = "";
      if (!joined && elapsed >= WIFI_JOIN_TIMEOUT_MS) failure = "wifi_join";
      else if (elapsed >= ONLINE_TIMEOUT_MS) failure = "cloud";
      this._network = {
        phase: failure ? "failed" : joined ? "cloud" : "joining",
        ssid,
        joined,
        elapsedS,
        failure,
      };
      this._emit();
      if (failure) return false;
    }
    return false;
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
      const nextState = clientStateForSession(session);
      if (nextState && !(preserveProgress && this._state === "progress" && nextState === "wifi")) {
        this._setState(nextState, { force: true });
      } else {
        this._emit();
      }
      if (nextState === "complete") clearOnboardingSessionId();
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    }
  }

  async reserveClaim() {
    const session = this._session;
    if (!session || isReprovisionSession(session)) return null;
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
    if (!this._claim || this.reprovision) {
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
      if (!this._isCurrent(epoch)) return null;
      if (activationNoLongerYours(error)) this._endStaleSession();
      else this._setError(error);
      return null;
    }
  }

  // The session outlived its robot's binding (unbound since, or bound again by
  // another flow): nothing here can finish, so drop it and say so instead of
  // leaving a bare 403 on a page that cannot be fixed.
  _endStaleSession() {
    this._resetSession();
    this._setState("prepare", { force: true });
    this._setError({ code: "ONBOARDING_SESSION_ENDED" });
  }

  _resetSession() {
    this.stopActivationPolling();
    clearOnboardingSessionId();
    this._session = null;
    this._claim = null;
    this._binding = null;
    this._activation = null;
    this._bootstrapPop = "";
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
    this._bootstrapPop = "";
    this._disposeBle();
  }

  async cancel() {
    const sessionId = this._session?.onboarding_session_id;
    const epoch = this._beginAttempt();
    this._setBusy(true);
    try {
      if (sessionId) {
        try {
          await this.api.cancelOnboardingSession(sessionId);
        } catch (error) {
          // Already over for the server (unbound, gone) or past the point it can
          // be cancelled (bound): either way there is nothing left to cancel and
          // leaving the flow is what the person asked for.
          if (!CANCEL_NOTHING_LEFT_STATUSES.has(error?.status)) throw error;
        }
      }
      if (!this._isCurrent(epoch)) return false;
      this._resetSession();
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
      const nextState = clientStateForSession(session);
      if (nextState) this._setState(nextState, { force: true });
      if (nextState === "complete" && isReprovisionSession(session)) {
        clearOnboardingSessionId();
        return session;
      }
      if (!(await this._loadSessionClaim(session, epoch))) return null;
      return session;
    } catch (error) {
      if (this._isCurrent(epoch)) this._setError(error);
      return null;
    } finally {
      if (this._isCurrent(epoch)) this._setBusy(false);
    }
  }

  async _continueOnlineSession(session, epoch) {
    if (session.state === "device_online") {
      // Online but not claimed yet: where the network watch hands over.
      this._network = { phase: "online", ssid: "", joined: true, elapsedS: 0, failure: "" };
      this._setState("progress", { force: true });
      await this.reserveClaim();
      return session;
    }
    this._setState(clientStateForSession(session), { force: true });
    if (!(await this._loadSessionClaim(session, epoch))) return null;
    return session;
  }

  /** False when a pause, dispose, or newer attempt superseded this one. */
  async _loadSessionClaim(session, epoch) {
    if (!session.claim_id || typeof this.api.getDeviceClaim !== "function") return true;
    try {
      const claim = await this.api.getDeviceClaim(session.claim_id);
      // The claim lookup is a second await under the same attempt fence.
      // A page pause/dispose or a newer resume must be able to discard it
      // before it mutates the controller.
      if (!this._isCurrent(epoch)) return false;
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
    return true;
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
    this._bootstrapPop = "";
    this._disposeBle();
    this.onChange = null;
  }
}

module.exports = {
  OnboardingController,
};
