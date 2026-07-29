const api = require("../../utils/api");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");

const defaultProfile = {
  display_name: "新朋友",
  auto_summary: true,
  voice_reply: true,
  gentle_reminders: false,
  reject_non_owner_voice: true,
  companion_id: defaultCompanionId,
};

const DELETE_CONFIRMATION_TEXT = "永久删除我的全部数据";

function profileFaceStyleFor(companionId) {
  const face = companionById(companionId).face;
  return (
    `left:${face.left};top:${face.top};width:${face.width};height:${face.height};` +
    `--profile-face-ink:${face.ink};--profile-eye-top:${face.eyeTop};` +
    `--profile-eye-bottom:${face.eyeBottom};--profile-eye-glow:${face.glow};`
  );
}

function companionFaceStyleFor(companion) {
  const face = companion.face;
  return (
    `left:${face.left};top:${face.top};width:${face.width};height:${face.height};` +
    `--companion-face-ink:${face.ink};--companion-eye-top:${face.eyeTop};` +
    `--companion-eye-bottom:${face.eyeBottom};--companion-eye-glow:${face.glow};`
  );
}

const companionCards = companions.map((companion) => ({
  ...companion,
  faceStyle: companionFaceStyleFor(companion),
}));

function formatDate(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function computeStats(items) {
  const days = (items || []).map((item) => ({
    date: item.day || item.date || "",
    count: item.message_count || 0,
  }));
  const totalDays = days.length;
  const moments = days.reduce((sum, item) => sum + item.count, 0);
  const dates = new Set(days.map((item) => item.date));
  const cursor = new Date();
  if (!dates.has(formatDate(cursor))) cursor.setDate(cursor.getDate() - 1);
  let streak = 0;
  while (dates.has(formatDate(cursor))) {
    streak += 1;
    cursor.setDate(cursor.getDate() - 1);
  }
  return { totalDays, moments, streak };
}

Page({
  data: {
    identity: null,
    profile: defaultProfile,
    profileFaceStyle: profileFaceStyleFor(defaultCompanionId),
    companions: companionCards,
    stats: { totalDays: 0, moments: 0, streak: 0 },
    loading: false,
    saving: false,
    error: "",
    showDelete: false,
    deleteConfirmation: "",
    deleting: false,
    deleteError: "",
    deleteConfirmText: DELETE_CONFIRMATION_TEXT,
    authenticated: false,
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    const authenticated = api.hasAuthenticatedSession();
    this.setData({ authenticated });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadProfile();
    this.loadStats();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      identity: null,
      profile: defaultProfile,
      profileFaceStyle: profileFaceStyleFor(defaultCompanionId),
      stats: { totalDays: 0, moments: 0, streak: 0 },
      saving: false,
      error: "",
      showDelete: false,
      deleteConfirmation: "",
      deleting: false,
      deleteError: "",
    });
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, identity, error: "" });
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        profile,
        profileFaceStyle: profileFaceStyleFor(profile.companion_id),
      });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "个人资料无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async loadStats() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    try {
      const result = await api.getMemoryDays(identity.user_id, 30);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ stats: computeStats(result.items) });
    } catch {
      // 统计只作展示，失败时保持默认值，不打断页面。
    }
  },

  onSwitch(event) {
    const key = event.currentTarget.dataset.key;
    this.setData({ [`profile.${key}`]: event.detail.value });
  },

  async chooseCompanion(event) {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const companionId = event.currentTarget.dataset.id;
    this.setData({
      "profile.companion_id": companionId,
      profileFaceStyle: profileFaceStyleFor(companionId),
    });
    await this.saveProfile();
  },

  async saveProfile() {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const identity = api.currentIdentity();
    if (!identity || this.data.saving) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ saving: true, error: "" });
    try {
      const profile = await api.updateProfile(identity.user_id, this.data.profile);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ profile: { ...this.data.profile, ...profile } });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "保存失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ saving: false });
    }
  },

  async loginFromProfile() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    this.setData({ authenticated: true });
    await Promise.all([this.loadProfile(), this.loadStats()]);
  },

  async openDigitalSelf() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    wx.navigateTo({ url: "/pages/digital-self/index" });
  },

  openPrivacy() {
    wx.navigateTo({ url: "/pages/privacy/index" });
  },

  async _finishLogout() {
    api.logoutLocal();
    this._enterGuestState();
  },

  async logout() {
    try {
      await api.logoutCurrentDevice();
    } catch {
      // 服务端登出失败时仍清理本地会话，保证可以重新登录。
    }
    await this._finishLogout();
  },

  logoutAll() {
    wx.showModal({
      title: "退出所有设备",
      content: "确定要结束所有设备上的登录吗？其他设备需要重新登录。",
      confirmText: "全部退出",
      confirmColor: "#ff6b8a",
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await api.logoutAllDevices();
        } catch {
          // 同上，失败也继续清理本地会话。
        }
        await this._finishLogout();
      },
    });
  },

  openDeleteModal() {
    this.setData({
      showDelete: true,
      deleteConfirmation: "",
      deleteError: "",
    });
  },

  closeDeleteModal() {
    if (this.data.deleting) return;
    this.setData({ showDelete: false });
  },

  noop() {},

  onDeleteConfirmation(event) {
    this.setData({ deleteConfirmation: event.detail.value });
  },

  async confirmDelete() {
    const { deleteConfirmation, deleting } = this.data;
    if (deleting) return;
    if (deleteConfirmation !== DELETE_CONFIRMATION_TEXT) {
      this.setData({ deleteError: `请完整输入「${DELETE_CONFIRMATION_TEXT}」。` });
      return;
    }
    this.setData({ deleting: true, deleteError: "" });
    try {
      await api.requestAccountDeletion({
        confirmation: deleteConfirmation,
      });
      this.setData({ showDelete: false, deleting: false });
      wx.showToast({ title: "注销申请已提交", icon: "success" });
      await this._finishLogout();
    } catch (error) {
      this.setData({
        deleting: false,
        deleteError: error?.message || "注销申请失败，请稍后重试。",
      });
    }
  },
});
