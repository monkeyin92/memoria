/*
 * 解除绑定后，把服务端 POST /v1/devices/{id}/binding/unbind 的返回如实读给家长。
 * 取值以 services/control_api/app/routes/identity_lifecycle.py 为准：
 *   child_layers_purged: not_applicable | not_requested | completed | pending
 *   subject_deletion:    not_requested  | completed | pending
 * 缺字段或取值不认识时不写对应那一行，不替服务端补结论。
 */

function keepLine(payload) {
  if (
    payload.child_layers_purged !== "not_requested" ||
    payload.subject_deletion !== "not_requested"
  ) {
    return "";
  }
  return "TA 的数据已保留，重新绑定时可以选回 TA";
}

function deletionLine(payload) {
  if (payload.child_layers_purged === "pending" || payload.subject_deletion === "pending") {
    return "TA 的数据正在删除中，完成后不会再显示";
  }
  if (
    payload.child_layers_purged === "completed" ||
    payload.subject_deletion === "completed"
  ) {
    return "TA 的近期摘要和长期记录已删除";
  }
  return "";
}

function consentsLine(count) {
  return Number.isInteger(count) && count > 0 ? `已撤回 ${count} 项授权。` : "";
}

function unbindResultLines(result) {
  const payload = result && typeof result === "object" ? result : {};
  return [
    keepLine(payload),
    deletionLine(payload),
    consentsLine(payload.consents_withdrawn),
  ].filter(Boolean);
}

module.exports = { unbindResultLines };
