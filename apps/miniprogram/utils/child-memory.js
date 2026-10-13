/**
 * Parent-facing child memory layers (Scheme A).
 * Only paraphrased session summaries and whitelist archive facts.
 * Never normalizes raw transcripts, audio, or utterance fields into view models.
 */

const ARCHIVE_FIELD_LABELS = Object.freeze({
  appellation: "怎么称呼",
  preference: "喜欢什么",
  pet_name: "小名 / 宠物名",
});

const CHILD_ARCHIVE_CONSENT = Object.freeze({
  kind: "child_archive",
  label: "长期小档案",
  description: "只记住称呼、喜好和小名这类短事实。关掉后不再写入，页面也不再展示。",
  policyVersion: "child-archive-v1",
});

function firstItems(payload) {
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload)) return payload;
  return [];
}

function displayStamp(value) {
  if (typeof value !== "string" || !value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${month}月${day}日 ${hour}:${minute}`;
}

function normalizeSessionSummaries(payload) {
  if (payload?.privacy?.contains_transcript === true) {
    throw new TypeError("会话小结响应含有不允许展示的原文标记");
  }
  const items = firstItems(payload)
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const recordId = typeof item.record_id === "string" ? item.record_id.trim() : "";
      const text = typeof item.text === "string" ? item.text.trim() : "";
      if (!recordId || !text) return null;
      const createdAt = typeof item.created_at === "string" ? item.created_at : "";
      return {
        recordId,
        text,
        sessionId: typeof item.session_id === "string" ? item.session_id : "",
        createdAt,
        createdLabel: displayStamp(createdAt) || "最近",
        retentionExpiresAt:
          typeof item.retention_expires_at === "string" ? item.retention_expires_at : "",
      };
    })
    .filter(Boolean);
  items.sort((left, right) => {
    const a = Date.parse(left.createdAt) || 0;
    const b = Date.parse(right.createdAt) || 0;
    return b - a;
  });
  return items;
}

function normalizeChildArchive(payload) {
  const items = firstItems(payload)
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const recordId = typeof item.record_id === "string" ? item.record_id.trim() : "";
      const text = typeof item.text === "string" ? item.text.trim() : "";
      const field = typeof item.field === "string" ? item.field.trim() : "";
      if (!recordId || !text || !ARCHIVE_FIELD_LABELS[field]) return null;
      const createdAt = typeof item.created_at === "string" ? item.created_at : "";
      const lastMentionedAt =
        typeof item.last_mentioned_at === "string" ? item.last_mentioned_at : "";
      return {
        recordId,
        field,
        fieldLabel: ARCHIVE_FIELD_LABELS[field],
        text,
        createdAt,
        createdLabel: displayStamp(createdAt) || "已保存",
        lastMentionedAt,
        lastMentionedLabel: displayStamp(lastMentionedAt),
      };
    })
    .filter(Boolean);
  items.sort((left, right) => {
    const a = Date.parse(left.lastMentionedAt || left.createdAt) || 0;
    const b = Date.parse(right.lastMentionedAt || right.createdAt) || 0;
    return b - a;
  });
  return items;
}

function archiveConsentFromPersonPayload(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const matches = items
    .filter((item) => item?.consent_kind === CHILD_ARCHIVE_CONSENT.kind)
    .sort((left, right) => {
      const a = Date.parse(left?.revoked_at || left?.granted_at || "") || 0;
      const b = Date.parse(right?.revoked_at || right?.granted_at || "") || 0;
      return b - a;
    });
  const active = matches.find((item) => item?.active === true) || null;
  return {
    ...CHILD_ARCHIVE_CONSENT,
    active: Boolean(active),
    consentId: typeof active?.consent_id === "string" ? active.consent_id : "",
    statusLabel: active ? "已开启" : "未开启",
  };
}

function assertsNoTranscriptFields(viewModels) {
  const banned = ["transcript", "user_text", "assistant_text", "utterance", "audio_url"];
  for (const item of viewModels) {
    for (const key of banned) {
      if (Object.prototype.hasOwnProperty.call(item, key)) {
        throw new TypeError(`child memory view must not expose ${key}`);
      }
    }
  }
}

module.exports = {
  ARCHIVE_FIELD_LABELS,
  CHILD_ARCHIVE_CONSENT,
  archiveConsentFromPersonPayload,
  assertsNoTranscriptFields,
  displayStamp,
  normalizeChildArchive,
  normalizeSessionSummaries,
};
