import {
  Brain,
  CalendarBlank,
  ChatTeardropDots,
  Sparkle,
} from "@phosphor-icons/react";

import { formatDay } from "../lib/date.js";
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
