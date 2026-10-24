# 天气查询出站字段

`WEATHER_LOOKUP_PROVIDER` 默认是 `off`。`LIVE_SEARCH_QWEN_ENABLED` 默认是 `false`。`build_domestic_weather` 在 `off` 时返回 `None`，也在 `domestic` 缺少 `QWEATHER_API_KEY` 或 `QWEATHER_API_HOST` 时返回 `None`。实时检索开关关闭，且没有可用的和风天气数据源时，规则层对天气问句以及下一轮只报城市的话固定回答「我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。」文稿是产品部定稿 weather-fallback-v1（2026-10-16）。这时没有天气客户端，也不调用通义实时检索，请求数是 0。这句话只作为文字回复，本变更不合成音频。

`LIVE_SEARCH_QWEN_ENABLED=true` 且进程持有 `DASHSCOPE_API_KEY` 时，本地规则答不了的天气问句交给通义实时检索。`domestic` 且密钥与域名都存在时，即使实时检索开关关闭，`DomesticWeather.resolve` 仍先在本地决定。不是天气问句，或日期窗口超过 7 天、单位不是「天」、窗口含糊或互相矛盾时，返回 `None`，请求数是 0。实时检索开着则把原话交给通义。关着则不再出站。是天气问句但允许表不能确定唯一城市时，返回 `你想查哪个城市呀？`，请求数是 0，通义不被调用。允许表命中且窗口可回答时，才发出下面的 GET。

域名是配置里的 `QWEATHER_API_HOST`，必须是裸主机名 `*.qweatherapi.com`。方法是 GET。正文为空。密钥放在请求头 `X-QW-Api-Key`。`httpx` 还会带上 `Host`、`Accept`、`Accept-Encoding`、`Connection`、`User-Agent`。客户端以 `trust_env=False` 创建，不读取 `HTTP_PROXY` 一类环境变量。

构造 `DomesticWeather` 时把进程内 `httpx` 日志级别升到 WARNING，并丢掉消息里含有 `qweatherapi.com` 的记录。这个 logger 是进程级的，因此同一进程里其他 `httpx` 客户端也不再打出 INFO 请求行。应用自己的日志只写 `model`、`start_offset`、`days`、`elapsed_ms` 和固定的 `reason`。日志里没有 URL、密钥、城市名、原话或异常文本。

## 出站字段

| 路径 | 位置 | 字段 |
|---|---|---|
| `/geo/v2/city/lookup` | query | `location` |
| `/geo/v2/city/lookup` | query | `range` |
| `/geo/v2/city/lookup` | query | `number` |
| `/v7/weather/3d` | query | `location` |
| `/v7/weather/7d` | query | `location` |
| `/v7/weather/now` | query | `location` |

`/geo/v2/city/lookup` 的 `location` 是允许表 `City.standard_name`。区县别名命中时仍然发送所属城市的标准名。`range` 是 `cn`。`number` 是 `10`。返回行要同时满足 `country` 为 `中国`、`name` 等于标准名、`adm1` 等于该城市的省，并且 `id` 是 9 位数字。这个 id 记在进程内，同一城市后面的查询不再请求地理接口。

预报需要的天数是从今天起算的结束日（含起始偏移）。1 到 3 天走 `/v7/weather/3d`，4 到 7 天走 `/v7/weather/7d`。这两处的 `location` 是上面的 9 位 id。问句里有「现在」或「当前」，并且窗口从今天开始时，再请求 `/v7/weather/now`，`location` 同样是这个 id。

请求不携带原话、抽取到的地片段、经纬度、设备 id、儿童 id 或会话 id。连接的源地址是服务器自己的地址。

口头回答里的城市名用标准名。这个查询不另写天气表。说出去的句子随后走原来的回复路径。
