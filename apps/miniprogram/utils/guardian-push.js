/*
 * 监护人危机提醒订阅（小程序一次性订阅消息）。
 *
 * 家长可见文案统一用「订阅安全提醒」，不用「危机提醒」。
 * - 服务端 push-config 关闭时不返回模板 ID，这里就永远不弹订阅框。
 * - wx.requestSubscribeMessage 只能由用户点击触发：subscribeCrisisAlerts
 *   在第一个 await 之前同步调起它，调用方必须直接在 tap 处理函数里调用。
 * - 每次“允许”只换来一次提醒；结果回传服务端记账，accept 附带 wx.login code。
 * - 设置页关闭走 opt_out：清零服务端剩余次数。微信一次性订阅无法在客户端撤销。
 */

const PARENT_SUBSCRIBE_LABEL = "开启安全提醒";

const SAFETY_SUBSCRIBE_HELPER =
  "家长打开小程序时首页会置顶显示";

const CRISIS_PUSH_EXPLANATION =
  "开启安全提醒后，孩子可能需要帮助时，微信会提醒你打开小程序查看。提醒不含任何对话内容，每次允许可收到一次。";

const SAFETY_SUBSCRIBE_OFF_TITLE = "关闭安全提醒？";
const SAFETY_SUBSCRIBE_OFF_CONFIRM =
  "关闭后，我们不再通过微信提醒你。如果孩子聊到需要你关注的话题，只会在你下次打开小程序时置顶显示，你可能不能及时知道。";
const SAFETY_SUBSCRIBE_OFF_KEEP = "保持开启";
const SAFETY_SUBSCRIBE_OFF_PROCEED = "仍要关闭";
const SAFETY_SUBSCRIBE_OFF_CAPTION =
  "安全提醒已关闭，打开小程序仍会看到置顶提醒。";
const SAFETY_SUBSCRIBE_UNKNOWN_CAPTION = "状态获取失败，点击重试";

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

function normalizePushSubscription(payload) {
  const remaining = Number.isInteger(payload?.remaining) ? payload.remaining : 0;
  return {
    known: true,
    templateId: typeof payload?.template_id === "string" ? payload.template_id : "",
    remaining: remaining > 0 ? remaining : 0,
    lastResult: typeof payload?.last_result === "string" ? payload.last_result : "",
    subscribed: payload?.subscribed === true || remaining > 0,
  };
}

function unknownPushSubscription() {
  return {
    known: false,
    templateId: "",
    remaining: 0,
    lastResult: "",
    subscribed: false,
  };
}

async function loadCrisisPushSubscription(api) {
  try {
    return normalizePushSubscription(await api.getGuardianPushSubscription());
  } catch (_error) {
    return unknownPushSubscription();
  }
}

function safetySubscribeView(safety) {
  if (!safety?.safetySubscribeKnown) {
    return {
      safetySubscribeKnown: false,
      safetySubscribeOn: false,
      safetySubscribeOffCaption: SAFETY_SUBSCRIBE_UNKNOWN_CAPTION,
    };
  }
  return {
    safetySubscribeKnown: true,
    safetySubscribeOn: safety.safetySubscribeOn === true,
    safetySubscribeOffCaption: safety.safetySubscribeOn
      ? ""
      : SAFETY_SUBSCRIBE_OFF_CAPTION,
  };
}

async function loadSafetySubscribeState(api) {
  const [config, subscription] = await Promise.all([
    loadCrisisPushConfig(api),
    loadCrisisPushSubscription(api),
  ]);
  if (!subscription.known) {
    return {
      config,
      subscription,
      safetySubscribeKnown: false,
      safetySubscribeOn: false,
    };
  }
  return {
    config,
    subscription,
    safetySubscribeKnown: true,
    safetySubscribeOn: Boolean(config.enabled && subscription.subscribed),
  };
}

async function optOutCrisisPushAlerts({ config, api } = {}) {
  const templateId = config?.templateId || "";
  if (!templateId || typeof api?.recordGuardianPushSubscription !== "function") {
    return { ok: false, remaining: 0 };
  }
  const recorded = await api.recordGuardianPushSubscription({
    templateId,
    result: "opt_out",
    loginCode: "",
  });
  const remaining = Number.isInteger(recorded?.remaining) ? recorded.remaining : 0;
  return { ok: remaining === 0, remaining };
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
      title: PARENT_SUBSCRIBE_LABEL,
      content: config.explanation || CRISIS_PUSH_EXPLANATION,
      confirmText: "订阅",
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
  PARENT_SUBSCRIBE_LABEL,
  SAFETY_SUBSCRIBE_HELPER,
  SAFETY_SUBSCRIBE_OFF_CAPTION,
  SAFETY_SUBSCRIBE_OFF_CONFIRM,
  SAFETY_SUBSCRIBE_OFF_KEEP,
  SAFETY_SUBSCRIBE_OFF_PROCEED,
  SAFETY_SUBSCRIBE_OFF_TITLE,
  SAFETY_SUBSCRIBE_UNKNOWN_CAPTION,
  loadCrisisPushConfig,
  loadCrisisPushSubscription,
  loadSafetySubscribeState,
  normalizeCrisisPushConfig,
  normalizePushSubscription,
  offerCrisisSubscription,
  optOutCrisisPushAlerts,
  safetySubscribeView,
  subscribeCrisisAlerts,
  unknownPushSubscription,
};
