/*
 * Sample API responses for the page runtime harness. Names and numbers are
 * examples. Any api function not listed resolves to null, matching a backend
 * that returned nothing, so pages must render their honest empty states.
 */

const NOW = new Date("2026-10-14T19:42:00+08:00");

function isoMinutesAgo(minutes) {
  return new Date(NOW.getTime() - minutes * 60 * 1000).toISOString();
}

function dayKey(offsetDays) {
  const date = new Date(NOW.getTime() - offsetDays * 86400000);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

const IDENTITY = { user_id: "u_parent", display_name: "乐乐妈妈", account_id: "AC8F2K9Q" };

const BINDING = {
  binding_id: "bind_1",
  device_id: "dev_1",
  declared_mode: "parent_for_child",
  status: "active",
  account_owner_id: "u_parent",
  primary_subject_ids: ["u_parent", "p_child"],
};

const RUNTIME_PROFILE = {
  valid: true,
  subject_category: "minor",
  obligations: [
    { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
    { code: "MAX_SESSION_SECONDS", params: { max_session_seconds: 1800 } },
  ],
  persona: { persona_id: "starlight" },
};

const SUMMARIES = [
  { record_id: "s1", text: "讲了《大熊和森林里的悄悄话》", created_at: isoMinutesAgo(30) },
  { record_id: "s2", text: "聊到了幼儿园的新朋友", created_at: isoMinutesAgo(120) },
  { record_id: "s3", text: "一起数了天上的星星", created_at: isoMinutesAgo(200) },
  { record_id: "s4", text: "问了很多关于恐龙的问题", created_at: isoMinutesAgo(60 * 26) },
];

const RECAP = {
  title: "孩子聊了 3 次",
  overview: "讲了《大熊和森林里的悄悄话》",
  highlights: ["聊到了幼儿园的新朋友", "一起数了天上的星星"],
};

const ARCHIVE = [
  { record_id: "a1", field: "appellation", text: "乐乐", created_at: isoMinutesAgo(60 * 24 * 9) },
  { record_id: "a2", field: "preference", text: "恐龙", last_mentioned_at: isoMinutesAgo(60 * 3) },
  { record_id: "a3", field: "preference", text: "画画", last_mentioned_at: isoMinutesAgo(60 * 30) },
  { record_id: "a4", field: "pet_name", text: "小乐宝", created_at: isoMinutesAgo(60 * 24 * 5) },
];

const ARCHIVE_CONSENT = {
  items: [{ consent_kind: "child_archive", consent_id: "c_archive", active: true, granted_at: isoMinutesAgo(60 * 24 * 10) }],
};

const archiveApi = {
  getPersonConsents: async () => ARCHIVE_CONSENT,
  getChildArchive: async () => ({ items: ARCHIVE }),
};

const PINNED_ALERT = {
  notification_id: "n1",
  minor_user_id: "p_child",
  minor_display_name: "乐乐",
  occurred_at: isoMinutesAgo(0),
  delivery_status: "no_subscription",
  pinned: true,
};

function baseApi(overrides = {}) {
  const known = {
    hasAuthenticatedSession: () => true,
    currentIdentity: () => IDENTITY,
    currentAuthEpoch: () => 1,
    isAuthEpochCurrent: () => true,
    selectDeviceBinding: () => {},
    readBindingManifest: () => BINDING,
    requireRuntimeCapability: () => true,
    syncDeviceBindings: async () => ({ status: "ready", binding: BINDING }),
    getActivationStatus: async () => ({ status: "ready_for_conversation", network: { internet: true } }),
    getRuntimeProfile: async () => RUNTIME_PROFILE,
    getDeviceSettings: async () => ({ wake_word_display: "小乐小乐", volume_limit: 60 }),
    getProfile: async () => ({ user_id: "u_parent", display_name: "乐乐妈妈", companion_id: "starlight", auto_summary: true }),
    getMemoryDays: async () => ({
      items: [
        { day: dayKey(0), message_count: 3, summary: { title: "今天", overview: "聊到了幼儿园的新朋友" } },
        { day: dayKey(1), message_count: 2, summary: { title: "昨天", overview: "问了很多关于恐龙的问题" } },
      ],
    }),
    getConversationReview: async () => ({ memory_candidates: [] }),
    getConversationSessions: async () => ({ items: [] }),
    getChildSessionSummaries: async () => ({ items: SUMMARIES }),
    getChildArchive: async () => ({ items: [] }),
    getGuardianNotifications: async () => ({ items: [PINNED_ALERT] }),
    getGuardianLinks: async () => ({ items: [] }),
    listPersonas: async () => ({ custom_personas: [], builtin_personas: [] }),
    getWakeWordCatalog: async () => ({ items: [] }),
    listPersonaAssignments: async () => ({ items: [] }),
    getPersonConsents: async () => ({ items: [] }),
    getGuardianConsents: async () => ({ items: [] }),
    getDeliveredCapabilities: async () => ({ items: [] }),
    getGuardianPushConfig: async () => ({ enabled: false }),
    getGuardianChildDays: async () => ({
      items: [3, 2, 1, 2, 1, 4].map((count, offset) => ({ day: dayKey(offset), message_count: count })),
    }),
    getGuardianChildMemories: async () => ({ candidates: [], confirmed: [] }),
    generateGuardianChildRecap: async () => ({ summary: RECAP }),
  };
  const merged = { ...known, ...overrides };
  return new Proxy(merged, {
    get(target, name) {
      if (name in target) return target[name];
      if (typeof name !== "string" || name === "then") return undefined;
      return async () => null;
    },
  });
}

const GUEST_API = { hasAuthenticatedSession: () => false, currentIdentity: () => null };

module.exports = { ARCHIVE, BINDING, IDENTITY, NOW, PINNED_ALERT, RECAP, RUNTIME_PROFILE, SUMMARIES, GUEST_API, archiveApi, baseApi, dayKey, isoMinutesAgo };
