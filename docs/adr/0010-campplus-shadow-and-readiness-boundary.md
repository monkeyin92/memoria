---
status: accepted
date: 2026-07-19
---

# 固定 CAM++ 供应链并将声纹识别保持为 shadow-only

Memoria 当前采用固定版本的 CAM++ 说话人 embedding 服务作为 `SpeakerAuthority` 的模型边界：

```text
campplus-cn-common@v1.0.0+ckpt.3388cf5f+onnx.7a39d2e5e566+fbank.v1
```

模型文件、导出修补、fbank 约定和 SHA-256 必须由独立 `speaker-model` 容器提供；Control API 不在自己的进程内加载 Torch/ModelScope，也不把模型塞进实时 Agent。Control API 的 `/health/ready` 每次请求都探测该服务的 `/health/ready`，并要求 HTTP 200、`status=ready` 和完全匹配的 `model_version`。模型宕机、版本漂移、响应非法或网络超时均使候选版本返回 503。

CAM++ 只提供 speaker embedding，不提供 replay/synthetic anti-spoof 结论。因此服务明确返回 `risk_assessment=unavailable`，并把 `replay_risk` 与 `synthetic_risk` 固定为 fail-closed sentinel `1.0`。模型结果可以用于登记、shadow 分类和权限降级，但不能单独激活 owner 的敏感能力；所有 shadow 结果保持 `uncertain`，直到独立 anti-spoof/liveness、授权真人样本和指标报告完成。

## 考虑过的选项

- 在 Control API 进程内直接加载模型：部署看似简单，但扩大内存/供应链边界，并让实时控制面与模型崩溃耦合。
- 继续使用 HTTP mock 或旧 log-mel 作为正式 owner 证明：容易通过测试，但无法证明真实模型 parity，也会产生 fail-open 权限风险。
- 把 CAM++ 的风险 sentinel 当作已完成 anti-spoof：会把“没有检测能力”误报成“低风险”，违反生物特征安全边界。
- 在没有独立活体/反重放服务时开放 owner 激活：会把回放、合成音和真人混在同一权限路径，风险不可接受。

## 后果

- 本地工程拥有可复现的真实 ONNX 推理、模型 digest、HTTP 鉴权、版本校验和 Control API 纵向合同；生产 Linux/amd64 镜像和真实样本仍需单独验收。
- guest/uncertain 仍可正常聊天，但不读取或污染主人私人记忆、Persona、原始语音和敏感动作。
- `speaker-model` 不公开宿主端口，只在 production Compose 内网暴露；Control API 使用独立映射 token。
- 只有在独立 anti-spoof/liveness、至少 200 条授权真人样本及 FAR/FRR/EER/unknown rejection 报告通过后，才可设计并开放正式 owner 激活；在此之前不得把 shadow 结果称为生产主人识别。
