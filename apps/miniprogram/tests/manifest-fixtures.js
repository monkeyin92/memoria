/*
 * 测试夹具：canonical BindingManifest（schema BindingManifest 23 字段）。
 * 生产代码对 manifest 做严格校验（utils/device-binding.js），测试必须
 * 使用与服务端 to_dict 一致的完整形状。
 */
function canonicalManifest(overrides = {}) {
  const validFrom = new Date(Date.now() - 86_400_000).toISOString();
  return {
    binding_id: "bd_1",
    device_id: "dev_1",
    declared_mode: "self_use",
    binding_version: 1,
    status: "active",
    reason: "create",
    supersedes_binding_id: null,
    family_space_id: null,
    account_owner_id: "person_owner",
    device_admin_ids: ["person_owner"],
    primary_subject_ids: ["person_owner"],
    guardian_ids: [],
    delegate_ids: [],
    emergency_contact_ids: [],
    member_ids: [],
    roles: [{ person_id: "person_owner", role: "account_owner", permissions: [] }],
    service_profile_version: "self-cn-v1",
    policy_bundle_version: "policy-cn-adult-v2",
    consent_snapshot_id: null,
    persona_assignment_id: "pa_1",
    valid_from: validFrom,
    valid_until: null,
    created_at: validFrom,
    ...overrides,
  };
}

module.exports = { canonicalManifest };
