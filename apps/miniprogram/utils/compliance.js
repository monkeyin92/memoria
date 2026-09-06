const SESSION_START_KEY = "memoria:miniprogram:foreground_session_start";
const REMINDER_SHOWN_KEY = "memoria:miniprogram:foreground_reminder_shown";
const FOREGROUND_REMINDER_MS = 2 * 60 * 60 * 1000;
const REMINDER_CHECK_MS = 60 * 1000;

const PRODUCT_POSITIONING =
  "Memoria 是家庭桌面档案终端：在设备上使用，在手机上查看和管理。";
const AI_DISCLOSURE =
  "设备回应由人工智能生成，会在交互中明确标识。";
const TRAINING_DEFAULT_OFF = "你的对话与声纹默认不用于模型训练。";
const MINOR_RESTRICTIONS =
  "未成年人账号不提供虚拟亲属或伴侣角色，敏感能力由服务端按主体授权。";

function startForegroundSession() {
  try {
    wx.setStorageSync(SESSION_START_KEY, Date.now());
    wx.removeStorageSync(REMINDER_SHOWN_KEY);
  } catch {
    // Storage is best effort; reminder still runs in-memory for this session.
  }
}

function endForegroundSession() {
  try {
    wx.removeStorageSync(SESSION_START_KEY);
    wx.removeStorageSync(REMINDER_SHOWN_KEY);
  } catch {
    // Ignore cleanup failures.
  }
}

function checkContinuousUseReminder() {
  let sessionStart = 0;
  let reminderShown = false;
  try {
    sessionStart = Number(wx.getStorageSync(SESSION_START_KEY) || 0);
    reminderShown = Boolean(wx.getStorageSync(REMINDER_SHOWN_KEY));
  } catch {
    return;
  }
  if (!sessionStart || reminderShown) return;
  if (Date.now() - sessionStart < FOREGROUND_REMINDER_MS) return;
  try {
    wx.setStorageSync(REMINDER_SHOWN_KEY, true);
  } catch {
    // Continue showing the reminder even if persistence fails.
  }
  wx.showModal({
    title: "休息一下",
    content:
      "你已连续使用 Memoria 超过 2 小时。" +
      PRODUCT_POSITIONING +
      AI_DISCLOSURE +
      "请合理安排使用时间。",
    showCancel: false,
    confirmText: "知道了",
  });
}

module.exports = {
  SESSION_START_KEY,
  REMINDER_SHOWN_KEY,
  FOREGROUND_REMINDER_MS,
  REMINDER_CHECK_MS,
  PRODUCT_POSITIONING,
  AI_DISCLOSURE,
  TRAINING_DEFAULT_OFF,
  MINOR_RESTRICTIONS,
  startForegroundSession,
  endForegroundSession,
  checkContinuousUseReminder,
};
