import { request } from "./client.js";

export function getPersonaStatus() {
  return request("/v1/persona/status");
}

export function grantPersonaConsent() {
  return request("/v1/persona/consent", {
    method: "POST",
    body: JSON.stringify({
      accepted: true,
      policy_version: "persona-learning-v1",
    }),
  });
}

export function revokePersonaConsent() {
  return request("/v1/persona/consent", { method: "DELETE" });
}

export function getPersonaTraits() {
  return request("/v1/persona/traits");
}

export function reviewPersonaTrait(traitId, action, payload = {}) {
  return request(`/v1/persona/traits/${encodeURIComponent(traitId)}/review`, {
    method: "POST",
    body: JSON.stringify({ action, ...payload }),
  });
}

export function getPersonaVersions() {
  return request("/v1/persona/versions");
}


export function rollbackPersonaVersion(versionId) {
  return request(`/v1/persona/versions/${encodeURIComponent(versionId)}/rollback`, {
    method: "POST",
  });
}
