// A page opened from a share card or scan sits alone in the stack, so going
// back has to land on the tab or page that owns it instead.
const TAB_PAGES = Object.freeze([
  "/pages/home/index",
  "/pages/memory/index",
  "/pages/companion/index",
  "/pages/device/index",
  "/pages/profile/index",
]);

function leavePage(fallbackUrl = "/pages/home/index") {
  const stack = typeof getCurrentPages === "function" ? getCurrentPages() : [];
  if (stack.length > 1) {
    wx.navigateBack({ delta: 1 });
    return "back";
  }
  if (TAB_PAGES.includes(fallbackUrl)) wx.switchTab({ url: fallbackUrl });
  else wx.redirectTo({ url: fallbackUrl });
  return "fallback";
}

module.exports = { TAB_PAGES, leavePage };
