# coturn（生产示例）

生产环境使用 coturn 的 REST HMAC 短期凭证。`static-auth-secret` 只写入
服务端 secret store，并通过 `COTURN_SHARED_SECRET` 注入 Control API；它不会
下发到浏览器或小程序。

```conf
use-auth-secret
static-auth-secret=<same value as COTURN_SHARED_SECRET>
realm=memoria
listening-port=3478
tls-listening-port=5349
fingerprint
no-cli
```

在 Control API 配置 `COTURN_URLS=turn:turn.example.com:3478,turns:turn.example.com:5349`。
证书、监听地址和 relay 网段按部署环境补充；不要把真实 secret 提交到仓库。
