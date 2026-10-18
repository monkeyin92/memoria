/*
 * 解除绑定后，把服务端 POST /v1/devices/{id}/binding/unbind 的返回如实读给家长。
 * 取值以 services/control_api/app/routes/identity_lifecycle.py 为准：
 *   child_layers_purged: not_applicable | completed | pending
 *   subject_deletion:    not_requested  | completed | pending
 * 缺字段或取值不认识时不写对应那一行，不替服务端补结论。
 */

function childLayersLine(value) {
  if (value === "completed") return "TA 的近期摘要和长期记录已清除。";
  if (value === "pending") return "TA 的近期摘要和长期记录没有全部清除，请不要当作已清除。";
  return "";
}

function subjectDeletionLine(value) {
  if (value === "completed") return "TA 的记忆和对话数据已删除。";
  if (value === "pending") return "TA 的记忆和对话数据正在删除，后台会继续完成。";
  return "";
}

function consentsLine(count) {
  return Number.isInteger(count) && count > 0 ? `已撤回 ${count} 项授权。` : "";
}

function unbindResultLines(result) {
  const payload = result && typeof result === "object" ? result : {};
  return [
    childLayersLine(payload.child_layers_purged),
    subjectDeletionLine(payload.subject_deletion),
    consentsLine(payload.consents_withdrawn),
  ].filter(Boolean);
}

/* 绑定的使用人不是账号本人时，服务端解绑都会清除这两层，与所选「保留/删除」无关。 */
function unbindClearsChildLayers(declaredMode) {
  return declaredMode !== "self_use";
}

module.exports = { unbindResultLines, unbindClearsChildLayers };
