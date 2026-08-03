export {
  bootstrapIdentity,
  createAnonymousIdentity,
  getAccessToken,
  loginAccount,
  logoutAllDevices,
  logoutCurrentDevice,
  registerAccount,
} from "./api/client.js";

export {
  createSession,
  exchangeOmniSdp,
  reconnectMediaSession,
  fallbackMediaSession,
  renewMediaSession,
  getInteractionCapabilities,
  notifyRtcRecovered,
  publishOmniTelemetry,
  stopResponse,
} from "./api/session.js";

export {
  cachePendingMessage,
  flushPendingMessages,
  getMemoryDays,
  saveMessage,
  summarizeDay,
} from "./api/memory.js";

export {
  exportAccountArchive,
  getLifeTimeline,
  getMemoryReviewQueue,
  getRawVoiceConsent,
  grantRawVoiceConsent,
  reviewMemoryClaim,
  revokeRawVoiceConsent,
  searchLifeArchive,
} from "./api/archive.js";

export { getProfile, updateProfile } from "./api/profile.js";

export {
  getPersonaStatus,
  getPersonaTraits,
  getPersonaVersions,
  grantPersonaConsent,
  reviewPersonaTrait,
  revokePersonaConsent,
  rollbackPersonaVersion,
} from "./api/persona.js";

export {
  createGrowthTask,
  getGrowthOverview,
  getGrowthTasks,
  respondGrowthTask,
  reviewGrowthOwnerAction,
  transitionGrowthTask,
} from "./api/growth.js";

export {
  addSelfModelClaimCounterexample,
  getSelfModel,
  reviewSelfModelClaim,
  reviewSelfModelDecisionCase,
  reviewSelfModelRelationshipProfile,
} from "./api/self-model.js";

export {
  approveDigitalSelfVersion,
  beginDigitalSelfTesting,
  buildDigitalSelfVersion,
  freezeDigitalSelfVersion,
  getDigitalSelfVersion,
  getDigitalSelfVersions,
  revokeDigitalSelfVersion,
  rollbackDigitalSelfVersion,
} from "./api/digital-self.js";

export {
  chooseFidelityTrial,
  completeFidelityEvaluation,
  getFidelityEvaluation,
  getFidelityEvaluations,
  getSelfPreviewCapability,
  getSelfPreviewSources,
  issueSelfPreviewGrant,
  revokeSelfPreviewGrant,
  startFidelityEvaluation,
  submitSelfPreviewFeedback,
} from "./api/preview.js";

export {
  activateLegacyGrant,
  createLegacyGrant,
  getLegacyGrants,
  getLegacyShellPreferences,
  revokeLegacyGrant,
  updateLegacyShellPreferences,
} from "./api/legacy.js";

export {
  activateVoiceProfile,
  createVoiceBlindTrial,
  enrollSpeakerProfiles,
  enrollVoiceProfile,
  evaluateVoiceProfile,
  getSpeakerProfiles,
  getVoiceProfiles,
  grantVoiceConsent,
  previewVoiceBlindTrial,
  revokeSpeakerProfile,
  revokeVoiceConsent,
  revokeVoiceProfile,
} from "./api/voice-profile.js";

export { deleteAccountData } from "./api/account.js";
