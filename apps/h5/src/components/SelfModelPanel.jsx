import { useCallback, useEffect, useRef, useState } from "react";
import {
  Brain,
  CheckCircle,
  Scales,
  UsersThree,
  WarningCircle,
} from "@phosphor-icons/react";

import {
  addSelfModelClaimCounterexample,
  getSelfModel,
  reviewSelfModelClaim,
  reviewSelfModelDecisionCase,
  reviewSelfModelRelationshipProfile,
} from "../api.js";

const highSensitivityClaimTypes = new Set([
  "value",
  "decision_rule",
  "red_line",
  "conflict",
  "support",
]);

const claimTypeLabels = {
  belief: "信念",
  preference: "偏好",
  value: "价值排序",
  decision_rule: "决策规则",
  red_line: "边界与底线",
  uncertainty: "不确定方式",
  conflict: "冲突处理",
  support: "支持方式",
};

const statusLabels = {
  candidate: "待本人审核",
  confirmed: "已确认",
  disputed: "存在争议",
  retracted: "已撤回",
  superseded: "历史版本",
  approved: "已批准",
  revoked: "已撤销",
};

const effectiveReasonLabels = {
  not_approved: "尚未完成本人审核",
  hypothetical_decision: "只是情境推演，不是本人真实经历",
  no_longer_endorsed: "本人已不再认同当时选择",
  unresolved_conflict: "仍有未解决的矛盾",
  negative_evidence: "存在本人否定证据",
  missing_owner_adopted_source: "缺少可采用的本人来源",
  step_up_review_required: "需要密码复核",
  owner_counterexample_required: "需要本人补充例外或反例",
};

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

