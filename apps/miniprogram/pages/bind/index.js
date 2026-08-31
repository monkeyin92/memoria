const api = require("../../utils/api");
const { companions } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const {
  MODE_META,
  PRIMARY_RELATIONSHIPS,
  AGE_BAND_LABELS,
  MODE_AGE_BANDS,
  consentOffersFor,
} = require("../../utils/device-binding");
const { isExpired } = require("../../utils/device-onboarding/state");

const SESSION_MINUTE_OPTIONS = [15, 30, 45, 60, 90, 120];
const FAMILY_MEMBER_AGE_BANDS = ["under_14", "14_17", "adult"];
const MEMORY_LEVEL_OPTIONS = [
  { value: "personal", label: "完整个人记忆" },
  { value: "none", label: "仅当前对话，不长期记忆" },
];
const INTERVIEW_FREQUENCY_OPTIONS = [
  { value: "low", label: "偶尔主动问候" },
  { value: "medium", label: "适度主动采访" },
  { value: "off", label: "不主动采访" },
];
const SPEECH_SPEED_OPTIONS = [
  { value: "slow", label: "慢速清晰" },
  { value: "normal", label: "正常语速" },
];
const ADMIN_VISIBILITY_OPTIONS = [
  { value: "device_status", label: "只看设备状态与提醒" },
  { value: "device_status_and_reminders", label: "设备状态 + 周报聚合" },
];

const modeCards = Object.entries(MODE_META).map(([id, meta]) => ({
  id,
  title: meta.title,
  tagline: meta.tagline,
  description: meta.description,
}));

const personaOptions = companions.map((companion) => ({
  id: companion.id,
  name: companion.name,
}));

let familyDraftCounter = 0;

function freshFamilyDraft() {
  familyDraftCounter += 1;
  return {
    key: `family-member-${familyDraftCounter}`,
    nickname: "",
    ageBand: "under_14",
    ageBandIndex: 0,
    isMinor: true,
  };
}

function defaultForm(mode, identity) {
  if (mode === "parent_for_child") {
    return {
      childNickname: "",
      childAgeBand: "under_14",
      tutorEnabled: true,
      englishPracticeEnabled: true,
      maxSessionMinutes: 30,
      quietHoursStart: "21:00",
      quietHoursEnd: "07:00",
      subjectSource: "new",
      existingSubjectIndex: 0,
    };
  }
  if (mode === "self_use") {
    return {
      selfNickname: identity?.display_name || "",
      memoryLevelIndex: 0,
      interviewFrequencyIndex: 0,
    };
  }
  if (mode === "child_for_parent") {
    return {
      adminIsBuyer: true,
      parentNickname: "",
      speechSpeedIndex: 0,
      adminVisibilityIndex: 0,
    };
  }
  return {
    familyName: "",
    adminNickname: identity?.display_name || "",
    sharedPersonaEnabled: true,
    familyDrafts: [freshFamilyDraft()],
  };
}

