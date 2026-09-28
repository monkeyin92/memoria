const api = require("../../utils/api");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const { readBindingManifest } = require("../../utils/device-binding");

// 选定的伙伴也是已绑定设备的人格：服务端在保存后把它下发到设备，
// 下一次对话起换声音，待机形象随后刷新。没有绑定设备时只提示已保存。
function savedToastTitle() {
  return readBindingManifest() ? "已保存，设备将同步切换" : "已保存";
}

// 卡组里每张卡相对当前卡的位置：0 在最前，±1、±2 依次叠在后面左右露出一角。
// 五位伙伴循环排列，所以左右各露两张，刚好铺满一圈。
const STACK = Object.freeze({
  0: { x: 0, y: 0, scale: 1, opacity: 1, z: 10 },
  1: { x: 64, y: 28, scale: 0.9, opacity: 0.92, z: 8 },
  2: { x: 116, y: 52, scale: 0.8, opacity: 0.7, z: 6 },
});
// 拖过这个距离（px）松手就换到下一张。
const SWIPE_THRESHOLD_PX = 56;

function stackOffset(index, current, count) {
  const half = Math.floor(count / 2);
  return ((index - current + count + half) % count) - half;
}

function cardStyle(offset, dragPx = 0) {
  const slot = STACK[Math.abs(offset)] || STACK[2];
  const direction = offset < 0 ? -1 : 1;
  const tilt = offset === 0 ? dragPx * 0.04 : 0;
  const drag = offset === 0 ? ` translateX(${dragPx}px) rotate(${tilt}deg)` : "";
  return (
    `z-index:${slot.z};opacity:${slot.opacity};` +
    `transform:translateX(${direction * slot.x}rpx) translateY(${slot.y}rpx) scale(${slot.scale})${drag};`
  );
}

function deckCards(current, dragPx = 0) {
  return companions.map((companion, index) => {
    const offset = stackOffset(index, current, companions.length);
    return {
      id: companion.id,
      name: companion.name,
      tagline: companion.tagline,
      description: companion.description,
      voiceName: companion.voiceName,
      tone: companion.tone,
      previewSrc: `/assets/voices/${companion.voiceId}.mp3`,
      offset,
      front: offset === 0,
      style: cardStyle(offset, dragPx),
    };
  });
}

function companionIndex(companionId) {
  const index = companions.findIndex((item) => item.id === companionId);
  return index >= 0 ? index : 0;
}

Page({
  data: {
    cards: deckCards(companionIndex(defaultCompanionId)),
    currentIndex: companionIndex(defaultCompanionId),
    dragging: false,
    profile: { companion_id: defaultCompanionId },
    currentName: companionById(defaultCompanionId).name,
    currentTone: companionById(defaultCompanionId).tone,
    browsingId: defaultCompanionId,
    browsingName: companionById(defaultCompanionId).name,
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
      // 打开时停在当前伙伴这张卡上。
      this._showCard(companionIndex(companion.id));
    } catch (error) {
      wx.showToast({ title: error?.message || "资料暂时无法读取", icon: "none" });
    }
  },

  _showCard(index) {
    const count = companions.length;
    const current = ((index % count) + count) % count;
    const companion = companions[current];
    if (this.data.previewingId && this.data.previewingId !== companion.id) this._stopPreview();
    this.setData({
      currentIndex: current,
      cards: deckCards(current),
      browsingId: companion.id,
      browsingName: companion.name,
    });
  },

  onDeckTouchStart(event) {
    const touch = event.touches?.[0];
    if (!touch) return;
    this._touchStartX = touch.clientX;
    this._touchStartY = touch.clientY;
    this._dragPx = 0;
    this._horizontal = null;
  },

  onDeckTouchMove(event) {
    const touch = event.touches?.[0];
    if (!touch || this._touchStartX === undefined) return;
    const dx = touch.clientX - this._touchStartX;
    const dy = touch.clientY - this._touchStartY;
    // 先判断手势方向：竖着滑留给页面滚动，横着滑才拖卡片。
    if (this._horizontal === null && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
      this._horizontal = Math.abs(dx) > Math.abs(dy);
    }
    if (!this._horizontal) return;
    this._dragPx = dx;
    const frontIndex = this.data.currentIndex;
    this.setData({
      dragging: true,
      [`cards[${frontIndex}].style`]: cardStyle(0, dx),
    });
  },

  onDeckTouchEnd() {
    const dx = this._dragPx || 0;
    this._touchStartX = undefined;
    this._dragPx = 0;
    if (!this._horizontal) return;
    this._horizontal = null;
    this.setData({ dragging: false });
    if (dx <= -SWIPE_THRESHOLD_PX) {
      this._showCard(this.data.currentIndex + 1);
    } else if (dx >= SWIPE_THRESHOLD_PX) {
      this._showCard(this.data.currentIndex - 1);
    } else {
      this._showCard(this.data.currentIndex);
    }
  },

  // 点后面露出的卡片直接翻到它。
  onCardTap(event) {
    const offset = Number(event.currentTarget.dataset.offset);
    if (!Number.isInteger(offset) || offset === 0) return;
    this._showCard(this.data.currentIndex + offset);
  },

  onDotTap(event) {
    const index = Number(event.currentTarget.dataset.index);
    if (Number.isInteger(index)) this._showCard(index);
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

  // 自定义声音在「我的」里录制/上传；这一行只是入口，不在这里采集音频。
  openVoiceClone() {
    wx.switchTab({ url: "/pages/profile/index" });
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
