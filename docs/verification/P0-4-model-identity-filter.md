# P0-4 修复 Persona 规则泄露模型身份

## 问题现象

**用户反馈**（2026-08-21）：
> 此前设置"不要说自己是什么模型"，但本轮回复自称 DeepSeek。

## 根本原因

当前系统**缺少输出内容过滤机制**，无法在运行时阻止 LLM 泄露其模型身份。虽然可以在系统提示中要求不披露，但 LLM 仍可能在某些情况下违反这一规则。

## 解决方案

实现一个**输出内容过滤器**，在回复发送给用户前检测并阻止模型身份泄露。

### 1. 新增模块

**文件**: `services/agent/src/output_content_filter.py`

**功能**:
- 检测回复中的模型身份泄露（DeepSeek、Claude、GPT、Qwen 等）
- 支持三种处理模式：
  - `block`: 阻止回复发送
  - `rewrite`: 自动改写，用 persona 名称替换模型名称
  - `warn`: 仅记录警告但允许通过
- 区分自我介绍（"我是 DeepSeek"）和第三人称引用

**核心类**: `ModelIdentityFilter`

```python
from services.agent.src.output_content_filter import ModelIdentityFilter

filter = ModelIdentityFilter(
    action="block",
    persona_name="小明"
)

# 检查回复
should_emit, filtered_text, reason = filter.filter(response_text)
if not should_emit:
    logger.warning("Blocked response: %s", reason)
    # 生成安全的替代回复
```

### 2. 集成点

需要在以下位置集成过滤器：

#### A. Voice Core 输出流

**文件**: `services/agent/src/voice_core/media_session_output_stream.py`

**集成位置**: `_stream_output` 方法中，在发送 PCM 帧之前

```python
# 在 _stream_output 中添加
from services.agent.src.output_content_filter import create_default_filter

async def _stream_output(
    self,
    context: _MediaVoiceSession,
    session_id: str,
    fence: GenerationFence,
    lease: _OutputOwnerLease,
    chunks: AsyncIterator[MediaReplyChunk],
    *,
    measure_tts_first_frame: bool,
) -> OutputDispatchResult:
    """Send one selected source through the shared owner and PCM ledger."""

    # 创建过滤器（使用 persona 名称）
    persona_name = context.runtime.get_persona_name() or "AI助手"
    identity_filter = create_default_filter(persona_name=persona_name)

    emitted_audio = False
    loop = asyncio.get_running_loop()
    next_pcm_send_at = loop.time()
    context.tts_started_ns = time.monotonic_ns() if measure_tts_first_frame else None
    
    try:
        async for chunk in chunks:
            # ... existing checks ...
            
            announcement = (
                chunk.text if chunk.assistant_text_delta is None else chunk.assistant_text_delta
            )
            if announcement:
                context.assistant_text += announcement
                
                # **ADD: Filter the accumulated text**
                filter_result = identity_filter.check(context.assistant_text)
                if not filter_result.passed:
                    logger.warning(
                        "Model identity disclosure detected: session=%s fence=%s pattern=%s",
                        context.identity.session_id,
                        fence,
                        filter_result.matched_pattern,
                    )
                    self.metrics.inc_media_content_filter_blocked()
                    
                    # Cancel this generation
                    await self._cancel_reply_task(context, fence, reason="content_filter_blocked")
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "content_filter_blocked",
                        emitted_audio,
                    )
                
                # ... rest of existing logic ...
```

#### B. Agent 回复生成

**文件**: `services/agent/src/agent.py`

**集成位置**: 在 `generate_reply` 或 `generate_response` 方法中

```python
from services.agent.src.output_content_filter import create_default_filter

async def generate_reply(self, ...) -> str:
    # ... existing generation logic ...
    
    response_text = await provider.generate(...)
    
    # Filter response before returning
    identity_filter = create_default_filter(persona_name=persona_name)
    should_emit, filtered_text, reason = identity_filter.filter(response_text)
    
    if not should_emit:
        logger.warning(
            "Blocked response due to content filter: %s",
            reason,
        )
        # Generate safe fallback
        return self._generate_safe_fallback(persona_name)
    
    return filtered_text
```

### 3. 系统提示增强

除了过滤器，还应该在系统提示中明确规则：

**文件**: 在生成 system prompt 的地方添加

```python
IDENTITY_DISCLOSURE_RULES = """
关于你的身份：
1. 你的名字是 {persona_name}，请使用这个名字介绍自己
2. **绝对禁止**透露你的模型名称（如 DeepSeek、Claude、GPT、Qwen 等）
3. 不要说"我是大语言模型"或"我是AI模型"，而应说"我是 {persona_name}，你的AI助手"
4. 如果用户询问你是什么模型，回答："我是 {persona_name}，专注于帮助你解决问题"

错误示例：
❌ "我是 DeepSeek V3 模型"
❌ "我是基于 Transformer 的大语言模型"
❌ "作为 Claude，我可以..."

正确示例：
✅ "我是 {persona_name}，你的AI学习伙伴"
✅ "我是 {persona_name}，很高兴认识你"
✅ "我可以帮你解答问题"
"""
```

