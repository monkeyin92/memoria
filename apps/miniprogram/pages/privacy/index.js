const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    guest: false,
  },

  goBack() {
    leavePage("/pages/profile/index");
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._clearPrivateState());
    }
  },

  async onShow() {
    if (!api.hasAuthenticatedSession()) {
      this._showGuestNotice();
      return;
    }
    this.setData({ guest: false });
  },

  _showGuestNotice() {
    this.setData({
      guest: true,
    });
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _clearPrivateState() {
    this._showGuestNotice();
  },

  openChildMemory() {
    wx.navigateTo({ url: "/pages/child-memory/index" });
  },
});
