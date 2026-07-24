const storage = require("./utils/storage");

App({
  globalData: {
    accessToken: "",
    identity: null,
  },

  onLaunch() {
    this.globalData.identity = storage.readIdentitySnapshot();
  },

  setAuthenticatedIdentity(response) {
    const identity = storage.normalizeIdentity(response);
    if (!identity || typeof response.access_token !== "string" || !response.access_token) {
      throw new Error("账号身份响应无效");
    }
    this.globalData.identity = identity;
    this.globalData.accessToken = response.access_token;
    storage.writeIdentitySnapshot(identity);
    return identity;
  },

  clearAuthenticatedIdentity() {
    this.globalData.identity = null;
    this.globalData.accessToken = "";
    storage.clearIdentitySnapshot();
  },
});
