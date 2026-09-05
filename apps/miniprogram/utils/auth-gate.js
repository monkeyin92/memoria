const api = require("./api");

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

function openLogin({ reason, redirect, skipRestore = false }) {
  const target = redirect || currentRoute();
  wx.navigateTo({
    url:
      `/pages/auth/index?redirect=${encodeURIComponent(target)}` +
      `&reason=${encodeURIComponent(reason || "protected_action")}` +
      (skipRestore ? "&skip_restore=1" : ""),
  });
}

async function requireLogin({ reason = "protected_action", redirect } = {}) {
  if (api.hasAuthenticatedSession()) return true;
  try {
    await api.restoreWechatIdentity();
    return true;
  } catch (error) {
    openLogin({
      reason,
      redirect,
      skipRestore: error?.code === "phone_authorization_required",
    });
    return false;
  }
}

module.exports = {
  currentRoute,
  openLogin,
  requireLogin,
};
