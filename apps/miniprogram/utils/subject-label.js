/*
 * 绑定初始化时填写的「使用者备注」。首页「当前使用者」只显示这条备注，
 * 不使用微信昵称，也不等待设备上的说话人确认。
 *
 * BindingManifest 是固定 23 字段合同，不能把备注写进去；因此按
 * binding_id + device_id 存在本地。解绑或换绑定时清理，避免串台。
 */
const STORAGE_KEY = "memoria:miniprogram:device:subject-label";
const MAX_LABEL = 16;

function storageAvailable() {
  try {
    return typeof wx !== "undefined" && typeof wx.setStorageSync === "function";
  } catch {
    return false;
  }
}

function normalizeSubjectLabel(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_LABEL);
}

function subjectLabelFromBindForm(mode, form = {}, extras = {}) {
  if (mode === "self_use") return normalizeSubjectLabel(form.selfNickname);
  if (mode === "parent_for_child") {
    if (form.subjectSource === "existing") {
      return (
        normalizeSubjectLabel(form.subjectAlias) ||
        normalizeSubjectLabel(extras.existingLabel)
      );
    }
    return normalizeSubjectLabel(form.childNickname);
  }
  if (mode === "child_for_parent") return normalizeSubjectLabel(form.parentNickname);
  if (mode === "family_shared") return normalizeSubjectLabel(form.subjectAlias);
  return "";
}

function saveSubjectLabel({ bindingId, deviceId, label } = {}) {
  const normalized = normalizeSubjectLabel(label);
  const binding_id = String(bindingId || "").trim();
  const device_id = String(deviceId || "").trim();
  if (!normalized || !binding_id || !device_id || !storageAvailable()) return false;
  try {
    wx.setStorageSync(STORAGE_KEY, {
      binding_id,
      device_id,
      label: normalized,
    });
    return true;
  } catch {
    return false;
  }
}

function readSubjectLabel(binding) {
  const bindingId = String(binding?.binding_id || "").trim();
  const deviceId = String(binding?.device_id || "").trim();
  if (!bindingId || !deviceId || !storageAvailable()) return "";
  try {
    const stored = wx.getStorageSync(STORAGE_KEY);
    if (!stored || typeof stored !== "object") return "";
    if (stored.binding_id !== bindingId || stored.device_id !== deviceId) return "";
    return normalizeSubjectLabel(stored.label);
  } catch {
    return "";
  }
}

function clearSubjectLabel() {
  if (!storageAvailable()) return;
  try {
    wx.removeStorageSync(STORAGE_KEY);
  } catch {
    // 清理失败不影响绑定流程。
  }
}

module.exports = {
  STORAGE_KEY,
  MAX_LABEL,
  normalizeSubjectLabel,
  subjectLabelFromBindForm,
  saveSubjectLabel,
  readSubjectLabel,
  clearSubjectLabel,
};
