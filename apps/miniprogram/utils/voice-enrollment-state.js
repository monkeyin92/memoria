/**
 * One enrollment state table for the profile page and the privacy revoke path.
 * Pages look up this table. They do not compare over_budget or
 * reconciliation_required on their own.
 *
 * reconciliation_required means a person has to reconcile the provider result.
 * over_budget means the clone is still running, past the promised minute.
 * The first wins when both are true. Neither forges a ready state or a percentage.
 */

const VOICE_CLONE_STATUS_RANK = Object.freeze({
  enrolling: 0,
  candidate: 1,
  active: 2,
  failed: 3,
  revoked: 4,
});

const OVER_BUDGET_MESSAGE = "已经比预计久了一点。可以先去忙别的，回来这一页再看就行。";
const RECONCILIATION_TITLE = "需要人工核对这次声音";
const RECONCILIATION_MESSAGE =
  "这次声音需要人工核对，还不能当成已经完成。请稍后再看，或到隐私页撤回并删除样本。";

function budgetHint(enrollment) {
  const elapsedSeconds = Math.max(0, Math.round((Number(enrollment?.elapsed_ms) || 0) / 1000));
  if (enrollment?.expected_total_ms) {
    return `预计 1 分钟左右，已等待 ${elapsedSeconds} 秒。`;
  }
  return "正在处理，可以留在这页看进度。";
}

function idleMessage(payload) {
  if (payload?.consent && !payload.consent.revoked_at) {
    return "还没有声音样本。录一段或上传音频后，大约一分钟就能用。";
  }
  return "还没有自定义声音样本。";
}

const VOICE_ENROLLMENT_STATES = Object.freeze({
  idle: Object.freeze({
    title: "还没有自定义声音样本。",
    message: idleMessage,
    hint: () => "",
    actions: Object.freeze(["record", "upload"]),
    showProgress: false,
    forget: false,
  }),
  queued: Object.freeze({
    title: "正在准备你的声音",
    message: () => "正在准备你的声音",
    hint: budgetHint,
    actions: Object.freeze(["wait"]),
    showProgress: true,
    forget: true,
  }),
  cloning: Object.freeze({
    title: "正在生成你的声音",
    message: () => "正在生成你的声音",
    hint: budgetHint,
    actions: Object.freeze(["wait"]),
    showProgress: true,
    forget: true,
  }),
  activating: Object.freeze({
    title: "正在让机器人用上它",
    message: () => "声音已经生成，正在接到设备上。",
    hint: budgetHint,
    actions: Object.freeze(["wait"]),
    showProgress: true,
    forget: true,
  }),
  ready: Object.freeze({
    title: "自定义声音已就绪",
    message: () => "自定义声音已就绪。下次在设备上说话就会用；听着不像，再录一段即可。",
    hint: () => "",
    actions: Object.freeze(["record", "upload"]),
    showProgress: false,
    forget: true,
  }),
  failed: Object.freeze({
    title: "这次没生成成功",
    message: () => "这次没生成成功。请换一段 10–60 秒、更清楚的人声再试。",
    hint: () => "",
    actions: Object.freeze(["record", "upload"]),
    showProgress: false,
    forget: true,
  }),
  revoked: Object.freeze({
    title: "声音样本已撤回",
    message: () => "声音样本已撤回。删除未完成时可以再试一次撤回。",
    hint: () => "",
    actions: Object.freeze(["record", "upload"]),
    showProgress: false,
    forget: true,
  }),
  forgotten: Object.freeze({
    title: "声音样本已删除",
    message: () => "声音样本已删除。",
    hint: () => "",
    actions: Object.freeze([]),
    showProgress: false,
    forget: false,
  }),
  over_budget: Object.freeze({
    title: "仍在处理",
    message: () => OVER_BUDGET_MESSAGE,
    hint: () => OVER_BUDGET_MESSAGE,
    actions: Object.freeze(["leave"]),
    showProgress: true,
    forget: true,
  }),
  reconciliation_required: Object.freeze({
    title: RECONCILIATION_TITLE,
    message: () => RECONCILIATION_MESSAGE,
    hint: () => RECONCILIATION_MESSAGE,
    actions: Object.freeze(["wait_human", "revoke"]),
    showProgress: true,
    forget: true,
  }),
});

function actionFlags(actions) {
  const allowed = new Set(actions || []);
  return {
    record: allowed.has("record"),
    upload: allowed.has("upload"),
    leave: allowed.has("leave"),
    revoke: allowed.has("revoke"),
    wait: allowed.has("wait"),
    waitHuman: allowed.has("wait_human"),
  };
}

function progressPercent(progress) {
  const value = Number(progress);
  if (!Number.isFinite(value)) return null;
  return Math.round(Math.max(0, Math.min(1, value)) * 100);
}

function legacyStatusLabel(payload) {
  if (!payload) return "暂时读不到自定义声音状态。";
  const items = Array.isArray(payload.items) ? payload.items : [];
  if (items.some((item) => item.status === "active")) {
    return VOICE_ENROLLMENT_STATES.ready.message();
  }
  if (items.some((item) => item.status === "enrolling")) {
    return "正在生成自定义声音，通常一分钟内完成。结果就在这一页，不用去别处看。";
  }
  if (items.some((item) => item.status === "candidate")) {
    return VOICE_ENROLLMENT_STATES.activating.message();
  }
  if (items.some((item) => item.status === "failed")) {
    return VOICE_ENROLLMENT_STATES.failed.message();
  }
  return idleMessage(payload);
}

