import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  Bell,
  Brain,
  CalendarBlank,
  CaretRight,
  ChatTeardropDots,
  Check,
  Fingerprint,
  HandPalm,
  House,
  Microphone,
  MicrophoneSlash,
  Notebook,
  PencilSimple,
  PhoneDisconnect,
  ShieldCheck,
  SignOut,
  Sparkle,
  Trash,
  UserCircle,
  Waveform,
  X,
} from "@phosphor-icons/react";

import {
  bootstrapIdentity,
  cachePendingMessage,
  flushPendingMessages,
  getGrowthTasks,
  getMemoryDays,
  getProfile,
  loginAccount,
  logoutAllDevices,
  logoutCurrentDevice,
  registerAccount,
  saveMessage,
  summarizeDay,
  transitionGrowthTask,
  updateProfile,
} from "./api.js";
import { AuthScreen } from "./components/AuthScreen.jsx";
import { AccountDeletionForm } from "./components/AccountDeletionForm.jsx";
import { CompanionOnboarding } from "./components/CompanionOnboarding.jsx";
import { CompanionSwitcher } from "./components/CompanionSwitcher.jsx";
import { LifeArchivePanel } from "./components/LifeArchivePanel.jsx";
import { Mascot, MascotVisual } from "./components/Mascot.jsx";
import { useVoiceSession } from "./hooks/useVoiceSession.js";
import { localDateKey } from "./lib/date.js";
import {
  classifyEmotion,
  emotionFromVoice,
  emotionMeta,
} from "./lib/emotion.js";
import { companionById } from "./lib/companions.js";

const tabs = [
  { id: "home", label: "陪伴", Icon: House },
  { id: "memory", label: "回顾", Icon: Notebook },
  { id: "profile", label: "我的", Icon: UserCircle },
];

