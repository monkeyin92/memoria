import { clearDeletedIdentityState, getActiveIdentity, request } from "./client.js";
import { clearPendingMessagesForUser } from "./memory.js";

function clearDeletedIdentity() {
  const userId = getActiveIdentity()?.user_id;
  if (userId) {
    window.localStorage.removeItem(`memoria:profile:${userId}`);
    clearPendingMessagesForUser(userId);
  }
  clearDeletedIdentityState();
}

export async function deleteAccountData(password, confirmation) {
  const result = await request("/v1/archive/deletion-requests", {
    method: "POST",
    body: JSON.stringify({ password, confirmation }),
  });
  clearDeletedIdentity();
  return result;
}
