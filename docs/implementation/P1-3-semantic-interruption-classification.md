# P1-3 实现语义打断分类

## 目标

实现服务端语义打断分类器，区分用户语音的不同意图：
1. **Backchannel（附和）**："嗯"、"对"、"好的" - 不打断
2. **Stop Command（停止指令）**："停一下"、"等等" - 立即打断
3. **New Question（新问题）**：完整新问题 - 打断并开始新轮次
4. **Noise（噪声/回声）**：环境噪声或回声 - 忽略

避免"所有 VAD 都打断"的问题，实现智能打断判断。

## 背景

根据整改方案第 8.4 节，打断判断应该分三个阶段：

```text
1. Candidate（候选）：VAD 触发 → 本地 duck（降低音量）
2. Early ASR：获取部分识别结果 → 初步分类
3. Confirm（确认）：完整识别 + 语义分类 → 决定是否真正打断
```

这样可以：
- **减少误打断**：不会因为"嗯"、"对"等附和就打断
- **快速响应**：真正的打断意图可以在识别完成前就开始执行
- **优雅降级**：不确定时保守处理

## 技术方案

### 1. 分类器架构

```text
ASR Partial/Final Text
    ↓
Intent Classifier
    ├─ Rule-based Filter（快速路径）
    │  ├─ 停止词匹配
    │  ├─ 附和词匹配
    │  └─ 长度/标点判断
    ↓
    ├─ LLM Classifier（准确路径）
    │  └─ Few-shot prompt
    ↓
Classification Result
    ├─ BACKCHANNEL → duck only, continue
    ├─ STOP_COMMAND → cancel generation
    ├─ NEW_QUESTION → cancel + new turn
    └─ NOISE → ignore
```

### 2. 实现方案

#### 步骤 2.1：定义打断分类

**新增文件**：`services/agent/src/voice_core/interruption_classifier.py`

