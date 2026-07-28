import { request } from "./client.js";

export function getProfile(userId) {
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`);
}

export function updateProfile(userId, profile) {
  const payload = {};
  for (const field of [
    "display_name",
    "bio",
    "auto_summary",
    "voice_reply",
    "gentle_reminders",
    "reject_non_owner_voice",
    "companion_id",
  ]) {
    if (profile[field] !== undefined) payload[field] = profile[field];
  }
  if (profile.timezone !== undefined) payload.timezone = profile.timezone;
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}
