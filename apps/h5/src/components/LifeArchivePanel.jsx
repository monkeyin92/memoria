import { useEffect, useState } from "react";
import {
  CheckCircle,
  ClockCounterClockwise,
  MagnifyingGlass,
  PencilSimple,
  WarningCircle,
} from "@phosphor-icons/react";

import {
  getLifeTimeline,
  getMemoryReviewQueue,
  reviewMemoryClaim,
  searchLifeArchive,
} from "../api.js";

const categoryLabels = {
  family_story: "家庭故事",
  family_value: "家风家训",
  parenting: "育儿理念",
  work_experience: "工作经验",
  life_wisdom: "处世智慧",
  preference: "偏好",
  person_fact: "人物",
  relationship: "关系",
  life_event: "人生事件",
  qa_experience: "经验问答",
  other: "其他",
};

function itemsOf(result) {
  return Array.isArray(result) ? result : result?.items || [];
}

function dateLabel(value) {
  if (!value) return "时间待确认";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

function reasonLabel(reason) {
  return reason === "conflicting_values"
    ? "这条记忆与已有内容存在冲突"
    : "这条候选记忆需要你确认";
}

export function LifeArchivePanel() {
  const [timeline, setTimeline] = useState([]);
  const [reviewItems, setReviewItems] = useState([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [corrections, setCorrections] = useState({});
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let active = true;
    void Promise.all([getLifeTimeline(), getMemoryReviewQueue()])
      .then(([timelineResult, reviewResult]) => {
        if (!active) return;
        setTimeline(itemsOf(timelineResult));
        setReviewItems(itemsOf(reviewResult));
      })
      .catch(() => {
        if (active) setError("人生知识库暂时无法同步，请稍后重试。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const search = async (event) => {
    event.preventDefault();
    const value = query.trim();
    if (!value) return;
    setSearching(true);
    setError("");
    try {
      setResults(itemsOf(await searchLifeArchive(value)));
    } catch {
      setError("没有完成这次搜索，请稍后重试。");
    } finally {
      setSearching(false);
    }
  };

  const review = async (item, action) => {
    const correctedValue = corrections[item.item_id]?.trim() || null;
    if (action === "correct" && !correctedValue) return;
    setBusy(item.item_id);
    setError("");
    setNotice("");
    try {
      const result = await (action === "correct"
        ? reviewMemoryClaim(item.item_id, action, correctedValue)
        : reviewMemoryClaim(item.item_id, action));
      if (result.status === "disputed") {
        setReviewItems((current) =>
          current.map((entry) =>
            entry.item_id === item.item_id ? { ...entry, status: "disputed" } : entry,
          ),
        );
        setNotice("已标记为有争议，等待你之后修正或确认。");
      } else {
        setReviewItems((current) =>
          current.filter((entry) => entry.item_id !== item.item_id),
        );
        setNotice(action === "correct" ? "修正已保存并保留原始证据。" : "记忆已确认。");
      }
    } catch {
      setError("这条记忆没有更新成功，请稍后重试。");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="life-archive" aria-label="人生知识库">
      <div className="life-archive-heading">
        <div>
          <p className="eyebrow">有来源、可纠正、可撤销</p>
          <h2>人生知识库</h2>
        </div>
        <span>{timeline.length + results.length} 条线索</span>
      </div>

      <section className="archive-block" aria-labelledby="life-timeline-title">
        <div className="archive-block-title">
          <ClockCounterClockwise size={20} weight="fill" aria-hidden="true" />
          <h3 id="life-timeline-title">人生时间线</h3>
        </div>
        {loading ? (
          <p className="archive-empty" role="status">正在整理时间线…</p>
        ) : timeline.length ? (
          <div className="life-timeline-list">
            {timeline.map((item) => (
              <article key={item.timeline_id} className="life-timeline-item">
                <time dateTime={item.event_start}>{dateLabel(item.event_start)}</time>
                <div>
                  <strong>{item.title}</strong>
                  <span>{categoryLabels[item.category] || item.category}</span>
                </div>
                <small data-status={item.status}>
                  {item.status === "confirmed" ? "已确认" : "候选"}
                </small>
              </article>
            ))}
          </div>
        ) : (
          <p className="archive-empty">继续聊聊后，重要经历会按时间出现在这里。</p>
        )}
      </section>

      <section className="archive-block" aria-labelledby="archive-search-title">
        <div className="archive-block-title">
          <MagnifyingGlass size={20} weight="bold" aria-hidden="true" />
          <h3 id="archive-search-title">记忆搜索</h3>
        </div>
        <form className="archive-search" onSubmit={search}>
          <label htmlFor="life-archive-query">搜索人生知识库</label>
          <div>
            <input
              id="life-archive-query"
              type="search"
              maxLength="500"
              placeholder="人物、故事、经验或一句话"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="submit" disabled={!query.trim() || searching}>
              {searching ? "搜索中…" : "搜索记忆"}
            </button>
          </div>
        </form>
        {results.length > 0 && (
          <div className="archive-search-results" aria-live="polite">
            {results.map((item) => (
              <article key={`${item.kind}-${item.item_id}`}>
                <div>
                  <span>{categoryLabels[item.category] || item.category}</span>
                  <small>{item.status === "confirmed" ? "已确认" : "候选"}</small>
                </div>
                <strong>{item.title}</strong>
                <p>{item.snippet}</p>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="archive-block" aria-labelledby="memory-review-title">
        <div className="archive-block-title">
          <WarningCircle size={20} weight="fill" aria-hidden="true" />
          <h3 id="memory-review-title">待你确认</h3>
          <span>{reviewItems.length}</span>
        </div>
        {reviewItems.length ? (
          <div className="memory-review-list">
            {reviewItems.map((item) => (
              <article key={item.item_id} className="memory-review-item">
                <div className="memory-review-meta">
                  <span>{categoryLabels[item.category] || item.category}</span>
                  <small>{reasonLabel(item.reason)}</small>
                </div>
                <p>{item.value}</p>
                <div className="memory-review-actions">
                  <button
                    type="button"
                    disabled={busy === item.item_id}
                    onClick={() => void review(item, "confirm")}
                  >
                    <CheckCircle size={16} weight="fill" aria-hidden="true" />
                    确认这条记忆
                  </button>
                  <button
                    type="button"
                    disabled={busy === item.item_id}
                    onClick={() => void review(item, "dispute")}
                  >
                    标记有争议
                  </button>
                </div>
                <div className="memory-correction">
                  <label htmlFor={`memory-correction-${item.item_id}`}>
                    修正这条记忆
                  </label>
                  <div>
                    <input
                      id={`memory-correction-${item.item_id}`}
                      maxLength="8000"
                      value={corrections[item.item_id] || ""}
                      onChange={(event) =>
                        setCorrections((current) => ({
                          ...current,
                          [item.item_id]: event.target.value,
                        }))
                      }
                    />
                    <button
                      type="button"
                      disabled={!corrections[item.item_id]?.trim() || busy === item.item_id}
                      onClick={() => void review(item, "correct")}
                      aria-label={`保存“${item.value}”的修正`}
                    >
                      <PencilSimple size={16} aria-hidden="true" />
                      保存修正
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="archive-empty">当前没有需要确认或处理冲突的记忆。</p>
        )}
      </section>

      {notice && <p className="archive-notice" role="status">{notice}</p>}
      {error && <p className="memory-error" role="alert">{error}</p>}
    </section>
  );
}
