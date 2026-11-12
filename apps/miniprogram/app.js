const storage = require("./utils/storage");
const compliance = require("./utils/compliance");

App({
  globalData: {
    accessToken: "",
    accessTokenExpiresAt: 0,
    authEpoch: 0,
    identity: null,
    openUnbindSheetOnDevice: false,
  },
  _authClearedListeners: new Set(),
  _reminderTimer: null,

  onLaunch() {
    const snapshot = storage.readAuthSnapshot();
    if (!snapshot) return;
    this.globalData.identity = snapshot.identity;
    this.globalData.accessToken = snapshot.accessToken;
    this.globalData.accessTokenExpiresAt = snapshot.expiresAt;
  },

  onShow() {
    compliance.startForegroundSession();
    if (this._reminderTimer) clearInterval(this._reminderTimer);
    this._reminderTimer = setInterval(
      () => compliance.checkContinuousUseReminder(),
      compliance.REMINDER_CHECK_MS,
    );
    compliance.checkContinuousUseReminder();
  },

  onHide() {
    compliance.endForegroundSession();
    if (this._reminderTimer) {
      clearInterval(this._reminderTimer);
      this._reminderTimer = null;
    }
  },

  setAuthenticatedIdentity(response) {
    const identity = storage.normalizeIdentity(response);
    if (!identity || typeof response.access_token !== "string" || !response.access_token) {
      throw new Error("账号身份响应无效");
    }
    const previous = this.globalData.identity;
    if (!previous || previous.user_id !== identity.user_id ||
        previous.account_type !== identity.account_type) {
      this.clearAuthenticatedIdentity();
    } else {
      this.globalData.authEpoch += 1;
    }
    const expiresAt = Date.now() + Math.max(0, Number(response.expires_in || 0)) * 1000;
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
    require("./utils/api").clearDeviceBindingContext();
    require("./utils/child-consent").clearChildInfoConsent();
    require("./utils/device-onboarding/session-store").clearOnboardingSessionId();
    for (const listener of [...this._authClearedListeners]) {
      try {
        listener();
      } catch {
        // One page cleanup must not prevent the remaining pages from clearing.
      }
    }
  },
});