Page({
  data: {
    step: "loading",
    claimId: "",
    onboardingSessionId: "",
    claimLoading: true,
    claim: null,
    modeCards,
    declaredMode: "",
    modeMeta: null,
    ageBands: [],
    ageBandIndex: 0,
    form: null,
    offers: [],
    personaOptions,
    personaIndex: 0,
    existingSubjectOptions: [],
    subjectSourceOptions: [
      { value: "new", label: "新建孩子档案" },
      { value: "existing", label: "使用已有档案" },
    ],
    sessionMinuteOptions: SESSION_MINUTE_OPTIONS,
    sessionMinuteIndex: 1,
    memoryLevelOptions: MEMORY_LEVEL_OPTIONS,
    interviewFrequencyOptions: INTERVIEW_FREQUENCY_OPTIONS,
    speechSpeedOptions: SPEECH_SPEED_OPTIONS,
    adminVisibilityOptions: ADMIN_VISIBILITY_OPTIONS,
    error: "",
    submitting: false,
    manifest: null,
  },

  onLoad(options = {}) {
    this._claimId = typeof options.claim_id === "string" ? options.claim_id : "";
    this._onboardingSessionId =
      typeof options.onboarding_session_id === "string" ? options.onboarding_session_id : "";
    // claim_id is server-issued and uniquely scopes the binding Saga.  A
    // deterministic operation key lets reload/retry resume the same intent.
    this._idempotencyKey = this._claimId ? `bind-${this._claimId}` : "";
    this.setData({
      claimId: this._claimId,
      onboardingSessionId: this._onboardingSessionId,
    });
  },

  async onShow() {
    if (!(await requireLogin({ reason: "bind_device" }))) return;
    if (this._claimLoaded) return;
    await this.loadClaim();
  },

  async loadClaim() {
    if (!this._claimId || !this._onboardingSessionId) {
      this._claimLoaded = true;
      this.setData({
        claimLoading: false,
        step: "error",
        error: "绑定入口缺少服务端确认的 claim_id 或启用会话，请返回设备页重新开始。",
      });
      return null;
    }
    this.setData({ claimLoading: true, step: "loading", error: "" });
    try {
      const claim = await api.getDeviceClaim(this._claimId);
      if (claim.onboarding_session_id !== this._onboardingSessionId) {
        throw new Error("认领不属于当前启用会话，已拒绝继续绑定。");
      }
      if (claim.status === "expired" || isExpired(claim.expires_at)) {
        const error = new Error("认领保留已过期，请返回启用流程重新保留。");
        error.code = "CLAIM_EXPIRED";
        throw error;
      }
      if (!["reserved", "binding_committing", "binding_created"].includes(claim.status)) {
        const error = new Error("当前认领状态不允许创建绑定。");
        error.code = "CLAIM_CONFLICT";
        throw error;
      }
      this._claimLoaded = true;
      this.setData({ claim, claimLoading: false, step: "mode", error: "" });
      return claim;
    } catch (error) {
      this._claimLoaded = true;
      this.setData({
        claimLoading: false,
        step: "error",
        error: error?.message || "认领状态读取失败，请返回启用流程重试。",
      });
      return null;
    }
  },

  chooseMode(event) {
    const mode = event.currentTarget.dataset.mode;
    const modeSeq = (this._modeSeq = (this._modeSeq || 0) + 1);
    const identity = api.currentIdentity();
    const form = defaultForm(mode, identity);
    const bands =
      mode === "family_shared" ? FAMILY_MEMBER_AGE_BANDS : MODE_AGE_BANDS[mode];
    const ageBands = bands.map((value) => ({
      value,
      label: AGE_BAND_LABELS[value],
    }));
    const offers = consentOffersFor(mode).map((offer) => ({
      ...offer,
      checked: offer.defaultChecked,
    }));
    const acceptedOfferIds = offers
      .filter((offer) => offer.checked)
      .map((offer) => offer.id);
    this.setData({
      declaredMode: mode,
      modeMeta: MODE_META[mode],
      form,
      ageBands,
      ageBandIndex: 0,
      offers,
      acceptedOfferIds,
      existingSubjectOptions: [],
      error: "",
    });
    if (mode === "parent_for_child") {
      this.loadExistingSubjects(modeSeq);
    }
    this.setData({ step: "form" });
  },

  async loadExistingSubjects(modeSeq = this._modeSeq) {
    try {
      const links = await api.getGuardianLinks();
      if (modeSeq !== this._modeSeq || this.data.declaredMode !== "parent_for_child") {
        return;
      }
      const options = links
        .filter((link) => link.status === "active")
        .map((link, index) => ({
          minorUserId: link.minorUserId,
          label: link.displayName || `孩子 ${index + 1}`,
        }));
      this.setData({ existingSubjectOptions: options });
    } catch {
      if (modeSeq !== this._modeSeq || this.data.declaredMode !== "parent_for_child") {
        return;
      }
      this.setData({ existingSubjectOptions: [] });
    }
  },

  onSubjectSourceChange(event) {
    this.setData({ "form.subjectSource": event.detail.value, error: "" });
  },

  onExistingSubjectChange(event) {
    this.setData({ "form.existingSubjectIndex": Number(event.detail.value), error: "" });
  },

  onFieldInput(event) {
    const key = event.currentTarget.dataset.key;
    this.setData({ [`form.${key}`]: event.detail.value, error: "" });
  },

  onAgeBandChange(event) {
    const index = Number(event.detail.value);
    const value = this.data.ageBands[index]?.value || "";
    this.setData({ ageBandIndex: index, "form.childAgeBand": value, error: "" });
  },

  onSwitch(event) {
    const key = event.currentTarget.dataset.key;
    this.setData({ [`form.${key}`]: event.detail.value });
  },

  onSessionMinutesChange(event) {
    const index = Number(event.detail.value);
    this.setData({
      sessionMinuteIndex: index,
      "form.maxSessionMinutes": SESSION_MINUTE_OPTIONS[index],
    });
  },

  onQuietStartChange(event) {
    this.setData({ "form.quietHoursStart": event.detail.value });
  },

  onQuietEndChange(event) {
    this.setData({ "form.quietHoursEnd": event.detail.value });
  },

  onPickerChange(event) {
    const key = event.currentTarget.dataset.key;
    const index = Number(event.detail.value);
    this.setData({ [`form.${key}`]: index, error: "" });
  },

  onPersonaChange(event) {
    this.setData({ personaIndex: Number(event.detail.value) });
  },

  addFamilyMember() {
    const drafts = [...this.data.form.familyDrafts, freshFamilyDraft()];
    this.setData({ "form.familyDrafts": drafts, error: "" });
  },

  removeFamilyMember(event) {
    const index = Number(event.currentTarget.dataset.index);
    const drafts = this.data.form.familyDrafts.filter((_, itemIndex) => itemIndex !== index);
    if (drafts.length === 0) drafts.push(freshFamilyDraft());
    this.setData({ "form.familyDrafts": drafts });
  },

  onFamilyMemberInput(event) {
    const index = Number(event.currentTarget.dataset.index);
    this.setData({ [`form.familyDrafts[${index}].nickname`]: event.detail.value, error: "" });
  },

  onFamilyMemberAgeBand(event) {
    const index = Number(event.currentTarget.dataset.index);
    const bandIndex = Number(event.detail.value);
    const value = this.data.ageBands[bandIndex]?.value || "adult";
    const isMinor = value !== "adult";
    this.setData({
      [`form.familyDrafts[${index}].ageBand`]: value,
      [`form.familyDrafts[${index}].ageBandIndex`]: bandIndex,
      [`form.familyDrafts[${index}].isMinor`]: isMinor,
      error: "",
    });
  },

  onFamilyMemberMinor(event) {
    const index = Number(event.currentTarget.dataset.index);
    this.setData({
      [`form.familyDrafts[${index}].isMinor`]: event.detail.value,
      [`form.familyDrafts[${index}].ageBand`]: event.detail.value ? "under_14" : "adult",
      [`form.familyDrafts[${index}].ageBandIndex`]: event.detail.value ? 0 : 2,
    });
  },

  toggleOffer(event) {
    const offerId = event.currentTarget.dataset.id;
    const target = this.data.offers.find((offer) => offer.id === offerId);
    if (!target || target.requiresParentSelfAcceptance) return;
    const offers = this.data.offers.map((offer) =>
      offer.id === offerId ? { ...offer, checked: !offer.checked } : offer,
    );
    const acceptedOfferIds = offers.filter((offer) => offer.checked).map((offer) => offer.id);
    this.setData({ offers, acceptedOfferIds, error: "" });
  },

  validateForm() {
    const mode = this.data.declaredMode;
    const form = this.data.form;
    if (mode === "parent_for_child") {
      if (form.subjectSource === "new" && !form.childNickname.trim()) {
        return "请填写孩子的昵称。";
      }
      if (form.subjectSource === "existing" && this.data.existingSubjectOptions.length === 0) {
        return "还没有可用的孩子档案，请选择新建档案。";
      }
    }
    if (mode === "child_for_parent" && !form.parentNickname.trim()) {
      return "请填写父母怎么称呼（例如：妈妈、爸爸、爷爷）。";
    }
    if (mode === "family_shared") {
      if (!form.familyName.trim()) return "请给家庭空间起个名字。";
      const emptyMember = form.familyDrafts.some((draft) => !draft.nickname.trim());
      if (emptyMember) return "请填写每位家庭成员的称呼。";
    }
    return "";
  },

  goToReview() {
    const error = this.validateForm();
    if (error) {
      this.setData({ error });
      return;
    }
    this.setData({ step: "review", error: "" });
  },

  backToForm() {
    this.setData({ step: "form", error: "" });
  },

  backToMode() {
    this.setData({ step: "mode", error: "" });
  },

  _buildRequest() {
    const identity = api.currentIdentity();
    if (!identity) throw new Error("登录状态已失效，请重新登录。");
    const mode = this.data.declaredMode;
    const form = this.data.form;
    const preferences = {};
    let subjectPersonId = "new";
    let subjectDraft = null;
    if (mode === "parent_for_child") {
      if (form.subjectSource === "existing" && this.data.existingSubjectOptions.length > 0) {
        subjectPersonId =
          this.data.existingSubjectOptions[form.existingSubjectIndex]?.minorUserId || "new";
      }
      if (subjectPersonId === "new") {
        subjectDraft = {
          display_name: form.childNickname.trim(),
          age_band: this.data.ageBands[this.data.ageBandIndex]?.value || form.childAgeBand,
        };
      }
      preferences.tutor_enabled = form.tutorEnabled;
      preferences.english_practice_enabled = form.englishPracticeEnabled;
      preferences.memory_level = "growth_summary";
      preferences.max_session_minutes = form.maxSessionMinutes;
      preferences.quiet_hours = {
        start: form.quietHoursStart,
        end: form.quietHoursEnd,
      };
    } else if (mode === "self_use") {
      subjectPersonId = identity.user_id;
      preferences.memory_level =
        MEMORY_LEVEL_OPTIONS[form.memoryLevelIndex]?.value || "personal";
      preferences.interview_frequency =
        INTERVIEW_FREQUENCY_OPTIONS[form.interviewFrequencyIndex]?.value || "low";
    } else if (mode === "child_for_parent") {
      subjectDraft = { display_name: form.parentNickname.trim(), age_band: "adult" };
      preferences.speech_speed = SPEECH_SPEED_OPTIONS[form.speechSpeedIndex]?.value || "slow";
      preferences.memory_level = "none";
      preferences.admin_visibility =
        ADMIN_VISIBILITY_OPTIONS[form.adminVisibilityIndex]?.value || "device_status";
    } else {
      subjectPersonId = identity.user_id;
      preferences.memory_level = "family_shared";
      preferences.shared_persona_enabled = form.sharedPersonaEnabled;
    }
    const primarySubject = {
      person_id: subjectPersonId,
      relationship: PRIMARY_RELATIONSHIPS[mode],
    };
    if (subjectDraft) primarySubject.subject_draft = subjectDraft;
    return {
      claim_id: this._claimId,
      onboarding_session_id: this._onboardingSessionId,
      declared_mode: mode,
      account_owner_person_id: identity.user_id,
      primary_subject: primarySubject,
      persona_selection: this.data.personaOptions[this.data.personaIndex]?.id || "",
      service_preferences: preferences,
      consent_offer_ids: this.data.acceptedOfferIds,
    };
  },

  async submitBinding() {
    if (this.data.submitting) return;
    this.setData({ submitting: true, error: "" });
    try {
      const request = this._buildRequest();
      const manifest = await api.createDeviceBinding(request, {
        idempotencyKey: this._idempotencyKey,
      });
      this.setData({ manifest, step: "done" });
    } catch (error) {
      this.setData({ error: error?.message || "绑定失败，请稍后重试。" });
    } finally {
      this.setData({ submitting: false });
    }
  },

  openDevicePage() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  continueActivation() {
    const pages = typeof getCurrentPages === "function" ? getCurrentPages() : [];
    const previous = pages[pages.length - 2];
    if (typeof previous?.onBindingCreated === "function") {
      previous.onBindingCreated(this.data.manifest);
      wx.navigateBack({ delta: 1 });
      return;
    }
    wx.redirectTo({
      url:
        `/pages/device-onboarding/index?session_id=${encodeURIComponent(this._onboardingSessionId)}`,
    });
  },


  goHome() {
    wx.switchTab({ url: "/pages/device/index" });
  },
});