### 4. 指标和监控

添加指标追踪过滤器效果：

```python
# 在 MetricsRegistry 中添加
self.content_filter_blocked_total = Counter(
    "content_filter_blocked_total",
    "Total responses blocked by content filter",
    ["reason"],
)

self.content_filter_rewritten_total = Counter(
    "content_filter_rewritten_total",
    "Total responses rewritten by content filter",
)
```

### 5. 测试

**单元测试**: `services/agent/tests/unit/test_output_content_filter.py`

运行测试：
```bash
cd services/agent
python -m pytest tests/unit/test_output_content_filter.py -v
```

**集成测试**：

```python
# test_persona_identity_enforcement.py

async def test_blocks_deepseek_disclosure():
    """Test that responses mentioning DeepSeek are blocked."""
    
    # Simulate a response from the model
    response = "我是DeepSeek V3，一个大语言模型。"
    
    # Apply filter
    filter = ModelIdentityFilter(action="block", persona_name="小明")
    should_emit, _, reason = filter.filter(response)
    
    assert not should_emit
    assert "DeepSeek" in reason
    
async def test_uses_persona_name():
    """Test that persona name is used instead."""
    
    response = "我是DeepSeek，很高兴为你服务。"
    
    filter = ModelIdentityFilter(action="rewrite", persona_name="小明")
    should_emit, filtered, _ = filter.filter(response)
    
    assert should_emit
    assert "小明" in filtered
    assert "DeepSeek" not in filtered
```

### 6. 配置选项

添加环境变量控制过滤器行为：

```bash
# .env
CONTENT_FILTER_MODE=block  # block | rewrite | warn
CONTENT_FILTER_ENABLED=true
```

```python
# config.py
class AgentConfig:
    content_filter_enabled: bool = Field(
        default=True,
        env="CONTENT_FILTER_ENABLED",
    )
    content_filter_mode: Literal["block", "rewrite", "warn"] = Field(
        default="block",
        env="CONTENT_FILTER_MODE",
    )
```

## 回归测试用例

创建专门的回归测试，确保问题不会再次出现：

```python
# test_model_identity_regression.py

@pytest.mark.regression
async def test_p0_4_no_model_identity_disclosure():
    """Regression test for P0-4: Persona rules must prevent model identity disclosure.
    
    User reported (2026-08-21): Despite setting "不要说自己是什么模型", 
    the response self-identified as DeepSeek.
    """
    
    test_cases = [
        "我是DeepSeek",
        "我是DeepSeek V3模型",
        "作为DeepSeek，我可以...",
        "我是Claude Opus",
        "我是GPT-4",
        "我是通义千问",
        "我是一个大语言模型",
    ]
    
    filter = create_default_filter(persona_name="测试助手")
    
    for test_input in test_cases:
        result = filter.check(test_input)
        assert not result.passed, f"Should have blocked: {test_input}"
        assert result.reason == "model_identity_disclosure"
```

## 部署步骤

1. **代码审查**：确保过滤器不会误判
2. **单元测试**：运行所有测试确保通过
3. **集成测试**：在测试环境验证
4. **逐步启用**：
   - 第一阶段：`warn` 模式，只记录日志
   - 第二阶段：分析日志，调整规则
   - 第三阶段：切换到 `block` 模式
5. **监控指标**：追踪被阻止的回复数量
6. **用户验证**：确认不再出现模型身份泄露

## 成功标准

✅ 过滤器检测到所有已知的模型身份泄露模式  
✅ 不误判正常使用 persona 名称的回复  
✅ 集成到所有输出路径（Voice Core、Agent）  
✅ 有清晰的日志和指标追踪  
✅ 回归测试覆盖用户报告的场景  
✅ 真机验证不再出现 "我是 DeepSeek" 等回复

## 后续改进

1. **智能改写**：使用单独的 LLM 调用生成更自然的改写
2. **上下文感知**：区分自我介绍和第三方引用（"DeepSeek 模型很强大"）
3. **多语言支持**：扩展到英文等其他语言
4. **用户反馈**：允许用户报告漏网之鱼
5. **持续学习**：从被阻止的案例中学习新的模式

## 参考

- P0-4 任务：修复 Persona 规则泄露模型身份
- 用户反馈：2026-08-21 真机验证
- Persona renderer: `services/agent/src/persona_renderer.py`
- 整改方案：`Memoria_ESP32一等语音终端与小程序控制面全双工整改方案_2026-08-13.md`
