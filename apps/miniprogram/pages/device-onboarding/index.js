const { requireLogin } = require("../../utils/auth-gate");
const { OnboardingController } = require("../../utils/device-onboarding/onboarding-controller");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");
const { isActivationReady } = require("../../utils/device-onboarding/contracts");
const { getConnectedWifi } = require("../../utils/device-onboarding/wifi-model");

const NETWORK_LABELS = Object.freeze({
  credentials_received: "已安全发送网络信息",
  associating: "正在连接路由器",
  got_ip: "已获取网络地址",
  dns_ready: "DNS 已就绪",
  internet_ready: "互联网可用",
  cloud_connected: "Memoria 已连接",
  device_proof_accepted: "设备身份已验证",
});

const ACTIVATION_LABELS = Object.freeze({
  pending_manifest: "等待生成设备配置",
  manifest_ready: "配置已生成，等待机器人拉取",
  device_downloading: "机器人正在下载配置",
  device_applied: "机器人已应用配置",
  device_acknowledged: "机器人已确认配置",
  ready_for_conversation: "已准备好对话",
  failed: "激活失败",
  expired: "激活已过期",
  unknown: "等待设备状态",
});

function displayWifi(network) {
  if (!network) return "尚未读取网络状态";
  return network.status ? NETWORK_LABELS[network.status] || network.status : "网络状态未知";
}

function progressRows(steps, index) {
  return (steps || []).map((step, stepIndex) => ({
    ...step,
    done: stepIndex < index,
    active: stepIndex === index,
  }));
}

