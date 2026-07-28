import { request } from "./client.js";
import { isJsonObject } from "./shared.js";

const growthDimensionKeys = new Set([
  "life_chapters",
  "important_people",
  "expression",
  "decision_cases",
  "relationship_models",
  "voice",
  "legacy",
]);
const growthDimensionStatuses = new Set([
  "empty",
  "emerging",
  "supported",
  "conflicted",
]);
const growthReadinessStatuses = new Set([
  "ready",
  "stale",
  "not_built",
  "dependency_pending",
]);
const growthTaskKinds = new Set([
  "natural_chat",
  "life_interview",
  "scenario_choice",
  "decision_review",
]);
const growthTaskStatuses = new Set([
  "draft",
  "active",
  "paused",
  "completed",
  "cancelled",
]);
const growthOwnerTargetKinds = new Set([
  "memory_claim",
  "persona_trait",
  "source_event",
  "digital_self_version",
  "person_entity",
  "timeline_entry",
  "relationship",
  "voice_profile",
  "cognitive_claim",
  "decision_case",
  "relationship_profile",
]);
const growthSourceWeights = new Set(["strong", "normal", "weak"]);

function invalidGrowthResponse(message = "成长地图响应无效") {
  throw new Error(message);
}

function requireGrowthString(value, message) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(message);
  }
  return value.trim();
}

function parseGrowthDate(value) {
  if (value === undefined || value === null) return value ?? null;
  const parsed = requireGrowthString(value, "成长地图时间字段无效");
  if (Number.isNaN(new Date(parsed).getTime())) {
    invalidGrowthResponse();
  }
  return parsed;
}

function parseGrowthSource(value) {
  if (!isJsonObject(value)) invalidGrowthResponse();
  const source = {
    event_id: requireGrowthString(value.event_id, "成长来源事件标识无效"),
    kind: requireGrowthString(value.kind, "成长来源类型无效"),
    event_type: requireGrowthString(value.event_type, "成长来源事件类型无效"),
  };
  for (const field of ["target_kind", "target_id", "label"]) {
    if (value[field] !== undefined && value[field] !== null) {
      source[field] = requireGrowthString(value[field], "成长来源字段无效");
    }
  }
  if (!growthSourceWeights.has(value.weight)) {
    invalidGrowthResponse();
  }
  source.weight = value.weight;
  if (value.occurred_at !== undefined) source.occurred_at = parseGrowthDate(value.occurred_at);
  return source;
}

function parseGrowthOverview(value) {
  if (!isJsonObject(value) || !Array.isArray(value.dimensions)) {
    invalidGrowthResponse();
  }
  return {
    dimensions: value.dimensions.map((dimension) => {
      if (
        !isJsonObject(dimension) ||
        !growthDimensionKeys.has(dimension.key) ||
        !growthDimensionStatuses.has(dimension.status) ||
        !Array.isArray(dimension.adopted_sources) ||
        !isJsonObject(dimension.rejected_reason_counts) ||
        !Array.isArray(dimension.conflicts) ||
        !Array.isArray(dimension.recent_changes) ||
        !Array.isArray(dimension.dependency_blockers) ||
        !isJsonObject(dimension.version_readiness)
      ) {
        invalidGrowthResponse();
      }
      const rejectedReasonCounts = {};
      for (const [reason, count] of Object.entries(dimension.rejected_reason_counts)) {
        if (!Number.isInteger(count) || count < 0) invalidGrowthResponse();
        rejectedReasonCounts[reason] = count;
      }
      const conflicts = dimension.conflicts.map((conflict) => {
        if (
          !isJsonObject(conflict) ||
          typeof conflict.target_kind !== "string" ||
          !conflict.target_kind ||
          typeof conflict.target_id !== "string" ||
          !conflict.target_id ||
          typeof conflict.event_id !== "string" ||
          !conflict.event_id ||
          typeof conflict.action !== "string" ||
          !conflict.action
        ) {
          invalidGrowthResponse();
        }
        return conflict;
      });
      const recentChanges = dimension.recent_changes.map((change) => {
        if (
          !isJsonObject(change) ||
          typeof change.event_id !== "string" ||
          !change.event_id ||
          typeof change.event_type !== "string" ||
          !change.event_type
        ) {
          invalidGrowthResponse();
        }
        if (typeof change.occurred_at !== "string") invalidGrowthResponse();
        return {
          ...change,
          occurred_at: parseGrowthDate(change.occurred_at),
        };
      });
      const readiness = dimension.version_readiness;
      if (
        !growthReadinessStatuses.has(readiness.status) ||
        (readiness.version_id !== undefined &&
          readiness.version_id !== null &&
          (typeof readiness.version_id !== "string" || !readiness.version_id))
      ) {
        invalidGrowthResponse();
      }
      return {
        key: dimension.key,
        status: dimension.status,
        adopted_sources: dimension.adopted_sources.map(parseGrowthSource),
        rejected_reason_counts: rejectedReasonCounts,
        conflicts,
        recent_changes: recentChanges,
        dependency_blockers: dimension.dependency_blockers.map((blocker) =>
          requireGrowthString(blocker, "成长依赖阻塞项无效"),
        ),
        version_readiness: {
          version_id: readiness.version_id || null,
          status: readiness.status,
        },
      };
    }),
  };
}

