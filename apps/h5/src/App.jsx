import { useCallback, useEffect, useState } from "react";
import {
  Bell,
  Brain,
  CalendarBlank,
  CaretRight,
  ChatTeardropDots,
  Check,
  HandPalm,
  House,
  Microphone,
  MicrophoneSlash,
  Notebook,
  PencilSimple,
  PhoneDisconnect,
  ShieldCheck,
  Sparkle,
  UserCircle,
  Waveform,
} from "@phosphor-icons/react";

import {
  bootstrapIdentity,
  cachePendingMessage,
  flushPendingMessages,
  getMemoryDays,
  getProfile,
  saveMessage,
  summarizeDay,
  updateProfile,
} from "./api.js";
import { Mascot } from "./components/Mascot.jsx";
import { useVoiceSession } from "./hooks/useVoiceSession.js";
import { localDateKey } from "./lib/date.js";
import { classifyEmotion, emotionMeta } from "./lib/emotion.js";

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
};

const today = () => localDateKey();
const voiceBackendStorageKey = "memoria:voice-backend";
const voiceBackendOptions = [
  {
    id: "cascade",
    label: "级联",
    title: "现有级联：LiveKit + FunASR + Qwen + CosyVoice",
  },
  {
    id: "qwen_omni",
    label: "Omni Flash",
    title: "Qwen3.5-Omni-Flash-Realtime 端到端语音",
  },
  {
    id: "qwen_omni_plus",
    label: "Omni Plus",
    title: "Qwen3.5-Omni-Plus-Realtime 端到端语音（更高质量）",
  },
];

