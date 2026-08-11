import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ChatText,
  House,
  Notebook,
  PaperPlaneRight,
  PhoneDisconnect,
  ShieldCheck,
  Sparkle,
  StopCircle,
  UserCircle,
} from "@phosphor-icons/react";

import {
  bootstrapIdentity,
  cachePendingMessage,
  createDeviceBinding,
  createDeviceSession,
  flushPendingMessages,
  getFidelityEvaluations,
  getGrowthTasks,
  getMemoryDays,
  getProfile,
  getRuntimeProfile,
  getSelfPreviewCapability,
  getSelfPreviewSources,
  issueSelfPreviewGrant,
  loginAccount,
  logoutAllDevices,
  logoutCurrentDevice,
  registerAccount,
  resolveSessionSubject,
  revokeSelfPreviewGrant,
  saveMessage,
  setActiveSubject,
  startFidelityEvaluation,
  chooseFidelityTrial,
  completeFidelityEvaluation,
  submitSelfPreviewFeedback,
  summarizeDay,
  transitionGrowthTask,
  updateProfile,
} from "./api.js";
import {
  capabilityGateMessage,
  sensitiveEntryBlockReason,
  SENSITIVE_ENTRIES,
} from "./lib/multiSubject/gates.js";
import {
  readBindingManifest,
  readLatestCachedRuntimeProfile,
} from "./lib/multiSubject/bindingManifest.js";
import { MODE_TITLES } from "./lib/multiSubject/modeMeta.js";
import { AuthScreen } from "./components/AuthScreen.jsx";
import { CompanionOnboarding } from "./components/CompanionOnboarding.jsx";
import { CompanionSwitcher } from "./components/CompanionSwitcher.jsx";
import { Mascot, MascotVisual } from "./components/Mascot.jsx";
import { MemoryScreen } from "./components/MemoryScreen.jsx";
import { ProfileScreen } from "./components/ProfileScreen.jsx";
import { useVoiceSession } from "./hooks/useVoiceSession.js";
import { localDateKey } from "./lib/date.js";
import {
  classifyEmotion,
  emotionFromVoice,
} from "./lib/emotion.js";
import { companionById } from "./lib/companions.js";

const tabs = [
  { id: "home", label: "陪伴", Icon: House },
  { id: "memory", label: "回顾", Icon: Notebook },
  { id: "profile", label: "我的", Icon: UserCircle },
];

const defaultProfile = {
  display_name: "新朋友",
  auto_summary: true,
  voice_reply: true,
  gentle_reminders: false,
  reject_non_owner_voice: true,
  companion_id: null,
};

