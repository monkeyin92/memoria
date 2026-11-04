/* tabBar 页面只能用 wx.switchTab 打开（navigateTo / redirectTo 会直接失败，
 * 且 switchTab 不接受 query）。小程序运行时无法可靠 require app.json，
 * 这里保留一份副本，由 tests/tab-navigation.test.js 校验与 app.json 的
 * tabBar.list 完全一致。 */
const TAB_ROUTES = new Set([
  "/pages/home/index",
  "/pages/memory/index",
  "/pages/companion/index",
  "/pages/device/index",
  "/pages/profile/index",
]);

function routeOf(url) {
  return typeof url === "string" ? url.split("?", 1)[0] : "";
}

function isTabRoute(url) {
  return TAB_ROUTES.has(routeOf(url));
}

module.exports = { TAB_ROUTES, routeOf, isTabRoute };
