/**
 * Parent-facing child memory layers (Scheme A).
 * Only paraphrased session summaries and whitelist archive facts.
 * Never normalizes raw transcripts, audio, or utterance fields into view models.
 */

const ARCHIVE_FIELD_LABELS = Object.freeze({
  preference: "兴趣",
  appellation: "称呼",
  pet_name: "小名",
});

const ARCHIVE_FIELD_ORDER = Object.freeze(["preference", "appellation", "pet_name"]);

const CHILD_ARCHIVE_CONSENT = Object.freeze({
  kind: "child_archive",
  label: "长期记忆",
  description:
    "开启后，memoria 会记住孩子提到的兴趣等白名单内容。你可以随时关闭，关闭后已有内容可删除。",
  policyVersion: "child-archive-v1",
});

const PRIVACY_LINE = "我们自己的服务器不保存录音和原话。摘要 30 天后自动删除。";
const EMPTY_RECENT =
  "还没有记忆。孩子和 memoria 聊过之后，这里会出现摘要。";
const EMPTY_ARCHIVE_ON = "开启后，孩子聊到的兴趣会出现在这里。";
const CONSENT_OFF_DIALOG =
  "关闭后不会再写入新内容。已有内容可以继续查看和删除。";
const DELETE_CONFIRM = "删除后无法恢复";
const CLEARED_TOAST = "已清空，剩余 0 条";

function firstItems(payload) {
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload)) return payload;
  return [];
}

function displayDateLabel(value) {
  if (typeof value !== "string" || !value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const month = date.getMonth() + 1;
  const day = date.getDate();
  return `${month} 月 ${day} 日`;
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
        dateLabel: displayDateLabel(createdAt) || "最近",
        retentionExpiresAt:
          typeof item.retention_expires_at === "string" ? item.retention_expires_at : "",
        swipeOpen: false,
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
      const stamp = lastMentionedAt || createdAt;
      return {
        recordId,
        field,
        fieldLabel: ARCHIVE_FIELD_LABELS[field],
        text,
        createdAt,
        lastMentionedAt,
        dateLabel: displayDateLabel(stamp) || "已保存",
        swipeOpen: false,
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

function groupArchiveByCategory(items) {
  const buckets = new Map();
  for (const field of ARCHIVE_FIELD_ORDER) {
    buckets.set(field, {
      field,
      label: ARCHIVE_FIELD_LABELS[field],
      items: [],
    });
  }
  for (const item of items) {
    const group = buckets.get(item.field);
    if (group) group.items.push(item);
  }
  return [...buckets.values()].filter((group) => group.items.length > 0);
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

function boundChildFromManifest(binding) {
  if (
    !binding ||
    binding.declared_mode !== "parent_for_child" ||
    binding.status !== "active"
  ) {
    return null;
  }
  const ownerId = typeof binding.account_owner_id === "string" ? binding.account_owner_id : "";
  const subjects = Array.isArray(binding.primary_subject_ids) ? binding.primary_subject_ids : [];
  const personId = subjects.find(
    (id) => typeof id === "string" && id && id !== ownerId,
  );
  if (!personId) return null;
  return { personId, displayName: "孩子" };
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
  ARCHIVE_FIELD_ORDER,
  CHILD_ARCHIVE_CONSENT,
  CLEARED_TOAST,
  CONSENT_OFF_DIALOG,
  DELETE_CONFIRM,
  EMPTY_ARCHIVE_ON,
  EMPTY_RECENT,
  PRIVACY_LINE,
  archiveConsentFromPersonPayload,
  assertsNoTranscriptFields,
  boundChildFromManifest,
  displayDateLabel,
  groupArchiveByCategory,
  normalizeChildArchive,
  normalizeSessionSummaries,
};
