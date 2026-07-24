/*
 * 发布前将这里的域名加入微信公众平台的 request / socket 合法域名。
 * 这个文件不包含 token、LiveKit participant token 或任何服务端密钥。
 */
const CONTROL_API_BASE_URL = "https://aigcnice.com:8443/memoria-api";

module.exports = {
  CONTROL_API_BASE_URL,
};
