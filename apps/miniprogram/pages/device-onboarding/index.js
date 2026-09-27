const { requireLogin } = require("../../utils/auth-gate");
const { OnboardingController } = require("../../utils/device-onboarding/onboarding-controller");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");
const { isActivationReady } = require("../../utils/device-onboarding/contracts");
const { getConnectedWifi } = require("../../utils/device-onboarding/wifi-model");
const { isMemoriaDeviceQr } = require("../../utils/device-onboarding/qr-code");

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

// The four things a person can follow while the robot gets online.
function networkRows(network) {
  const phase = network?.phase || "joining";
  const failure = network?.failure || "";
  const online = phase === "online";
  const joined = online || Boolean(network?.joined);
  const ssid = network?.ssid ? `「${network.ssid}」` : "";
  const row = (key, label, done, active, failed) => ({ key, label, done, active, failed });
  return [
    row("sent", "网络信息已安全送达机器人", true, false, false),
    row("wifi", `机器人连接 Wi‑Fi${ssid}`, joined, !joined && !failure, failure === "wifi_join"),
    row("cloud", "连接 Memoria 云端", online, joined && !online && !failure, failure === "cloud"),
    row("proof", "设备身份验证通过", online, false, false),
  ];
}

function networkFailureText(network) {
  const ssid = network?.ssid ? `「${network.ssid}」` : "这个 Wi‑Fi";
  if (network?.failure === "wifi_join") {
    return `机器人 45 秒内没有连上${ssid}。请确认密码正确、是 2.4 GHz 网络（机器人不支持 5 GHz），并让机器人离路由器近一些。`;
  }
  if (network?.failure === "cloud") {
    return "机器人已连上 Wi‑Fi，但还没连上 Memoria 云端。如果这个网络需要网页登录（酒店、公司网络）或限制了外网，请换一个网络；也可以再等一会儿。";
  }
  return "";
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
    reprovision: false,
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
    network: null,
    networkRows: [],
    networkFailed: false,
    networkFailure: "",
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
    // Opened by WeChat's scanner from the board's bind link: `q` is the
    // encoded link. Start a fresh scan with it once the user is signed in.
    this._linkedQr = "";
    if (typeof options.q === "string" && options.q) {
      try {
        this._linkedQr = decodeURIComponent(options.q);
      } catch {
        this._linkedQr = options.q;
      }
      this._freshStart = true;
      this._initialSessionId = "";
    }
    this._controller = new OnboardingController({
      mode: this._mode,
      onChange: (snapshot) => this._applySnapshot(snapshot),
    });
    this.setData({ mode: this._mode, reprovision: this._mode === "reprovision" });
  },

  async onShow() {
    if (!(await requireLogin({ reason: "manage_device" }))) return;
    if (this._linkedQr) {
      const linkedQr = this._linkedQr;
      this._linkedQr = "";
      this._resumed = true;
      await this._controller.introspectQr(linkedQr);
    } else if (this._initialSessionId && !this._resumed) {
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
    // A resumed or re-shown progress step follows the robot again.
    if (this.data.state === "progress" && !this.data.network) this._controller.watchNetwork();
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

  _syncNavigationTitle(reprovision) {
    if (this._navigationReprovision === reprovision) return;
    this._navigationReprovision = reprovision;
    wx.setNavigationBarTitle?.({ title: reprovision ? "重新配网" : "配网引导" });
  },

  _applySnapshot(snapshot) {
    if (this._unloaded) return;
    // The server's purpose can turn an add-device scan into a reprovision.
    this._syncNavigationTitle(Boolean(snapshot.reprovision));
    const activationStatus = snapshot.activation?.status || "unknown";
    const progressIndexValue = Number(snapshot.progressIndex || 0);
    this.setData({
      state: snapshot.state,
      stateLabel: snapshot.stateLabel,
      reprovision: Boolean(snapshot.reprovision),
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
      network: snapshot.network || null,
      networkRows: networkRows(snapshot.network),
      networkFailed: snapshot.network?.phase === "failed",
      networkFailure: networkFailureText(snapshot.network),
    });
    // 使用人身份来自设备绑定，不做声音身份识别；完成设置后不再自动发起
    // speaker enrollment intent。
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
        // Do not store or log these values. The controller unwraps a bind
        // link and passes only the device payload to introspect. A code tied
        // to this Mini Program may come back as its page path in `path`.
        const scanned = [result?.result, result?.path].filter(
          (value) => typeof value === "string" && value,
        );
        this._controller.introspectQr(scanned.find(isMemoriaDeviceQr) || scanned[0] || "");
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
    this._controller.watchNetwork();
  },

  retryWifi() {
    this._controller.retryWifi();
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
    const reprovision = this.data.reprovision;
    wx.showModal({
      title: reprovision ? "取消重新配网？" : "取消本次启用？",
      content: "会断开当前蓝牙连接，但不会修改已经绑定的设备。",
      confirmText: reprovision ? "取消配网" : "取消启用",
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