```python
"""Interruption intent classification for barge-in handling."""

from __future__ import annotations

from enum import StrEnum
from dataclasses import dataclass
from typing import Optional


class InterruptionIntent(StrEnum):
    """Classification of user speech during assistant playback."""
    
    BACKCHANNEL = "backchannel"  # "嗯"、"对"、"好的" - 不打断
    STOP_COMMAND = "stop_command"  # "停一下"、"等等" - 立即停止
    NEW_QUESTION = "new_question"  # 完整新问题 - 打断并开始新轮次
    NOISE = "noise"  # 环境噪声或回声 - 忽略
    UNCERTAIN = "uncertain"  # 不确定 - 保守处理


@dataclass(frozen=True, slots=True)
class InterruptionClassification:
    """Result of interruption intent classification."""
    
    intent: InterruptionIntent
    confidence: float  # 0.0 - 1.0
    text: str
    reasoning: str = ""
    
    def should_cancel_generation(self) -> bool:
        """Whether this intent should cancel current generation."""
        return self.intent in (
            InterruptionIntent.STOP_COMMAND,
            InterruptionIntent.NEW_QUESTION,
        )
    
    def should_start_new_turn(self) -> bool:
        """Whether this intent should start a new conversation turn."""
        return self.intent == InterruptionIntent.NEW_QUESTION


# Common backchannel expressions
BACKCHANNEL_PATTERNS = [
    # Single syllable acknowledgments
    "嗯", "哦", "啊", "呃",
    
    # Agreement
    "对", "是", "好", "行", "可以",
    "对对", "是是", "好好", "对的", "是的", "好的",
    
    # Understanding
    "明白", "知道了", "懂了", "了解",
    
    # Continuation encouragement
    "然后呢", "接着说", "继续",
]

# Stop command patterns
STOP_COMMAND_PATTERNS = [
    "停", "停一下", "停下", "停止",
    "等等", "等一下", "等会",
    "暂停", "别说了", "不要说了",
    "够了", "行了", "好了好了",
    "安静", "闭嘴",
]


class RuleBasedInterruptionClassifier:
    """Fast rule-based classification for common patterns."""
    
    def classify(self, text: str) -> Optional[InterruptionClassification]:
        """
        Classify interruption intent using rules.
        
        Returns None if rules cannot determine intent with confidence.
        """
        if not text or not text.strip():
            return InterruptionClassification(
                intent=InterruptionIntent.NOISE,
                confidence=0.9,
                text=text,
                reasoning="Empty or whitespace-only text",
            )
        
        text_clean = text.strip().lower()
        
        # Check backchannel patterns
        if text_clean in BACKCHANNEL_PATTERNS:
            return InterruptionClassification(
                intent=InterruptionIntent.BACKCHANNEL,
                confidence=0.95,
                text=text,
                reasoning=f"Matched backchannel pattern: {text_clean}",
            )
        
        # Check stop command patterns
        if text_clean in STOP_COMMAND_PATTERNS:
            return InterruptionClassification(
                intent=InterruptionIntent.STOP_COMMAND,
                confidence=0.95,
                text=text,
                reasoning=f"Matched stop command pattern: {text_clean}",
            )
        
        # Very short text (1-2 chars) likely backchannel or noise
        if len(text_clean) <= 2:
            return InterruptionClassification(
                intent=InterruptionIntent.BACKCHANNEL,
                confidence=0.7,
                text=text,
                reasoning="Very short text, likely backchannel",
            )
        
        # Long text with question marks likely new question
        if len(text_clean) > 10 and ("?" in text or "？" in text):
            return InterruptionClassification(
                intent=InterruptionIntent.NEW_QUESTION,
                confidence=0.8,
                text=text,
                reasoning="Long text with question mark",
            )
        
        # Long text (> 15 chars) likely new question
        if len(text_clean) > 15:
            return InterruptionClassification(
                intent=InterruptionIntent.NEW_QUESTION,
                confidence=0.75,
                text=text,
                reasoning="Long text, likely new question",
            )
        
        # Cannot determine with confidence
        return None


class LLMInterruptionClassifier:
    """LLM-based classification for ambiguous cases."""
    
    CLASSIFICATION_PROMPT = """你是一个对话意图分类器。当用户在 AI 助手说话时插话，你需要判断用户的意图。

分类类别：
1. backchannel - 附和、确认、鼓励继续（如"嗯"、"对"、"然后呢"）
2. stop_command - 明确要求停止（如"停一下"、"等等"、"别说了"）
3. new_question - 新的完整问题或话题
4. noise - 环境噪声、不清楚的声音、回声

用户说："{text}"

请返回 JSON 格式：
{{
  "intent": "backchannel|stop_command|new_question|noise",
  "confidence": 0.0-1.0,
  "reasoning": "简短说明理由"
}}
"""
    
    def __init__(self, llm_provider):
        self.llm_provider = llm_provider
    
    async def classify(self, text: str) -> InterruptionClassification:
        """Classify using LLM for ambiguous cases."""
        
        prompt = self.CLASSIFICATION_PROMPT.format(text=text)
        
        try:
            response = await self.llm_provider.generate(
                prompt=prompt,
                max_tokens=100,
                temperature=0.1,
            )
            
            # Parse JSON response
            import json
            result = json.loads(response)
            
            intent_str = result.get("intent", "uncertain")
            confidence = result.get("confidence", 0.5)
            reasoning = result.get("reasoning", "")
            
            try:
                intent = InterruptionIntent(intent_str)
            except ValueError:
                intent = InterruptionIntent.UNCERTAIN
            
            return InterruptionClassification(
                intent=intent,
                confidence=confidence,
                text=text,
                reasoning=reasoning,
            )
            
        except Exception as e:
            # Fallback to uncertain
            return InterruptionClassification(
                intent=InterruptionIntent.UNCERTAIN,
                confidence=0.5,
                text=text,
                reasoning=f"LLM classification failed: {e}",
            )


class HybridInterruptionClassifier:
    """
    Hybrid classifier: fast rules first, LLM for ambiguous cases.
    """
    
    def __init__(self, llm_provider):
        self.rule_based = RuleBasedInterruptionClassifier()
        self.llm_based = LLMInterruptionClassifier(llm_provider)
    
    async def classify(
        self,
        text: str,
        *,
        use_llm_fallback: bool = True,
    ) -> InterruptionClassification:
        """
        Classify interruption intent.
        
        Args:
            text: ASR transcription
            use_llm_fallback: Whether to use LLM for uncertain cases
        
        Returns:
            Classification result
        """
        
        # Try rule-based first (fast path)
        rule_result = self.rule_based.classify(text)
        
        if rule_result and rule_result.confidence >= 0.8:
            # High confidence from rules, use it
            return rule_result
        
        if not use_llm_fallback:
            # LLM disabled, return rule result or uncertain
            if rule_result:
                return rule_result
            else:
                return InterruptionClassification(
                    intent=InterruptionIntent.UNCERTAIN,
                    confidence=0.5,
                    text=text,
                    reasoning="Rule-based uncertain, LLM disabled",
                )
        
        # Low confidence or no rule match, use LLM
        llm_result = await self.llm_based.classify(text)
        
        # Combine rule and LLM results
        if rule_result and llm_result.intent == rule_result.intent:
            # Both agree, boost confidence
            return InterruptionClassification(
                intent=rule_result.intent,
                confidence=min(1.0, (rule_result.confidence + llm_result.confidence) / 2 + 0.1),
                text=text,
                reasoning=f"Rule+LLM agree: {llm_result.reasoning}",
            )
        
        # Use LLM result
        return llm_result
```

