import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowClockwise, ChatTeardropDots, Check, WarningCircle } from "@phosphor-icons/react";

import {
  createGrowthTask,
  getGrowthOverview,
  getGrowthTasks,
  respondGrowthTask,
  reviewGrowthOwnerAction,
  transitionGrowthTask,
} from "../api.js";

const dimensionLabels = {
  life_chapters: "人生章节",
  important_people: "重要的人",
  expression: "表达方式",
  decision_cases: "决策案例",
  relationship_models: "关系模式",
  voice: "声音",
  legacy: "传承",
};

const dimensionStatusLabels = {
  empty: "尚未形成",
  emerging: "正在形成",
  supported: "已有支持",
  conflicted: "存在冲突",
};

const readinessLabels = {
  ready: "版本已就绪",
  stale: "版本待更新",
  not_built: "尚未构建",
  dependency_pending: "等待依赖",
};

const rejectionLabels = {
  claim_candidate: "记忆仍待确认",
  claim_disputed: "记忆存在争议",
  claim_retracted: "记忆已撤销",
  timeline_candidate: "时间线片段仍待确认",
  timeline_disputed: "时间线片段存在争议",
  timeline_retracted: "时间线片段已撤销",
  person_not_confirmed: "人物信息仍待确认",
  persona_candidate: "表达特征仍待确认",
  persona_disabled: "表达特征已停用",
  persona_source_ineligible: "表达来源不符合主人证据要求",
  relationship_not_confirmed: "关系信息仍待确认",
  voice_not_ready: "声音档案尚未通过完整评估",
  not_owner: "不是主人原话",
  simulated_or_non_companion: "来自模拟或非陪伴模式",
  owner_projection_ineligible: "没有主人投影资格",
  negative_action: "属于否决证据",
  action_not_whitelisted: "不是可采纳的主人动作",
  unsupported_event_type: "不是可采纳的证据类型",
};

const blockerLabels = {
  dependency_pending: "等待后续领域能力接入",
};

const conflictActionLabels = {
  not_me: "不像我",
  would_not_say: "我不会这样说",
};

const taskLabels = {
  natural_chat: "自然聊天",
  life_interview: "人生访谈",
  scenario_choice: "情境选择",
  decision_review: "决策复盘",
};

const taskStatusLabels = {
  draft: "待开始",
  active: "进行中",
  paused: "已暂停",
  completed: "已完成",
  cancelled: "已取消",
};

const sourceTargetKinds = new Set([
  "memory_claim",
  "persona_trait",
  "source_event",
  "digital_self_version",
  "person_entity",
  "timeline_entry",
  "relationship",
  "voice_profile",
]);

const fallbackGrowthOverview = () => Promise.resolve({ dimensions: [] });
const fallbackGrowthTasks = () => Promise.resolve({ items: [] });

