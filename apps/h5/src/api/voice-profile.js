import { request } from "./client.js";

export function getSpeakerProfiles() {
  return request("/v1/speakers");
}

export function enrollSpeakerProfiles(samples) {
  return request("/v1/speakers/enrollments", {
    method: "POST",
    body: JSON.stringify({
      consent_policy_version: "speaker-biometric-v1",
      consent_accepted: true,
      samples,
    }),
  });
}

export function revokeSpeakerProfile(profileId, reason = "用户在 H5 撤销声纹档案") {
  return request(`/v1/speakers/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
    body: JSON.stringify({ reason }),
  });
}

export function getVoiceProfiles() {
  return request("/v1/voices/profiles");
}

export function grantVoiceConsent() {
  return request("/v1/voices/consent", {
    method: "POST",
    body: JSON.stringify({
      accepted: true,
      policy_version: "voice-clone-v1",
    }),
  });
}

export function revokeVoiceConsent() {
  return request("/v1/voices/consent", { method: "DELETE" });
}

export function enrollVoiceProfile(sample) {
  return request("/v1/voices/enrollments", {
    method: "POST",
    body: JSON.stringify(sample),
  });
}

export function createVoiceBlindTrial(profileId) {
  return request(
    `/v1/voices/profiles/${encodeURIComponent(profileId)}/blind-trials`,
    { method: "POST" },
  );
}

export function previewVoiceBlindTrial(trialId, slot, text) {
  return request(
    `/v1/voices/blind-trials/${encodeURIComponent(trialId)}/preview`,
    {
      method: "POST",
      body: JSON.stringify({ slot, text }),
    },
    { responseType: "blob" },
  );
}

export function evaluateVoiceProfile(profileId, evaluation) {
  return request(
    `/v1/voices/profiles/${encodeURIComponent(profileId)}/evaluations`,
    {
      method: "POST",
      body: JSON.stringify(evaluation),
    },
  );
}

export function activateVoiceProfile(profileId) {
  return request(`/v1/voices/profiles/${encodeURIComponent(profileId)}/activate`, {
    method: "POST",
  });
}

export function revokeVoiceProfile(profileId) {
  return request(`/v1/voices/profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
}