const defaultProfile = {
  display_name: "新朋友",
  bio: "慢慢说，我会认真听。",
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
// Cascade only for now. Omni / Audio Flash E2E backends are temporarily off.
const VOICE_BACKEND = "cascade";

function formatDay(dateString) {
  const date = new Date(`${dateString}T12:00:00`);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(date);
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
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

export function App() {
  const [identity, setIdentity] = useState(null);
  const [identityReady, setIdentityReady] = useState(false);
  const [identityError, setIdentityError] = useState("");
  const [activeTab, setActiveTab] = useState("home");
  const [profile, setProfile] = useState(defaultProfile);
  const [profileReady, setProfileReady] = useState(false);
  const [draftProfile, setDraftProfile] = useState(defaultProfile);
  const [editingProfile, setEditingProfile] = useState(false);
  const [digitalSelfOpen, setDigitalSelfOpen] = useState(false);
  const [companionSwitchOpen, setCompanionSwitchOpen] = useState(false);
  const [privacyDataOpen, setPrivacyDataOpen] = useState(false);
  const [accountDeletionOpen, setAccountDeletionOpen] = useState(false);
  const [memoryDays, setMemoryDays] = useState([]);
  const [selectedDay, setSelectedDay] = useState(today());
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [summaryRunning, setSummaryRunning] = useState(false);
  const [preferenceSaving, setPreferenceSaving] = useState(false);
  const [preferenceError, setPreferenceError] = useState("");
  const [activeGrowthTask, setActiveGrowthTask] = useState(null);
  const activeUserIdRef = useRef("");
  const growthCompletionIdsRef = useRef({});
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
    async (line) => {
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
      try {
        await saveMessage(message);
      } catch {
        if (activeUserIdRef.current === userId) cachePendingMessage(message);
      }
    },
    [userId],
  );

  const voice = useVoiceSession({
    userId,
    onFinalTranscript: handleFinalTranscript,
    voiceReplyEnabled: profile.voice_reply,
    voiceBackend: VOICE_BACKEND,
    learningTaskId: activeGrowthTask?.task_id || null,
  });

  const loadMemories = useCallback(async () => {
    if (!userId) return;
    const requestedUserId = userId;
    setMemoryLoading(true);
    setMemoryError("");
    try {
      await flushPendingMessages();
      if (activeUserIdRef.current !== requestedUserId) return;
      const result = await getMemoryDays(requestedUserId);
      if (activeUserIdRef.current !== requestedUserId) return;
      const rawDays = Array.isArray(result) ? result : result.items || [];
      const days = rawDays.map(normalizeMemoryDay);
      setMemoryDays(days);
      setSelectedDay((current) =>
        days.length && !days.some((day) => day.date === current)
          ? days[0].date
          : current,
      );
    } catch {
      if (activeUserIdRef.current === requestedUserId) {
        setMemoryError("暂时没有连上回顾服务，对话内容会先安全保存在本机。");
      }
    } finally {
      if (activeUserIdRef.current === requestedUserId) setMemoryLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (!userId) return;
    const requestedUserId = userId;
    setPreferenceSaving(false);
    setPreferenceError("");
    setProfileReady(false);
    setProfile(defaultProfile);
    setDraftProfile(defaultProfile);
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
          bio: result.bio || defaultProfile.bio,
          companion_id: hasCompanionSelection
            ? result.companion_id
            : "starlight",
        };
        setProfile(next);
        setDraftProfile(next);
      } catch {
        if (activeUserIdRef.current !== requestedUserId) return;
        if (local) {
          const next = { ...defaultProfile, ...local };
          setProfile(next);
          setDraftProfile(next);
        } else if (identity?.account_type === "registered") {
          const next = { ...defaultProfile, companion_id: "starlight" };
          setProfile(next);
          setDraftProfile(next);
        }
      } finally {
        if (activeUserIdRef.current === requestedUserId) setProfileReady(true);
      }
    })();
  }, [userId]);

  useEffect(() => {
    if (activeTab !== "home") void loadMemories();
  }, [activeTab, loadMemories]);

  const activeMemory =
    memoryDays.find((day) => day.date === selectedDay) || memoryDays[0] || null;
  const activeCompanion = companionById(profile.companion_id);
  const sessionCompanion = companionById(
    voice.session?.interaction?.companion_style_id || profile.companion_id,
  );

  const runSummary = async () => {
    setSummaryRunning(true);
    setMemoryError("");
    try {
      await summarizeDay(userId, today());
      await loadMemories();
      setSelectedDay(today());
    } catch {
      setMemoryError("今天还没有足够的对话可以生成回顾。");
    } finally {
      setSummaryRunning(false);
    }
  };

  const finishConversation = async () => {
    const endingSession = voice.session;
    await voice.end();
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
  };

  const saveProfile = async () => {
    const next = {
      ...draftProfile,
      display_name: draftProfile.display_name.trim() || "新朋友",
      bio: draftProfile.bio.trim().slice(0, 80),
    };
    setProfile(next);
    setDraftProfile(next);
    window.localStorage.setItem(
      profileStorageKey(userId),
      JSON.stringify(next),
    );
    setEditingProfile(false);
    try {
      await updateProfile(userId, next);
    } catch {
      // Local profile remains usable and will be retried on a later edit.
    }
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
      setDraftProfile(confirmed);
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
    const voiceReset = voice.reset ? voice.reset() : voice.end();
    setCurrentIdentity(null);
    setIdentityError("");
    setProfile(defaultProfile);
    setDraftProfile(defaultProfile);
    setProfileReady(false);
    setMemoryDays([]);
    setSelectedDay(today());
    setMemoryLoading(false);
    setMemoryError("");
    setSummaryRunning(false);
    setPreferenceSaving(false);
    setPreferenceError("");
    setActiveGrowthTask(null);
    growthCompletionIdsRef.current = {};
    setEditingProfile(false);
    setDigitalSelfOpen(false);
    setCompanionSwitchOpen(false);
    setPrivacyDataOpen(false);
    setAccountDeletionOpen(false);
    setActiveTab("home");
    await voiceReset.catch(() => undefined);
  };

  const handleLogout = async (allDevices) => {
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
        onRegister={async (username, password) => {
          const account = await registerAccount(username, password);
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
              setDraftProfile(next);
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
        privacyDataOpen
          ? "privacy-data"
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
            <header className="topbar home-topbar">
              <div>
                <p className="eyebrow">{formatDay(today())}</p>
                <h1>{greeting()}，{profile.display_name}</h1>
              </div>
              <button
                type="button"
                className="avatar-button"
                aria-label="打开个人信息"
                onClick={() => setActiveTab("profile")}
              >
                <MascotVisual
                  companionId={sessionCompanion.id}
                  emotion="neutral"
                  className="avatar-mascot"
                />
              </button>
            </header>

            <div className="voice-status-row">
              <div className="status-pill" data-state={voice.uiState} role="status">
                <span className="status-dot" />
                {voice.statusLabel}
              </div>
            </div>

            <div className="mascot-wrap">
              <Mascot
                emotion={emotionFromVoice(voice.emotionHint?.label)}
                uiState={voice.uiState}
                companionId={sessionCompanion.id}
                active={Boolean(voice.session)}
                disabled={voice.uiState === "connecting"}
                onActivate={() => {
                  if (!voice.session) void voice.start();
                }}
              />
            </div>

            <div className="conversation-area" aria-live="polite">
              {voice.latestTranscript ? (
                <div
                  className={`transcript-card ${voice.latestTranscript.speaker}`}
                >
                  <span>
                    {voice.latestTranscript.speaker === "assistant"
                      ? sessionCompanion.name
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
                        ? "想说什么都可以"
                        : "今天想聊点什么？"}
                  </h2>
                  <p>
                    {voice.uiState === "speaker_enroll"
                      ? "请用正常音量连续说大约四秒，例如“我是主人，请记住我的声音”。登记后会优先听你，减少旁边人插话。"
                      : voice.session
                        ? "不用按住按钮，我会听完再回应。"
                        : "轻触吉祥物，开始一次实时语音对话。"}
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

            {voice.session && (
              <div className="voice-controls" aria-label="对话控制">
                <button
                  type="button"
                  className="round-control"
                  aria-label={voice.micEnabled ? "关闭麦克风" : "打开麦克风"}
                  onClick={() => void voice.toggleMic()}
                >
                  {voice.micEnabled ? (
                    <Microphone size={23} weight="fill" />
                  ) : (
                    <MicrophoneSlash size={23} weight="fill" />
                  )}
                </button>
                <button
                  type="button"
                  className="round-control stop-control"
                  aria-label="停止回答"
                  onClick={() => void voice.stopAssistant()}
                >
                  <HandPalm size={24} weight="fill" />
                </button>
                <button
                  type="button"
                  className="round-control end-control"
                  aria-label="结束对话"
                  onClick={() => void finishConversation()}
                >
                  <PhoneDisconnect size={24} weight="fill" />
                </button>
              </div>
            )}
          </section>
        )}

        {activeTab === "memory" && (
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
                setDigitalSelfOpen(false);
                setCompanionSwitchOpen(true);
              }}
            />
          </Suspense>
        )}

        {activeTab === "profile" && companionSwitchOpen && (
          <CompanionSwitcher
            userId={userId}
            currentCompanionId={profile.companion_id}
            onBack={() => {
              setCompanionSwitchOpen(false);
              setDigitalSelfOpen(true);
            }}
            onComplete={(savedProfile) => {
              const next = { ...profile, ...savedProfile };
              setProfile(next);
              setDraftProfile(next);
              window.localStorage.setItem(profileStorageKey(userId), JSON.stringify(next));
              setCompanionSwitchOpen(false);
              setDigitalSelfOpen(true);
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

        {activeTab === "profile" && !digitalSelfOpen && !companionSwitchOpen && !privacyDataOpen && (
          <ProfileScreen
            profile={profile}
            draftProfile={draftProfile}
            setDraftProfile={setDraftProfile}
            memoryDays={memoryDays}
            editing={editingProfile}
            setEditing={setEditingProfile}
            onSave={saveProfile}
            onToggle={togglePreference}
            preferenceSaving={preferenceSaving}
            preferenceError={preferenceError}
            onOpenDigitalSelf={() => setDigitalSelfOpen(true)}
            onOpenPrivacyData={() => setPrivacyDataOpen(true)}
            onLogoutCurrent={() => handleLogout(false)}
            onLogoutAll={() => handleLogout(true)}
            onAccountDeleted={handleAccountDeleted}
            accountDeletionOpen={accountDeletionOpen}
            setAccountDeletionOpen={setAccountDeletionOpen}
          />
        )}

        <div ref={voice.audioContainerRef} hidden aria-hidden="true" />

        {!digitalSelfOpen && !companionSwitchOpen && !privacyDataOpen && !accountDeletionOpen && <nav className="bottom-nav" aria-label="主导航">
          {tabs.map(({ id, label, Icon }) => (
            <button
              type="button"
              key={id}
              className={activeTab === id ? "active" : ""}
              aria-current={activeTab === id ? "page" : undefined}
              onClick={() => {
                setDigitalSelfOpen(false);
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

function MemoryScreen({
  activeMemory,
  days,
  selectedDay,
  loading,
  error,
  summaryRunning,
  onSelectDay,
  onRunSummary,
  onStartChat,
}) {
  return (
    <section className="screen memory-screen" aria-label="每日回顾">
      <header className="topbar page-topbar">
        <div>
          <p className="eyebrow">每天多懂自己一点</p>
          <h1>回顾</h1>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="生成今天的回顾"
          disabled={summaryRunning}
          onClick={() => void onRunSummary()}
        >
          <Sparkle size={21} weight="fill" />
        </button>
      </header>

      {days.length > 0 && (
        <div className="day-strip" role="group" aria-label="选择日期">
          {days.slice(0, 7).map((day) => (
            <button
              type="button"
              key={day.date}
              className={day.date === selectedDay ? "active" : ""}
              aria-pressed={day.date === selectedDay}
              onClick={() => onSelectDay(day.date)}
            >
              <span>{formatDay(day.date).split("日")[0]}日</span>
              <small>{day.message_count} 段</small>
            </button>
          ))}
        </div>
      )}

      <div className="memory-scroll">
        {loading && !activeMemory ? (
          <div className="empty-memory">
            <span className="loading-orbit" />
            <h2>正在整理你的片刻</h2>
            <p>把散落在对话里的线索，慢慢放到一起。</p>
          </div>
        ) : activeMemory ? (
          <>
            <article className="summary-card">
              <div className="summary-meta">
                <span className="date-badge">
                  <CalendarBlank size={16} weight="fill" />
                  {formatDay(activeMemory.date)}
                </span>
                <span
                  className="mood-badge"
                  style={{
                    "--mood-color":
                      emotionMeta[activeMemory.mood]?.color ||
                      emotionMeta.neutral.color,
                  }}
                >
                  {emotionMeta[activeMemory.mood]?.label || "平静"}
                </span>
              </div>
              <h2>{activeMemory.title || "今天的你，值得被看见"}</h2>
              <p>
                {activeMemory.summary ||
                  "今天的对话已经被好好收起，等你想回看的时候，我都在。"}
              </p>
              <div className="summary-source">
                <Brain size={16} weight="fill" />
                {activeMemory.source === "llm"
                  ? "由 LLM 从今日对话中整理"
                  : "本地安全摘要，接通模型后会进一步优化"}
              </div>
            </article>

            <section className="memory-section">
              <div className="section-heading">
                <h3>今天的重要片刻</h3>
                <span>{activeMemory.highlights.length}</span>
              </div>
              <div className="highlight-list">
                {activeMemory.highlights.map((item, index) => (
                  <div className="highlight-item" key={`${item}-${index}`}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <p>{item}</p>
                  </div>
                ))}
              </div>
            </section>

            <article className="tomorrow-card">
              <div className="card-icon">
                <Sparkle size={21} weight="fill" />
              </div>
              <div>
                <span>给明天的你</span>
                <p>
                  {activeMemory.suggestion ||
                    "留一点空白给自己，明天再慢慢往前走。"}
                </p>
              </div>
            </article>
          </>
        ) : (
          <div className="empty-memory">
            <div className="empty-icon">
              <ChatTeardropDots size={33} weight="fill" />
            </div>
            <h2>第一篇回顾，等你来写</h2>
            <p>完成一次语音聊天后，我会把情绪、片刻和下一步轻轻整理好。</p>
            <button type="button" onClick={onStartChat}>去聊聊</button>
          </div>
        )}
        <LifeArchivePanel />
        {error && <p className="memory-error">{error}</p>}
      </div>
    </section>
  );
}

function ProfileScreen({
  profile,
  draftProfile,
  setDraftProfile,
  memoryDays,
  editing,
  setEditing,
  onSave,
  onToggle,
  preferenceSaving,
  preferenceError,
  onOpenDigitalSelf,
  onOpenPrivacyData,
  onLogoutCurrent,
  onLogoutAll,
  onAccountDeleted,
  accountDeletionOpen,
  setAccountDeletionOpen,
}) {
  const [accountDeletionBusy, setAccountDeletionBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState("");
  const [logoutError, setLogoutError] = useState("");
  const [logoutAllConfirmationOpen, setLogoutAllConfirmationOpen] =
    useState(false);
  const companion = companionById(profile.companion_id);
  const momentCount = memoryDays.reduce(
    (total, day) => total + day.message_count,
    0,
  );
  const runLogout = async (scope) => {
    setLogoutBusy(scope);
    setLogoutError("");
    try {
      await (scope === "all" ? onLogoutAll() : onLogoutCurrent());
    } catch {
      setLogoutError("退出失败，请检查网络后重试。");
    } finally {
      setLogoutBusy("");
    }
  };
  return (
    <section
      className={`screen profile-screen ${editing || accountDeletionOpen ? "sheet-open" : ""}`}
      aria-label="个人信息"
    >
      <header
        className="topbar page-topbar"
        inert={accountDeletionOpen ? true : undefined}
        aria-hidden={accountDeletionOpen || undefined}
      >
        <div>
          <p className="eyebrow">你的陪伴空间</p>
          <h1>我的</h1>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="编辑个人信息"
          onClick={() => setEditing(true)}
        >
          <PencilSimple size={20} weight="bold" />
        </button>
      </header>

      <div
        className="profile-scroll"
        inert={accountDeletionOpen ? true : undefined}
        aria-hidden={accountDeletionOpen || undefined}
      >
        <section className="profile-hero">
          <div className="profile-avatar">
            <MascotVisual
              companionId={companion.id}
              emotion="happy"
              className="profile-mascot"
              ariaLabel={`${companion.name}陪伴机器人`}
            />
          </div>
          <div>
            <h2>{profile.display_name}</h2>
            <p>{profile.bio}</p>
          </div>
        </section>

        <section className="stats-grid" aria-label="陪伴数据">
          <div><strong>{memoryDays.length}</strong><span>聊过的天</span></div>
          <div><strong>{momentCount}</strong><span>记住的片刻</span></div>
          <div><strong>{Math.min(memoryDays.length, 7)}</strong><span>连续陪伴</span></div>
        </section>

        <section className="settings-card">
          <h3>陪伴偏好</h3>
          <PreferenceRow
            Icon={Fingerprint}
            title="过滤明显旁人（实验）"
            caption="默认过滤明显旁人；关闭后访客可聊，但仍不能访问或写入主人回顾"
            checked={profile.reject_non_owner_voice}
            onToggle={() => void onToggle("reject_non_owner_voice")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Brain}
            title="自动生成每日回顾"
            caption="每次聊天结束后静默整理"
            checked={profile.auto_summary}
            onToggle={() => void onToggle("auto_summary")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Waveform}
            title="语音回应"
            caption="让 Memoria 用声音陪你"
            checked={profile.voice_reply}
            onToggle={() => void onToggle("voice_reply")}
            disabled={preferenceSaving}
          />
          <PreferenceRow
            Icon={Bell}
            title="温柔提醒"
            caption="即将开放 · 在合适的时候问候你"
            checked={profile.gentle_reminders}
            disabled
          />
          {preferenceError && <p className="inline-error" role="alert">{preferenceError}</p>}
        </section>

        <button
          type="button"
          className="privacy-card digital-self-entry"
          onClick={onOpenDigitalSelf}
        >
          <span className="privacy-icon"><Fingerprint size={22} weight="fill" /></span>
          <span><strong>数字心智与声音</strong><small>人格学习、声纹识别与声音复刻</small></span>
          <CaretRight size={19} weight="bold" />
        </button>

        <button type="button" className="privacy-card" onClick={onOpenPrivacyData}>
          <span className="privacy-icon"><ShieldCheck size={22} weight="fill" /></span>
          <span><strong>隐私与数据</strong><small>专属凭证保护你的对话</small></span>
          <CaretRight size={19} weight="bold" />
        </button>

        <section className="account-session-card" aria-labelledby="account-session-title">
          <h3 id="account-session-title">账户与设备</h3>
          <button
            type="button"
            className="account-logout-entry"
            disabled={Boolean(logoutBusy)}
            aria-busy={logoutBusy === "current" || undefined}
            onClick={() => void runLogout("current")}
          >
            <SignOut size={19} weight="bold" aria-hidden="true" />
            <span>
              <strong>{logoutBusy === "current" ? "正在退出…" : "退出当前设备"}</strong>
              <small>其他已登录设备保持在线</small>
            </span>
          </button>
          <button
            type="button"
            className="account-logout-entry account-logout-all"
            disabled={Boolean(logoutBusy)}
            onClick={() => {
              setLogoutError("");
              setLogoutAllConfirmationOpen(true);
            }}
          >
            <SignOut size={19} weight="bold" aria-hidden="true" />
            <span>
              <strong>退出所有设备</strong>
              <small>所有设备都需要重新登录</small>
            </span>
          </button>

          {logoutAllConfirmationOpen && (
            <div
              className="account-logout-confirmation"
              role="alertdialog"
              aria-label="确认退出所有设备"
            >
              <p>确定要结束所有设备上的登录吗？</p>
              <div>
                <button
                  type="button"
                  disabled={Boolean(logoutBusy)}
                  onClick={() => setLogoutAllConfirmationOpen(false)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={Boolean(logoutBusy)}
                  aria-busy={logoutBusy === "all" || undefined}
                  onClick={() => void runLogout("all")}
                >
                  {logoutBusy === "all" ? "正在退出…" : "确认退出所有设备"}
                </button>
              </div>
            </div>
          )}
          {logoutError && (
            <p className="inline-error" role="alert">{logoutError}</p>
          )}
        </section>

        <button
          type="button"
          className="account-delete-entry"
          onClick={() => setAccountDeletionOpen(true)}
        >
          <Trash size={19} weight="bold" aria-hidden="true" />
          <span>
            <strong>注销账号</strong>
            <small>永久删除账号及全部数据</small>
          </span>
        </button>
      </div>

      {editing && (
        <div className="sheet-backdrop" role="presentation">
          <form
            className="profile-sheet"
            onSubmit={(event) => {
              event.preventDefault();
              void onSave();
            }}
          >
            <div className="sheet-handle" />
            <div className="sheet-title">
              <div><span>个人信息</span><h2>想让我怎么称呼你？</h2></div>
              <button type="submit" aria-label="保存个人信息">
                <Check size={22} weight="bold" />
              </button>
            </div>
            <label>
              称呼
              <input
                autoFocus
                maxLength={30}
                value={draftProfile.display_name}
                onChange={(event) =>
                  setDraftProfile({
                    ...draftProfile,
                    display_name: event.target.value,
                  })
                }
              />
            </label>
            <label>
              写给 Memoria 的一句话
              <textarea
                rows={3}
                maxLength={80}
                value={draftProfile.bio}
                onChange={(event) =>
                  setDraftProfile({ ...draftProfile, bio: event.target.value })
                }
              />
            </label>
            <button
              type="button"
              className="sheet-cancel"
              onClick={() => {
                setDraftProfile(profile);
                setEditing(false);
              }}
            >
              取消
            </button>
          </form>
        </div>
      )}

      {accountDeletionOpen && (
        <div className="sheet-backdrop account-delete-backdrop" role="presentation">
          <section
            className="account-delete-sheet"
            role="dialog"
            aria-modal="true"
            aria-labelledby="account-delete-title"
            onKeyDown={(event) => {
              if (event.key === "Escape" && !accountDeletionBusy) {
                setAccountDeletionOpen(false);
              }
            }}
          >
            <div className="account-delete-sheet-header">
              <div>
                <span>危险操作</span>
                <h2 id="account-delete-title">注销账号</h2>
              </div>
              <button
                type="button"
                aria-label="关闭注销账号确认"
                disabled={accountDeletionBusy}
                onClick={() => setAccountDeletionOpen(false)}
              >
                <X size={21} weight="bold" aria-hidden="true" />
              </button>
            </div>
            <AccountDeletionForm
              autoFocus
              onBusyChange={setAccountDeletionBusy}
              onDeleted={onAccountDeleted}
              submitLabel="注销账号并删除全部数据"
            />
          </section>
        </div>
      )}
    </section>
  );
}

function PreferenceRow({
  Icon,
  title,
  caption,
  checked,
  onToggle,
  disabled = false,
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`preference-row ${disabled ? "disabled" : ""}`}
      onClick={onToggle}
      disabled={disabled}
    >
      <span className="preference-icon"><Icon size={21} weight="fill" /></span>
      <span className="preference-copy"><strong>{title}</strong><small>{caption}</small></span>
      <span className={`switch ${checked ? "on" : ""}`} aria-hidden="true"><i /></span>
    </button>
  );
}
