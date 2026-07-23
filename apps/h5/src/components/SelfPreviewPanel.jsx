import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  Brain,
  CheckCircle,
  FileText,
  LockKey,
  ShieldCheck,
  StopCircle,
  WarningCircle,
} from "@phosphor-icons/react";

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const perspectiveCopy = {
  owner: {
    label: "我的视角",
    description: "按账户主人自己的提问方式模拟。",
  },
  child: {
    label: "孩子视角",
    description: "只模拟孩子会怎样提问，不改变资料范围。",
  },
  friend: {
    label: "朋友视角",
    description: "只模拟朋友会怎样提问，不改变资料范围。",
  },
};

const categoryLabels = {
  fact: "事实",
  decision: "决策",
  relationship: "关系",
  humor: "幽默",
  emotion: "情绪回应",
  unknown: "未知",
  privacy: "隐私",
};

function availableVersions(versions) {
  return (Array.isArray(versions) ? versions : []).filter((version) =>
    ["approved", "frozen"].includes(version?.status),
  );
}

function versionLabel(version) {
  const labels = {
    draft: "草稿",
    testing: "测试中",
    approved: "已批准",
    frozen: "已冻结",
    revoked: "已撤销",
  };
  return version
    ? `v${version.version_number} · ${labels[version.status] || version.status}`
    : "尚未选择";
}

function sourceKey(answer) {
  return answer?.answer_id || `${answer?.turn_id || "answer"}:${answer?.generation_id || "generation"}`;
}

function entryKey(entry) {
  if (!entry || typeof entry !== "object") return null;
  const id =
    entry.claim_id ||
    entry.trait_id ||
    entry.case_id ||
    entry.profile_id ||
    entry.item_id;
  return id ? `${entry.entry_type || entry.type || "entry"}:${id}` : null;
}

function manifestEntries(version) {
  return Array.isArray(version?.manifest?.entries) ? version.manifest.entries : [];
}

function compareVersionEntries(left, right) {
  const entriesFor = (version) =>
    new Map(
      manifestEntries(version)
        .map((entry) => [entryKey(entry), entry])
        .filter(([key]) => key),
    );
  const leftEntries = entriesFor(left);
  const rightEntries = entriesFor(right);
  const added = [...rightEntries.keys()].filter((key) => !leftEntries.has(key)).sort();
  const removed = [...leftEntries.keys()].filter((key) => !rightEntries.has(key)).sort();
  const changed = [...rightEntries.keys()]
    .filter((key) => leftEntries.has(key))
    .filter(
      (key) =>
        JSON.stringify(leftEntries.get(key)) !==
        JSON.stringify(rightEntries.get(key)),
    )
    .sort();
  return { added, removed, changed };
}

function errorReason(capability) {
  const missing = capability?.missing;
  if (Array.isArray(missing) && missing.length) {
    return missing
      .map((item) => ({
        self_preview_runtime: "数字自我预览运行时将在后续阶段开放",
        registered_owner: "仅注册账户主人可以进入预览",
        verified_owner_voice: "需要先完成正式主人声纹登记并保持启用",
        approved_digital_self_version: "需要先建立并批准数字分身版本",
        preview_version_stale_or_fidelity_unready:
          "已批准版本需要完成忠实度批准，且不能有待处理纠正",
        preview_state_unavailable: "暂时无法核验预览安全条件，请稍后重试",
      }[item] || item))
      .join("；");
  }
  if (capability?.registered_owner === false) return "仅注册账户主人可以进入预览。";
  return "服务端尚未确认可安全进入预览。";
}

function statusOf(activePreview) {
  return activePreview?.status || "idle";
}

