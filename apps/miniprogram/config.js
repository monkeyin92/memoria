/*
 * 发布前将这里的域名加入微信公众平台的 request / uploadFile / downloadFile 合法域名。
 * 小程序已退出实时语音：不申请 socket 合法域名，也不再出现媒体网关地址。
 * 这个文件不包含 access token、媒体票据、LiveKit participant token 或任何服务端密钥。
 */
const CONTROL_API_BASE_URL = "https://aigcnice.com:8443/memoria-api";

module.exports = {
  CONTROL_API_BASE_URL,
};
