// Tab pages draw their own large title under navigationStyle "custom", so they
// need the status bar height. env(safe-area-inset-top) reads 0 on Android WeChat.
function statusBarHeight() {
  try {
    const info = typeof wx.getWindowInfo === "function" ? wx.getWindowInfo() : wx.getSystemInfoSync();
    const height = Number(info && info.statusBarHeight);
    return Number.isFinite(height) && height > 0 ? Math.round(height) : 20;
  } catch (error) {
    return 20;
  }
}

function statusBarStyle() {
  return `--mp-status-bar: ${statusBarHeight()}px;`;
}

module.exports = { statusBarHeight, statusBarStyle };
