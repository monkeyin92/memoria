import {
  SERVICE_MODE_DEFAULT,
  isConfirmationMethod,
  isServiceMode,
  validateSubjectResolution,
} from "./contracts.js";

/**
 * 主体解析（§9.2 POST /v1/sessions/resolve-subject）响应规范化。
 *
 * 结构语义以 canonical validateSubjectResolution 为唯一来源；候选主体只
 * 保留 canonical CandidateSubject（person_id/display_name/confidence）。
 * 未知 resolution / 未知 confirmation method 一律 fail closed，不猜测。
 */
function failClosedResolution(reasons) {
  return {
    valid: false,
    fail_reasons: reasons,
    resolution: "confirmation_required",
    candidate_subjects: [],
    temporary_service_mode: SERVICE_MODE_DEFAULT,
    allowed_confirmation_methods: [],
    runtime_profile_id: null,
  };
}

export function normalizeSubjectResolution(payload) {
  const reasons = validateSubjectResolution(payload);
  if (reasons.length > 0) return failClosedResolution(reasons);
  return {
    valid: true,
    fail_reasons: [],
    resolution: payload.resolution,
    candidate_subjects: payload.candidate_subjects.map((item) => ({
      person_id: item.person_id,
      display_name: item.display_name,
      confidence: item.confidence,
    })),
    temporary_service_mode: isServiceMode(payload.temporary_service_mode)
      ? payload.temporary_service_mode
      : SERVICE_MODE_DEFAULT,
    allowed_confirmation_methods: payload.allowed_confirmation_methods.filter(
      (item) => isConfirmationMethod(item),
    ),
    runtime_profile_id: payload.runtime_profile_id,
  };
}
