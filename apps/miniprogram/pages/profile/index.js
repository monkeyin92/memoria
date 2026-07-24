const api = require("../../utils/api");
const { companions, defaultCompanionId } = require("../../utils/companions");

const defaultProfile = {
  display_name: "新朋友",
  bio: "慢慢说，我会认真听。",
  auto_summary: true,
  voice_reply: true,
  gentle_reminders: false,
  reject_non_owner_voice: true,
  companion_id: defaultCompanionId,
};

Page({
  data: {
    identity: null,
    profile: defaultProfile,
    companions,
    loading: false,
    saving: false,
    error: "",
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.navigateTo({ url: "/pages/auth/index?mode=login" });
      return;
    }
    this.loadProfile();
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    this.setData({ loading: true, identity, error: "" });
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      this.setData({ profile });
    } catch (error) {
      this.setData({ error: error?.message || "个人资料无法加载。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  onNameInput(event) {
    this.setData({ "profile.display_name": event.detail.value });
  },

  onBioInput(event) {
    this.setData({ "profile.bio": event.detail.value });
  },

  onSwitch(event) {
    const key = event.currentTarget.dataset.key;
    this.setData({ [`profile.${key}`]: event.detail.value });
  },

  async chooseCompanion(event) {
    const companionId = event.currentTarget.dataset.id;
    this.setData({ "profile.companion_id": companionId });
    await this.saveProfile();
  },

  async saveProfile() {
    const identity = api.currentIdentity();
    if (!identity || this.data.saving) return;
    this.setData({ saving: true, error: "" });
    try {
      const profile = await api.updateProfile(identity.user_id, this.data.profile);
      this.setData({ profile: { ...this.data.profile, ...profile } });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      this.setData({ error: error?.message || "保存失败。" });
    } finally {
      this.setData({ saving: false });
    }
  },

  openDigitalSelf() {
    wx.navigateTo({ url: "/pages/digital-self/index" });
  },

  openPrivacy() {
    wx.navigateTo({ url: "/pages/privacy/index" });
  },

  logout() {
    api.logoutLocal();
    wx.reLaunch({ url: "/pages/auth/index?mode=login" });
  },
});
