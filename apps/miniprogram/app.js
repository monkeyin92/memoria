const storage = require("./utils/storage");

App({
  globalData: {
    accessToken: "",
    accessTokenExpiresAt: 0,
    authEpoch: 0,
    identity: null,
  },
  _authClearedListeners: new Set(),

  onLaunch() {
    const snapshot = storage.readAuthSnapshot();
    if (!snapshot) return;
    this.globalData.identity = snapshot.identity;
    this.globalData.accessToken = snapshot.accessToken;
    this.globalData.accessTokenExpiresAt = snapshot.expiresAt;
  },

  setAuthenticatedIdentity(response) {
    const identity = storage.normalizeIdentity(response);
    if (!identity || typeof response.access_token !== "string" || !response.access_token) {
      throw new Error("账号身份响应无效");
    }
    const expiresAt = Date.now() + Math.max(0, Number(response.expires_in || 0)) * 1000;
    this.globalData.authEpoch += 1;
    this.globalData.identity = identity;
    this.globalData.accessToken = response.access_token;
    this.globalData.accessTokenExpiresAt = expiresAt;
    storage.writeAuthSnapshot({
      identity,
      accessToken: response.access_token,
      expiresAt,
    });
    return identity;
  },

  subscribeAuthCleared(listener) {
    this._authClearedListeners.add(listener);
    return () => this._authClearedListeners.delete(listener);
  },

  clearAuthenticatedIdentity() {
    this.globalData.authEpoch += 1;
    this.globalData.identity = null;
    this.globalData.accessToken = "";
    this.globalData.accessTokenExpiresAt = 0;
    storage.clearAuthSnapshot();
    for (const listener of [...this._authClearedListeners]) {
      try {
        listener();
      } catch {
        // One page cleanup must not prevent the remaining pages from clearing.
      }
    }
  },
});
