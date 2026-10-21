/*
 * 解除绑定后，把服务端 POST /v1/devices/{id}/binding/unbind 的返回如实读给家长。
 * 取值以 services/control_api/app/routes/identity_lifecycle.py 为准：
 *   child_layers_purged: not_applicable | completed | pending
 *   subject_deletion:    not_requested  | completed | pending
 *   identity_cleared:    not_applicable | completed | pending
 * 解绑一律清除孩子信息与记忆层，因此没有「已保留」这一行。缺字段或取值不
 * 认识时不写对应那一行，不替服务端补结论。
 */

function deletionLine(payload) {
  if (
    payload.child_layers_purged === "pending" ||
    payload.subject_deletion === "pending" ||
    payload.identity_cleared === "pending"
  ) {
    return "TA 的数据正在删除中，完成后不会再显示";
  }
  if (
    payload.child_layers_purged === "completed" ||
    payload.subject_deletion === "completed" ||
    payload.identity_cleared === "completed"
  ) {
    return "TA 的近期摘要和长期记录已删除";
  }
  return "";
}

function consentsLine(payload) {
  const count = payload.consents_withdrawn;
  return Number.isInteger(count) && count > 0 ? `已撤回 ${count} 项授权。` : "";
}

function unbindResultLines(result) {
  const payload = result && typeof result === "object" ? result : {};
  return [deletionLine(payload), consentsLine(payload)].filter(Boolean);
}

module.exports = { unbindResultLines };
