// 自定义人格创建页（P1-03/P1-04）：名字 + 描述，或手填服务端同一套受控字段。
// 服务端只接受十一个受控字段（超域值拒绝、不收窄），创建即冻结 v1。
const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const { companions } = require("../../utils/companions");

const ENUM_FIELDS = Object.freeze([
  {
    key: "warmth",
    label: "温度",
    options: [
      { value: "warm", label: "温暖" },
      { value: "bright", label: "明亮" },
      { value: "soft", label: "柔和" },
      { value: "calm", label: "沉稳" },
      { value: "reserved", label: "克制" },
    ],
  },
  {
    key: "directness",
    label: "直接程度",
    options: [
      { value: "gentle", label: "委婉" },
      { value: "direct", label: "直接" },
    ],
  },
  {
    key: "response_length",
    label: "回复长度",
    options: [
      { value: "brief", label: "简短" },
      { value: "balanced", label: "适中" },
    ],
  },
  {
    key: "question_frequency",
    label: "提问频率",
    options: [
      { value: "rare", label: "很少" },
      { value: "occasional", label: "偶尔" },
      { value: "frequent", label: "经常" },
    ],
  },
  {
    key: "interview_depth",
    label: "访谈深度",
    options: [
      { value: "light", label: "轻" },
      { value: "structured", label: "有条理" },
      { value: "on_explicit_invitation", label: "你邀请时" },
    ],
  },
  {
    key: "default_voice_emotion",
    label: "默认语气",
    options: [
      { value: "neutral", label: "自然" },
      { value: "happy", label: "轻快" },
    ],
  },
]);

const DEFAULT_FIELDS = Object.freeze({
  style_description: "",
  warmth: "warm",
  directness: "gentle",
  response_length: "brief",
  question_frequency: "rare",
  interview_depth: "light",
  welcome_text: "嗨，我在。",
  conversation_instruction: "",
  voice_instruction: "",
  default_voice_emotion: "neutral",
  default_voice_rate: 1.0,
});

const TEXT_FIELDS = Object.freeze([
  { key: "style_description", label: "一句话风格", placeholder: "例如：温和的陪伴者", maxlength: 60 },
  { key: "welcome_text", label: "打招呼", placeholder: "例如：嗨，我在。", maxlength: 60 },
  { key: "conversation_instruction", label: "说话方式", placeholder: "例如：说话短一点，像朋友。", maxlength: 200 },
  { key: "voice_instruction", label: "声音提示", placeholder: "例如：轻松自然", maxlength: 120 },
]);

function indexOfOption(options, value) {
  const index = options.findIndex((option) => option.value === value);
  return index >= 0 ? index : 0;
}