function parseGrowthTask(value) {
  if (
    !isJsonObject(value) ||
    typeof value.task_id !== "string" ||
    !value.task_id ||
    !growthTaskKinds.has(value.kind) ||
    !growthTaskStatuses.has(value.status) ||
    !Number.isInteger(value.revision) ||
    value.revision < 0 ||
    typeof value.created_at !== "string" ||
    !value.created_at ||
    Number.isNaN(new Date(value.created_at).getTime()) ||
    typeof value.updated_at !== "string" ||
    !value.updated_at ||
    Number.isNaN(new Date(value.updated_at).getTime())
  ) {
    invalidGrowthResponse("成长任务响应无效");
  }
  const task = {
    task_id: value.task_id,
    kind: value.kind,
    status: value.status,
    revision: value.revision,
    created_at: value.created_at,
    updated_at: value.updated_at,
  };
  for (const field of ["prompt_id", "prompt"]) {
    if (value[field] !== undefined && value[field] !== null) {
      task[field] = requireGrowthString(value[field], "成长任务提示字段无效");
    } else {
      task[field] = null;
    }
  }
  return task;
}

function parseGrowthTaskList(value) {
  if (!isJsonObject(value) || !Array.isArray(value.items)) {
    invalidGrowthResponse("成长任务列表响应无效");
  }
  return { items: value.items.map(parseGrowthTask) };
}

function requireGrowthTaskKind(kind) {
  const value = requireGrowthString(kind, "成长任务类型无效");
  if (!growthTaskKinds.has(value)) throw new Error("成长任务类型无效");
  return value;
}

function requireGrowthTaskStatus(status) {
  const value = requireGrowthString(status, "成长任务状态无效");
  if (!growthTaskStatuses.has(value)) throw new Error("成长任务状态无效");
  return value;
}

function requireGrowthRevision(revision) {
  if (!Number.isInteger(revision) || revision < 0) {
    throw new Error("成长任务版本号无效");
  }
  return revision;
}

export function getGrowthOverview() {
  return request("/v1/growth/overview").then(parseGrowthOverview);
}

export function getGrowthTasks() {
  return request("/v1/growth/tasks").then(parseGrowthTaskList);
}

export function createGrowthTask(eventId, kind, promptId = null) {
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    kind: requireGrowthTaskKind(kind),
  };
  if (promptId !== null && promptId !== undefined) {
    body.prompt_id = requireGrowthString(promptId, "成长提示标识无效");
  }
  return request("/v1/growth/tasks", {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

export function transitionGrowthTask(taskId, eventId, toStatus, expectedRevision) {
  const id = requireGrowthString(taskId, "成长任务标识无效");
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    to_status: requireGrowthTaskStatus(toStatus),
    expected_revision: requireGrowthRevision(expectedRevision),
  };
  return request(`/v1/growth/tasks/${encodeURIComponent(id)}/transitions`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

const growthResponseStructuredFields = new Set([
  "options",
  "constraints",
  "chosen_option",
  "rejected_options",
  "outcome",
  "reflection",
  "still_endorsed",
]);

function requireGrowthResponseList(value, message) {
  if (!Array.isArray(value) || value.length > 32) throw new Error(message);
  return value.map((item) => {
    const text = requireGrowthString(item, message);
    if (text.length > 1000) throw new Error(message);
    return text;
  });
}

function requireGrowthResponseText(value, message, maxLength) {
  const text = requireGrowthString(value, message);
  if (text.length > maxLength) throw new Error(message);
  return text;
}

function growthResponseStructuredBody(structured) {
  if (structured === undefined || structured === null) return null;
  if (!isJsonObject(structured)) throw new Error("成长任务结构化回答无效");
  const body = {};
  for (const [field, value] of Object.entries(structured)) {
    if (!growthResponseStructuredFields.has(field)) {
      throw new Error("成长任务结构化回答无效");
    }
    if (field === "options" || field === "constraints" || field === "rejected_options") {
      body[field] = requireGrowthResponseList(value, "成长任务选项无效");
    } else if (field === "chosen_option") {
      body[field] = requireGrowthResponseText(value, "成长任务已选方案无效", 1000);
    } else if (field === "outcome" || field === "reflection") {
      body[field] = requireGrowthResponseText(value, "成长任务复盘内容无效", 4000);
    } else if (typeof value === "boolean") {
      body[field] = value;
    } else {
      throw new Error("成长任务结构化回答无效");
    }
  }
  return Object.keys(body).length ? body : null;
}

export function respondGrowthTask(taskId, eventId, expectedRevision, answer, structured) {
  const id = requireGrowthString(taskId, "成长任务标识无效");
  if (typeof answer !== "string") throw new Error("请先写下你的回答");
  const structuredBody = growthResponseStructuredBody(structured);
  const trimmedAnswer = answer.trim();
  if (!trimmedAnswer && !structuredBody) throw new Error("请先写下你的回答");
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    expected_revision: requireGrowthRevision(expectedRevision),
    answer: trimmedAnswer,
    ...structuredBody,
  };
  return request(`/v1/growth/tasks/${encodeURIComponent(id)}/responses`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

export function reviewGrowthOwnerAction(eventId, action, targetKind, targetId) {
  const allowedActions = new Set(["not_me", "would_not_say"]);
  if (!allowedActions.has(action)) throw new Error("成长来源操作无效");
  const target = requireGrowthString(targetKind, "目标类型无效");
  if (!growthOwnerTargetKinds.has(target)) throw new Error("目标类型无效");
  return request("/v1/growth/owner-actions", {
    method: "POST",
    body: JSON.stringify({
      event_id: requireGrowthString(eventId, "成长事件标识无效"),
      action,
      target_kind: target,
      target_id: requireGrowthString(targetId, "目标标识无效"),
    }),
  }).then((value) => {
    if (!isJsonObject(value)) invalidGrowthResponse("成长来源操作响应无效");
    return value;
  });
}
