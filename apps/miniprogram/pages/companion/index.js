const api = require("../../utils/api");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { parseCustomPersona } = require("../../utils/custom-persona");
const { requireLogin } = require("../../utils/auth-gate");

Page({
  data: {
    companions,
    profile: { companion_id: defaultCompanionId },
    customPersonaActive: false,
    currentName: companionById(defaultCompanionId).name,
    currentTone: companionById(defaultCompanionId).tone,
    saving: false,
  },

  async onShow() {
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
      const custom = parseCustomPersona(profile?.bio);
      const companion = companionById(profile?.companion_id);
      this.setData({
        profile: { companion_id: companion.id, ...profile },
        customPersonaActive: custom.active,
        currentName: custom.active ? custom.name || companion.name : companion.name,
        currentTone: custom.active ? "你提供的声音样本" : companion.tone,
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
      customPersonaActive: false,
      currentName: companion.name,
      currentTone: companion.tone,
    });
    await this._saveCatalog(companion.id);
  },

  chooseCatalogVoice() {
    const companion = companionById(this.data.profile.companion_id);
    this.setData({
      customPersonaActive: false,
      currentName: companion.name,
      currentTone: companion.tone,
    });
  },

  chooseCustomPersona() {
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
        bio: parseCustomPersona(this.data.profile.bio).active ? "" : this.data.profile.bio,
      });
      const companion = companionById(profile.companion_id || companionId);
      this.setData({
        profile: { ...this.data.profile, ...profile, companion_id: companion.id },
        customPersonaActive: false,
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
