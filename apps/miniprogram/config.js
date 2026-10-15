/*
 * 发布前将这里的域名加入微信公众平台的 request / uploadFile / downloadFile 合法域名。
 * 小程序已退出实时语音：不申请 socket 合法域名，也不再出现媒体网关地址。
 * 这个文件不包含 access token、媒体票据、LiveKit participant token 或任何服务端密钥。
 */
const CONTROL_API_BASE_URL = "https://aginice.cn:8443/memoria-api";

const SUBSCRIPTION_PUSH_ENABLED = false;

// 镜像 agent 环境变量 LLM_FAILOVER_ENABLED。control-api 不下发这个开关。
// 打开时先发布 true 的小程序，再打开 agent。关闭时先关 agent，再发布 false。
const LLM_FAILOVER_ENABLED = false;

module.exports = {
  CONTROL_API_BASE_URL,
  SUBSCRIPTION_PUSH_ENABLED,
  LLM_FAILOVER_ENABLED,
};
