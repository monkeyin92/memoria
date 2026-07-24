const api = require("../../utils/api");

Page({
  data: {
    mode: "register",
    username: "",
    password: "",
    loading: false,
    error: "",
  },

  onLoad(options) {
    if (options.mode === "login") this.setData({ mode: "login" });
  },

  onUsernameInput(event) {
    this.setData({ username: event.detail.value, error: "" });
  },

  onPasswordInput(event) {
    this.setData({ password: event.detail.value, error: "" });
  },

  toggleMode() {
    this.setData({
      mode: this.data.mode === "register" ? "login" : "register",
      error: "",
    });
  },

  async submit() {
    const username = this.data.username.trim();
    const password = this.data.password;
    if (!username || !password) {
      this.setData({ error: "请填写用户名和密码。" });
      return;
    }
    this.setData({ loading: true, error: "" });
    try {
      if (this.data.mode === "register") {
        await api.registerAccount(username, password);
      } else {
        await api.loginAccount(username, password);
      }
      wx.switchTab({ url: "/pages/home/index" });
    } catch (error) {
      this.setData({ error: error?.message || "账号操作未完成，请稍后重试。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  async startAnonymous() {
    this.setData({ loading: true, error: "" });
    try {
      await api.issueAnonymousIdentity();
      wx.switchTab({ url: "/pages/home/index" });
    } catch (error) {
      this.setData({ error: error?.message || "暂时无法开始体验。" });
    } finally {
      this.setData({ loading: false });
    }
  },
});
