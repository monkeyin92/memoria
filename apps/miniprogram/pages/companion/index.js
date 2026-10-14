const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const { readBindingManifest } = require("../../utils/device-binding");

// 选定的伙伴也是已绑定设备的人格：服务端在保存后把它下发到设备，
// 下一次对话起换声音，待机形象随后刷新。没有绑定设备时只提示已保存。
function savedToastTitle() {
  return readBindingManifest() ? "已保存，设备将同步切换" : "已保存";
}

function roleCards() {
  return companions.map((companion) => ({
    id: companion.id,
    name: companion.name,
    tagline: companion.tagline,
    tone: companion.tone,
    previewSrc: `/assets/voices/${companion.voiceId}.mp3`,
  }));
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    cards: roleCards(),
    profile: { companion_id: defaultCompanionId },
    currentName: companionById(defaultCompanionId).name,
    currentTone: companionById(defaultCompanionId).tone,
    previewingId: "",
    saving: false,
  },

  async onShow() {
    if (typeof this.getTabBar === "function" && this.getTabBar()) { this.getTabBar().setData({ selected: 2 }); }
    if (!(await requireLogin({ reason: "edit_profile", redirect: "/pages/companion/index" }))) {
      return;
    }
    await this.loadProfile();
  },

  onHide() {
    this._stopPreview();
  },

  onUnload() {
    this._stopPreview();
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

  togglePreview(event) {
    const card = this.data.cards.find((item) => item.id === event.currentTarget.dataset.id);
    if (!card) return;
    if (this.data.previewingId === card.id) {
      this._stopPreview();
      return;
    }
    this._stopPreview();
    const audio = wx.createInnerAudioContext({ useWebAudioImplement: false });
    audio.obeyMuteSwitch = false;
    audio.src = card.previewSrc;
    const done = () => {
      if (this._previewAudio === audio) this._stopPreview();
    };
    audio.onEnded(done);
    audio.onError(done);
    this._previewAudio = audio;
    this.setData({ previewingId: card.id });
    audio.play();
  },

  _stopPreview() {
    const audio = this._previewAudio;
    this._previewAudio = null;
    if (audio) {
      try {
        audio.stop();
        audio.destroy();
      } catch {
        // 已经结束或被系统回收的音频无需再处理。
      }
    }
    if (this.data.previewingId) this.setData({ previewingId: "" });
  },

  async chooseCompanion(event) {
    const companionId = event.currentTarget.dataset.id;
    const companion = companionById(companionId);
    if (companion.id === this.data.profile?.companion_id) return;
    const previous = {
      "profile.companion_id": this.data.profile?.companion_id,
      currentName: this.data.currentName,
      currentTone: this.data.currentTone,
    };
    this.setData({
      "profile.companion_id": companion.id,
      currentName: companion.name,
      currentTone: companion.tone,
    });
    // The card flips optimistically; if the save does not land (login declined,
    // already saving, server error) show the companion the account still has.
    if (!(await this._saveCatalog(companion.id))) this.setData(previous);
  },

  openCustomPersona() {
    wx.navigateTo({ url: "/pages/persona-custom/index" });
  },

  async _saveCatalog(companionId) {
    if (!(await requireLogin({ reason: "edit_profile" }))) return false;
    const identity = api.currentIdentity();
    if (!identity || this.data.saving) return false;
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
      const title = savedToastTitle();
      // 带图标的 toast 只显示 7 个字；较长的提示用纯文字。
      wx.showToast({ title, icon: title.length > 7 ? "none" : "success" });
      return true;
    } catch (error) {
      wx.showToast({ title: error?.message || "保存失败", icon: "none" });
      return false;
    } finally {
      this.setData({ saving: false });
    }
  },
});
