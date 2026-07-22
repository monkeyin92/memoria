---
status: accepted
date: 2026-07-19
---

# Agent 与 Control API 之间使用按能力拆分的内部令牌

Memoria 的 Agent 需要追加证据、上报进程心跳、读取人生记忆、读取人格胶囊、解析当前声音档案和读取会话模式政策。共享一个“内部万能 token”会让任一调用路径被攻破后获得全部内部权限，也难以独立轮换和审计。生产环境因此使用 `archive_write`、`agent_heartbeat`、`memory_read`、`persona_read`、`voice_resolution`、`interaction_policy` 六个互不相同的 capability token；SpeakerAuthority 继续使用独立 token。Control API 的每个内部路由只接受对应能力，Agent 只加载已启用模块需要的令牌。

## Considered Options

- 一个共享内部 token：配置最少，但权限范围过大，泄露后的影响不可控。
- 复用账户 Bearer token：可减少凭据类型，但 Agent 不是终端用户，且会混淆账户身份与服务能力。
- 直接依赖内网地址可信：没有应用层认证，容器或反向代理配置错误会直接暴露内部权限。

## Consequences

- 生产启动校验要求 capability token 至少 32 字符且两两不同；与 SpeakerAuthority token 也不能复用。
- token 只保存在服务端 secret 文件或 secret manager，不进入 H5 bundle、浏览器存储、日志、导出或文档示例值。
- 每个能力可独立吊销、轮换、限流和审计；代价是生产 secret 配置项增加。
- 开发环境可显式回退旧 token 以兼容本地测试，生产环境禁止这种回退。
