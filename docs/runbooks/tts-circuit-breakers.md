# TTS 熔断

豆包有一个进程级鉴权闩锁，阶跃有一个熔断器。两边的参数不是同一套。生产现在只有豆包一条路（`TTS_PROVIDER=doubao`），阶跃保留给回滚镜像。

## 豆包鉴权闩锁

豆包握手被拒（WebSocket 返回 HTTP 401 或 403）时，记下这个资源，`DOUBAO_TTS_AUTH_LATCH_OPEN_S` 秒内后续会话不再握手，直接返回 `DoubaoAuthRejectedError`。代码默认 300 秒，`.env.example` 和 `infra/memoria.env.production.example` 都有这一行。

窗口按 resource 分别记（`seed-tts-2.0` 与 `seed-icl-2.0` 是分开授权的）。个人音色被拒不会连累正常角色音色。窗口到点后放行一次握手，被拒就重新计时，成功就清掉。控制台改权限后不用重启 bridge。

只有鉴权拒绝会打开闩锁。网络错误仍然走池自己的 `CircuitBreaker`（3 次失败、30 秒），不跨会话。原因写在 `services/agent/src/providers/doubao_tts.py` 的 `DoubaoAuthLatch`：设备会话每次都新建自己的池，池级熔断器从来累计不到三次。生产 2026-10-25 的 bridge 日志就是每建一次会话先打一行 `TTS pool warm failed … error=InvalidStatus`，再抛 `TTSUnavailableError`，因为火山从 10-21 21:49 起就没再给这个 app 的 seedtts 授权。

计数器 `tts_auth_rejected_total{provider="volcengine_doubao"}` 只记拒绝次数，不带文本和账号。

被闩锁挡下的会话不会静默：每次跳过握手打一行 `Doubao auth latch open; skipping handshake resource_id=…`，随后照常抛 `TTSUnavailableError`，设备拿到兜底话术。恢复授权后最多等一个窗口，下一句自动回到豆包，`/health/ready` 的冒烟是更早的信号。

## 阶跃

`StepFunFailoverConfig.from_env` 读取下面三个变量。变量不存在时用代码默认值。

- `STEPFUN_TTS_BREAKER_FAILURES` 的代码默认值是 3。普通失败要累计到这个次数才打开熔断。
- `STEPFUN_TTS_BREAKER_OPEN_S` 的代码默认值是 30 秒。
- `STEPFUN_TTS_AUTH_BREAKER_OPEN_S` 的代码默认值是 300 秒。

状态码 401、402、403 在第一次失败时打开鉴权熔断，时长是 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`。供应商文案里出现鉴权或余额标记时同样处理。标记写在 `services/agent/src/providers/stepfun_tts.py` 的 `_AUTH_MARKERS`。

其他状态码仍走普通窗口。一次 500 或 503 不打开熔断。累计 3 次后打开 30 秒。到时放行一次探测。探测成功后熔断关闭，下一句回到阶跃。

进程环境如果带了 `STEPFUN_TTS_AUTH_BREAKER_OPEN_S`，用环境里的值，不用代码默认值。`.env.example` 和 `infra/memoria.env.production.example` 把这个变量设为 300，与代码默认值相同。

## 豆包传输熔断

`DoubaoTTSPool` 使用 `CircuitBreaker()`，不传入失败次数或打开时长。`DoubaoTTSConfig.from_env` 也不读这些参数。豆包传输熔断不能用环境变量配置。

固定值在 `services/agent/src/providers/reliability.py` 的 `CircuitBreaker`。

- `failure_threshold` 是 3。
- `open_seconds` 是 30。
- `half_open_successes` 是 2。半开之后要连续成功两次才关闭。

要改豆包熔断时长，改这段代码。没有对应的环境变量。这个熔断器是每个池一个，所以只对同一个会话里的连续失败有意义；跨会话的那一类由上面的鉴权闩锁负责。

豆包的失败不会打开阶跃的鉴权熔断。下一句普通回复会再试阶跃。