Page({
  data: {
    state: "prepare",
    stateLabel: "准备设备",
    mode: "add",
    busy: false,
    scanning: false,
    error: "",
    errorCode: "",
    session: null,
    claim: null,
    device: null,
    provisioning: null,
    activation: null,
    activationLabel: "等待设备状态",
    activationReady: false,
    networkLabel: "尚未读取网络状态",
    progressRows: [],
    progressIndex: 0,
    wifiNetworks: [],
    connectedWifi: "",
    selectedSsid: "",
    wifiPassword: "",
    showPassword: false,
  },

  onLoad(options = {}) {
    this._unloaded = false;
    this._mode = options.mode === "reprovision" ? "reprovision" : "add";
    this._freshStart = options.fresh === "1" || options.fresh === 1 || options.fresh === true;
    this._initialSessionId =
      typeof options.session_id === "string" && options.session_id ? options.session_id : "";
    this._controller = new OnboardingController({
      onChange: (snapshot) => this._applySnapshot(snapshot),
    });
    this.setData({ mode: this._mode });
  },

  async onShow() {
    if (!(await requireLogin({ reason: "manage_device" }))) return;
    if (this._initialSessionId && !this._resumed) {
      this._resumed = true;
      await this._controller.resume(this._initialSessionId);
    } else if (!this._freshStart && !this._resumed) {
      let storedSessionId = "";
      try {
        storedSessionId = readOnboardingSessionId();
      } catch {
        storedSessionId = "";
      }
      if (storedSessionId) {
        this._resumed = true;
        await this._controller.resume(storedSessionId);
      }
    }
    if (this.data.state === "activation") this._controller.startActivationPolling();
    await this._refreshConnectedWifi();
  },

  onHide() {
    this._clearWifiPassword();
    this._controller?.pause();
  },

  onUnload() {
    this._unloaded = true;
    this._clearWifiPassword();
    this._controller?.dispose();
    this._controller = null;
  },

  _applySnapshot(snapshot) {
    if (this._unloaded) return;
    const activationStatus = snapshot.activation?.status || "unknown";
    const progressIndexValue = Number(snapshot.progressIndex || 0);
    this.setData({
      state: snapshot.state,
      stateLabel: snapshot.stateLabel,
      busy: snapshot.busy,
      error: snapshot.error,
      errorCode: snapshot.errorCode,
      session: snapshot.session,
      claim: snapshot.claim,
      device: snapshot.device,
      provisioning: snapshot.provisioning,
      activation: snapshot.activation,
      activationLabel: ACTIVATION_LABELS[activationStatus] || activationStatus,
      activationReady: Boolean(snapshot.activationReady || isActivationReady(activationStatus)),
      networkLabel: displayWifi(snapshot.session?.network_status),
      wifiNetworks: snapshot.wifiNetworks || [],
      progressIndex: progressIndexValue,
      progressRows: progressRows(snapshot.progressSteps, progressIndexValue),
    });
  },

  _clearWifiPassword() {
    if (this.data.wifiPassword) this.setData({ wifiPassword: "" });
    this._controller?.clearSensitiveInput();
  },

  startFlow() {
    this._controller.startScan();
  },

  scanQr() {
    if (this.data.busy || this.data.scanning) return;
    this.setData({ scanning: true, error: "", errorCode: "" });
    wx.scanCode({
      onlyFromCamera: false,
      success: (result) => {
        // Do not trim, store, or log this value. The controller passes the
        // scanner's raw payload to POST /v1/device-bootstrap/introspect.
        const rawPayload = typeof result?.result === "string" ? result.result : "";
        this._controller.introspectQr(rawPayload);
      },
      fail: (error) => {
        if (!this._unloaded) {
          const errCode = Number.isFinite(Number(error?.errCode)) ? `微信错误码 ${error.errCode}` : "";
          const errMsg = typeof error?.errMsg === "string" ? error.errMsg.slice(0, 96) : "";
          const detail = [errCode, errMsg].filter(Boolean).join("，");
          this.setData({
            errorCode: "QR_INVALID",
            error: `扫码未完成，请重新扫描机器人屏幕上的二维码。${detail ? `（${detail}）` : ""}`,
          });
        }
      },
      complete: () => {
        if (!this._unloaded) this.setData({ scanning: false });
      },
    });
  },

  connectBle() {
    this._controller.connectBle();
  },

  retryBle() {
    this._controller.connectBle();
  },

  onSsidInput(event) {
    this.setData({ selectedSsid: event.detail.value || "", error: "" });
  },

  onWifiPasswordInput(event) {
    this.setData({ wifiPassword: event.detail.value || "", error: "" });
  },

  onWifiNetworkTap(event) {
    const ssid = event.currentTarget.dataset.ssid;
    if (typeof ssid !== "string" || !ssid) return;
    this.setData({ selectedSsid: ssid, error: "" });
  },

  togglePassword() {
    this.setData({ showPassword: !this.data.showPassword });
  },

  async _refreshConnectedWifi() {
    try {
      const result = await getConnectedWifi();
      if (!result || this._unloaded) return;
      this.setData({
        connectedWifi: result.ssid,
        selectedSsid: this.data.selectedSsid || result.ssid,
      });
    } catch {
      // The phone SSID is only a convenience hint; BLE scan remains authoritative.
    }
  },

  async submitWifi() {
    if (this.data.busy) return;
    const success = await this._controller.provisionWifi({
      ssid: this.data.selectedSsid,
      password: this.data.wifiPassword,
    });
    this._clearWifiPassword();
    if (success) this.setData({ showPassword: false });
  },

  retryProgress() {
    this._controller.refreshSession({ preserveProgress: true });
  },

  reserveClaim() {
    this._controller.reserveClaim();
  },

  continueInitialize() {
    if (!this._controller.beginInitialize()) return;
    const claim = this._controller.claim;
    if (!claim) return;
    wx.navigateTo({
      url:
        `/pages/bind/index?claim_id=${encodeURIComponent(claim.claim_id)}&onboarding_session_id=${encodeURIComponent(claim.onboarding_session_id)}`,
    });
  },



  refreshActivation() {
    this._controller.refreshActivation();
  },

  cancelFlow() {
    if (this.data.busy) return;
    wx.showModal({
      title: "取消本次启用？",
      content: "会断开当前蓝牙连接，但不会修改已经绑定的设备。",
      confirmText: "取消启用",
      cancelText: "继续设置",
      success: (result) => {
        if (!result.confirm) return;
        this._controller.cancel().then((cancelled) => {
          if (cancelled && !this._unloaded) wx.switchTab({ url: "/pages/device/index" });
        });
      },
    });
  },

  openDevicePage() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  onBindingCreated(manifest) {
    this._resumed = true;
    this._controller.onBindingCreated(manifest);
    this._controller.startActivationPolling();
  },
});