Page({
  data: {
    loading: false,
    loadingError: "",
    personas: [],
    acting: false,
    error: "",
    notice: "",
    name: "",
    freeText: "",
    draftId: "",
    structuring: false,
    fields: { ...DEFAULT_FIELDS },
    enumFields: ENUM_FIELDS.map((field) => ({ ...field, index: 0 })),
    textFields: TEXT_FIELDS,
    voiceOptions: companions.map((companion) => ({
      id: companion.id,
      name: companion.name,
    })),
    voiceIndex: 0,
    rateValue: 100,
  },

  async onShow() {
    if (
      !(await requireLogin({
        reason: "custom_persona",
        redirect: "/pages/persona-custom/index",
      }))
    )
      return;
    this.loadPersonas();
  },

  async loadPersonas() {
    if (!api.hasAuthenticatedSession()) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, loadingError: "" });
    try {
      const payload = await api.listPersonas();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        personas: Array.isArray(payload?.custom_personas) ? payload.custom_personas : [],
      });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ loadingError: error?.message || "自定义人格列表读取失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  onNameInput(event) {
    this.setData({ name: event.detail.value, error: "" });
  },

  onFreeTextInput(event) {
    this.setData({ freeText: event.detail.value, error: "" });
  },

  onTextFieldInput(event) {
    const key = event.currentTarget.dataset.field;
    if (!key) return;
    this.setData({ [`fields.${key}`]: event.detail.value, error: "" });
  },

  onEnumChange(event) {
    const key = event.currentTarget.dataset.field;
    const index = Number(event.detail.value);
    const field = ENUM_FIELDS.find((item) => item.key === key);
    if (!field || !Number.isInteger(index) || !field.options[index]) return;
    const enumFields = this.data.enumFields.map((item) =>
      item.key === key ? { ...item, index } : item,
    );
    this.setData({
      [`fields.${key}`]: field.options[index].value,
      enumFields,
      error: "",
    });
  },

  onVoiceChange(event) {
    const index = Number(event.detail.value);
    if (!Number.isInteger(index) || !this.data.voiceOptions[index]) return;
    this.setData({ voiceIndex: index, error: "" });
  },

  onRateChange(event) {
    const value = Number(event.detail.value);
    if (!Number.isFinite(value) || value < 90 || value > 110) return;
    this.setData({
      rateValue: Math.round(value),
      "fields.default_voice_rate": Math.round(value) / 100,
      error: "",
    });
  },

  // 结构化是纯函数：只在服务端接入时可用，未接入必须如实说明而不是假装整理过。
  async structure() {
    const freeText = String(this.data.freeText || "").trim();
    if (!freeText) {
      this.setData({ error: "先写几句它怎么说话，再让 AI 整理。" });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ structuring: true, error: "", notice: "" });
    try {
      const result = await api.structureCustomPersona(freeText);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const structured = result?.structured || {};
      const enumFields = this.data.enumFields.map((item) => ({
        ...item,
        index:
          item.key in structured
            ? indexOfOption(item.options, structured[item.key])
            : item.index,
      }));
      this.setData({
        fields: { ...this.data.fields, ...structured },
        enumFields,
        draftId: typeof result?.draft_id === "string" ? result.draft_id : "",
        rateValue: Math.round(
          Number(structured.default_voice_rate ?? this.data.fields.default_voice_rate) *
            100,
        ),
        notice: "已按 AI 整理结果预填，确认无误后再创建。",
      });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const unavailable = error?.code === "persona_structuring_unavailable";
      this.setData({
        error: unavailable
          ? "服务端还没接入 AI 整理，请直接手填下面这些受控字段。"
          : error?.message || "整理失败，请稍后重试或直接手填。",
      });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ structuring: false });
    }
  },

  async create() {
    const name = String(this.data.name || "").trim();
    if (!name) {
      this.setData({ error: "先给这个自定义人格起个名字（16 字以内）。" });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ acting: true, error: "", notice: "" });
    try {
      const record = await api.createCustomPersona({
        displayName: name,
        structured: { ...this.data.fields },
        fallbackDesignedVoice:
          this.data.voiceOptions[this.data.voiceIndex]?.id || "starlight",
      });
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        name: "",
        freeText: "",
        draftId: "",
        fields: { ...DEFAULT_FIELDS },
        enumFields: ENUM_FIELDS.map((field) => ({ ...field, index: 0 })),
        rateValue: 100,
        notice: `已创建并冻结：${record?.display_name || name}（${
          record?.persona_id || "cu_…"
        }）。可在设备页分配给某位使用人。`,
      });
      await this.loadPersonas();
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "创建失败，请稍后重试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },

  async remove(event) {
    const personaId = event.currentTarget.dataset.personaId;
    if (!personaId || this.data.acting) return;
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "删除自定义人格？",
        content: "删除后无法恢复；仍在使用中的话会先让你确认一次。",
        confirmColor: "#ba4255",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ acting: true, error: "", notice: "" });
    try {
      await api.deleteCustomPersona(personaId);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ notice: "已删除自定义人格。" });
      await this.loadPersonas();
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "删除失败，请稍后重试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },
});
