const api = require("./api");
const { LOGIN_REQUIRED_COPY } = require("./guest-access");

function currentRoute() {
  try {
    const pages = getCurrentPages();
    const page = pages[pages.length - 1];
    if (!page?.route) return "/pages/home/index";
    const query = Object.entries(page.options || {})
      .filter(([, value]) => value !== undefined && value !== null)
      .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
      .join("&");
    return `/${page.route}${query ? `?${query}` : ""}`;
  } catch {
    return "/pages/home/index";
  }
}

function openLogin({ reason, redirect } = {}) {
  const target = redirect || currentRoute();
  wx.navigateTo({
    url:
      `/pages/auth/index?redirect=${encodeURIComponent(target)}` +
      `&reason=${encodeURIComponent(reason || "protected_action")}`,
  });
}

function requireLogin({ reason = "protected_action", redirect } = {}) {
  if (api.hasAuthenticatedSession()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (confirmed) => {
      if (settled) return;
      settled = true;
      if (confirmed) openLogin({ reason, redirect });
      resolve(false);
    };
    wx.showModal({
      title: "需要登录",
      content: LOGIN_REQUIRED_COPY,
      confirmText: "去登录",
      cancelText: "先看看",
      success: (result) => finish(Boolean(result?.confirm)),
      fail: () => finish(false),
    });
  });
}

module.exports = {
  currentRoute,
  openLogin,
  requireLogin,
};
