const api = require("../../utils/api");
const guardianPush = require("../../utils/guardian-push");
const { SUBSCRIPTION_PUSH_ENABLED } = require("../../config");
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
    try {
      this._notificationId = decodeURIComponent(query?.id || "");
    } catch {
      this._notificationId = "";
    }
    this._safety = null;
    this._loadSafety();
    return this.loadAlert();
  },

  onShow() {
    if (this.data.alertResult.errorKind === "auth" && api.hasAuthenticatedSession()) {
      this._loadSafety();
      return this.loadAlert();
    }
  },

  _loadSafety() {
    if (!SUBSCRIPTION_PUSH_ENABLED) return;
    const seq = (this._safetySeq || 0) + 1;
    this._safetySeq = seq;
    guardianPush.loadSafetySubscribeState(api).then((safety) => {
      if (seq === this._safetySeq) this._safety = safety;
    });
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
    // 续订要在这次点击里同步调起；它的结果不影响已读。
    const safety = this._safety;
    const topUp =
      safety?.config?.enabled && safety.subscription?.known && safety.subscription.lastResult === "accept"
        ? guardianPush.subscribeCrisisAlerts({ config: safety.config, api }).catch(() => null)
        : Promise.resolve(null);
    this.setData({ acknowledging: true, ackError: "" });
    const result = await acknowledgeSafetyAlert(api, alert.notificationId);
    await topUp;
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