#### 步骤 2.2：集成到 Voice Core

**修改文件**：`services/agent/src/voice_core/interruption_policy.py`

```python
"""Interruption policy and handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from .interruption_classifier import (
    HybridInterruptionClassifier,
    InterruptionClassification,
    InterruptionIntent,
)


class InterruptionAction(StrEnum):
    """Action to take based on interruption classification."""
    
    IGNORE = "ignore"  # 忽略，继续播放
    DUCK = "duck"  # 降低音量，但不取消
    CANCEL = "cancel"  # 取消当前 generation
    CANCEL_AND_NEW_TURN = "cancel_and_new_turn"  # 取消并开始新轮次


@dataclass
class InterruptionDecision:
    """Decision on how to handle an interruption."""
    
    action: InterruptionAction
    classification: InterruptionClassification
    should_retain_audio: bool  # 是否保留音频用于新轮次
    
    @property
    def should_cancel(self) -> bool:
        return self.action in (
            InterruptionAction.CANCEL,
            InterruptionAction.CANCEL_AND_NEW_TURN,
        )


class InterruptionPolicy:
    """Policy for handling interruptions during assistant playback."""
    
    def __init__(self, classifier: HybridInterruptionClassifier):
        self.classifier = classifier
    
    async def evaluate_interruption(
        self,
        text: str,
        *,
        is_partial: bool,
        playback_progress: float,  # 0.0 - 1.0
        context: dict,
    ) -> InterruptionDecision:
        """
        Evaluate whether to interrupt based on user speech.
        
        Args:
            text: ASR transcription (partial or final)
            is_partial: Whether this is partial ASR result
            playback_progress: How far into playback (0.0 = start, 1.0 = end)
            context: Additional context (e.g., conversation history)
        
        Returns:
            Decision on how to handle interruption
        """
        
        # Classify intent
        classification = await self.classifier.classify(
            text,
            use_llm_fallback=(not is_partial),  # Only use LLM for final
        )
        
        # Decide action based on intent
        action = self._decide_action(
            classification,
            is_partial=is_partial,
            playback_progress=playback_progress,
        )
        
        # Decide whether to retain audio
        should_retain = classification.should_start_new_turn()
        
        return InterruptionDecision(
            action=action,
            classification=classification,
            should_retain_audio=should_retain,
        )
    
    def _decide_action(
        self,
        classification: InterruptionClassification,
        *,
        is_partial: bool,
        playback_progress: float,
    ) -> InterruptionAction:
        """Decide action based on classification and context."""
        
        intent = classification.intent
        confidence = classification.confidence
        
        # Backchannel: duck or ignore
        if intent == InterruptionIntent.BACKCHANNEL:
            if playback_progress < 0.8:
                # Early in playback, just duck
                return InterruptionAction.DUCK
            else:
                # Near end, ignore
                return InterruptionAction.IGNORE
        
        # Noise: always ignore
        if intent == InterruptionIntent.NOISE:
            return InterruptionAction.IGNORE
        
        # Stop command: cancel immediately
        if intent == InterruptionIntent.STOP_COMMAND:
            if confidence >= 0.7:
                return InterruptionAction.CANCEL
            else:
                # Low confidence, duck first
                return InterruptionAction.DUCK if is_partial else InterruptionAction.CANCEL
        
        # New question: cancel and prepare new turn
        if intent == InterruptionIntent.NEW_QUESTION:
            if confidence >= 0.75:
                return InterruptionAction.CANCEL_AND_NEW_TURN
            else:
                # Low confidence, duck first
                return InterruptionAction.DUCK if is_partial else InterruptionAction.CANCEL_AND_NEW_TURN
        
        # Uncertain: conservative approach
        if intent == InterruptionIntent.UNCERTAIN:
            if is_partial:
                # Partial result, just duck
                return InterruptionAction.DUCK
            elif len(classification.text) > 10:
                # Long text, likely real interruption
                return InterruptionAction.CANCEL_AND_NEW_TURN
            else:
                # Short text, duck or ignore
                return InterruptionAction.DUCK
        
        # Default: duck
        return InterruptionAction.DUCK
```