function eventId() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `growth-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function safeDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

function errorMessage(error) {
  return error instanceof Error && error.message ? error.message : "操作没有完成，请稍后重试。";
}

function callOrFallback(fn, fallback) {
  return typeof fn === "function" ? fn : fallback;
}

function weightLabel(weight) {
  if (weight === "strong") return "直接表达";
  if (weight === "normal") return "开放访谈";
  return "弱候选";
}

function SourceActions({ source, sourceName, busy, onAction }) {
  if (
    !source?.target_kind ||
    !source?.target_id ||
    !sourceTargetKinds.has(source.target_kind)
  ) return null;
  return (
    <div className="growth-source-actions">
      <button
        type="button"
        className="button-quiet"
        aria-label={`不像我：${sourceName}`}
        disabled={busy}
        onClick={() => onAction(source, "not_me")}
      >
        不像我
      </button>
      <button
        type="button"
        className="button-quiet"
        aria-label={`我不会这样说：${sourceName}`}
        disabled={busy}
        onClick={() => onAction(source, "would_not_say")}
      >
        我不会这样说
      </button>
    </div>
  );
}

function DimensionCard({ dimension, busy, onSourceAction }) {
  const reasonEntries = Object.entries(dimension.rejected_reason_counts || {});
  return (
    <article className="growth-dimension-card" data-status={dimension.status}>
      <div className="growth-card-heading">
        <div>
          <p className="growth-card-kicker">成长维度</p>
          <h3>{dimensionLabels[dimension.key] || dimension.key}</h3>
        </div>
        <span className="growth-status" data-status={dimension.status}>
          {dimensionStatusLabels[dimension.status] || dimension.status}
        </span>
      </div>
      <p className="growth-source-count">采用来源 {dimension.adopted_sources.length} 条</p>
      {dimension.adopted_sources.length > 0 && (
        <ul className="growth-source-list">
          {dimension.adopted_sources.map((source, index) => (
            <li key={`${source.target_kind}:${source.target_id}:${source.event_id}`}>
              <div>
                <strong>{source.label || source.kind}</strong>
                <small>{weightLabel(source.weight)}</small>
                {source.occurred_at && <small>{safeDate(source.occurred_at)}</small>}
              </div>
              <SourceActions
                source={source}
                sourceName={`${dimensionLabels[dimension.key] || dimension.key}来源 ${index + 1}，${source.label || source.kind}`}
                busy={Boolean(busy)}
                onAction={onSourceAction}
              />
            </li>
          ))}
        </ul>
      )}
      {reasonEntries.length > 0 && (
        <div className="growth-detail-block">
          <strong>拒绝原因</strong>
          <p>
            {reasonEntries
              .map(([reason, count]) => `${rejectionLabels[reason] || "暂未采用"} ${count} 条`)
              .join("、")}
          </p>
        </div>
      )}
      {dimension.conflicts.length > 0 && (
        <div className="growth-detail-block">
          <strong>冲突</strong>
          <ul>
            {dimension.conflicts.map((conflict) => (
              <li key={`${conflict.event_id}-${conflict.target_id}`}>
                {conflictActionLabels[conflict.action] || "等待本人处理"}
              </li>
            ))}
          </ul>
        </div>
      )}
      {dimension.recent_changes.length > 0 && (
        <div className="growth-detail-block">
          <strong>最近变化</strong>
          <p>{dimension.recent_changes.map((change) => `${change.event_type}（${safeDate(change.occurred_at)}）`).join("、")}</p>
        </div>
      )}
      {dimension.dependency_blockers.length > 0 && (
        <div className="growth-detail-block growth-blocker">
          <strong>依赖阻塞</strong>
          <ul>
            {dimension.dependency_blockers.map((blocker) => (
              <li key={blocker}>{blockerLabels[blocker] || "等待后续能力接入"}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="growth-readiness">
        <span>版本状态</span>
        <strong>{readinessLabels[dimension.version_readiness.status] || dimension.version_readiness.status}</strong>
        {dimension.version_readiness.version_id && <small>{dimension.version_readiness.version_id}</small>}
      </div>
    </article>
  );
}

export function GrowthMapPanel({
  onStartChat,
  onOverview,
  refreshToken = "",
  voiceSessionActive = false,
}) {
  const [overview, setOverview] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [answers, setAnswers] = useState({});
  const eventIds = useRef({});
  const createGenerations = useRef({});
  const feedbackGenerations = useRef({});
  const loadOverview = callOrFallback(getGrowthOverview, fallbackGrowthOverview);
  const loadTasks = callOrFallback(getGrowthTasks, fallbackGrowthTasks);

  const reload = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError("");
    const results = await Promise.allSettled([loadOverview(), loadTasks()]);
    if (results[0].status === "fulfilled") {
      const nextOverview = results[0].value || { dimensions: [] };
      setOverview(nextOverview);
      onOverview?.(nextOverview);
    }
    if (results[1].status === "fulfilled") setTasks(results[1].value?.items || []);
    if (results.some((result) => result.status === "rejected")) {
      setError("成长地图暂时无法同步，请稍后重试。");
    }
    if (!silent) setLoading(false);
  }, [loadOverview, loadTasks, onOverview]);

  useEffect(() => {
    void reload();
  }, [refreshToken, reload]);

  const action = async (key, callback, success = "") => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const result = await callback();
      if (success) setNotice(success);
      return result;
    } catch (actionError) {
      setError(errorMessage(actionError));
      return null;
    } finally {
      setBusy("");
    }
  };

  const eventIdFor = (key) => {
    if (!eventIds.current[key]) eventIds.current[key] = eventId();
    return eventIds.current[key];
  };

  const sourceAction = (source, actionName) => {
    const generationKey = `${source.event_id}:${actionName}`;
    const generation = feedbackGenerations.current[generationKey] || 0;
    const feedbackKey = `feedback:${generationKey}:${generation}`;
    return action(
      feedbackKey,
      () => callOrFallback(
        reviewGrowthOwnerAction,
        () => Promise.reject(new Error("成长来源操作暂不可用")),
      )(eventIdFor(feedbackKey), actionName, source.target_kind, source.target_id)
        .then(async (result) => {
          feedbackGenerations.current[generationKey] = generation + 1;
          await reload({ silent: true });
          return result;
        }),
      "已记录你的纠正，成长地图正在刷新。",
    );
  };

  const startTask = async (kind) => {
    if (kind === "natural_chat" && voiceSessionActive) {
      setError("请先结束当前对话，再开始新的自然聊天任务。");
      return;
    }
    const generation = createGenerations.current[kind] || 0;
    const createKey = `task:${kind}:create:${generation}`;
    const created = await action(
      createKey,
      () => callOrFallback(
        createGrowthTask,
        () => Promise.reject(new Error("成长任务暂不可用")),
      )(eventIdFor(createKey), kind).then(async (task) => {
        if (task.status === "draft") {
          const startKey = `task:${task.task_id}:start:${task.revision}`;
          return transitionGrowthTask(
            task.task_id,
            eventIdFor(startKey),
            "active",
            task.revision,
          );
        }
        return task;
      }),
      kind === "natural_chat"
        ? "自然聊天任务已开始。"
        : "任务已开始，写下你的回答吧。",
    );
    if (created) {
      createGenerations.current[kind] = generation + 1;
      setTasks((current) => [...current.filter((task) => task.task_id !== created.task_id), created]);
      if (kind === "natural_chat") onStartChat?.(created);
    }
  };

  const activateTask = async (task) => {
    if (task.kind === "natural_chat" && voiceSessionActive) {
      setError("请先结束当前对话，再继续这个自然聊天任务。");
      return;
    }
    const transitionKey = `task:${task.task_id}:active:${task.revision}`;
    const updated = await action(
      transitionKey,
      () => callOrFallback(
        transitionGrowthTask,
        () => Promise.reject(new Error("成长任务暂不可用")),
      )(
        task.task_id,
        eventIdFor(transitionKey),
        "active",
        task.revision,
      ),
      task.status === "paused" ? "任务已继续。" : "任务已开始。",
    );
    if (updated) {
      setTasks((current) =>
        current.map((item) => item.task_id === updated.task_id ? updated : item),
      );
      if (updated.kind === "natural_chat") onStartChat?.(updated);
    }
  };

  const submitAnswer = async (task) => {
    const answer = answers[task.task_id]?.trim();
    if (!answer) {
      setError("请先写下你的回答。");
      return;
    }
    const updated = await action(
      `answer-${task.task_id}`,
      () => callOrFallback(
        respondGrowthTask,
        () => Promise.reject(new Error("成长任务回答暂不可用")),
      )(
        task.task_id,
        eventIdFor(`task:${task.task_id}:response:${task.revision}`),
        task.revision,
        answer,
      ).then(async (nextTask) => {
        if (nextTask.status === "completed") return nextTask;
        const completeKey = `task:${task.task_id}:complete:${nextTask.revision}`;
        return transitionGrowthTask(
          nextTask.task_id,
          eventIdFor(completeKey),
          "completed",
          nextTask.revision,
        );
      }),
      "回答已保存，成长地图会在同步后更新。",
    );
    if (updated) {
      setTasks((current) => current.map((item) => item.task_id === updated.task_id ? updated : item));
      setAnswers((current) => ({ ...current, [task.task_id]: "" }));
      await reload({ silent: true });
    }
  };

  return (
    <section className="digital-section growth-map-section" aria-labelledby="growth-map-title">
      <div className="digital-section-heading">
        <span className="digital-section-icon"><ChatTeardropDots size={22} weight="fill" aria-hidden="true" /></span>
        <div>
          <p>从被确认的原话开始</p>
          <h2 id="growth-map-title">成长地图</h2>
        </div>
      </div>
      <p className="growth-map-boundary">
        这里展示的是定性成长状态和证据来源，不是完成百分比；每一条材料都可以被你纠正。
      </p>
      <div className="growth-map-feedback" aria-live="polite">
        {loading && <span role="status">正在读取成长地图…</span>}
        {error && (
          <span role="alert">
            <WarningCircle size={16} weight="fill" aria-hidden="true" />
            {error}
            <button type="button" className="button-quiet" onClick={() => void reload()} disabled={Boolean(busy)}>
              <ArrowClockwise size={15} aria-hidden="true" /> 重试
            </button>
          </span>
        )}
        {!loading && !error && notice && <span><Check size={16} weight="bold" aria-hidden="true" />{notice}</span>}
      </div>
      {!loading && (
        <>
          <div className="growth-dimension-grid">
            {(overview?.dimensions || []).map((dimension) => (
              <DimensionCard
                key={dimension.key}
                dimension={dimension}
                busy={busy}
                onSourceAction={sourceAction}
              />
            ))}
          </div>
          <div className="growth-task-section">
            <div className="growth-subheading">
              <h3>成长任务</h3>
              <span>四种方式，任选其一</span>
            </div>
            <div className="growth-task-grid">
              {Object.entries(taskLabels).map(([kind, label]) => {
                const task = tasks
                  .filter((item) => item.kind === kind)
                  .sort((left, right) => {
                    const rightTime = new Date(right.updated_at).getTime();
                    const leftTime = new Date(left.updated_at).getTime();
                    return rightTime - leftTime || right.revision - left.revision;
                  })[0];
                return (
                  <article className="growth-task-card" key={kind} data-status={task?.status || "empty"}>
                    <div className="growth-task-title">
                      <h4>{label}</h4>
                      {task && <span className="growth-status" data-status={task.status}>{taskStatusLabels[task.status]}</span>}
                    </div>
                    {kind === "natural_chat" && task?.status === "active" ? (
                      <button
                        type="button"
                        className="button-primary"
                        onClick={() => onStartChat?.(task)}
                        disabled={voiceSessionActive}
                      >
                        {voiceSessionActive ? "当前对话进行中" : "继续聊天"}
                      </button>
                    ) : task ? (
                      <>
                        <p>{task.prompt || "把这次经历写下来，帮助地图变得更准确。"}</p>
                        {task.status === "completed" ? (
                          <>
                            <p className="growth-task-complete"><Check size={16} aria-hidden="true" />已完成</p>
                            <button type="button" className="button-quiet" onClick={() => void startTask(kind)} disabled={Boolean(busy)}>
                              再次开始
                            </button>
                          </>
                        ) : task.status === "cancelled" ? (
                          <button type="button" className="button-quiet" onClick={() => void startTask(kind)} disabled={Boolean(busy)}>
                            再次开始
                          </button>
                        ) : task.status === "draft" || task.status === "paused" ? (
                          <button
                            type="button"
                            className="button-primary"
                            onClick={() => void activateTask(task)}
                            disabled={Boolean(busy) || (
                              kind === "natural_chat" && voiceSessionActive
                            )}
                          >
                            {kind === "natural_chat" && voiceSessionActive
                              ? "请先结束当前对话"
                              : task.status === "paused"
                                ? "继续任务"
                                : "开始任务"}
                          </button>
                        ) : kind === "natural_chat" ? null : (
                          <form onSubmit={(event) => { event.preventDefault(); void submitAnswer(task); }}>
                            <label htmlFor={`growth-answer-${task.task_id}`}>你的回答</label>
                            <textarea
                              id={`growth-answer-${task.task_id}`}
                              value={answers[task.task_id] || ""}
                              onChange={(event) => setAnswers((current) => ({ ...current, [task.task_id]: event.target.value }))}
                              rows={3}
                              disabled={Boolean(busy)}
                            />
                            <button type="submit" className="button-primary" disabled={Boolean(busy)}>
                              {busy === `answer-${task.task_id}` ? "正在保存…" : "保存回答"}
                            </button>
                          </form>
                        )}
                      </>
                    ) : (
                      <>
                        <p>用一次短任务补充这项材料，完成后会显示在对应维度。</p>
                        <button
                          type="button"
                          className="button-quiet"
                          onClick={() => void startTask(kind)}
                          disabled={Boolean(busy) || (
                            kind === "natural_chat" && voiceSessionActive
                          )}
                        >
                          {kind === "natural_chat"
                            ? voiceSessionActive
                              ? "请先结束当前对话"
                              : "开始自然聊天"
                            : "开始任务"}
                        </button>
                      </>
                    )}
                  </article>
                );
              })}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
