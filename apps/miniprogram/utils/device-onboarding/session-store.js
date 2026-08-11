"use strict";

const STORAGE_KEY = "memoria:miniprogram:onboarding_session_id";
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

function validSessionId(value) {
  return typeof value === "string" && ID_PATTERN.test(value);
}

function saveOnboardingSessionId(value) {
  if (!validSessionId(value)) throw new TypeError("onboarding_session_id 无效");
  wx.setStorageSync(STORAGE_KEY, value);
}

function readOnboardingSessionId() {
  try {
    const value = wx.getStorageSync(STORAGE_KEY);
    return validSessionId(value) ? value : "";
  } catch {
    return "";
  }
}

function clearOnboardingSessionId() {
  try {
    wx.removeStorageSync(STORAGE_KEY);
  } catch {
    // The in-memory controller remains authoritative for the current page.
  }
}

module.exports = {
  STORAGE_KEY,
  validSessionId,
  saveOnboardingSessionId,
  readOnboardingSessionId,
  clearOnboardingSessionId,
};
