const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { openLogin } = require("../../utils/auth-gate");
const { acknowledgeSafetyAlert, loadSafetyAlert } = require("../../utils/crisis-alerts");

const ACK_FAILED = "还没记下，请再点一次。";

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    loading: true,
    alert: null,
    alertResult: { status: "not_found" },
    acknowledging: false,
    ackError: "",
  },

  onLoad(query) {
    this._notificationId = query?.id || "";
    this.loadAlert();
  },

  onShow() {
    if (this.data.alertResult.errorKind === "auth" && api.hasAuthenticatedSession()) {
      return this.loadAlert();
    }
  },

  async loadAlert() {
    if (this._loadingAlert) return;
    this._loadingAlert = true;
    this.setData({ loading: true, alert: null });
    try {
      const alertResult = await loadSafetyAlert(api, this._notificationId);
      this.setData({
        loading: false,
        alert: alertResult.status === "ok" ? alertResult.alert : null,
        alertResult,
      });
    } finally {
      this._loadingAlert = false;
    }
  },

  retryLoadAlert() {
    return this.loadAlert();
  },

  openAttentionLogin() {
    openLogin({
      reason: "view_attention",
      redirect: `/pages/attention/index?id=${encodeURIComponent(this._notificationId || "")}`,
    });
  },

  async acknowledge() {
    const { alert } = this.data;
    if (!alert) {
      this.goBack();
      return;
    }
    if (this._acknowledging) return;
    this._acknowledging = true;
    this.setData({ acknowledging: true, ackError: "" });
    const result = await acknowledgeSafetyAlert(api, alert.notificationId);
    if (result === "saved" || result === "gone") {
      this.goBack();
      return;
    }
    this._acknowledging = false;
    this.setData({ acknowledging: false, ackError: ACK_FAILED });
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