function resolveVoiceEnrollmentState(item) {
  if (!item || typeof item !== "object") return "idle";
  const enrollment = item.enrollment && typeof item.enrollment === "object" ? item.enrollment : null;
  const status = typeof item.status === "string" ? item.status : "";
  const operation = typeof enrollment?.operation_state === "string" ? enrollment.operation_state : "";
  if (
    operation === "reconciliation_required" ||
    enrollment?.state === "reconciliation_required" ||
    enrollment?.stage === "reconciliation_required"
  ) {
    return "reconciliation_required";
  }
  if (status === "revoked" && item.deletion_status === "completed") return "forgotten";
  if (enrollment && typeof enrollment.state === "string" && VOICE_ENROLLMENT_STATES[enrollment.state]) {
    return enrollment.state;
  }
  if (enrollment?.over_budget === true && enrollment.stage !== "done" && enrollment.stage !== "failed") {
    return "over_budget";
  }
  if (!enrollment) {
    if (status === "active") return "ready";
    if (status === "failed") return "failed";
    if (status === "revoked") return "revoked";
    if (status === "candidate") return "activating";
    if (status === "enrolling") return "cloning";
    return "idle";
  }
  if (enrollment.stage === "done" || status === "active") return "ready";
  if (status === "revoked") return "revoked";
  if (enrollment.stage === "failed" || status === "failed") return "failed";
  if (enrollment.stage === "queued" || enrollment.stage === "cloning" || enrollment.stage === "activating") {
    return enrollment.stage;
  }
  return "idle";
}

function pickRankedItem(items, requireEnrollment) {
  let best = null;
  let bestRank = Infinity;
  for (const item of items) {
    if (requireEnrollment && !item?.enrollment) continue;
    const rank = VOICE_CLONE_STATUS_RANK[item?.status] ?? Object.keys(VOICE_CLONE_STATUS_RANK).length;
    if (rank < bestRank) {
      best = item;
      bestRank = rank;
    }
  }
  return best;
}

function viewFor(state, extra) {
  const spec = VOICE_ENROLLMENT_STATES[state] || VOICE_ENROLLMENT_STATES.idle;
  return {
    voiceCloneState: state,
    voiceCloneActions: actionFlags(spec.actions),
    voiceCloneEnrollmentOverBudget: state === "over_budget",
    ...extra,
  };
}

function idleVoiceCloneView() {
  return viewFor("idle", {
    voiceCloneEnrollment: null,
    voiceCloneStatusLabel: VOICE_ENROLLMENT_STATES.idle.title,
  });
}

function voiceCloneStatusData(label) {
  return {
    ...idleVoiceCloneView(),
    voiceCloneStatusLabel: label,
  };
}

function presentVoiceClone(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const item = pickRankedItem(items, true);
  if (!item) {
    // Old servers omit enrollment. Keep the previous sentences and skip the bar.
    const fallback = pickRankedItem(items, false);
    const state = fallback ? resolveVoiceEnrollmentState(fallback) : "idle";
    return viewFor(state, {
      voiceCloneEnrollment: null,
      voiceCloneStatusLabel: legacyStatusLabel(payload),
    });
  }

  const state = resolveVoiceEnrollmentState(item);
  const spec = VOICE_ENROLLMENT_STATES[state];
  if (!spec.showProgress) {
    return viewFor(state, {
      voiceCloneEnrollment: null,
      voiceCloneStatusLabel: spec.message(payload, item.enrollment),
    });
  }

  const enrollment = item.enrollment;
  const percent = progressPercent(enrollment.progress);
  const hideProgress = state === "reconciliation_required" || percent === null;
  const label = state === "reconciliation_required"
    ? spec.title
    : (enrollment.stage_label || spec.title);
  return viewFor(state, {
    voiceCloneStatusLabel: label,
    voiceCloneEnrollment: {
      label,
      state,
      hasProgress: !hideProgress,
      progressPercent: hideProgress ? 0 : percent,
      progressLabel: hideProgress ? "" : `${percent}%`,
      hint: spec.hint(enrollment),
    },
  });
}

function voiceSamplesToForget(items) {
  return (Array.isArray(items) ? items : []).filter((item) => {
    if (!item || typeof item.profile_id !== "string" || !item.profile_id) return false;
    const spec = VOICE_ENROLLMENT_STATES[resolveVoiceEnrollmentState(item)];
    return spec?.forget === true;
  });
}

function shouldRevokeVoiceConsent(listing, targets) {
  const consent = listing?.consent;
  if (!consent || typeof consent !== "object") return false;
  if (!consent.revoked_at) return true;
  return Array.isArray(targets) && targets.length > 0;
}

module.exports = {
  VOICE_ENROLLMENT_STATES,
  OVER_BUDGET_MESSAGE,
  RECONCILIATION_MESSAGE,
  resolveVoiceEnrollmentState,
  presentVoiceClone,
  idleVoiceCloneView,
  voiceCloneStatusData,
  voiceSamplesToForget,
  shouldRevokeVoiceConsent,
  actionFlags,
};
