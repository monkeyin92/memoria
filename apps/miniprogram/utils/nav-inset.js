// env(safe-area-inset-top) reads 0 on Android WeChat.
function statusBarHeight() {
  try {
    const info = typeof wx.getWindowInfo === "function" ? wx.getWindowInfo() : wx.getSystemInfoSync();
    const height = Number(info && info.statusBarHeight);
    return Number.isFinite(height) && height > 0 ? Math.round(height) : 20;
  } catch (error) {
    return 20;
  }
}

// Bottom edge of the WeChat capsule, so content on the right of the first row
// can sit below it. Android places the capsule lower than iOS.
function capsuleBottom(statusBar) {
  try {
    const rect = wx.getMenuButtonBoundingClientRect();
    const bottom = Number(rect && rect.bottom);
    return Number.isFinite(bottom) && bottom > statusBar ? Math.round(bottom) : statusBar + 40;
  } catch (error) {
    return statusBar + 40;
  }
}

function statusBarStyle() {
  const statusBar = statusBarHeight();
  return `--mp-status-bar: ${statusBar}px; --mp-capsule-bottom: ${capsuleBottom(statusBar)}px;`;
}

module.exports = { statusBarHeight, statusBarStyle };
