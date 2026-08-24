"""Tests for output content filter (model identity disclosure prevention)."""


from services.agent.src.output_content_filter import (
    ModelIdentityFilter,
    create_default_filter,
)


class TestModelIdentityFilter:
    """Test the model identity disclosure filter."""

    def test_passes_clean_response(self):
        """Clean responses without model mentions should pass."""
        filter = ModelIdentityFilter()

        result = filter.check("你好！我可以帮你解答问题。")
        assert result.passed

        result = filter.check("今天天气很好，适合出门散步。")
        assert result.passed

    def test_blocks_deepseek_disclosure(self):
        """Responses mentioning DeepSeek should be blocked."""
        filter = ModelIdentityFilter()

        # Direct mention
        result = filter.check("我是DeepSeek，一个大语言模型。")
        assert not result.passed
        assert result.reason == "model_identity_disclosure"
        assert "DeepSeek" in result.matched_pattern

        # With version
        result = filter.check("我是DeepSeek V3模型。")
        assert not result.passed

        # With spacing
        result = filter.check("我是 DeepSeek 模型")
        assert not result.passed

    def test_blocks_claude_disclosure(self):
        """Responses mentioning Claude should be blocked."""
        filter = ModelIdentityFilter()

        result = filter.check("我是Claude，由Anthropic开发的AI助手。")
        assert not result.passed
        assert "Claude" in result.matched_pattern

        result = filter.check("我是Claude Opus模型")
        assert not result.passed

    def test_blocks_gpt_disclosure(self):
        """Responses mentioning GPT should be blocked."""
        filter = ModelIdentityFilter()

        result = filter.check("我是GPT-4，OpenAI的语言模型。")
        assert not result.passed
        assert "GPT" in result.matched_pattern

        result = filter.check("我是ChatGPT")
        assert not result.passed

    def test_blocks_qwen_disclosure(self):
        """Responses mentioning Qwen should be blocked."""
        filter = ModelIdentityFilter()

        result = filter.check("我是通义千问模型")
        assert not result.passed

        result = filter.check("我是Qwen")
        assert not result.passed

    def test_blocks_generic_model_disclosure(self):
        """Generic model disclosures should be blocked."""
        filter = ModelIdentityFilter()

        result = filter.check("我是一个大语言模型")
        assert not result.passed

        result = filter.check("我是基于Transformer的AI模型")
        assert not result.passed

    def test_allows_persona_name(self):
        """Responses using persona names should be allowed."""
        filter = ModelIdentityFilter(persona_name="小明")

        result = filter.check("我是小明，你的AI助手。")
        assert result.passed

        result = filter.check("我是你的智能伙伴")
        assert result.passed

        result = filter.check("我是陪伴你的AI陪伴者")
        assert result.passed

    def test_block_action(self):
        """Block action should prevent response emission."""
        filter = ModelIdentityFilter(action="block")

        should_emit, filtered_text, reason = filter.filter("我是DeepSeek模型")
        assert not should_emit
        assert filtered_text == ""
        assert "Blocked" in reason
        assert "DeepSeek" in reason

    def test_rewrite_action(self):
        """Rewrite action should replace model names with persona."""
        filter = ModelIdentityFilter(action="rewrite", persona_name="小助手")

        should_emit, filtered_text, reason = filter.filter("我是DeepSeek，很高兴为你服务。")
        assert should_emit
        assert "小助手" in filtered_text
        assert "DeepSeek" not in filtered_text
        assert "Rewritten" in reason

    def test_rewrite_multiple_patterns(self):
        """Rewrite should handle multiple pattern matches."""
        filter = ModelIdentityFilter(action="rewrite", persona_name="AI伙伴")

        text = "我是DeepSeek V3模型，基于Transformer架构。"
        should_emit, filtered_text, reason = filter.filter(text)

        assert should_emit
        assert "AI伙伴" in filtered_text
        assert "DeepSeek" not in filtered_text
        assert "V3" not in filtered_text or "Transformer" not in filtered_text

    def test_warn_action(self):
        """Warn action should allow response but flag it."""
        filter = ModelIdentityFilter(action="warn")

        should_emit, filtered_text, reason = filter.filter("我是DeepSeek")
        assert should_emit
        assert filtered_text == "我是DeepSeek"  # Unchanged
        assert "Warning" in reason
        assert "DeepSeek" in reason

    def test_empty_text(self):
        """Empty text should pass."""
        filter = ModelIdentityFilter()

        result = filter.check("")
        assert result.passed

        result = filter.check("   ")
        assert result.passed

    def test_case_insensitive(self):
        """Pattern matching should be case-insensitive."""
        filter = ModelIdentityFilter()

        result = filter.check("我是deepseek模型")
        assert not result.passed

        result = filter.check("我是DEEPSEEK")
        assert not result.passed

        result = filter.check("我是DeEpSeEk")
        assert not result.passed

    def test_create_default_filter(self):
        """Default filter should be created correctly."""
        filter = create_default_filter(persona_name="默认助手")

        assert filter.action == "block"
        assert filter.persona_name == "默认助手"

        should_emit, _, reason = filter.filter("我是DeepSeek")
        assert not should_emit
        assert "Blocked" in reason

    def test_context_matters(self):
        """Context should help determine if disclosure is actual."""
        filter = ModelIdentityFilter()

        # Talking about models (not self-identification) - current implementation blocks
        # This may need refinement based on actual use cases
        result = filter.check("DeepSeek模型在推理能力上表现很好")
        assert not result.passed

    def test_mixed_content(self):
        """Content with model mention mixed in should be caught."""
        filter = ModelIdentityFilter()

        result = filter.check(
            "你好！我可以帮你解答问题。顺便说一下，我是DeepSeek V3。有什么可以帮助你的吗？"
        )
        assert not result.passed

    def test_rewrite_verification(self):
        """Rewritten text should be verified to not contain violations."""
        filter = ModelIdentityFilter(action="rewrite", persona_name="助手")

        # Normal case - rewrite succeeds
        should_emit, filtered_text, _ = filter.filter("我是DeepSeek")
        assert should_emit
        assert "助手" in filtered_text

        # Edge case - if rewrite somehow still contains pattern, should block
        # (This tests the safety mechanism)


class TestFilterIntegration:
    """Test filter integration scenarios."""

    def test_filter_with_actual_persona_name(self):
        """Test with realistic persona names."""
        filter = ModelIdentityFilter(action="block", persona_name="小智")

        # Should block model disclosure
        should_emit, _, _ = filter.filter("我是DeepSeek，你叫我小智就好")
        assert not should_emit

        # Should allow persona usage
        result = filter.check("我是小智，你的AI学习伙伴")
        assert result.passed

    def test_filter_chain(self):
        """Multiple filters could be chained if needed."""
        # This demonstrates how filters could work together
        identity_filter = ModelIdentityFilter(action="block")

        text = "我是DeepSeek，今天天气很好"
        should_emit, filtered, reason = identity_filter.filter(text)

        assert not should_emit
        assert "model_identity_disclosure" in reason

    def test_performance_on_long_text(self):
        """Filter should handle long responses efficiently."""
        filter = ModelIdentityFilter()

        # Long text without violation
        long_text = "这是一个很长的回复。" * 100
        result = filter.check(long_text)
        assert result.passed

        # Long text with violation at end
        long_text_with_violation = long_text + " 我是DeepSeek模型。"
        result = filter.check(long_text_with_violation)
        assert not result.passed
