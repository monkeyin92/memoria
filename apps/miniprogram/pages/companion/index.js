const api = require("../../utils/api");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");

Page({
  data: {
    companions,
    profile: { companion_id: defaultCompanionId },
    currentName: companionById(defaultCompanionId).name,
    currentTone: companionById(defaultCompanionId).tone,
    saving: false,
  },

  async onShow() {
    if (typeof this.getTabBar === "function" && this.getTabBar()) { this.getTabBar().setData({ selected: 2 }); }
    if (!(await requireLogin({ reason: "edit_profile", redirect: "/pages/companion/index" }))) {
      return;
    }
    await this.loadProfile();
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    try {
      const profile = await api.getProfile(identity.user_id);
      const companion = companionById(profile?.companion_id);
      this.setData({
        profile: { companion_id: companion.id, ...profile },
        currentName: companion.name,
        currentTone: companion.tone,
      });
    } catch (error) {
      wx.showToast({ title: error?.message || "资料暂时无法读取", icon: "none" });
    }
  },

  async chooseCompanion(event) {
    const companionId = event.currentTarget.dataset.id;
    const companion = companionById(companionId);
    this.setData({
      "profile.companion_id": companion.id,
      currentName: companion.name,
      currentTone: companion.tone,
    });
    await this._saveCatalog(companion.id);
  },

  chooseCatalogVoice() {
    const companion = companionById(this.data.profile.companion_id);
    this.setData({
      currentName: companion.name,
      currentTone: companion.tone,
    });
  },

  // 自定义声音在「我的」里录制/上传；这一行只是入口，不在这里采集音频。
  openVoiceClone() {
    wx.switchTab({ url: "/pages/profile/index" });
  },

  saveCurrent() {
    return this._saveCatalog(this.data.profile.companion_id);
  },

  async _saveCatalog(companionId) {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const identity = api.currentIdentity();
    if (!identity || this.data.saving) return;
    this.setData({ saving: true });
    try {
      const profile = await api.updateProfile(identity.user_id, {
        ...this.data.profile,
        companion_id: companionId,
      });
      const companion = companionById(profile.companion_id || companionId);
      this.setData({
        profile: { ...this.data.profile, ...profile, companion_id: companion.id },
        currentName: companion.name,
        currentTone: companion.tone,
      });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      wx.showToast({ title: error?.message || "保存失败", icon: "none" });
    } finally {
      this.setData({ saving: false });
    }
  },
});
