# TTS 熔断

阶跃主路和豆包备用路各有一个熔断器。两边的参数不是同一套。

## 阶跃

`StepFunFailoverConfig.from_env` 读取下面三个变量。变量不存在时用代码默认值。

- `STEPFUN_TTS_BREAKER_FAILURES` 的代码默认值是 3。普通失败要累计到这个次数才打开熔断。
- `STEPFUN_TTS_BREAKER_OPEN_S` 的代码默认值是 30 秒。
- `STEPFUN_TTS_AUTH_BREAKER_OPEN_S` 的代码默认值是 300 秒。

状态码 401、402、403 在第一次失败时打开鉴权熔断，时长是 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`。供应商文案里出现鉴权或余额标记时同样处理。标记写在 `services/agent/src/providers/stepfun_tts.py` 的 `_AUTH_MARKERS`。

其他状态码仍走普通窗口。一次 500 或 503 不打开熔断。累计 3 次后打开 30 秒。到时放行一次探测。探测成功后熔断关闭，下一句回到阶跃。

进程环境如果带了 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`，用环境里的值，不用代码默认值。`.env.example` 和 `infra/memoria.env.production.example` 把这个变量设为 3600。加载这两个文件的进程用 3600 秒。

## 豆包

`DoubaoTTSPool` 使用 `CircuitBreaker()`，不传入失败次数或打开时长。`DoubaoTTSConfig.from_env` 也不读这些参数。豆包熔断不能用环境变量配置。

固定值在 `services/agent/src/providers/reliability.py` 的 `CircuitBreaker`。

- `failure_threshold` 是 3。
- `open_seconds` 是 30。
- `half_open_successes` 是 2。半开之后要连续成功两次才关闭。

要改豆包熔断时长，改这段代码。没有对应的环境变量。

豆包的失败不会打开阶跃的鉴权熔断。下一句普通回复会再试阶跃。