#### 步骤 2.3：集成到媒体会话

**修改文件**：`services/agent/src/voice_core/media_session_output_stream.py`

```python
from .interruption_policy import InterruptionPolicy, InterruptionAction

class MediaSessionOutputStream:
    
    async def on_vad_triggered(
        self,
        context: _MediaVoiceSession,
        partial_text: Optional[str] = None,
    ):
        """Handle VAD trigger during playback."""
        
        if not context.playback_active:
            # Not playing, normal VAD handling
            return
        
        # 1. Local duck immediately
        await self._duck_playback(context, amount=0.5)
        
        # 2. Wait for ASR partial (if not provided)
        if partial_text is None:
            partial_text = await self._wait_for_asr_partial(context, timeout=0.5)
        
        if not partial_text:
            # No text yet, keep ducked and wait
            return
        
        # 3. Classify interruption intent
        decision = await context.interruption_policy.evaluate_interruption(
            partial_text,
            is_partial=True,
            playback_progress=context.playback.get_progress(context.fence),
            context={},
        )
        
        logger.info(
            "Interruption decision: session=%s text=%r intent=%s action=%s confidence=%.2f",
            context.identity.session_id,
            partial_text,
            decision.classification.intent,
            decision.action,
            decision.classification.confidence,
        )
        
        # 4. Take action
        if decision.action == InterruptionAction.IGNORE:
            # Restore volume, continue
            await self._unduck_playback(context)
        
        elif decision.action == InterruptionAction.DUCK:
            # Keep ducked, wait for final ASR
            pass
        
        elif decision.should_cancel:
            # Cancel generation
            await self._cancel_reply_task(
                context,
                context.fence,
                reason=f"interrupted_{decision.classification.intent}",
            )
            
            if decision.action == InterruptionAction.CANCEL_AND_NEW_TURN:
                # Start new turn with this audio
                await self._start_new_turn_from_interruption(context, partial_text)
    
    async def on_asr_final(
        self,
        context: _MediaVoiceSession,
        final_text: str,
    ):
        """Handle ASR final result during interruption."""
        
        if not context.ducked:
            # Not in interruption flow
            return
        
        # Re-classify with final text
        decision = await context.interruption_policy.evaluate_interruption(
            final_text,
            is_partial=False,
            playback_progress=context.playback.get_progress(context.fence),
            context={},
        )
        
        logger.info(
            "Final interruption decision: session=%s text=%r intent=%s action=%s",
            context.identity.session_id,
            final_text,
            decision.classification.intent,
            decision.action,
        )
        
        # Take final action
        if decision.action == InterruptionAction.IGNORE:
            await self._unduck_playback(context)
        
        elif decision.should_cancel:
            await self._cancel_reply_task(
                context,
                context.fence,
                reason=f"interrupted_{decision.classification.intent}",
            )
            
            if decision.action == InterruptionAction.CANCEL_AND_NEW_TURN:
                await self._start_new_turn_from_interruption(context, final_text)
```

