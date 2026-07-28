import { getActiveIdentity, request } from "./client.js";
import { createClientMessageId } from "./shared.js";

function ensureClientMessageId(message) {
  if (typeof message?.client_message_id === "string" && message.client_message_id) {
    return message;
  }
  message.client_message_id = createClientMessageId();
  return message;
}


export function saveMessage(message) {
  if (message?.history_eligible !== true) return Promise.resolve(null);
  const payload = { ...ensureClientMessageId(message) };
  delete payload.history_eligible;
  return request("/v1/memory/messages", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMemoryDays(userId, limit = 14) {
  return request(
    `/v1/memory/days?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
  );
}

export function summarizeDay(userId, date) {
  return request(`/v1/memory/days/${encodeURIComponent(date)}/summary`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}


function pendingMessagesKey(userId) {
  return `memoria:pending-messages:${userId}`;
}

function readPendingMessages(key) {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}


export function clearPendingMessagesForUser(userId) {
  window.localStorage.removeItem(pendingMessagesKey(userId));
  const legacyKey = "memoria:pending-messages";
  const remaining = readPendingMessages(legacyKey).filter(
    (message) => message?.user_id !== userId,
  );
  if (remaining.length) {
    window.localStorage.setItem(legacyKey, JSON.stringify(remaining));
  } else {
    window.localStorage.removeItem(legacyKey);
  }
}

export function cachePendingMessage(message) {
  if (!message?.user_id || message.history_eligible !== true) return;
  const key = pendingMessagesKey(message.user_id);
  const pending = readPendingMessages(key);
  pending.push(ensureClientMessageId(message));
  window.localStorage.setItem(key, JSON.stringify(pending.slice(-80)));
}

export async function flushPendingMessages() {
  const identity = getActiveIdentity();
  const userId = identity?.user_id;
  if (!userId) return;
  const isCurrentIdentity = () => getActiveIdentity() === identity;
  const key = pendingMessagesKey(userId);
  const legacyKey = "memoria:pending-messages";
  const legacy = readPendingMessages(legacyKey);
  const pending = [
    ...readPendingMessages(key),
    ...legacy.filter((message) => message?.user_id === userId),
  ].map(ensureClientMessageId);
  const otherAccounts = legacy.filter((message) => message?.user_id !== userId);
  if (otherAccounts.length) {
    window.localStorage.setItem(legacyKey, JSON.stringify(otherAccounts));
  } else {
    window.localStorage.removeItem(legacyKey);
  }
  if (!pending.length) return;
  window.localStorage.setItem(key, JSON.stringify(pending));
  const failed = [];
  for (const message of pending) {
    if (!isCurrentIdentity()) return;
    try {
      await saveMessage(message);
    } catch {
      if (!isCurrentIdentity()) return;
      failed.push(message);
    }
  }
  if (!isCurrentIdentity()) return;
  window.localStorage.setItem(key, JSON.stringify(failed));
}
