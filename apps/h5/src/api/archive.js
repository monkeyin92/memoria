import { request } from "./client.js";

export function getLifeTimeline(limit = 30) {
  return request(`/v1/archive/life-timeline?limit=${limit}`);
}

export function searchLifeArchive(query, limit = 30) {
  return request(
    `/v1/archive/search?q=${encodeURIComponent(query.trim())}` +
      `&include_candidates=true&limit=${limit}`,
  );
}

export function getMemoryReviewQueue() {
  return request("/v1/archive/review-queue");
}

export function reviewMemoryClaim(claimId, action, correctedValue = null) {
  const payload = { action };
  if (action === "correct") payload.corrected_value = correctedValue;
  return request(`/v1/archive/memories/${encodeURIComponent(claimId)}/review`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent");
}

export function grantRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent", {
    method: "POST",
    body: JSON.stringify({
      policy_version: "raw-voice-archive-v1",
      retention_policy: "account_lifetime",
    }),
  });
}

export function revokeRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent", { method: "DELETE" });
}


export function exportAccountArchive(password) {
  return request("/v1/archive/exports", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}