const today = () => localDateKey();
const profileStorageKey = (userId) => `memoria:profile:${userId}`;
const growthEventId = () =>
  globalThis.crypto?.randomUUID?.()
  || `growth-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const fidelityCategories = [
  "fact",
  "decision",
  "relationship",
  "humor",
  "emotion",
  "unknown",
  "privacy",
];
const DigitalSelfPanel = lazy(() =>
  import("./components/DigitalSelfPanel.jsx").then((module) => ({
    default: module.DigitalSelfPanel,
  })),
);
const PrivacyDataPanel = lazy(() =>
  import("./components/PrivacyDataPanel.jsx").then((module) => ({
    default: module.PrivacyDataPanel,
  })),
);
const DeviceBindingFlow = lazy(() =>
  import("./components/DeviceBindingFlow.jsx").then((module) => ({
    default: module.DeviceBindingFlow,
  })),
);
const DeviceSubjectPanel = lazy(() =>
  import("./components/DeviceSubjectPanel.jsx").then((module) => ({
    default: module.DeviceSubjectPanel,
  })),
);
// Cascade only for now. Omni / Audio Flash E2E backends are temporarily off.
const VOICE_BACKEND = "cascade";

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

function homeStatusLabel(voice) {
  if (voice.inputMode !== "text") return voice.statusLabel;
  return {
    connecting: "正在打开文字对话",
    thinking: "正在想",
    speaking: "正在回复",
    reconnecting: "正在恢复连接",
    closed: "文字对话已结束",
  }[voice.uiState] || "文字对话中";
}

function provisionalSpeakerLabel(line, floorState = "silence") {
  if (!line.provisional) return "你";
  const currentFloor = line.floorState || floorState;
  if (currentFloor === "user_holds_floor") return "你 · 正在说";
  if (currentFloor === "uncertain") return "你 · 正在确认";
  return "你 · 正在听";
}

function normalizeMemoryDay(item) {
  const summary = item.summary || {};
  const moodMap = {
    positive: "happy",
    calm: "neutral",
    mixed: "curious",
    low: "curious",
    tense: "upset",
  };
  return {
    date: item.day || item.date,
    message_count: item.message_count || 0,
    source: ["deepseek", "qwen"].includes(item.source) ? "llm" : item.source,
    title: summary.title || item.title,
    summary: summary.overview || item.overview || item.summary_text,
    highlights: summary.highlights || item.highlights || [],
    mood: moodMap[summary.mood || item.mood] || summary.mood || item.mood || "neutral",
    suggestion: summary.suggestion || item.suggestion,
  };
}

function addPersistedMemoryMessage(days, date) {
  const index = days.findIndex((day) => day.date === date);
  if (index < 0) {
    return [
      {
        date,
        message_count: 1,
        source: null,
        title: null,
        summary: null,
        highlights: [],
        mood: "neutral",
        suggestion: null,
      },
      ...days,
    ];
  }
  return days.map((day, dayIndex) =>
    dayIndex === index
      ? { ...day, message_count: (day.message_count || 0) + 1 }
      : day,
  );
}

function latestFidelityEvaluations(items) {
  const latest = {};
  for (const evaluation of Array.isArray(items) ? items : []) {
    const existing = latest[evaluation.version_id];
    if (
      !existing ||
      new Date(evaluation.created_at).getTime() >
        new Date(existing.created_at).getTime()
    ) {
      latest[evaluation.version_id] = evaluation;
    }
  }
  return latest;
}

function fidelityView(evaluation, versionId = null) {
  if (!evaluation) {
    return {
      status: "idle",
      version_id: versionId,
      categories: fidelityCategories,
      current_item: null,
      all_answered: false,
      coverage_gaps: [],
    };
  }
  const trials = Array.isArray(evaluation.trials) ? evaluation.trials : [];
  const availableTrials = trials.filter((trial) => trial.available === true);
  const coverageGaps = trials.filter((trial) => trial.available !== true);
  const currentItem =
    evaluation.status === "active"
      ? availableTrials.find((trial) => !trial.preferred_slot) || null
      : null;
  const summary = evaluation.summary || {};
  return {
    ...summary,
    evaluation_id: evaluation.evaluation_id,
    version_id: evaluation.version_id,
    manifest_sha256: evaluation.manifest_sha256,
    status: evaluation.status,
    verdict: evaluation.verdict,
    categories: fidelityCategories,
    trials,
    current_item: currentItem,
    all_answered: availableTrials.every((trial) => trial.preferred_slot),
    coverage_gaps: coverageGaps,
    metrics: summary.actual_metrics || summary.metrics || {},
    targets: summary.targets || {},
    gates: summary.gates || {},
  };
}

function previewAnswerKey(answer) {
  return (
    answer?.answer_id ||
    `${answer?.session_id || "session"}:${answer?.turn_id ?? "turn"}:${answer?.generation_id ?? "generation"}`
  );
}

export function App() {
  const [identity, setIdentity] = useState(null);
  const [identityReady, setIdentityReady] = useState(false);
  const [identityError, setIdentityError] = useState("");
  const [activeTab, setActiveTab] = useState("home");
  const [profile, setProfile] = useState(defaultProfile);
  const [profileReady, setProfileReady] = useState(false);
  const [digitalSelfOpen, setDigitalSelfOpen] = useState(false);
  const [speakerEnrollmentOpen, setSpeakerEnrollmentOpen] = useState(false);
  const [speakerEnrollmentNotice, setSpeakerEnrollmentNotice] = useState("");
  const [companionSwitchOpen, setCompanionSwitchOpen] = useState(false);
  const [companionSwitchOrigin, setCompanionSwitchOrigin] = useState("profile");
  const [privacyDataOpen, setPrivacyDataOpen] = useState(false);
  const [accountDeletionOpen, setAccountDeletionOpen] = useState(false);
  const [bindFlowOpen, setBindFlowOpen] = useState(false);
  const [devicePanelOpen, setDevicePanelOpen] = useState(false);
  const [deviceBinding, setDeviceBinding] = useState(null);
  const [deviceProfile, setDeviceProfile] = useState(null);
  const [authoritySessionId, setAuthoritySessionId] = useState(null);
  const [deviceResolution, setDeviceResolution] = useState(null);
  const [deviceLoading, setDeviceLoading] = useState(false);
  const [deviceError, setDeviceError] = useState("");
  const [deviceOffline, setDeviceOffline] = useState(
    typeof navigator !== "undefined" && navigator.onLine === false,
  );
  const [deviceMultipleSpeakers, setDeviceMultipleSpeakers] = useState(false);
  const deviceRefreshEpochRef = useRef(0);
  const [memoryDays, setMemoryDays] = useState([]);
  const [selectedDay, setSelectedDay] = useState(today());
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [summaryRunning, setSummaryRunning] = useState(false);
  const [preferenceSaving, setPreferenceSaving] = useState(false);
  const [preferenceError, setPreferenceError] = useState("");
  const [activeGrowthTask, setActiveGrowthTask] = useState(null);
  const [selfPreviewCapability, setSelfPreviewCapability] = useState(null);
  const [activePreview, setActivePreview] = useState(null);
  const [activeLegacy, setActiveLegacy] = useState(null);
  const [previewSourceDetails, setPreviewSourceDetails] = useState({});
  const [previewBusy, setPreviewBusy] = useState("");
  const [fidelityEvaluations, setFidelityEvaluations] = useState([]);
  const [fidelityFocusVersionId, setFidelityFocusVersionId] = useState("");
  const [textDraft, setTextDraft] = useState("");
  const [textTurnPending, setTextTurnPending] = useState(false);
  const activeUserIdRef = useRef("");
  const growthCompletionIdsRef = useRef({});
  const pendingMessageSavesRef = useRef(new Set());
  const persistedMemoryMessageKeysRef = useRef(new Set());
  const memoryLoadEpochRef = useRef(0);
  const userId = identity?.user_id || "";

  const setCurrentIdentity = useCallback((nextIdentity) => {
    activeUserIdRef.current = nextIdentity?.user_id || "";
    setIdentity(nextIdentity);
  }, []);

  const loadIdentity = useCallback(async () => {
    setIdentityError("");
    try {
      setCurrentIdentity(await bootstrapIdentity());
    } catch {
      setIdentityError("暂时无法建立安全身份，请检查网络后重试。");
    } finally {
      setIdentityReady(true);
    }
  }, [setCurrentIdentity]);

  useEffect(() => {
    void loadIdentity();
  }, [loadIdentity]);

  const handleFinalTranscript = useCallback(
    (line) => {
      if (
        !userId ||
        activeUserIdRef.current !== userId ||
        line.history_eligible !== true
      ) {
        return;
      }
      const nextEmotion = classifyEmotion(line.text);
      const message = {
        user_id: userId,
        role: line.speaker,
        text: line.text,
        emotion: nextEmotion,
        history_eligible: true,
      };
      const messageKey = `${line.speaker}:${line.turn_id}:${line.generation_id}:${line.text}`;
      const pending = (async () => {
        try {
          await saveMessage(message);
          if (
            activeUserIdRef.current === userId &&
            !persistedMemoryMessageKeysRef.current.has(messageKey)
          ) {
            persistedMemoryMessageKeysRef.current.add(messageKey);
            setMemoryDays((current) => addPersistedMemoryMessage(current, today()));
          }
        } catch {
          if (activeUserIdRef.current === userId) cachePendingMessage(message);
        }
      })();
      pendingMessageSavesRef.current.add(pending);
      void pending.then(
        () => pendingMessageSavesRef.current.delete(pending),
        () => pendingMessageSavesRef.current.delete(pending),
      );
      return pending;
    },
    [userId],
  );

  const voice = useVoiceSession({
    userId,
    onFinalTranscript: handleFinalTranscript,
    voiceReplyEnabled: profile.voice_reply,
    voiceBackend: VOICE_BACKEND,
    learningTaskId: activeGrowthTask?.task_id || null,
    interactionMode:
      activePreview?.status === "starting" ||
      activePreview?.status === "running"
        ? "self_preview"
        : activeLegacy?.status === "starting" ||
            activeLegacy?.status === "running"
          ? "legacy"
        : "companion",
    previewGrantId: activePreview?.grant_id || null,
    legacyGrantId: activeLegacy?.grant_id || null,
  });
  const textInputReady =
    voice.uiState === "ready" || voice.uiState === "listening";
  const voiceSessionId = voice.session?.session_id || null;

  /*
   * 设备/主体状态（多用户整改）。
   *
   * 敏感入口只由“当前有效 profile.capabilities”驱动（D-07），展示层红线
   * 再叠加 degraded / unknown_safe / 未确认说话人 / 离线 / 多人场景的
   * 隐藏与拒绝；绝不从年龄/关系/本地缓存推断权限。
   *
   * authority session 与 media/voice session 分离：
   * - authoritySessionId = 当前 voice session id；或从严格未过期的缓存
   *   profile 取出候选 session id 后，经 GET /runtime-profile?session_id=
   *   服务端重新验证为 current 的 session id（本地缓存只提供候选，绝不
   *   只信缓存）；刷新失败即清 profile、敏感门禁关闭。
   * - voiceActive 只用于声纹录取互斥和主体切换前是否 reset media。
   */
  const refreshDeviceState = useCallback(async () => {
    const epoch = deviceRefreshEpochRef.current + 1;
    deviceRefreshEpochRef.current = epoch;
    setDeviceLoading(true);
    setDeviceError("");
    try {
      const manifest = readBindingManifest();
      if (!manifest) {
        setDeviceBinding(null);
        setDeviceProfile(null);
        setAuthoritySessionId(null);
        setDeviceResolution(null);
        setDeviceMultipleSpeakers(false);
        return;
      }
      setDeviceBinding(manifest);
      // 1) 候选 authority session：当前 voice session 优先；否则从严格
      //    未过期缓存取 session_id（仅作候选，必须经服务端重新验证）。
      let candidateSessionId = voiceSessionId;
      if (!candidateSessionId) {
        const cached = readLatestCachedRuntimeProfile({
          deviceId: manifest.device_id,
          bindingId: manifest.binding_id,
          bindingVersion: manifest.binding_version,
        });
        if (cached && cached.valid === true && cached.session_id) {
          candidateSessionId = cached.session_id;
        }
      }
      const environment = {
        offline: deviceOffline,
        multiple_speakers: deviceMultipleSpeakers,
      };
      const [resolutionResult, profileResult] = await Promise.allSettled([
        resolveSessionSubject({
          deviceId: manifest.device_id,
          sessionId: candidateSessionId,
          environment,
        }),
        candidateSessionId
          ? getRuntimeProfile(manifest.device_id, {
              sessionId: candidateSessionId,
            })
          : Promise.resolve(null),
      ]);
      if (epoch !== deviceRefreshEpochRef.current) return;
      if (resolutionResult.status === "fulfilled") {
        setDeviceResolution(resolutionResult.value);
      } else {
        setDeviceResolution(null);
      }
      const profile =
        profileResult.status === "fulfilled" ? profileResult.value : null;
      const resolution =
        resolutionResult.status === "fulfilled" ? resolutionResult.value : null;
      setDeviceMultipleSpeakers(
        Boolean(
          resolution &&
            (resolution.valid !== true ||
              resolution.temporary_service_mode === "unknown_safe"),
        ),
      );
      if (
        profile &&
        profile.valid === true &&
        typeof candidateSessionId === "string" &&
        candidateSessionId
      ) {
        setDeviceProfile(profile);
        setAuthoritySessionId(candidateSessionId);
      } else if (profile && profile.valid !== true) {
        // 服务端返回 fail-closed 降级对象（过期/校验失败）：保留用于
        // 展示原因，权威 session 仍记为该候选，敏感门禁关闭。
        setDeviceProfile(profile);
        if (typeof candidateSessionId === "string" && candidateSessionId) {
          setAuthoritySessionId(candidateSessionId);
        } else {
          setAuthoritySessionId(null);
        }
      } else {
        // 刷新失败（503 / 网络 / 晚到被弃）：清 profile 与权威会话，
        // 敏感门禁 fail closed，绝不回退到本地缓存。
        setDeviceProfile(null);
        setAuthoritySessionId(null);
      }
      if (
        (resolutionResult.status === "rejected" ||
          (profileResult.status === "rejected" && candidateSessionId)) &&
        epoch === deviceRefreshEpochRef.current
      ) {
        setDeviceError("服务端暂时无法提供有效的 Runtime Profile，敏感功能保持关闭。");
      }
    } finally {
      if (epoch === deviceRefreshEpochRef.current) setDeviceLoading(false);
    }
  }, [deviceOffline, voiceSessionId]);

  useEffect(() => {
    const syncOnline = () => {
      setDeviceOffline(
        typeof navigator !== "undefined" && navigator.onLine === false,
      );
    };
    window.addEventListener("online", syncOnline);
    window.addEventListener("offline", syncOnline);
    void refreshDeviceState();
    return () => {
      window.removeEventListener("online", syncOnline);
      window.removeEventListener("offline", syncOnline);
    };
  }, [refreshDeviceState]);

  const deviceDisplayContext = {
    offline: deviceOffline,
    multipleSpeakers: deviceMultipleSpeakers,
  };
  const gateResultFor = (entryKey) => {
    const entry = SENSITIVE_ENTRIES.find((item) => item.key === entryKey);
    if (!entry) return { allowed: false, reason: "unavailable" };
    if (!deviceBinding) return { allowed: false, reason: "no_binding" };
    if (!authoritySessionId || !deviceProfile) {
      return { allowed: false, reason: "unavailable" };
    }
    if (deviceProfile.valid !== true) {
      return { allowed: false, reason: "invalid_profile" };
    }
    const blockReason = sensitiveEntryBlockReason(
      entry,
      deviceProfile,
      deviceDisplayContext,
    );
    if (blockReason) {
      return { allowed: false, reason: blockReason };
    }
    return { allowed: true, reason: "allowed" };
  };
  const sensitiveGates = Object.fromEntries(
    SENSITIVE_ENTRIES.map((entry) => [
      entry.key,
      gateResultFor(entry.key).allowed,
    ]),
  );
  const gateMessage = (entryKey) => {
    const entry = SENSITIVE_ENTRIES.find((item) => item.key === entryKey);
    return entry
      ? capabilityGateMessage(gateResultFor(entryKey), entry.capability)
      : "";
  };
  const deviceSummary = deviceBinding
    ? {
        modeTitle: MODE_TITLES[deviceBinding.declared_mode] || deviceBinding.declared_mode,
        bindingVersion: deviceBinding.binding_version,
        activeSubjectLabel:
          deviceProfile?.active_subject_id &&
          deviceProfile.valid === true &&
          !deviceProfile.degraded &&
          !deviceMultipleSpeakers
            ? deviceResolution?.candidate_subjects?.find(
                (candidate) =>
                  candidate.person_id === deviceProfile.active_subject_id,
              )?.display_name || deviceProfile.active_subject_id
            : null,
        degraded: Boolean(
          deviceProfile && (deviceProfile.degraded || deviceMultipleSpeakers),
        ),
      }
    : null;

  const stopVoiceForSubjectSwitch = useCallback(async () => {
    await voice.reset();
  }, [voice.reset]);

  const handleSubjectSwitched = useCallback(
    (nextProfile) => {
      setDeviceProfile(nextProfile);
      setDeviceBinding(readBindingManifest());
    },
    [],
  );

  const restartVoiceAfterSwitch = useCallback(async () => {
    await voice.start();
  }, [voice.start]);

  const waitForPendingMessageSaves = useCallback(async () => {
    await voice.flushPersistence?.();
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const pending = [...pendingMessageSavesRef.current];
      if (!pending.length) return;
      await Promise.allSettled(pending);
    }
  }, [voice.flushPersistence]);

  useEffect(() => {
    if (
      !voice.session ||
      voice.inputMode !== "text" ||
      voice.error ||
      !textInputReady
    ) {
      setTextTurnPending(false);
    }
  }, [
    textInputReady,
    voice.error,
    voice.inputMode,
    voice.session,
  ]);

  const refreshSelfPreviewState = useCallback(async () => {
    if (identity?.account_type !== "registered") {
      setSelfPreviewCapability({
        status: "blocked",
        conversational: false,
        registered_owner: false,
        active_owner_voice: false,
        missing: ["registered_owner"],
        versions: [],
      });
      setFidelityEvaluations([]);
      return;
    }
    const [capabilityResult, evaluationsResult] = await Promise.allSettled([
      getSelfPreviewCapability(),
      getFidelityEvaluations(),
    ]);
    if (capabilityResult.status === "fulfilled") {
      setSelfPreviewCapability(capabilityResult.value);
    } else {
      setSelfPreviewCapability({
        status: "blocked",
        conversational: false,
        registered_owner: true,
        active_owner_voice: false,
        missing: ["preview_state_unavailable"],
        versions: [],
      });
    }
    if (evaluationsResult.status === "fulfilled") {
      setFidelityEvaluations(evaluationsResult.value.items || []);
    }
  }, [identity?.account_type]);

  const upsertFidelityEvaluation = useCallback((evaluation) => {
    setFidelityEvaluations((current) => [
      evaluation,
      ...current.filter(
        (item) => item.evaluation_id !== evaluation.evaluation_id,
      ),
    ]);
    setFidelityFocusVersionId(evaluation.version_id);
    return evaluation;
  }, []);

  useEffect(() => {
    if (!digitalSelfOpen) return;
    void refreshSelfPreviewState();
  }, [digitalSelfOpen, refreshSelfPreviewState]);

  const fidelityByVersion = useMemo(() => {
    const latest = latestFidelityEvaluations(fidelityEvaluations);
    return Object.fromEntries(
      Object.entries(latest).map(([versionId, evaluation]) => [
        versionId,
        fidelityView(evaluation, versionId),
      ]),
    );
  }, [fidelityEvaluations]);

  const fidelitySummary = useMemo(() => {
    const focusVersionId =
      fidelityFocusVersionId ||
      activePreview?.version_id ||
      Object.keys(fidelityByVersion)[0] ||
      null;
    return fidelityByVersion[focusVersionId] || fidelityView(null, focusVersionId);
  }, [
    activePreview?.version_id,
    fidelityByVersion,
    fidelityFocusVersionId,
  ]);

  const previewAnswers = useMemo(() => {
    if (!activePreview) return [];
    const sessionId =
      voice.session?.session_id || activePreview.session_id || null;
    return (voice.transcripts || [])
      .filter(
        (line) =>
          line.speaker === "assistant" &&
          line.final === true &&
          line.heard === true &&
          line.previewProvenance,
      )
      .map((line) => {
        const answer = {
          answer_id: `${sessionId || "preview"}:${line.turnId}:${line.generationId}`,
          session_id: sessionId,
          turn_id: line.turnId,
          generation_id: line.generationId,
          tool_epoch: line.toolEpoch,
          text: line.text,
          epistemic_status: line.epistemic_status,
          disclosures: line.disclosures || [],
          source_refs: line.source_refs || [],
          version_id: line.version_id,
          manifest_sha256: line.manifest_sha256,
        };
        return {
          ...answer,
          ...(previewSourceDetails[previewAnswerKey(answer)] || {}),
        };
      });
  }, [
    activePreview,
    previewSourceDetails,
    voice.session?.session_id,
    voice.transcripts,
  ]);

  const loadMemories = useCallback(async () => {
    if (!userId) return;
    const requestedUserId = userId;
    const loadEpoch = memoryLoadEpochRef.current + 1;
    memoryLoadEpochRef.current = loadEpoch;
    setMemoryLoading(true);
    setMemoryError("");
    try {
      await flushPendingMessages();
      await waitForPendingMessageSaves();
      if (
        activeUserIdRef.current !== requestedUserId ||
        memoryLoadEpochRef.current !== loadEpoch
      ) return;
      const result = await getMemoryDays(requestedUserId);
      if (
        activeUserIdRef.current !== requestedUserId ||
        memoryLoadEpochRef.current !== loadEpoch
      ) return;
      const rawDays = Array.isArray(result) ? result : result.items || [];
      const days = rawDays.map(normalizeMemoryDay);
      setMemoryDays(days);
      setSelectedDay((current) =>
        days.some((day) => day.date === current) ? current : today(),
      );
    } catch {
      if (
        activeUserIdRef.current === requestedUserId &&
        memoryLoadEpochRef.current === loadEpoch
      ) {
        setMemoryError("暂时没有连上回顾服务，对话内容会先安全保存在本机。");
      }
    } finally {
      if (
        activeUserIdRef.current === requestedUserId &&
        memoryLoadEpochRef.current === loadEpoch
      ) {
        setMemoryLoading(false);
      }
    }
  }, [userId, waitForPendingMessageSaves]);

  useEffect(() => {
    if (!userId) return;
    const requestedUserId = userId;
    persistedMemoryMessageKeysRef.current.clear();
    memoryLoadEpochRef.current += 1;
    setMemoryDays([]);
    setSelectedDay(today());
    setPreferenceSaving(false);
    setPreferenceError("");
    setProfileReady(false);
    setProfile(defaultProfile);
    void (async () => {
      let local = null;
      try {
        local = JSON.parse(
          window.localStorage.getItem(profileStorageKey(userId)) || "null",
        );
      } catch {
        local = null;
      }
      try {
        const result = await getProfile(requestedUserId);
        if (activeUserIdRef.current !== requestedUserId) return;
        const hasCompanionSelection = Object.prototype.hasOwnProperty.call(
          result,
          "companion_id",
        );
        const next = {
          ...defaultProfile,
          ...(local || {}),
          ...result,
          companion_id: hasCompanionSelection
            ? result.companion_id
            : "starlight",
        };
        setProfile(next);
      } catch {
        if (activeUserIdRef.current !== requestedUserId) return;
        if (local) {
          const next = { ...defaultProfile, ...local };
          setProfile(next);
        } else if (identity?.account_type === "registered") {
          const next = { ...defaultProfile, companion_id: "starlight" };
          setProfile(next);
        }
      } finally {
        if (activeUserIdRef.current === requestedUserId) setProfileReady(true);
      }
    })();
  }, [userId]);

  useEffect(() => {
    if (activeTab !== "home") void loadMemories();
  }, [activeTab, loadMemories]);

  const activeMemory = memoryDays.find((day) => day.date === selectedDay) || null;
  const activeCompanion = companionById(profile.companion_id);
  const sessionCompanion = companionById(
    voice.session?.interaction?.companion_style_id || profile.companion_id,
  );
  const selfPreviewSession =
    activePreview?.status === "starting" ||
    activePreview?.status === "running" ||
    voice.session?.interaction?.interaction_mode === "self_preview";
  const legacySession =
    activeLegacy?.status === "starting" ||
    activeLegacy?.status === "running" ||
    voice.session?.interaction?.interaction_mode === "legacy";
  const mascotEmotion =
    voice.uiState === "speaking"
      ? voice.assistantExpression?.expression || "neutral"
      : emotionFromVoice(voice.emotionHint?.label);

  const runSummary = async () => {
    setSummaryRunning(true);
    setMemoryError("");
    try {
      await waitForPendingMessageSaves();
      await summarizeDay(userId, today());
      await loadMemories();
      setSelectedDay(today());
    } catch {
      setMemoryError("今天还没有足够的对话可以生成回顾。");
    } finally {
      setSummaryRunning(false);
    }
  };

  const stopLegacy = useCallback(async () => {
    try {
      await voice.end();
    } finally {
      setActiveLegacy(null);
    }
  }, [voice.end]);

  const stopSelfPreview = useCallback(async () => {
    const preview = activePreview;
    const grantId =
      preview?.grant_id ||
      voice.session?.interaction?.preview_grant_id ||
      null;
    if (!preview && !grantId) {
      await voice.end();
      return;
    }
    setPreviewBusy("self-preview-stop");
    setActivePreview((current) =>
      current ? { ...current, status: "stopping" } : current,
    );
    try {
      await voice.end();
    } finally {
      if (grantId) {
        await revokeSelfPreviewGrant(grantId).catch(() => undefined);
      }
      setActivePreview((current) =>
        !grantId || current?.grant_id === grantId ? null : current,
      );
      setPreviewSourceDetails({});
      setPreviewBusy("");
      await refreshSelfPreviewState().catch(() => undefined);
    }
  }, [
    activePreview,
    refreshSelfPreviewState,
    voice.end,
    voice.session?.interaction?.preview_grant_id,
  ]);

  const startSelfPreview = useCallback(
    async ({ versionId, manifestSha256, password, perspective }) => {
      if (voice.session || activeLegacy) {
        throw new Error("请先结束当前陪伴对话，再进入数字分身预览。");
      }
      setPreviewBusy("self-preview-start");
      setPreviewSourceDetails({});
      let grant = null;
      try {
        grant = await issueSelfPreviewGrant({
          versionId,
          manifestSha256,
          password,
          perspective,
        });
        setActivePreview({ ...grant, status: "starting" });
        const created = await voice.start({
          interactionMode: "self_preview",
          previewGrantId: grant.grant_id,
        });
        if (!created) {
          throw new Error("数字分身预览会话没有建立，请重新确认后再试。");
        }
        const interaction = created.interaction || {};
        const runningPreview = {
          ...grant,
          status: "running",
          session_id: created.session_id,
          version_id: interaction.digital_self_version_id,
          manifest_sha256: interaction.manifest_sha256,
          perspective: interaction.perspective,
        };
        setActivePreview(runningPreview);
        setFidelityFocusVersionId(runningPreview.version_id);
        setDigitalSelfOpen(false);
        setActiveTab("home");
        return runningPreview;
      } catch (error) {
        await voice.end().catch(() => undefined);
        if (grant?.grant_id) {
          await revokeSelfPreviewGrant(grant.grant_id).catch(() => undefined);
        }
        setActivePreview(null);
        setPreviewSourceDetails({});
        throw error;
      } finally {
        setPreviewBusy("");
      }
    },
    [activeLegacy, voice.end, voice.session, voice.start],
  );

  const startLegacy = useCallback(
    async ({ legacyGrantId, legacyActorRole }) => {
      if (voice.session || activePreview) {
        throw new Error("请先结束当前对话，再进入传承模式。");
      }
      setActiveLegacy({
        grant_id: legacyGrantId,
        actor_role: legacyActorRole,
        status: "starting",
      });
      try {
        const created = await voice.start({
          interactionMode: "legacy",
          legacyGrantId,
        });
        if (!created) {
          throw new Error("传承会话没有建立，请确认授权仍然有效。");
        }
        const running = {
          grant_id: legacyGrantId,
          actor_role: created.interaction?.legacy_actor_role,
          shell_id: created.interaction?.legacy_shell_id || null,
          owner_account_id: created.interaction?.resource_owner_account_id,
          status: "running",
          session_id: created.session_id,
        };
        setActiveLegacy(running);
        setDigitalSelfOpen(false);
        setActiveTab("home");
        return created;
      } catch (error) {
        await voice.end().catch(() => undefined);
        setActiveLegacy(null);
        throw error;
      }
    },
    [activePreview, voice.end, voice.session, voice.start],
  );

  const expandSelfPreviewSources = useCallback(async (answer) => {
    const result = await getSelfPreviewSources({
      sessionId: answer.session_id,
      turnId: answer.turn_id,
      generationId: answer.generation_id,
      toolEpoch: answer.tool_epoch,
    });
    setPreviewSourceDetails((current) => ({
      ...current,
      [previewAnswerKey(answer)]: { sources_expanded: result.items },
    }));
    return result.items;
  }, []);

  const submitPreviewFeedback = useCallback(
    async (answer, { action, correction }) => {
      const targetSourceEventIds = [
        ...new Set(
          (answer.source_refs || []).flatMap((source) => {
            if (Array.isArray(source.source_event_ids)) {
              return source.source_event_ids;
            }
            return source.source_event_id ? [source.source_event_id] : [];
          }),
        ),
      ];
      const result = await submitSelfPreviewFeedback({
        sessionId: answer.session_id,
        turnId: answer.turn_id,
        generationId: answer.generation_id,
        toolEpoch: answer.tool_epoch,
        versionId: answer.version_id,
        manifestSha256: answer.manifest_sha256,
        action,
        targetSourceEventIds,
        correctionText: correction || null,
      });
      await stopSelfPreview();
      setFidelityFocusVersionId(answer.version_id);
      await refreshSelfPreviewState().catch(() => undefined);
      return result;
    },
    [refreshSelfPreviewState, stopSelfPreview],
  );

  const startFidelity = useCallback(
    async (version, password) => {
      setPreviewBusy("fidelity-start");
      try {
        const evaluation = await startFidelityEvaluation({
          versionId: version.version_id,
          manifestSha256: version.manifest_sha256,
          password,
        });
        return upsertFidelityEvaluation(evaluation);
      } finally {
        setPreviewBusy("");
      }
    },
    [upsertFidelityEvaluation],
  );

  const submitFidelityChoice = useCallback(
    async ({ itemId, preferredSlot, versionId }) => {
      const evaluation =
        latestFidelityEvaluations(fidelityEvaluations)[versionId];
      if (!evaluation) {
        throw new Error("忠实度评测状态已经变化，请重新开始。");
      }
      setPreviewBusy("fidelity-choice");
      try {
        const updated = await chooseFidelityTrial({
          evaluationId: evaluation.evaluation_id,
          trialId: itemId,
          preferredSlot,
        });
        return upsertFidelityEvaluation(updated);
      } finally {
        setPreviewBusy("");
      }
    },
    [fidelityEvaluations, upsertFidelityEvaluation],
  );

  const finishFidelity = useCallback(
    async ({ evaluationId, verdict, rationale }) => {
      setPreviewBusy("fidelity-verdict");
      try {
        const updated = await completeFidelityEvaluation({
          evaluationId,
          verdict,
          rationale: rationale || null,
        });
        upsertFidelityEvaluation(updated);
        await refreshSelfPreviewState().catch(() => undefined);
        return updated;
      } finally {
        setPreviewBusy("");
      }
    },
    [refreshSelfPreviewState, upsertFidelityEvaluation],
  );

  useEffect(() => {
    if (
      activePreview?.status !== "running" ||
      voice.session ||
      voice.uiState !== "closed"
    ) {
      return;
    }
    const grantId = activePreview.grant_id;
    setPreviewBusy("self-preview-stop");
    setActivePreview((current) =>
      current ? { ...current, status: "stopping" } : current,
    );
    void (async () => {
      await revokeSelfPreviewGrant(grantId).catch(() => undefined);
      setActivePreview((current) =>
        current?.grant_id === grantId ? null : current,
      );
      setPreviewSourceDetails({});
      setPreviewBusy("");
      await refreshSelfPreviewState().catch(() => undefined);
    })();
  }, [
    activePreview,
    refreshSelfPreviewState,
    voice.session,
    voice.uiState,
  ]);

  useEffect(() => {
    if (
      activeLegacy?.status !== "running" ||
      voice.session ||
      voice.uiState !== "closed"
    ) {
      return;
    }
    setActiveLegacy(null);
  }, [activeLegacy?.status, voice.session, voice.uiState]);

  const finishConversation = async () => {
    const endingSession = voice.session;
    if (
      activePreview ||
      endingSession?.interaction?.interaction_mode === "self_preview"
    ) {
      await stopSelfPreview();
      return;
    }
    if (
      activeLegacy ||
      endingSession?.interaction?.interaction_mode === "legacy"
    ) {
      await stopLegacy();
      return;
    }
    await voice.end();
    await waitForPendingMessageSaves();
    if (
      activeGrowthTask?.kind === "natural_chat" &&
      activeGrowthTask.status === "active" &&
      endingSession?.learning_task_id === activeGrowthTask.task_id
    ) {
      const completionId = (
        growthCompletionIdsRef.current[activeGrowthTask.task_id]
        ||= growthEventId()
      );
      try {
        const completed = await transitionGrowthTask(
          activeGrowthTask.task_id,
          completionId,
          "completed",
          activeGrowthTask.revision,
        );
        setActiveGrowthTask(null);
        delete growthCompletionIdsRef.current[completed.task_id];
      } catch {
        try {
          const tasks = await getGrowthTasks();
          const current = (tasks?.items || []).find(
            (task) => task.task_id === activeGrowthTask.task_id,
          );
          if (current?.status === "active") {
            await transitionGrowthTask(
              current.task_id,
              completionId,
              "completed",
              current.revision,
            );
          }
        } catch {
          // The server remains authoritative; the task can be resumed from its map.
        } finally {
          setActiveGrowthTask(null);
          delete growthCompletionIdsRef.current[activeGrowthTask.task_id];
        }
      }
    }
    if (profile.auto_summary) {
      try {
        await summarizeDay(userId, today());
      } catch {
        // The conversation still ends cleanly when summary service is unavailable.
      }
    }
    await loadMemories();
  };

  const togglePreference = async (field) => {
    if (preferenceSaving) return;
    const requestedUserId = userId;
    const next = { ...profile, [field]: !profile[field] };
    setPreferenceSaving(true);
    setPreferenceError("");
    try {
      const saved = await updateProfile(requestedUserId, next);
      if (activeUserIdRef.current !== requestedUserId) return;
      const confirmed = saved ? { ...next, ...saved } : next;
      setProfile(confirmed);
      window.localStorage.setItem(
        profileStorageKey(requestedUserId),
        JSON.stringify(confirmed),
      );
      if (field === "voice_reply" && confirmed.voice_reply) {
        void voice.resumeAudio(true);
      }
    } catch {
      if (activeUserIdRef.current === requestedUserId) {
        setPreferenceError("偏好保存失败，请检查网络后重试。");
      }
    } finally {
      if (activeUserIdRef.current === requestedUserId) setPreferenceSaving(false);
    }
  };

  const handleAccountDeleted = async () => {
    activeUserIdRef.current = "";
    deviceRefreshEpochRef.current += 1;
    const voiceReset = voice.reset ? voice.reset() : voice.end();
    setCurrentIdentity(null);
    setIdentityError("");
    setProfile(defaultProfile);
    setProfileReady(false);
    setMemoryDays([]);
    setSelectedDay(today());
    setMemoryLoading(false);
    setMemoryError("");
    setSummaryRunning(false);
    setPreferenceSaving(false);
    setPreferenceError("");
    setActiveGrowthTask(null);
    setSelfPreviewCapability(null);
    setActivePreview(null);
    setActiveLegacy(null);
    setPreviewSourceDetails({});
    setPreviewBusy("");
    setFidelityEvaluations([]);
    setFidelityFocusVersionId("");
    setBindFlowOpen(false);
    setDevicePanelOpen(false);
    setDeviceBinding(null);
    setDeviceProfile(null);
    setAuthoritySessionId(null);
    setDeviceResolution(null);
    setDeviceLoading(false);
    setDeviceError("");
    setDeviceMultipleSpeakers(false);
    growthCompletionIdsRef.current = {};
    setDigitalSelfOpen(false);
    setSpeakerEnrollmentOpen(false);
    setSpeakerEnrollmentNotice("");
    setCompanionSwitchOpen(false);
    setPrivacyDataOpen(false);
    setAccountDeletionOpen(false);
    setActiveTab("home");
    await voiceReset.catch(() => undefined);
  };

  const handleLogout = async (allDevices) => {
    if (
      activePreview ||
      voice.session?.interaction?.interaction_mode === "self_preview"
    ) {
      await stopSelfPreview();
    }
    if (
      activeLegacy ||
      voice.session?.interaction?.interaction_mode === "legacy"
    ) {
      await stopLegacy();
    }
    await (allDevices ? logoutAllDevices() : logoutCurrentDevice());
    await handleAccountDeleted();
  };

  if (!identityReady || identityError || (identity && !profileReady)) {
    return (
      <main className="mobile-prototype" data-page="bootstrap">
        <div className="app-surface">
          <section className="screen bootstrap-screen" aria-label="正在准备陪伴空间">
            <div className="empty-memory">
              {!identityError && <span className="loading-orbit" />}
              <h2>{identityError ? "还差一点连接" : "正在准备你的陪伴空间"}</h2>
              <p>
                {identityError || "正在同步你的账号和陪伴偏好。"}
              </p>
              {identityError && (
                <button type="button" onClick={() => void loadIdentity()}>
                  重新连接
                </button>
              )}
            </div>
          </section>
        </div>
      </main>
    );
  }

  if (!identity || identity.account_type === "anonymous") {
    return (
      <AuthScreen
        preservesExistingData={identity?.account_type === "anonymous"}
        onLogin={async (username, password) => {
          const account = await loginAccount(username, password);
          setProfileReady((ready) => ready && account.user_id === userId);
          setCurrentIdentity(account);
        }}
        onRegister={async (username, password, displayName) => {
          const account = await registerAccount(username, password, displayName);
          setProfileReady((ready) => ready && account.user_id === userId);
          setCurrentIdentity(account);
        }}
      />
    );
  }

  if (!profile.companion_id) {
    return (
      <main className="mobile-prototype" data-page="companion-onboarding">
        <div className="app-surface">
          <CompanionOnboarding
            userId={userId}
            onComplete={(savedProfile) => {
              const next = { ...profile, ...savedProfile };
              setProfile(next);
              window.localStorage.setItem(
                profileStorageKey(userId),
                JSON.stringify(next),
              );
            }}
          />
        </div>
      </main>
    );
  }

  return (
    <main
      className="mobile-prototype"
      data-page={
        bindFlowOpen
          ? "device-bind"
          : devicePanelOpen
            ? "device-subject"
          : privacyDataOpen
          ? "privacy-data"
          : speakerEnrollmentOpen
            ? "speaker-enrollment"
          : companionSwitchOpen
            ? "companion-switch"
          : digitalSelfOpen
            ? "digital-self"
            : activeTab
      }
    >
      <div className="app-surface">
        {activeTab === "home" && (
          <section className="screen home-screen" aria-label="实时陪伴">
            <header className="home-header">
              <h1 className="home-heading">
                <span className="hero-greeting">{greeting()}，{profile.display_name}</span>
                <span className="hero-title">
                  慢慢说，<em>我会认真听。</em>
                </span>
              </h1>
              <div className="companion-chip" aria-label={`当前伙伴：${sessionCompanion.name}`}>
                <MascotVisual
                  companionId={sessionCompanion.id}
                  emotion={mascotEmotion}
                  className="companion-chip-mascot"
                />
                <span>{sessionCompanion.name} · {sessionCompanion.tagline}</span>
              </div>
            </header>

            {selfPreviewSession && (
              <article className="self-preview-home-disclosure" role="note">
                <ShieldCheck size={19} weight="fill" aria-hidden="true" />
                <div>
                  <strong>数字分身预览，不代表本人</strong>
                  <span>
                    固定版本 {activePreview?.manifest_sha256?.slice(0, 8) || "校验中"}
                    {" · "}
                    仅受控模拟，不写入主人历史
                  </span>
                </div>
              </article>
            )}

            {legacySession && (
              <article className="self-preview-home-disclosure" role="note">
                <ShieldCheck size={19} weight="fill" aria-hidden="true" />
                <div>
                  <strong>冻结数字分身，不是本人</strong>
                  <span>
                    {activeLegacy?.actor_role === "owner_preview"
                      ? "本人预演"
                      : "授权接收人会话"}
                    {" · "}
                    新对话只进入独立关系外壳，不改写主人核心
                  </span>
                </div>
              </article>
            )}

            <div className="mascot-wrap">
              <Mascot
                emotion={mascotEmotion}
                uiState={voice.uiState}
                companionId={sessionCompanion.id}
                active={Boolean(voice.session)}
                disabled={
                  voice.uiState === "connecting" || Boolean(previewBusy)
                }
              />
            </div>

            <div className="voice-status-row">
              <div className="status-pill" data-state={voice.uiState} role="status">
                <span className="status-dot" />
                {homeStatusLabel(voice)}
              </div>
            </div>

            <div className="voice-start-panel">
              {!voice.session && <p>{sessionCompanion.description}</p>}
              {(!voice.session || voice.inputMode === "voice") && (
                <button
                  type="button"
                  className="start-voice-button"
                  disabled={voice.uiState === "connecting" || Boolean(previewBusy)}
                  aria-label={voice.session ? "结束对话" : "开始语音对话"}
                  onClick={() => {
                    if (voice.session) void finishConversation();
                    else void voice.start();
                  }}
                >
                  {voice.session ? (
                    <PhoneDisconnect size={19} weight="fill" aria-hidden="true" />
                  ) : (
                    <Sparkle size={19} weight="fill" aria-hidden="true" />
                  )}
                  {voice.session ? "结束语音对话" : "开始语音对话"}
                </button>
              )}
              {voice.session &&
                voice.inputMode === "voice" &&
                voice.uiState === "speaking" && (
                  <button
                    type="button"
                    className="text-mode-button stop-answer-button"
                    aria-label="停止回答"
                    onClick={() => void voice.stopAssistant()}
                  >
                    <StopCircle size={18} weight="fill" aria-hidden="true" />
                    停止回答
                  </button>
                )}
              {!voice.session && !selfPreviewSession && !legacySession && (
                <button
                  type="button"
                  className="text-mode-button"
                  disabled={voice.uiState === "connecting" || Boolean(previewBusy)}
                  onClick={() => void voice.start({ inputMode: "text" })}
                >
                  <ChatText size={18} weight="bold" aria-hidden="true" />
                  使用文字对话
                </button>
              )}
              {voice.session && voice.inputMode === "text" && (
                <form
                  className="text-composer"
                  onSubmit={async (event) => {
                    event.preventDefault();
                    const message = textDraft.trim();
                    if (!message || textTurnPending || !textInputReady) return;
                    setTextTurnPending(true);
                    if (await voice.sendText(message)) {
                      setTextDraft("");
                    } else {
                      setTextTurnPending(false);
                    }
                  }}
                >
                  <label htmlFor="home-text-message">输入你想说的话</label>
                  <div className="text-composer-row">
                    <textarea
                      id="home-text-message"
                      rows={2}
                      maxLength={500}
                      value={textDraft}
                      onChange={(event) => setTextDraft(event.target.value)}
                      placeholder="例如：今天发生了一件想和你聊聊的事"
                    />
                    <button
                      type="submit"
                      className="text-send-button"
                      disabled={
                        !textDraft.trim() ||
                        textTurnPending ||
                        !textInputReady
                      }
                      aria-label="发送文字消息"
                    >
                      <PaperPlaneRight size={20} weight="fill" aria-hidden="true" />
                      <span>发送</span>
                    </button>
                  </div>
                  <button
                    type="button"
                    className="text-exit-button"
                    aria-label="退出文字对话"
                    onClick={() => void finishConversation()}
                  >
                    <PhoneDisconnect size={18} weight="fill" aria-hidden="true" />
                    退出文字对话
                  </button>
                </form>
              )}
            </div>

            <div className="conversation-area" aria-live="polite">
              <div className="conversation-heading" aria-hidden="true">
                <strong>实时对话</strong>
                <span>仅显示当前发言</span>
              </div>
              {voice.latestTranscript ? (
                <div
                  className={`transcript-card ${voice.latestTranscript.speaker}${
                    voice.latestTranscript.provisional ? " provisional" : ""
                  }`}
                  aria-busy={voice.latestTranscript.provisional === true}
                >
                  <span>
                  {voice.latestTranscript.speaker === "assistant"
                    ? selfPreviewSession
                      ? "数字分身"
                      : legacySession
                        ? "冻结数字分身"
                        : sessionCompanion.name
                    : voice.latestTranscript.provisional
                      ? provisionalSpeakerLabel(voice.latestTranscript, voice.floorState)
                      : "你"}
                  </span>
                  <p>{voice.latestTranscript.text}</p>
                </div>
              ) : (
                <div className="welcome-copy">
                  <h2>
                    {voice.uiState === "speaker_enroll"
                      ? "先登记你的声音"
                      : voice.session
                        ? selfPreviewSession
                          ? "正在预览已批准的数字分身"
                          : legacySession
                            ? "正在与冻结数字分身对话"
                          : "想说什么都可以"
                        : "今天想聊点什么？"}
                  </h2>
                  <p>
                    {voice.uiState === "speaker_enroll"
                      ? "请用正常音量连续说大约四秒，例如“我是主人，请记住我的声音”。登记后会优先听你，减少旁边人插话。"
                      : voice.session
                        ? selfPreviewSession
                          ? "回答会标明事实、推断或未知；可在数字心智中展开来源并纠正。"
                          : legacySession
                            ? "回答只使用当前授权范围；没有足够资料时会明确说不知道。"
                          : "不用按住按钮，我会听完再回应。"
                        : "可以语音说，也可以安静地打字；文字模式不会打开麦克风。"}
                  </p>
                </div>
              )}
              {voice.error && <p className="inline-error">{voice.error}</p>}
              {voice.audioBlocked && profile.voice_reply && (
                <button
                  type="button"
                  className="audio-recovery"
                  onClick={() => void voice.resumeAudio(true)}
                >
                  轻触恢复声音
                </button>
              )}
            </div>

          </section>
        )}

        {activeTab === "memory" && !sensitiveGates.memory_recall && (
          <section className="screen memory-screen" aria-label="私人回顾已关闭">
            <header className="topbar page-topbar">
              <div>
                <p className="eyebrow">私人回顾</p>
                <h1>回顾</h1>
              </div>
            </header>
            <div className="empty-memory">
              <h2>私人记忆暂时不可用</h2>
              <p>{gateMessage("memory_recall")}</p>
              <button
                type="button"
                className="button-primary"
                onClick={() => {
                  setActiveTab("profile");
                  setDevicePanelOpen(true);
                }}
              >
                前往设备与成员
              </button>
            </div>
          </section>
        )}

        {activeTab === "memory" && sensitiveGates.memory_recall && (
          <MemoryScreen
            activeMemory={activeMemory}
            days={memoryDays}
            selectedDay={selectedDay}
            loading={memoryLoading}
            error={memoryError}
            summaryRunning={summaryRunning}
            onSelectDay={setSelectedDay}
            onRunSummary={runSummary}
            onStartChat={() => setActiveTab("home")}
          />
        )}

        {activeTab === "profile" && digitalSelfOpen && (
          <Suspense
            fallback={
              <section className="screen digital-self-screen" aria-label="正在加载数字心智与声音">
                <div className="digital-loading" role="status">
                  <span className="loading-orbit" />
                  正在打开数字心智与声音…
                </div>
              </section>
            }
          >
            <DigitalSelfPanel
              onBack={() => setDigitalSelfOpen(false)}
              voiceSessionActive={Boolean(voice.session)}
              accountType={identity.account_type}
              selfPreviewCapability={selfPreviewCapability}
              activePreview={activePreview}
              previewAnswers={previewAnswers}
              previewBusy={previewBusy}
              fidelitySummary={fidelitySummary}
              fidelityByVersion={fidelityByVersion}
              onStartPreview={startSelfPreview}
              onStopPreview={stopSelfPreview}
              onStartLegacy={startLegacy}
              onExpandPreviewSources={expandSelfPreviewSources}
              onSubmitPreviewFeedback={submitPreviewFeedback}
              onStartFidelity={startFidelity}
              onSubmitFidelity={submitFidelityChoice}
              onCompleteFidelity={finishFidelity}
              onSelectFidelityVersion={(version) =>
                setFidelityFocusVersionId(version.version_id)
              }
              onOpenFidelity={(version) =>
                setFidelityFocusVersionId(version.version_id)
              }
              onStartChat={(task) => {
                if (task?.kind === "natural_chat" && task.status === "active") {
                  setActiveGrowthTask(task);
                }
                setDigitalSelfOpen(false);
                setActiveTab("home");
              }}
              onAccountDeleted={handleAccountDeleted}
              onOpenArchive={() => {
                setDigitalSelfOpen(false);
                setActiveTab("memory");
              }}
              onChangeCompanion={() => {
                setCompanionSwitchOrigin("digital-self");
                setDigitalSelfOpen(false);
                setCompanionSwitchOpen(true);
              }}
            />
          </Suspense>
        )}

        {activeTab === "profile" && speakerEnrollmentOpen && (
          <CompanionOnboarding
            mode="voiceprint"
            companionId={profile.companion_id}
            onBack={() => setSpeakerEnrollmentOpen(false)}
            onComplete={() => {
              setSpeakerEnrollmentNotice(
                "新版主人声纹已进入影子评估；正式评估通过前不会获得主人权限。",
              );
              setSpeakerEnrollmentOpen(false);
            }}
          />
        )}

        {activeTab === "profile" && companionSwitchOpen && (
          <CompanionSwitcher
            userId={userId}
            currentCompanionId={profile.companion_id}
            backLabel={companionSwitchOrigin === "digital-self" ? "返回数字心智" : "返回我的"}
            onBack={() => {
              setCompanionSwitchOpen(false);
              if (companionSwitchOrigin === "digital-self") {
                setDigitalSelfOpen(true);
              }
            }}
            onComplete={(savedProfile) => {
              const next = { ...profile, ...savedProfile };
              setProfile(next);
              window.localStorage.setItem(profileStorageKey(userId), JSON.stringify(next));
              setCompanionSwitchOpen(false);
              if (companionSwitchOrigin === "digital-self") {
                setDigitalSelfOpen(true);
              }
            }}
          />
        )}

        {activeTab === "profile" && privacyDataOpen && (
          <Suspense
            fallback={
              <section className="screen digital-self-screen" aria-label="正在加载隐私与数据">
                <div className="digital-loading" role="status">
                  <span className="loading-orbit" />
                  正在打开隐私与数据…
                </div>
              </section>
            }
          >
            <PrivacyDataPanel onBack={() => setPrivacyDataOpen(false)} />
          </Suspense>
        )}

        {bindFlowOpen && (
          <Suspense
            fallback={
              <section className="screen device-bind-screen" aria-label="正在打开首次绑定">
                <div className="digital-loading" role="status">
                  <span className="loading-orbit" />
                  正在打开首次绑定…
                </div>
              </section>
            }
          >
            <DeviceBindingFlow
              identity={identity}
              onCreateBinding={createDeviceBinding}
              onComplete={async (manifest) => {
                setDeviceBinding(manifest);
                setBindFlowOpen(false);
                setDevicePanelOpen(true);
                await refreshDeviceState();
              }}
              onBack={() => setBindFlowOpen(false)}
            />
          </Suspense>
        )}

        {devicePanelOpen && (
          <Suspense
            fallback={
              <section className="screen device-subject-screen" aria-label="正在打开设备与成员">
                <div className="digital-loading" role="status">
                  <span className="loading-orbit" />
                  正在同步设备与成员…
                </div>
              </section>
            }
          >
            <DeviceSubjectPanel
              binding={deviceBinding}
              profile={deviceProfile}
              resolution={deviceResolution}
              displayContext={deviceDisplayContext}
              sessionId={authoritySessionId}
              voiceActive={Boolean(voice.session)}
              loading={deviceLoading}
              error={deviceError}
              onRefresh={() => refreshDeviceState()}
              onResolveSubject={() =>
                resolveSessionSubject({
                  deviceId: deviceBinding.device_id,
                  sessionId: authoritySessionId,
                  environment: {
                    offline: deviceOffline,
                    multiple_speakers: deviceMultipleSpeakers,
                  },
                }).then((resolution) => {
                  setDeviceResolution(resolution);
                  setDeviceMultipleSpeakers(
                    Boolean(
                      resolution &&
                        (resolution.valid !== true ||
                          resolution.temporary_service_mode === "unknown_safe"),
                    ),
                  );
                  return resolution;
                })
              }
              onSwitchSubject={(personId) =>
                setActiveSubject(authoritySessionId, {
                  personId,
                  confirmationMethod: "app_confirm",
                })
              }
              onBeforeSubjectSwitch={stopVoiceForSubjectSwitch}
              onSubjectSwitched={handleSubjectSwitched}
              onRestartVoice={restartVoiceAfterSwitch}
              onCreateSession={async () => {
                const created = await createDeviceSession(userId);
                setDeviceProfile(created.profile);
                setDeviceBinding(readBindingManifest());
                setAuthoritySessionId(created.profile.session_id);
                await refreshDeviceState();
                return created;
              }}
              onOpenBindFlow={() => {
                setDevicePanelOpen(false);
                setBindFlowOpen(true);
              }}
              onBack={() => {
                setDevicePanelOpen(false);
                setActiveTab("profile");
              }}
            />
          </Suspense>
        )}

        {activeTab === "profile" && !digitalSelfOpen && !speakerEnrollmentOpen && !companionSwitchOpen && !privacyDataOpen && (
          <ProfileScreen
            profile={profile}
            memoryDays={memoryDays}
            onToggle={togglePreference}
            preferenceSaving={preferenceSaving}
            preferenceError={preferenceError}
            onChangeCompanion={() => {
              setCompanionSwitchOrigin("profile");
              setCompanionSwitchOpen(true);
            }}
            onOpenSpeakerEnrollment={() => {
              if (!sensitiveGates.speaker_enrollment) return;
              setSpeakerEnrollmentNotice("");
              setSpeakerEnrollmentOpen(true);
            }}
            voiceSessionActive={Boolean(voice.session)}
            speakerEnrollmentNotice={speakerEnrollmentNotice}
            onOpenDigitalSelf={() => {
              if (!sensitiveGates.digital_self) return;
              setDigitalSelfOpen(true);
            }}
            onOpenPrivacyData={() => {
              if (!sensitiveGates.raw_voice_consent) return;
              setPrivacyDataOpen(true);
            }}
            deviceSummary={deviceSummary}
            sensitiveGates={sensitiveGates}
            gateMessage={gateMessage}
            onOpenDevicePanel={() => {
              setDevicePanelOpen(true);
              void refreshDeviceState();
            }}
            onOpenBindFlow={() => setBindFlowOpen(true)}
            onLogoutCurrent={() => handleLogout(false)}
            onLogoutAll={() => handleLogout(true)}
            onAccountDeleted={handleAccountDeleted}
            accountDeletionOpen={accountDeletionOpen}
            setAccountDeletionOpen={setAccountDeletionOpen}
          />
        )}

        <div ref={voice.audioContainerRef} hidden aria-hidden="true" />

        {!digitalSelfOpen && !speakerEnrollmentOpen && !companionSwitchOpen && !privacyDataOpen && !accountDeletionOpen && !bindFlowOpen && !devicePanelOpen && <nav className="bottom-nav" aria-label="主导航">
          {tabs.map(({ id, label, Icon }) => (
            <button
              type="button"
              key={id}
              className={activeTab === id ? "active" : ""}
              aria-current={activeTab === id ? "page" : undefined}
              onClick={() => {
                setDigitalSelfOpen(false);
                setSpeakerEnrollmentOpen(false);
                setCompanionSwitchOpen(false);
                setPrivacyDataOpen(false);
                setActiveTab(id);
              }}
            >
              <span>
                <Icon size={23} weight={activeTab === id ? "fill" : "regular"} />
              </span>
              {label}
            </button>
          ))}
        </nav>}
      </div>
    </main>
  );
}