function eventId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `self-model-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function errorMessage(error) {
  return error instanceof Error && error.message
    ? error.message
    : "认知材料操作没有完成，请稍后重试。";
}

function sourceLabel(source) {
  if (source.negative) return "本人否定";
  return source.relation === "counterexample" ? "例外/反例" : "本人依据";
}

function sourceDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).format(date);
}

function SourceSummary({ item }) {
  const supportCount = item.sources.filter(
    (source) => source.relation === "support" && !source.negative,
  ).length;
  const counterexampleCount = item.sources.filter(
    (source) => source.relation === "counterexample" && !source.negative,
  ).length;
  const negativeCount = item.sources.filter((source) => source.negative).length;
  return (
    <details className="self-model-source-summary">
      <summary>
        本人来源 {supportCount} 条
        <span aria-hidden="true"> · </span>
        例外/反例 {counterexampleCount} 条
        {negativeCount > 0 && (
          <>
            <span aria-hidden="true"> · </span>
            本人否定 {negativeCount} 条
          </>
        )}
      </summary>
      <ul>
        {item.sources.map((source) => (
          <li key={`${source.source_event_id}:${source.relation}:${source.negative}`}>
            <div>
              <strong>{sourceLabel(source)}</strong>
              <small>{sourceDate(source.occurred_at)}</small>
            </div>
            <p>{source.excerpt || "这条本人来源没有可展示的文字片段。"}</p>
          </li>
        ))}
      </ul>
    </details>
  );
}

function EffectiveState({ item }) {
  return (
    <div className="self-model-effective">
      <span className="digital-status" data-status={item.effective ? "active" : item.status}>
        {item.effective ? "当前可采用" : statusLabels[item.status] || item.status}
      </span>
      {!item.effective && item.effective_reasons.length > 0 && (
        <p>
          {item.effective_reasons
            .map((reason) => effectiveReasonLabels[reason] || "等待本人处理")
            .join("；")}
        </p>
      )}
    </div>
  );
}

function EmptyState({ title, body }) {
  return (
    <article className="digital-empty self-model-empty">
      <strong>{title}</strong>
      <p>{body}</p>
    </article>
  );
}

export function SelfModelPanel({ onChanged }) {
  const [model, setModel] = useState({
    claims: [],
    decision_cases: [],
    relationship_profiles: [],
  });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [counterexamples, setCounterexamples] = useState({});
  const [pending, setPending] = useState(null);
  const [password, setPassword] = useState("");
  const actionIds = useRef({});
  const dialogRef = useRef(null);
  const passwordInputRef = useRef(null);
  const triggerRef = useRef(null);
  const restoreFocusRef = useRef(false);

  const reload = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError("");
    try {
      setModel(await getSelfModel());
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (pending) {
      passwordInputRef.current?.focus();
      return;
    }
    if (restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus();
    }
  }, [pending]);

  const idempotencyKey = (key) => {
    if (!actionIds.current[key]) actionIds.current[key] = eventId();
    return actionIds.current[key];
  };

  const run = async (key, action, success) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      await action();
      await reload({ silent: true });
      setNotice(success);
      onChanged?.();
      return true;
    } catch (actionError) {
      await reload({ silent: true });
      setError(errorMessage(actionError));
      return false;
    } finally {
      setBusy("");
    }
  };

  const reviewClaim = (item, status, passwordValue = "") => {
    const key = `claim:${item.claim_id}:${item.version}:${status}`;
    return run(
      key,
      () =>
        reviewSelfModelClaim(
          item.claim_id,
          status,
          item.version,
          idempotencyKey(key),
          passwordValue,
        ),
      status === "confirmed" ? "认知主张已由你确认。" : "认知主张状态已更新。",
    );
  };

  const addCounterexample = async (item) => {
    const text = String(counterexamples[item.claim_id] || "").trim();
    if (!text) return;
    const key = `counterexample:${item.claim_id}:${item.version}`;
    const completed = await run(
      key,
      () =>
        addSelfModelClaimCounterexample(
          item.claim_id,
          text,
          item.version,
          idempotencyKey(key),
        ),
      "例外情境已保存，现在可以继续本人审核。",
    );
    if (completed) {
      setCounterexamples((current) => {
        const next = { ...current };
        delete next[item.claim_id];
        return next;
      });
    }
  };

  const reviewDecision = (item, status) => {
    const key = `decision:${item.case_id}:${item.version}:${status}`;
    return run(
      key,
      () =>
        reviewSelfModelDecisionCase(
          item.case_id,
          status,
          item.version,
          idempotencyKey(key),
        ),
      status === "confirmed" ? "真实决策案例已确认。" : "决策案例状态已更新。",
    );
  };

  const reviewRelationship = (item, status, passwordValue) => {
    const key = `relationship:${item.profile_id}:${item.version_number}:${status}`;
    return run(
      key,
      () =>
        reviewSelfModelRelationshipProfile(
          item.profile_id,
          item.version_number,
          status,
          status === "approved" ? "candidate" : "approved",
          idempotencyKey(key),
          passwordValue,
        ),
      status === "approved" ? "关系画像已批准。" : "关系画像已撤销。",
    );
  };

  const requestPassword = (event, config) => {
    triggerRef.current = event.currentTarget;
    setPassword("");
    setPending(config);
  };

  const closePending = () => {
    restoreFocusRef.current = true;
    setPending(null);
    setPassword("");
  };

  const confirmPending = async () => {
    if (!pending || password.length < 8) return;
    const completed = await pending.action(password);
    if (completed) closePending();
  };

  return (
    <section className="digital-section self-model-panel" aria-labelledby="self-model-title">
      <div className="digital-section-heading">
        <span className="digital-section-icon"><Brain size={22} weight="fill" /></span>
        <div>
          <p>从本人证据到可审核模型</p>
          <h2 id="self-model-title">认知、决策与关系</h2>
        </div>
        <span className="digital-status" data-status="pending">
          {loading ? "同步中" : "本人审核"}
        </span>
      </div>

      <p className="digital-section-copy">
        候选材料不会自动代表你。只有本人来源、必要的反例和明确审核都满足后，
        才能进入下一版数字分身。
      </p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {notice && <p className="inline-success" role="status">{notice}</p>}

      <div className="self-model-group">
        <div className="self-model-group-title">
          <Scales size={20} weight="fill" aria-hidden="true" />
          <h3>认知主张</h3>
        </div>
        {!loading && model.claims.length === 0 && (
          <EmptyState
            title="还没有认知主张"
            body="系统会先保存候选；价值、底线和决策规则还必须由你补充反例并用密码确认。"
          />
        )}
        {model.claims.map((item) => {
          const highSensitivity = highSensitivityClaimTypes.has(item.claim_type);
          const hasCounterexample = item.sources.some(
            (source) =>
              source.relation === "counterexample" && !source.negative,
          );
          return (
            <article className="self-model-card" key={item.claim_id}>
              <div className="self-model-card-heading">
                <div>
                  <small>{claimTypeLabels[item.claim_type] || item.claim_type}</small>
                  <strong>{item.statement}</strong>
                </div>
                <EffectiveState item={item} />
              </div>
              {item.context && <p>{item.context}</p>}
              <SourceSummary item={item} />
              {item.status === "candidate" && highSensitivity && !hasCounterexample && (
                <form
                  className="self-model-counterexample-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void addCounterexample(item);
                  }}
                >
                  <label>
                    <span>补充一个不适用的情境或例外</span>
                    <textarea
                      rows={3}
                      aria-label={`为认知主张补充例外：${item.statement}`}
                      value={counterexamples[item.claim_id] || ""}
                      onChange={(event) =>
                        setCounterexamples((current) => ({
                          ...current,
                          [item.claim_id]: event.target.value,
                        }))
                      }
                      placeholder="例如：在什么情况下你会做出不同选择？"
                    />
                  </label>
                  <button
                    type="submit"
                    className="button-secondary"
                    disabled={
                      Boolean(busy) ||
                      !String(counterexamples[item.claim_id] || "").trim()
                    }
                  >
                    保存例外
                  </button>
                </form>
              )}
              {item.status === "candidate" && (
                <div className="self-model-actions">
                  <button
                    type="button"
                    className="button-secondary"
                    aria-label={`确认认知主张：${item.statement}`}
                    disabled={Boolean(busy) || (highSensitivity && !hasCounterexample)}
                    onClick={(event) => {
                      if (highSensitivity) {
                        requestPassword(event, {
                          title: "确认高敏认知主张",
                          body: "价值、底线和决策规则会影响未来回答。请输入账号密码确认。",
                          danger: false,
                          action: (value) => reviewClaim(item, "confirmed", value),
                        });
                      } else {
                        void reviewClaim(item, "confirmed");
                      }
                    }}
                  >
                    <CheckCircle size={17} weight="bold" aria-hidden="true" />
                    {highSensitivity && !hasCounterexample ? "先补充例外" : "确认"}
                  </button>
                  <button
                    type="button"
                    className="button-quiet"
                    aria-label={`标记认知主张有争议：${item.statement}`}
                    disabled={Boolean(busy)}
                    onClick={() => void reviewClaim(item, "disputed")}
                  >
                    有争议
                  </button>
                </div>
              )}
            </article>
          );
        })}
      </div>

      <div className="self-model-group">
        <div className="self-model-group-title">
          <Scales size={20} weight="fill" aria-hidden="true" />
          <h3>决策案例</h3>
        </div>
        {!loading && model.decision_cases.length === 0 && (
          <EmptyState
            title="还没有决策案例"
            body="完成“情境选择”会形成假设候选；完成“决策复盘”会形成等待审核的真实案例。"
          />
        )}
        {model.decision_cases.map((item) => (
          <article className="self-model-card" key={item.case_id}>
            <div className="self-model-card-heading">
              <div>
                <small>{item.decision_kind === "real" ? "真实经历" : "情境推演"}</small>
                <strong>{item.context}</strong>
              </div>
              <EffectiveState item={item} />
            </div>
            <p>我的选择：{item.chosen_option}</p>
            <SourceSummary item={item} />
            {item.status === "candidate" && (
              <div className="self-model-actions">
                {item.decision_kind === "real" ? (
                  <button
                    type="button"
                    className="button-secondary"
                    aria-label={`确认真实决策案例：${item.context}`}
                    disabled={Boolean(busy)}
                    onClick={() => void reviewDecision(item, "confirmed")}
                  >
                    <CheckCircle size={17} weight="bold" aria-hidden="true" />
                    确认真实案例
                  </button>
                ) : (
                  <span className="self-model-hypothetical">
                    情境回答不会自动变成真实决策
                  </span>
                )}
                <button
                  type="button"
                  className="button-quiet"
                  aria-label={`撤回决策候选：${item.context}`}
                  disabled={Boolean(busy)}
                  onClick={() => void reviewDecision(item, "retracted")}
                >
                  撤回
                </button>
              </div>
            )}
          </article>
        ))}
      </div>

      <div className="self-model-group">
        <div className="self-model-group-title">
          <UsersThree size={20} weight="fill" aria-hidden="true" />
          <h3>关系画像</h3>
        </div>
        <p className="self-model-disclaimer">
          关系画像只描述你面对某个人时的称呼和沟通方式，不代表对方已经获得任何记忆访问权。
        </p>
        {!loading && model.relationship_profiles.length === 0 && (
          <EmptyState
            title="还没有关系画像"
            body="确认人物关系后，你可以单独审核称呼、语气、建议方式和禁区。"
          />
        )}
        {model.relationship_profiles.map((item) => (
          <article
            className="self-model-card"
            key={`${item.profile_id}:${item.version_number}`}
          >
            <div className="self-model-card-heading">
              <div>
                <small>版本 {item.version_number}</small>
                <strong>{item.salutation || "未设置称呼"}</strong>
              </div>
              <EffectiveState item={item} />
            </div>
            <p>语气：{item.tone || "等待本人设置"}</p>
            <p>建议方式：{item.advice_style || "等待本人设置"}</p>
            {item.boundaries.length > 0 && (
              <p>边界：{item.boundaries.join("；")}</p>
            )}
            <SourceSummary item={item} />
            {item.status === "candidate" && (
              <button
                type="button"
                className="button-secondary"
                aria-label={`批准关系画像：${item.salutation || item.profile_id}`}
                disabled={Boolean(busy)}
                onClick={(event) =>
                  requestPassword(event, {
                    title: "批准关系画像",
                    body: "批准后它可以进入下一版数字分身，但仍不会授予访问权限。",
                    danger: false,
                    action: (value) => reviewRelationship(item, "approved", value),
                  })
                }
              >
                批准关系画像
              </button>
            )}
            {item.status === "approved" && (
              <button
                type="button"
                className="button-danger"
                aria-label={`撤销关系画像：${item.salutation || item.profile_id}`}
                disabled={Boolean(busy)}
                onClick={(event) =>
                  requestPassword(event, {
                    title: "撤销关系画像",
                    body: "撤销后它不会进入后续数字分身版本，历史 manifest 仍保持不可变。",
                    danger: true,
                    action: (value) => reviewRelationship(item, "revoked", value),
                  })
                }
              >
                撤销关系画像
              </button>
            )}
          </article>
        ))}
      </div>

      {pending && (
        <form
          ref={dialogRef}
          className="digital-confirm"
          role="alertdialog"
          aria-label={pending.title}
          aria-modal="true"
          onKeyDown={(event) => {
            if (event.key === "Tab") {
              const focusable = Array.from(
                dialogRef.current?.querySelectorAll(FOCUSABLE_SELECTOR) || [],
              );
              if (focusable.length) {
                const currentIndex = focusable.indexOf(document.activeElement);
                const nextIndex = event.shiftKey
                  ? currentIndex <= 0
                    ? focusable.length - 1
                    : currentIndex - 1
                  : currentIndex === focusable.length - 1
                    ? 0
                    : currentIndex + 1;
                event.preventDefault();
                focusable[nextIndex]?.focus();
              }
              return;
            }
            if (event.key === "Escape" && !busy) closePending();
          }}
          onSubmit={(event) => {
            event.preventDefault();
            void confirmPending();
          }}
        >
          <WarningCircle size={22} weight="fill" aria-hidden="true" />
          <div>
            <strong>{pending.title}</strong>
            <p>{pending.body}</p>
            <label className="digital-field">
              <span>账号密码</span>
              <input
                ref={passwordInputRef}
                type="password"
                autoComplete="current-password"
                minLength={8}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <div className="digital-confirm-actions">
              <button
                type="button"
                className="button-quiet"
                onClick={closePending}
                disabled={Boolean(busy)}
              >
                取消
              </button>
              <button
                type="submit"
                className={pending.danger ? "button-danger" : "button-primary"}
                disabled={Boolean(busy) || password.length < 8}
              >
                {busy ? "正在处理…" : "确认"}
              </button>
            </div>
          </div>
        </form>
      )}
    </section>
  );
}
