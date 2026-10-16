const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { loadPinnedCrisisAlerts } = require("../../utils/crisis-alerts");
const { alertTimeLabel } = require("../../utils/trial-device-card");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    loading: true,
    alert: null,
  },

  onLoad(query) {
    this._notificationId = query?.id || "";
    this.loadAlert();
  },

  async loadAlert() {
    const alerts = await loadPinnedCrisisAlerts(api);
    const alert = alerts.find((item) => item.notificationId === this._notificationId) || alerts[0] || null;
    const view = alert && {
      notificationId: alert.notificationId,
      message: alert.message,
      timeLabel: alertTimeLabel(alert.occurredAt),
    };
    if (view && alert.detail) {
      view.detail = alert.detail;
    }
    this.setData({
      loading: false,
      alert: view,
    });
  },

  goBack() {
    leavePage("/pages/home/index");
  },
});
