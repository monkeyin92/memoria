const PRIVACY_SUMMARY =
  "首次登录仅使用微信提供的登录凭证、手机号授权结果，以及你主动选择的昵称和头像来建立账号。未登录浏览不会读取个人回顾或资料。";

function showPrivacySummary() {
  wx.showModal({
    title: "隐私说明",
    content: PRIVACY_SUMMARY,
    showCancel: false,
    confirmText: "知道了",
  });
}

// Older base libraries lack openPrivacyContract, and it fails when no guideline is configured.
function openPrivacyContract() {
  if (typeof wx.openPrivacyContract !== "function") {
    showPrivacySummary();
    return;
  }
  wx.openPrivacyContract({ fail: showPrivacySummary });
}

module.exports = { openPrivacyContract };