### 3. 配置与调优

#### Runtime Profile 配置

```json
{
  "device_id": "dev_xxx",
  "interruption_policy": {
    "allowed_barge_in": ["button", "local_keyword", "semantic"],
    "semantic_interruption": {
      "enabled": true,
      "use_llm_classifier": true,
      "confidence_threshold": 0.7,
      "backchannel_handling": "duck",  // duck | ignore
      "duck_volume": 0.5,
      "duck_duration_ms": 1000
    }
  }
}
```

## 验收标准

### 功能验收

✅ 分类器正确识别附和（"嗯"、"对"）→ 不打断  
✅ 分类器正确识别停止指令（"停一下"）→ 打断  
✅ 分类器正确识别新问题 → 打断并开始新轮次  
✅ 规则分类器响应快速（< 10ms）  
✅ LLM 分类器在规则不确定时启用  
✅ VAD 触发后自动 duck 音量  
✅ 确认打断后取消 generation  

### 性能验收

| 指标 | 目标 |
|---|---|
| 规则分类延迟 | < 10 ms |
| LLM 分类延迟 P95 | < 500 ms |
| 附和识别准确率 | ≥ 95% |
| 停止指令识别准确率 | ≥ 90% |
| 新问题识别准确率 | ≥ 85% |
| 误打断率（附和被误判） | < 5% |
| 总打断延迟 P95 | ≤ 700 ms |

### 真机验收矩阵

| 场景 | 预期分类 | 预期动作 |
|---|---|---|
| 播放时说"嗯" | BACKCHANNEL | DUCK or IGNORE |
| 播放时说"对对对" | BACKCHANNEL | DUCK or IGNORE |
| 播放时说"停一下" | STOP_COMMAND | CANCEL |
| 播放时说"等等" | STOP_COMMAND | CANCEL |
| 播放时问"那什么是...?" | NEW_QUESTION | CANCEL_AND_NEW_TURN |
| 播放时电视声音 | NOISE | IGNORE |
| 播放时回声 | NOISE | IGNORE |

## 监控指标

```python
# 添加指标
self.interruption_classification_total = Counter(
    "interruption_classification_total",
    "Total interruption classifications",
    ["intent", "action", "is_partial"],
)

self.interruption_classifier_latency = Histogram(
    "interruption_classifier_latency_ms",
    "Interruption classifier latency",
    ["classifier_type"],  # rule | llm
)

self.false_interruption_total = Counter(
    "false_interruption_total",
    "Total false interruptions (user feedback)",
)
```

## 下一步

完成 P1-3 后，继续：

- **P1-4**：AEC 声学矩阵完整验收
- **P2**：产品完整性功能

## 参考

- 整改方案第 8.4 节：语义打断判断
- P1-1：AEC Reference 对齐
- P1-2：本地停止词
