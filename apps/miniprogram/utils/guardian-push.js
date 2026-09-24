/*
 * 监护人危机提醒订阅（小程序一次性订阅消息）。
 *
 * - 服务端 push-config 关闭时不返回模板 ID，这里就永远不弹订阅框。
 * - wx.requestSubscribeMessage 只能由用户点击触发：subscribeCrisisAlerts
 *   在第一个 await 之前同步调起它，调用方必须直接在 tap 处理函数里调用。
 * - 每次“允许”只换来一次提醒；结果回传服务端记账，accept 附带 wx.login code。
 */

const CRISIS_PUSH_EXPLANATION =
  "开启后，孩子可能需要帮助时，微信会提醒你打开小程序查看。提醒不含任何对话内容，每次允许可收到一次。";

const RECORDABLE_RESULTS = Object.freeze(["accept", "reject", "ban"]);
// 20004：用户关闭了小程序订阅消息总开关，等同于 ban。
const MAIN_SWITCH_OFF = 20004;

function disabledCrisisPush() {
  return { enabled: false, templateId: "", explanation: "" };
}

function normalizeCrisisPushConfig(payload) {
  const rawTemplateId = payload?.template_ids?.crisis;
  const templateId = typeof rawTemplateId === "string" ? rawTemplateId.trim() : "";
  if (payload?.enabled !== true || !templateId) return disabledCrisisPush();
  return { enabled: true, templateId, explanation: CRISIS_PUSH_EXPLANATION };
}

async function loadCrisisPushConfig(api) {
  try {
    return normalizeCrisisPushConfig(await api.getGuardianPushConfig());
  } catch (_error) {
    // 读不到配置就当作关闭：绝不因为错误而弹订阅框。
    return disabledCrisisPush();
  }
}

function requestSubscribe(wxApi, templateId) {
  return new Promise((resolve) => {
    wxApi.requestSubscribeMessage({
      tmplIds: [templateId],
      success(result) {
        resolve(typeof result?.[templateId] === "string" ? result[templateId] : "");
      },
      fail(error) {
        resolve(error?.errCode === MAIN_SWITCH_OFF ? "ban" : "");
      },
    });
  });
}

async function subscribeCrisisAlerts({ config, api, wxApi = globalThis.wx } = {}) {
  if (!config?.enabled || !config.templateId) return { prompted: false, result: "" };
  if (typeof wxApi?.requestSubscribeMessage !== "function") {
    return { prompted: false, result: "" };
  }
  const result = await requestSubscribe(wxApi, config.templateId);
  if (!RECORDABLE_RESULTS.includes(result)) {
    return { prompted: true, result, recorded: false };
  }
  const loginCode = result === "accept" ? await api.wechatLoginCode() : "";
  await api.recordGuardianPushSubscription({
    templateId: config.templateId,
    result,
    loginCode,
  });
  return { prompted: true, result, recorded: true };
}

/* 非点击场景（如发起绑定成功后）：先用弹窗说明，用户点“开启”后再调起订阅。 */
function offerCrisisSubscription({ config, api, wxApi = globalThis.wx } = {}) {
  if (!config?.enabled || !config.templateId || typeof wxApi?.showModal !== "function") {
    return Promise.resolve({ prompted: false, result: "" });
  }
  return new Promise((resolve, reject) => {
    wxApi.showModal({
      title: "开启安全提醒",
      content: config.explanation || CRISIS_PUSH_EXPLANATION,
      confirmText: "开启",
      cancelText: "暂不",
      success(choice) {
        if (!choice?.confirm) {
          resolve({ prompted: false, result: "" });
          return;
        }
        subscribeCrisisAlerts({ config, api, wxApi }).then(resolve, reject);
      },
      fail() {
        resolve({ prompted: false, result: "" });
      },
    });
  });
}

module.exports = {
  CRISIS_PUSH_EXPLANATION,
  loadCrisisPushConfig,
  normalizeCrisisPushConfig,
  offerCrisisSubscription,
  subscribeCrisisAlerts,
};
