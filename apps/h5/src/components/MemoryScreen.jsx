import {
  Brain,
  CalendarBlank,
  ChatTeardropDots,
  Sparkle,
} from "@phosphor-icons/react";

import { formatDay, localDateKey } from "../lib/date.js";
import { emotionMeta } from "../lib/emotion.js";
import { LifeArchivePanel } from "./LifeArchivePanel.jsx";

export function MemoryScreen({
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
  const todayDate = localDateKey();
  const selectedIsToday = selectedDay === todayDate;
  const todayDay = days.find((day) => day.date === todayDate);
  const todayPlaceholder = {
    date: todayDate,
    message_count: 0,
    source: null,
    title: null,
    summary: null,
    highlights: [],
    mood: "neutral",
    suggestion: null,
    placeholder: true,
  };
  // Keep today visible while browsing older days.  The old implementation
  // only injected this placeholder for the selected date, so one click made
  // today's newly persisted conversation disappear from the strip.
  const visibleDays = [
    todayDay || todayPlaceholder,
    ...days.filter((day) => day.date !== todayDate),
  ]
    .sort((left, right) => right.date.localeCompare(left.date))
    .slice(0, 7);
  const selectedMemory =
    activeMemory || (selectedIsToday ? todayPlaceholder : null);

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

      {visibleDays.length > 0 && (
        <div className="day-strip" role="group" aria-label="选择日期">
          {visibleDays.slice(0, 7).map((day) => (
            <button
              type="button"
              key={day.date}
              data-date={day.date}
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
        {loading && !activeMemory && days.length === 0 ? (
          <div className="empty-memory">
            <span className="loading-orbit" />
            <h2>正在整理你的片刻</h2>
            <p>把散落在对话里的线索，慢慢放到一起。</p>
          </div>
        ) : selectedMemory ? (
          <>
            <article className="summary-card">
              <div className="summary-meta">
                <span className="date-badge">
                  <CalendarBlank size={16} weight="fill" />
                  {formatDay(selectedMemory.date)}
                </span>
                <span
                  className="mood-badge"
                  style={{
                    "--mood-color":
                      emotionMeta[selectedMemory.mood]?.color ||
                      emotionMeta.neutral.color,
                  }}
                >
                  {emotionMeta[selectedMemory.mood]?.label || "平静"}
                </span>
              </div>
              <h2>
                {selectedMemory.title ||
                  (selectedMemory.placeholder
                    ? "今天还没有可回顾的主人对话"
                    : null) ||
                  (selectedIsToday ? "今天的你，值得被看见" : "这一天的你，值得被看见")}
              </h2>
              <p>
                {selectedMemory.summary ||
                  (selectedMemory.placeholder
                    ? "完成一段被主人确认的对话后，这里会显示当天回顾；旁人或未确认的声音不会写入主人长期回顾。"
                    : null) ||
                  (selectedIsToday
                    ? "今天的对话已经被好好收起，等你想回看的时候，我都在。"
                    : "这一天的对话已经被好好收起，等你想回看的时候，我都在。")}
              </p>
              <div className="summary-source">
                <Brain size={16} weight="fill" />
                {selectedMemory.placeholder
                  ? "等待今天的主人对话"
                  : selectedMemory.source === "llm"
                  ? `由 LLM 从${selectedIsToday ? "今日" : "当日"}对话中整理`
                  : "本地安全摘要，接通模型后会进一步优化"}
              </div>
            </article>

            <section className="memory-section">
              <div className="section-heading">
                <h3>{selectedIsToday ? "今天的重要片刻" : "当天的重要片刻"}</h3>
                <span>{selectedMemory.highlights.length}</span>
              </div>
              <div className="highlight-list">
                {selectedMemory.highlights.map((item, index) => (
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
                  {selectedMemory.suggestion ||
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
