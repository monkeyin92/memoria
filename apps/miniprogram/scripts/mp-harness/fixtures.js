/*
 * Sample API responses for the page runtime harness. Names and numbers are
 * examples. Any api function not listed resolves to null, matching a backend
 * that returned nothing, so pages must render their honest empty states.
 */

const { normalizeGuardianSummary } = require("../../utils/guardian");
const { STATE_LABELS } = require("../../utils/device-onboarding/state");

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
  binding_version: 1,
  status: "active",
  reason: "create",
  supersedes_binding_id: null,
  family_space_id: null,
  account_owner_id: "u_parent",
  device_admin_ids: ["u_parent"],
  primary_subject_ids: ["u_parent", "p_child"],
  guardian_ids: ["u_parent"],
  delegate_ids: [],
  emergency_contact_ids: [],
  member_ids: [],
  roles: [
    { person_id: "u_parent", role: "account_owner", permissions: [] },
    { person_id: "u_parent", role: "guardian", permissions: [] },
    { person_id: "p_child", role: "primary_subject", permissions: [] },
  ],
  service_profile_version: "child-cn-v1",
  policy_bundle_version: "policy-cn-minor-v2",
  consent_snapshot_id: null,
  persona_assignment_id: "pa_1",
  valid_from: "2026-10-01T08:00:00.000Z",
  valid_until: null,
  created_at: "2026-10-01T08:00:00.000Z",
};

const STORAGE = { "memoria:miniprogram:device:binding-manifest": BINDING };

