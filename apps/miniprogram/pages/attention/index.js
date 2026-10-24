const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { acknowledgeSafetyAlert, loadSafetyAlert } = require("../../utils/crisis-alerts");

const ACK_FAILED = "还没记下，请再点一次。";

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    loading: true,
    alert: null,
    acknowledging: false,
    ackError: "",
  },

  onLoad(query) {
    this._notificationId = query?.id || "";
    this.loadAlert();
  },

  async loadAlert() {
    this.setData({ loading: false, alert: await loadSafetyAlert(api, this._notificationId) });
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