export function SelfPreviewPanel({
  versions,
  previewVersions,
  evaluationVersions,
  capability,
  busy,
  activePreview,
  answers = [],
  fidelitySummary,
  onStart,
  onStop,
  onExpandSources,
  onSubmitFeedback,
  onStartFidelity,
  onSubmitFidelity,
  onCompleteFidelity,
  onSelectFidelityVersion,
  onClose,
}) {
  const dialogRef = useRef(null);
  const passwordRef = useRef(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [evaluationVersionId, setEvaluationVersionId] = useState("");
  const [password, setPassword] = useState("");
  const [perspective, setPerspective] = useState("owner");
  const [compareVersionId, setCompareVersionId] = useState("");
  const [expandedAnswers, setExpandedAnswers] = useState(() => new Set());
  const [correctionDrafts, setCorrectionDrafts] = useState({});
  const [feedbackBusy, setFeedbackBusy] = useState("");
  const [fidelitySelection, setFidelitySelection] = useState("");
  const [fidelityPassword, setFidelityPassword] = useState("");
  const [fidelityRationale, setFidelityRationale] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");

  const previewableVersions = useMemo(
    () => {
      const candidates = availableVersions(previewVersions ?? versions);
      if (!Array.isArray(capability?.versions)) return candidates;
      const eligibleIds = new Set(
        capability.versions
          .filter(
            (version) =>
              version.preview_eligible === true &&
              version.version_stale === false &&
              version.fidelity_verdict === "approve",
          )
          .map((version) => version.version_id),
      );
      return candidates.filter((version) => eligibleIds.has(version.version_id));
    },
    [capability?.versions, previewVersions, versions],
  );
  const evaluableVersions = useMemo(
    () =>
      (evaluationVersions ?? versions ?? []).filter((version) =>
        ["testing", "approved", "frozen"].includes(version?.status),
      ),
    [evaluationVersions, versions],
  );
  const currentVersion = previewableVersions.find(
    (version) =>
      version.version_id === (activePreview?.version_id || selectedVersionId),
  ) || previewableVersions[0] || null;
  const currentEvaluationVersion =
    evaluableVersions.find(
      (version) =>
        version.version_id ===
        (evaluationVersionId ||
          fidelitySummary?.version_id ||
          fidelitySummary?.versionId),
    ) ||
    evaluableVersions[0] ||
    null;
  const active = ["running", "active"].includes(statusOf(activePreview));
  const capabilityAvailable =
    capability?.status === "available" &&
    capability?.registered_owner === true &&
    capability?.active_owner_voice === true &&
    !capability?.missing?.length;
  const previewBusy =
    typeof busy === "string" &&
    /^(self-preview|preview|fidelity)/.test(busy);
  const canStart = Boolean(
    onStart &&
      capabilityAvailable &&
      currentVersion &&
      password.trim().length >= 8 &&
      !previewBusy &&
      !active,
  );
  const fidelitySummaryMatches =
    !fidelitySummary?.version_id ||
    fidelitySummary.version_id === currentEvaluationVersion?.version_id;
  const currentItem = fidelitySummaryMatches
    ? fidelitySummary?.current_item || fidelitySummary?.currentItem || null
    : null;
  const fidelityStatus = fidelitySummaryMatches
    ? fidelitySummary?.status || "idle"
    : "idle";
  const fidelityUnavailable = ["unavailable", "blocked", "coverage_gap"].includes(
    fidelityStatus,
  );
  const compareVersion =
    previewableVersions.find((version) => version.version_id === compareVersionId) ||
    previewableVersions.find((version) => version.version_id !== currentVersion?.version_id) ||
    null;
  const compareDiff =
    currentVersion && compareVersion
      ? compareVersionEntries(currentVersion, compareVersion)
      : null;
  const fidelityCategories = Array.isArray(fidelitySummary?.categories)
    ? fidelitySummary.categories
    : Object.keys(categoryLabels);
  const fidelityCompleted = fidelityStatus === "completed";
  const canCompleteFidelity =
    fidelityStatus === "active" &&
    fidelitySummary?.all_answered === true &&
    Boolean(fidelitySummary?.evaluation_id) &&
    Boolean(onCompleteFidelity) &&
    !previewBusy;
  const canApproveFidelity =
    canCompleteFidelity &&
    (fidelitySummary?.coverage_gaps?.length || 0) === 0 &&
    fidelitySummary?.gates?.coverage_complete !== false &&
    fidelitySummary?.gates?.unsupported_fact !== false &&
    fidelitySummary?.gates?.unknown !== false &&
    fidelitySummary?.gates?.privacy !== false &&
    fidelitySummary?.gates?.identity_disclosure !== false &&
    fidelitySummary?.gates?.decision_inference_disclosure !== false &&
    fidelitySummary?.gates?.owner_blind_preference !== false;

  useEffect(() => {
    if (!selectedVersionId && previewableVersions[0]?.version_id) {
      setSelectedVersionId(previewableVersions[0].version_id);
    }
  }, [previewableVersions, selectedVersionId]);

  useEffect(() => {
    if (!evaluationVersionId) {
      const preferred = evaluableVersions.find(
        (version) =>
          version.version_id ===
          (fidelitySummary?.version_id || fidelitySummary?.versionId),
      );
      if (preferred?.version_id || evaluableVersions[0]?.version_id) {
        setEvaluationVersionId(
          preferred?.version_id || evaluableVersions[0].version_id,
        );
      }
    }
  }, [
    evaluationVersionId,
    evaluableVersions,
    fidelitySummary?.versionId,
    fidelitySummary?.version_id,
  ]);

  useEffect(() => {
    const focusVersionId =
      fidelitySummary?.version_id || fidelitySummary?.versionId;
    if (
      focusVersionId &&
      focusVersionId !== evaluationVersionId &&
      evaluableVersions.some((version) => version.version_id === focusVersionId)
    ) {
      setEvaluationVersionId(focusVersionId);
    }
  }, [
    evaluationVersionId,
    evaluableVersions,
    fidelitySummary?.versionId,
    fidelitySummary?.version_id,
  ]);

  useEffect(() => {
    if (
      compareVersionId &&
      !previewableVersions.some((version) => version.version_id === compareVersionId)
    ) {
      setCompareVersionId("");
    }
  }, [compareVersionId, previewableVersions]);

  useEffect(() => {
    if (activePreview?.version_id) setSelectedVersionId(activePreview.version_id);
    if (activePreview?.perspective && perspectiveCopy[activePreview.perspective]) {
      setPerspective(activePreview.perspective);
    }
  }, [activePreview?.perspective, activePreview?.version_id]);

  useEffect(() => {
    setFidelitySelection("");
  }, [currentItem?.item_id, currentItem?.trial_id]);

  useEffect(() => {
    if (!active) passwordRef.current?.focus();
    else dialogRef.current?.querySelector("button:not([disabled])")?.focus();
  }, [active]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose?.();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll(FOCUSABLE_SELECTOR)];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", onKeyDown);
    return () => dialog.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const start = async (event) => {
    event.preventDefault();
    if (!canStart) return;
    setActionError("");
    setActionNotice("");
    try {
      await onStart({
        versionId: currentVersion.version_id,
        manifestSha256: currentVersion.manifest_sha256,
        password: password.trim(),
        perspective,
      });
      setPassword("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "数字分身预览没有启动。");
    }
  };

  const expandSources = async (answer) => {
    const key = sourceKey(answer);
    if (!onExpandSources) return;
    setActionError("");
    try {
      await onExpandSources(answer);
      setExpandedAnswers((current) => {
        const next = new Set(current);
        next.add(key);
        return next;
      });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "回答来源暂时无法展开。");
    }
  };

  const submitFeedback = async (answer, action) => {
    if (!onSubmitFeedback) return;
    const key = sourceKey(answer);
    const correction = correctionDrafts[key] || "";
    if (action === "correction" && !correction.trim()) return;
    setFeedbackBusy(`${key}:${action}`);
    setActionError("");
    setActionNotice("");
    try {
      await onSubmitFeedback(answer, { action, correction: correction.trim() });
      setCorrectionDrafts((current) => ({ ...current, [key]: "" }));
      setActionNotice("反馈已记录；该版本已标记为需要重新构建和审核。");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "反馈没有记录成功。");
    } finally {
      setFeedbackBusy("");
    }
  };

  const startFidelity = async () => {
    if (
      !onStartFidelity ||
      !currentEvaluationVersion ||
      fidelityPassword.trim().length < 8
    ) {
      return;
    }
    setActionError("");
    setActionNotice("");
    try {
      await onStartFidelity(
        currentEvaluationVersion,
        fidelityPassword.trim(),
      );
      setFidelityPassword("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "忠实度评测没有启动。");
    }
  };

  const submitFidelity = async (preferredSlot) => {
    if (!onSubmitFidelity || !currentItem || !preferredSlot) return;
    setFidelitySelection(preferredSlot);
    setActionError("");
    try {
      await onSubmitFidelity({
        itemId:
          currentItem.trial_id || currentItem.item_id || currentItem.id,
        preferredSlot,
        versionId: currentEvaluationVersion?.version_id || null,
      });
    } catch (error) {
      setFidelitySelection("");
      setActionError(error instanceof Error ? error.message : "盲选结果没有保存。");
    }
  };

  const completeFidelity = async (verdict) => {
    if (!canCompleteFidelity) return;
    setActionError("");
    setActionNotice("");
    try {
      await onCompleteFidelity({
        evaluationId: fidelitySummary.evaluation_id,
        verdict,
        rationale: fidelityRationale.trim(),
      });
      setFidelityRationale("");
      setActionNotice(
        verdict === "approve"
          ? "忠实度评测已批准；现在可以继续批准该测试版本。"
          : "忠实度评测已否决；该版本不会进入正式预览。",
      );
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "评测结论没有保存。");
    }
  };

  return (
    <section
      ref={dialogRef}
      className="self-preview-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="self-preview-title"
    >
      <header className="self-preview-header">
        <button
          type="button"
          className="icon-button"
          aria-label="关闭数字分身预览"
          onClick={onClose}
        >
          <ArrowLeft size={21} weight="bold" />
        </button>
        <div>
          <p className="eyebrow">受控模拟 · 仅账户主人</p>
          <h2 id="self-preview-title">数字分身预览</h2>
        </div>
      </header>

      <div className="self-preview-scroll">
        <article className="self-preview-disclosure" role="note">
          <ShieldCheck size={22} weight="fill" aria-hidden="true" />
          <div>
            <strong>数字分身预览，不代表本人</strong>
            <p>这是对已批准版本的受控模拟，回答不会写入主人档案，也不会代替你执行决定。</p>
          </div>
        </article>

        <div className="self-preview-boundaries">
          <span><LockKey size={16} weight="fill" /> 孩子/朋友视角不会授予访问权</span>
          <span><Brain size={16} weight="fill" /> 伙伴风格不进入数字分身</span>
        </div>

        {actionError && (
          <p className="digital-error self-preview-action-message" role="alert">
            <WarningCircle size={17} weight="fill" />
            {actionError}
          </p>
        )}
        {actionNotice && (
          <p className="digital-notice self-preview-action-message" role="status">
            {actionNotice}
          </p>
        )}

        {!capabilityAvailable && (
          <article className="self-preview-blocked" role="alert">
            <WarningCircle size={20} weight="fill" aria-hidden="true" />
            <div>
              <strong>{capability?.registered_owner === false ? "仅注册账户主人可预览" : "数字分身预览当前不可用"}</strong>
              <p>{errorReason(capability)}</p>
            </div>
          </article>
        )}

        {capabilityAvailable && !active && (
          <form className="self-preview-start-card" onSubmit={start}>
            <div className="self-preview-card-title">
              <div>
                <span>先固定一个版本，再开始预览</span>
                <h3>选择已批准版本</h3>
              </div>
              <span className="digital-status" data-status={currentVersion?.status || "pending"}>
                {versionLabel(currentVersion)}
              </span>
            </div>
            <label className="self-preview-field" htmlFor="self-preview-version">
              预览版本
              <select
                id="self-preview-version"
                value={currentVersion?.version_id || ""}
                onChange={(event) => setSelectedVersionId(event.target.value)}
                disabled={!previewableVersions.length || previewBusy}
              >
                {!previewableVersions.length && <option value="">没有可用版本</option>}
                {previewableVersions.map((version) => (
                  <option key={version.version_id} value={version.version_id}>
                    {versionLabel(version)} · {version.manifest_sha256?.slice(0, 8)}
                  </option>
                ))}
              </select>
            </label>
            <div className="self-preview-perspective" role="group" aria-label="模拟视角">
              <span>模拟视角</span>
              <div>
                {Object.entries(perspectiveCopy).map(([key, copy]) => (
                  <button
                    type="button"
                    key={key}
                    className={perspective === key ? "active" : ""}
                    aria-pressed={perspective === key}
                    onClick={() => setPerspective(key)}
                    disabled={previewBusy}
                  >
                    {copy.label}
                  </button>
                ))}
              </div>
              <small>{perspectiveCopy[perspective].description}</small>
            </div>
            <label className="self-preview-field" htmlFor="self-preview-password">
              当前账号密码
              <input
                ref={passwordRef}
                id="self-preview-password"
                type="password"
                autoComplete="current-password"
                minLength={8}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <p className="self-preview-step-up">
              <LockKey size={15} weight="fill" /> 进入预览前需要当前密码确认；不会保存密码。
            </p>
            <button
              type="submit"
              className="button-primary full-width"
              disabled={!canStart}
            >
              {previewBusy ? "正在准备预览…" : "开始数字分身预览"}
            </button>
          </form>
        )}

        {active && (
          <article className="self-preview-running">
            <div>
              <span>当前固定版本</span>
              <strong>{versionLabel(currentVersion)} · {activePreview.manifest_sha256?.slice(0, 8)}</strong>
            </div>
            <div>
              <span>模拟视角</span>
              <strong>{perspectiveCopy[activePreview.perspective || perspective]?.label || perspectiveCopy.owner.label}</strong>
            </div>
            <button
              type="button"
              className="button-danger-outline full-width"
              onClick={onStop}
              disabled={!onStop || previewBusy}
            >
              <StopCircle size={18} weight="fill" /> {previewBusy ? "正在停止…" : "停止预览"}
            </button>
          </article>
        )}

        {active && (
          <section className="self-preview-answers" aria-labelledby="self-preview-answers-title">
            <div className="self-preview-card-title">
              <div>
                <span>每一轮都可回看</span>
                <h3 id="self-preview-answers-title">回答与来源</h3>
              </div>
              <span className="digital-status" data-status="active">模拟中</span>
            </div>
            {!answers.length && (
              <p className="digital-empty">还没有回答。开始说话后，这里会显示数字分身的回答。</p>
            )}
            {answers.map((answer) => {
              const key = sourceKey(answer);
              const expanded = expandedAnswers.has(key);
              const hasSources = Array.isArray(answer.source_refs) && answer.source_refs.length > 0;
              const sourceBoundFeedback =
                hasSources &&
                answer.epistemic_status !== "unknown" &&
                !answer.disclosures?.includes("privacy_refusal");
              return (
                <article className="self-preview-answer-card" key={key}>
                  <div className="self-preview-answer-meta">
                    <span>{answer.epistemic_status === "fact" ? "事实" : answer.epistemic_status === "inference" ? "推断" : "未知"}</span>
                    {answer.disclosures?.includes("digital_identity") && <small>数字身份已披露</small>}
                  </div>
                  <p>{answer.text || "（没有可展示的回答）"}</p>
                  <div className="self-preview-answer-actions">
                    <button
                      type="button"
                      className="button-quiet"
                      onClick={() => void expandSources(answer)}
                      disabled={!onExpandSources || !hasSources || expanded}
                    >
                      <FileText size={16} /> {expanded ? "来源已展开" : "展开回答来源"}
                    </button>
                    <button
                      type="button"
                      className="button-danger-outline"
                      onClick={() => void submitFeedback(answer, "not_like_me")}
                      disabled={
                        !onSubmitFeedback ||
                        !sourceBoundFeedback ||
                        Boolean(feedbackBusy)
                      }
                    >
                      不像我
                    </button>
                  </div>
                  {expanded && (
                    <div className="self-preview-source-list">
                      {(answer.sources_expanded || answer.source_details || answer.source_refs).map((source, index) => (
                        <div key={`${source.item_id || source.source_event_id || "source"}:${index}`}>
                          <strong>{source.kind || source.relation || "来源"}</strong>
                          <span>{source.excerpt || source.item_id || source.source_event_id || "已核验来源"}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  <label className="self-preview-correction">
                    <span>纠正这句话（可选）</span>
                    <textarea
                      rows="2"
                      value={correctionDrafts[key] || ""}
                      disabled={!sourceBoundFeedback}
                      onChange={(event) => setCorrectionDrafts((current) => ({
                        ...current,
                        [key]: event.target.value,
                      }))}
                      placeholder="例如：我不会这样表达……"
                    />
                    <button
                      type="button"
                      className="button-secondary"
                      onClick={() => void submitFeedback(answer, "correction")}
                      disabled={
                        !onSubmitFeedback ||
                        !sourceBoundFeedback ||
                        !correctionDrafts[key]?.trim() ||
                        Boolean(feedbackBusy)
                      }
                    >
                      {feedbackBusy === `${key}:correction` ? "正在记录…" : "提交纠正"}
                    </button>
                  </label>
                  <small className="self-preview-negative-note">
                    {sourceBoundFeedback
                      ? "“不像我”和纠正会作为高权重负面证据记录，不会直接改写已批准版本。"
                      : "未知或隐私回答没有可纠正来源，反馈入口保持关闭。"}
                  </small>
                </article>
              );
            })}
          </section>
        )}

        <section className="self-preview-fidelity" aria-labelledby="self-preview-fidelity-title">
          <div className="self-preview-card-title">
            <div>
              <span>保留问题 · 盲选回答</span>
              <h3 id="self-preview-fidelity-title">忠实度评测</h3>
            </div>
            <span className="digital-status" data-status={fidelityStatus || "pending"}>
              {fidelityStatus === "completed" ? "已完成" : "事实 · 决策 · 关系 · 幽默 · 情绪 · 未知 · 隐私"}
            </span>
          </div>
          <p className="digital-explainer">
            评测只比较“通用助手”和“数字分身”的回答，不显示槽位映射；结果用于批准或否决版本，不是声音盲测。
          </p>
          <div className="self-preview-category-list" aria-label="评测覆盖范围">
            {fidelityCategories.map((category) => (
              <span key={category}>{categoryLabels[category] || category}</span>
            ))}
          </div>
          {evaluableVersions.length > 0 && (
            <label className="self-preview-evaluation-version">
              评测版本
              <select
                aria-label="评测版本"
                value={currentEvaluationVersion?.version_id || ""}
                onChange={(event) => {
                  const versionId = event.target.value;
                  setEvaluationVersionId(versionId);
                  const selected = evaluableVersions.find(
                    (version) => version.version_id === versionId,
                  );
                  if (selected) onSelectFidelityVersion?.(selected);
                }}
                disabled={previewBusy}
              >
                {evaluableVersions.map((version) => (
                  <option key={version.version_id} value={version.version_id}>
                    {versionLabel(version)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {fidelityUnavailable && (
            <p className="self-preview-fidelity-unavailable" role="status">
              {fidelityStatus === "coverage_gap"
                ? "当前版本覆盖不足，补齐保留问题后才能评测。"
                : "忠实度评测当前不可用，服务端未确认可安全开始。"}
            </p>
          )}
          {["approve", "approved", "passed"].includes(
            (fidelitySummaryMatches ? fidelitySummary?.verdict : null) ||
              fidelityStatus,
          ) && (
            <p className="self-preview-fidelity-approved" role="status">
              <CheckCircle size={16} weight="fill" /> 主人已批准这轮评测，可以批准该测试版本。
            </p>
          )}
          {!currentItem && !fidelityCompleted && fidelityStatus !== "active" ? (
            <div className="self-preview-fidelity-start">
              <label className="self-preview-field" htmlFor="fidelity-password">
                当前账号密码
                <input
                  id="fidelity-password"
                  type="password"
                  autoComplete="current-password"
                  minLength={8}
                  value={fidelityPassword}
                  onChange={(event) => setFidelityPassword(event.target.value)}
                  disabled={previewBusy}
                />
              </label>
              <button
                type="button"
                className="button-secondary full-width"
                onClick={() => void startFidelity()}
                disabled={
                  !onStartFidelity ||
                  !currentEvaluationVersion ||
                  fidelityPassword.trim().length < 8 ||
                  fidelityUnavailable ||
                  previewBusy
                }
              >
                {previewBusy ? "正在准备评测…" : "开始忠实度评测"}
              </button>
            </div>
          ) : currentItem ? (
            <div className="self-preview-fidelity-item">
              <strong>{categoryLabels[currentItem.category] || currentItem.category || "保留问题"}</strong>
              <p>{currentItem.prompt}</p>
              <div className="self-preview-fidelity-answer-list">
                <article>
                  <span>回答 A</span>
                  <p>
                    {currentItem.slot_a ||
                      currentItem.answers?.a ||
                      currentItem.slots?.a ||
                      currentItem.slots?.A ||
                      "暂无回答 A"}
                  </p>
                </article>
                <article>
                  <span>回答 B</span>
                  <p>
                    {currentItem.slot_b ||
                      currentItem.answers?.b ||
                      currentItem.slots?.b ||
                      currentItem.slots?.B ||
                      "暂无回答 B"}
                  </p>
                </article>
              </div>
              <div className="self-preview-fidelity-slots">
                {["a", "b"].map((slot) => (
                  <button
                    type="button"
                    key={slot}
                    className={fidelitySelection === slot ? "selected" : ""}
                    onClick={() => void submitFidelity(slot)}
                    disabled={!onSubmitFidelity || fidelityUnavailable || previewBusy}
                  >
                    选择回答 {slot.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="self-preview-fidelity-conclusion">
              {fidelityCompleted ? (
                <p role="status">这轮评测已完成，结论不可在客户端改写。</p>
              ) : (
                <>
                  <p>所有可回答的保留题已完成。请明确批准或否决这轮评测。</p>
                  <label className="self-preview-correction">
                    <span>结论说明（可选）</span>
                    <textarea
                      rows="2"
                      value={fidelityRationale}
                      onChange={(event) =>
                        setFidelityRationale(event.target.value)
                      }
                      disabled={previewBusy}
                    />
                  </label>
                  <div className="self-preview-fidelity-verdict-actions">
                    <button
                      type="button"
                      className="button-danger-outline"
                      onClick={() => void completeFidelity("reject")}
                      disabled={!canCompleteFidelity}
                    >
                      否决此轮评测
                    </button>
                    <button
                      type="button"
                      className="button-primary"
                      onClick={() => void completeFidelity("approve")}
                      disabled={!canApproveFidelity}
                    >
                      批准此轮评测
                    </button>
                  </div>
                </>
              )}
              {Array.isArray(fidelitySummary?.coverage_gaps) &&
                fidelitySummary.coverage_gaps.length > 0 && (
                  <p className="self-preview-fidelity-unavailable">
                    {fidelitySummary.coverage_gaps.length} 个类别缺少已确认材料；
                    仍可否决，但不能通过安全批准门槛。
                  </p>
                )}
              </div>
          )}
          {fidelitySummaryMatches && fidelitySummary?.metrics && (
            <div className="self-preview-fidelity-metrics">
              {Object.entries(fidelitySummary.metrics).map(([key, value]) => (
                <span key={key}><strong>{value}</strong>{key}</span>
              ))}
            </div>
          )}
          {fidelitySummaryMatches && fidelitySummary?.verdict && (
            <p className="self-preview-fidelity-verdict" role="status">
              <CheckCircle size={17} weight="fill" /> 主人 verdict：{fidelitySummary.verdict === "approve" ? "批准" : "否决"}
            </p>
          )}
        </section>

        {previewableVersions.length > 1 && (
          <section className="self-preview-compare" aria-labelledby="self-preview-compare-title">
            <div className="self-preview-card-title">
              <div>
                <span>只比较已批准或已冻结 manifest</span>
                <h3 id="self-preview-compare-title">版本比较</h3>
              </div>
              <span className="digital-status" data-status="active">可追溯</span>
            </div>
            <div className="self-preview-compare-fields">
              <label>
                当前版本
                <select value={currentVersion?.version_id || ""} disabled>
                  <option value={currentVersion?.version_id || ""}>{versionLabel(currentVersion)}</option>
                </select>
              </label>
              <label>
                比较版本
                <select
                  aria-label="比较版本"
                  value={compareVersion?.version_id || ""}
                  onChange={(event) => setCompareVersionId(event.target.value)}
                >
                  <option value="">选择版本</option>
                  {previewableVersions
                    .filter((version) => version.version_id !== currentVersion?.version_id)
                    .map((version) => (
                      <option key={version.version_id} value={version.version_id}>
                        {versionLabel(version)}
                      </option>
                    ))}
                </select>
              </label>
            </div>
            {compareDiff && (
              <div className="self-preview-compare-summary">
                <span><strong>{compareDiff.added.length}</strong> 新增</span>
                <span><strong>{compareDiff.removed.length}</strong> 移除</span>
                <span><strong>{compareDiff.changed.length}</strong> 内容变化</span>
              </div>
            )}
          </section>
        )}
      </div>
    </section>
  );
}