const RUNTIME_PROFILE = {
  valid: true,
  runtime_profile_id: "rp_child",
  subject_category: "minor",
  obligations: [
    { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
    { code: "MAX_SESSION_SECONDS", params: { max_session_seconds: 900, extras: [["daily_chat_seconds", "1800"]] } },
  ],
  persona: { persona_id: "starlight" },
};

const OFFLINE_STORIES = [
  { id: "story_01", title: "小星星的枕头" },
  { id: "story_02", title: "月亮不着急" },
  { id: "story_03", title: "大熊和森林里的悄悄话" },
];

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

const GUARDIAN_SUMMARY = {
  minor_user_id: "p_child",
  window: { start: dayKey(6), end: dayKey(0) },
  emotion_distribution: { happy: 6, neutral: 4, fearful: 1 },
  topic_distribution: { interests: 5, school: 3, family: 2 },
  study_minutes: 95,
  source_event_count: 11,
};

const guardianApi = {
  getPersonConsents: async () => ({
    items: [
      ...ARCHIVE_CONSENT.items,
      { consent_kind: "weekly_report", consent_id: "c_weekly", active: true, granted_at: isoMinutesAgo(60 * 24 * 10) },
    ],
  }),
  getGuardianSummary: async (minorUserId) => normalizeGuardianSummary(GUARDIAN_SUMMARY, minorUserId),
};

const RAW_VOICE_CONSENT = {
  consent_id: "rv_1",
  policy_version: "raw-voice-archive-v1",
  retention_policy: "account_lifetime",
  granted_at: "2026-10-04T09:30:00+08:00",
};

function growthDimension(key, sources, status, readiness) {
  return {
    key,
    status,
    adopted_sources: Array.from({ length: sources }, (_, index) => ({ event_id: `${key}_${index}` })),
    version_readiness: { status: readiness },
  };
}

const digitalSelfApi = {
  getGrowthOverview: async () => ({
    dimensions: [
      growthDimension("life_chapters", 4, "supported", "ready"),
      growthDimension("important_people", 3, "supported", "stale"),
      growthDimension("expression", 1, "conflicted", "stale"),
      growthDimension("decision_cases", 0, "empty", "not_built"),
      growthDimension("relationship_models", 0, "empty", "not_built"),
      growthDimension("voice", 0, "empty", "dependency_pending"),
      growthDimension("legacy", 0, "empty", "dependency_pending"),
    ],
  }),
  getPersonaStatus: async () => ({ enabled: true }),
  getDigitalSelfVersions: async () => ({
    items: [{ version_id: "dsv_1", version_number: 1, status: "approved", manifest_sha256: "a1b2" }],
  }),
};

const personaApi = {
  listPersonas: async () => ({
    custom_personas: [
      { persona_id: "cu_0123456789abcdef0123456789abcd", display_name: "小北", persona_version: 1, fallback_designed_voice: "starlight" },
    ],
    builtin_personas: [],
  }),
};

const CLAIM = {
  claim_id: "claim_1",
  onboarding_session_id: "onb_1",
  device_id: "dev_7f3a",
  status: "reserved",
  expires_at: new Date(NOW.getTime() + 10 * 60 * 1000).toISOString(),
};

const bindApi = { getDeviceClaim: async () => CLAIM };
const BIND_QUERY = { claim_id: CLAIM.claim_id, onboarding_session_id: CLAIM.onboarding_session_id };

const SAFETY_ALERT = {
  notification_id: "n1",
  minor_user_id: "p_child",
  minor_display_name: "乐乐",
  occurred_at: isoMinutesAgo(0),
  occurred_label: "10月14日 19:42",
  delivery_status: "no_subscription",
  channel: "wechat_subscription",
  message: "有一条需要你关注的对话",
  category_label: "情绪和安全",
  summary: "孩子说到了和自己情绪、安全有关的内容",
  robot_said: "机器人已提醒孩子去找身边的大人",
  parent_action: "找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356",
  contains_transcript: false,
  contains_severity: false,
};

function safetyAlertApi(alerts) {
  const read = new Set();
  const ordered = [...alerts].sort((left, right) => {
    const byTime = String(right.occurred_at).localeCompare(String(left.occurred_at));
    return byTime || String(right.notification_id).localeCompare(String(left.notification_id));
  });
  const find = (id) => {
    const alert = ordered.find((item) => item.notification_id === id);
    if (!alert) throw Object.assign(new Error("guardian_notification_not_found"), { status: 404 });
    return alert;
  };
  return {
    getGuardianNotifications: async () => ({ items: ordered }),
    getUnreadGuardianNotifications: async () => {
      const [newest = null, ...others] = ordered.filter((alert) => !read.has(alert.notification_id));
      return { newest, others_count: others.length };
    },
    getGuardianNotification: async (id) => find(id),
    markGuardianNotificationRead: async (id) => {
      find(id);
      read.add(id);
      return { notification_id: id, read_at: NOW.toISOString() };
    },
  };
}

function baseApi(overrides = {}) {
  const known = {
    hasAuthenticatedSession: () => true,
    currentIdentity: () => IDENTITY,
    currentAuthEpoch: () => 1,
    isAuthEpochCurrent: () => true,
    selectDeviceBinding: () => {},
    readBindingManifest: () => BINDING,
    requireRuntimeCapability: async () => ({ allowed: true, reason: "allowed", profile: RUNTIME_PROFILE }),
    syncDeviceBindings: async () => ({ status: "ready", binding: BINDING }),
    getActivationStatus: async () => ({ status: "ready_for_conversation", network: { internet: true } }),
    getRuntimeProfile: async () => RUNTIME_PROFILE,
    requestPolicyDecision: async () => ({ effect: "allow", reason_code: "allowed", obligations: [] }),
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
    ...safetyAlertApi([SAFETY_ALERT]),
    getGuardianLinks: async () => [],
    listPersonas: async () => ({ custom_personas: [], builtin_personas: [] }),
    getWakeWordCatalog: async () => ({ items: [] }),
    getOfflineStories: async () => ({ items: OFFLINE_STORIES }),
    getSessionLimits: async () => ({
      max_session_minutes: 15,
      quiet_hours: { start: "21:00", end: "07:00" },
      daily_chat_minutes: 30,
      daily_chat_minute_choices: [15, 30, 45, 60],
      today_used_minutes: 18,
    }),
    listPersonaAssignments: async () => ({ items: [] }),
    getPersonConsents: async () => ({ items: [] }),
    getGuardianConsents: async () => ({ items: [] }),
    getDeliveredCapabilities: async () => ({ items: [] }),
    getGuardianPushConfig: async () => ({ enabled: false }),
    getRawVoiceConsent: async () => ({ consent: null }),
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

const authed = (overrides) => ({ api: baseApi(overrides), storage: STORAGE, now: NOW });
const guest = () => ({ api: baseApi(GUEST_API), now: NOW });

function longMemoryApi(days) {
  return {
    ...safetyAlertApi([]),
    getGuardianChildDays: async () => ({
      items: Array.from({ length: days }, (_, offset) => ({ day: dayKey(offset), message_count: (offset % 5) + 1 })),
    }),
    getChildSessionSummaries: async () => ({
      items: Array.from({ length: days * 3 }, (_, index) => ({
        record_id: `s_long_${index}`,
        text: `聊到了第 ${index + 1} 个有趣的话题，还讲了一个关于星星的故事`,
        created_at: isoMinutesAgo(index * 8 * 60),
      })),
    }),
  };
}

function longArchiveApi(facts) {
  const fields = ["appellation", "preference", "pet_name"];
  return {
    ...archiveApi,
    getChildArchive: async () => ({
      items: Array.from({ length: facts }, (_, index) => ({
        record_id: `a_long_${index}`,
        field: fields[index % fields.length],
        text: `第 ${index + 1} 条`,
        created_at: isoMinutesAgo(60 * index),
      })),
    }),
  };
}

const emptyMemoryApi = {
  getGuardianChildDays: async () => ({ items: [] }),
  getChildSessionSummaries: async () => ({ items: [] }),
  ...safetyAlertApi([]),
};

const ONBOARDING_DEVICE = { model: "Memoria M1", display_tail: "7F3A", firmware_version: "1.4.2", claim_status: "unclaimed" };
const WIFI_SCAN = [
  { ssid: "Home-2.4G", rssi: -52, security: "WPA2" },
  { ssid: "Home-Guest", rssi: -68, security: "open" },
  { ssid: "楼下咖啡店", rssi: -81, security: "WPA2" },
];

function onboardingSnapshot(state, extra = {}) {
  return { state, stateLabel: STATE_LABELS[state], reprovision: false, busy: false, error: "", device: ONBOARDING_DEVICE, ...extra };
}

module.exports = {
  ARCHIVE,
  BIND_QUERY,
  BINDING,
  CLAIM,
  GUARDIAN_SUMMARY,
  IDENTITY,
  NOW,
  SAFETY_ALERT,
  WIFI_SCAN,
  RAW_VOICE_CONSENT,
  RECAP,
  RUNTIME_PROFILE,
  STORAGE,
  SUMMARIES,
  GUEST_API,
  archiveApi,
  authed,
  baseApi,
  bindApi,
  dayKey,
  digitalSelfApi,
  emptyMemoryApi,
  guardianApi,
  guest,
  isoMinutesAgo,
  longArchiveApi,
  longMemoryApi,
  onboardingSnapshot,
  personaApi,
  safetyAlertApi,
};