function initialVoiceBackend() {
  try {
    const stored = window.localStorage.getItem(voiceBackendStorageKey);
    if (voiceBackendOptions.some((option) => option.id === stored)) {
      return stored;
    }
  } catch {
    // fall through
  }
  return "cascade";
}

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
  const [identityError, setIdentityError] = useState("");
  const [activeTab, setActiveTab] = useState("home");
  const [emotion, setEmotion] = useState("neutral");
  const [profile, setProfile] = useState(defaultProfile);
  const [profileReady, setProfileReady] = useState(false);
  const [draftProfile, setDraftProfile] = useState(defaultProfile);
  const [editingProfile, setEditingProfile] = useState(false);
  const [memoryDays, setMemoryDays] = useState([]);
  const [selectedDay, setSelectedDay] = useState(today());
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [summaryRunning, setSummaryRunning] = useState(false);
  const [voiceBackend, setVoiceBackend] = useState(initialVoiceBackend);
  const userId = identity?.user_id || "";

  const loadIdentity = useCallback(async () => {
    setIdentityError("");
    try {
      setIdentity(await bootstrapIdentity());
    } catch {
      setIdentityError("暂时无法建立安全身份，请检查网络后重试。");
    }
  }, []);

  useEffect(() => {
    void loadIdentity();
  }, [loadIdentity]);

  const handleFinalTranscript = useCallback(
    async (line) => {
      if (!userId) return;
      const nextEmotion = classifyEmotion(line.text);
      setEmotion(nextEmotion);
      const message = {
        user_id: userId,
        role: line.speaker,
        text: line.text,
        emotion: nextEmotion,
      };
      try {
        await saveMessage(message);
      } catch {
        cachePendingMessage(message);
      }
    },
    [userId],
  );

  const voice = useVoiceSession({
    userId,
    onFinalTranscript: handleFinalTranscript,
    voiceReplyEnabled: profile.voice_reply,
    voiceBackend,
  });

  const voiceBackendLocked =
    Boolean(voice.session) || voice.uiState === "connecting";
  const selectVoiceBackend = (nextBackend) => {
    if (voiceBackendLocked) return;
    setVoiceBackend(nextBackend);
    window.localStorage.setItem(voiceBackendStorageKey, nextBackend);
  };

  const loadMemories = useCallback(async () => {
    if (!userId) return;
    setMemoryLoading(true);
    setMemoryError("");
    try {
      await flushPendingMessages();
      const result = await getMemoryDays(userId);
      const rawDays = Array.isArray(result) ? result : result.items || [];
      const days = rawDays.map(normalizeMemoryDay);
      setMemoryDays(days);
      setSelectedDay((current) =>
        days.length && !days.some((day) => day.date === current)
          ? days[0].date
          : current,
      );
    } catch {
      setMemoryError("暂时没有连上回顾服务，对话内容会先安全保存在本机。");
    } finally {
      setMemoryLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (!userId) return;
    setProfileReady(false);
    void (async () => {
      let local = null;
      try {
        local = JSON.parse(window.localStorage.getItem("memoria:profile") || "null");
      } catch {
        local = null;
      }
      try {
        const result = await getProfile(userId);
        const next = {
          ...defaultProfile,
          ...(local || {}),
          ...result,
          bio: result.bio || defaultProfile.bio,
        };
        setProfile(next);
        setDraftProfile(next);
      } catch {
        if (local) {
          const next = { ...defaultProfile, ...local };
          setProfile(next);
          setDraftProfile(next);
        }
      } finally {
        setProfileReady(true);
      }
    })();
  }, [userId]);

  useEffect(() => {
    if (activeTab !== "home") void loadMemories();
  }, [activeTab, loadMemories]);

  useEffect(() => {
    if (voice.latestTranscript?.text) {
      setEmotion(classifyEmotion(voice.latestTranscript.text));
    }
  }, [voice.latestTranscript]);

  const activeMemory =
    memoryDays.find((day) => day.date === selectedDay) || memoryDays[0] || null;

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
    await voice.end();
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
    window.localStorage.setItem("memoria:profile", JSON.stringify(next));
    setEditingProfile(false);
    try {
      await updateProfile(userId, next);
    } catch {
      // Local profile remains usable and will be retried on a later edit.
    }
  };

  const togglePreference = (field) => {
    const next = { ...profile, [field]: !profile[field] };
    if (field === "voice_reply" && next.voice_reply) {
      void voice.resumeAudio(true);
    }
    setProfile(next);
    setDraftProfile(next);
    window.localStorage.setItem("memoria:profile", JSON.stringify(next));
    void updateProfile(userId, next).catch(() => undefined);
  };

  if (!identity || !profileReady) {
    return (
      <main className="mobile-prototype" data-page="bootstrap">
        <div className="app-surface">
          <section className="screen bootstrap-screen" aria-label="正在准备陪伴空间">
            <div className="empty-memory">
              {!identityError && <span className="loading-orbit" />}
              <h2>{identityError ? "还差一点连接" : "正在准备你的陪伴空间"}</h2>
              <p>
                {identityError ||
                  (identity
                    ? "正在同步你的陪伴偏好。"
                    : "正在建立专属匿名身份，不会把永久密钥放进浏览器。")}
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

  return (
    <main className="mobile-prototype" data-page={activeTab}>
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
                <img
                  src={`${import.meta.env.BASE_URL}assets/mascot-neutral.webp`}
                  alt=""
                />
              </button>
            </header>

            <div className="voice-status-row">
              <div
                className="voice-backend-selector"
                role="radiogroup"
                aria-label="语音模型"
              >
                {voiceBackendOptions.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    role="radio"
                    title={option.title}
                    aria-label={option.label}
                    aria-checked={voiceBackend === option.id}
                    disabled={voiceBackendLocked}
                    onClick={() => selectVoiceBackend(option.id)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <div className="status-pill" data-state={voice.uiState}>
                <span className="status-dot" />
                {voice.statusLabel}
              </div>
            </div>

            <div className="mascot-wrap">
              <Mascot
                emotion={emotion}
                uiState={voice.uiState}
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
                      ? "Memoria"
                      : "你"}
                  </span>
                  <p>{voice.latestTranscript.text}</p>
                </div>
              ) : (
                <div className="welcome-copy">
                  <h2>{voice.session ? "想说什么都可以" : "今天想聊点什么？"}</h2>
                  <p>
                    {voice.session
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

        {activeTab === "profile" && (
          <ProfileScreen
            profile={profile}
            draftProfile={draftProfile}
            setDraftProfile={setDraftProfile}
            memoryDays={memoryDays}
            editing={editingProfile}
            setEditing={setEditingProfile}
            onSave={saveProfile}
            onToggle={togglePreference}
          />
        )}

        <div ref={voice.audioContainerRef} hidden aria-hidden="true" />

        <nav className="bottom-nav" aria-label="主导航">
          {tabs.map(({ id, label, Icon }) => (
            <button
              type="button"
              key={id}
              className={activeTab === id ? "active" : ""}
              aria-current={activeTab === id ? "page" : undefined}
              onClick={() => setActiveTab(id)}
            >
              <span>
                <Icon size={23} weight={activeTab === id ? "fill" : "regular"} />
              </span>
              {label}
            </button>
          ))}
        </nav>
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
}) {
  const momentCount = memoryDays.reduce(
    (total, day) => total + day.message_count,
    0,
  );
  return (
    <section
      className={`screen profile-screen ${editing ? "sheet-open" : ""}`}
      aria-label="个人信息"
    >
      <header className="topbar page-topbar">
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

      <div className="profile-scroll">
        <section className="profile-hero">
          <div className="profile-avatar">
            <img
              src={`${import.meta.env.BASE_URL}assets/mascot-happy.webp`}
              alt="Memoria 吉祥物"
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
            Icon={Brain}
            title="自动生成每日回顾"
            caption="每次聊天结束后静默整理"
            checked={profile.auto_summary}
            onToggle={() => onToggle("auto_summary")}
          />
          <PreferenceRow
            Icon={Waveform}
            title="语音回应"
            caption="让 Memoria 用声音陪你"
            checked={profile.voice_reply}
            onToggle={() => onToggle("voice_reply")}
          />
          <PreferenceRow
            Icon={Bell}
            title="温柔提醒"
            caption="即将开放 · 在合适的时候问候你"
            checked={profile.gentle_reminders}
            disabled
          />
        </section>

        <button type="button" className="privacy-card">
          <span className="privacy-icon"><ShieldCheck size={22} weight="fill" /></span>
          <span><strong>隐私与数据</strong><small>专属凭证保护你的对话</small></span>
          <CaretRight size={19} weight="bold" />
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
