"""Output content filter to prevent model identity disclosure.

This filter enforces the Persona rule: the assistant must not disclose which
specific AI model it is (e.g., "我是 DeepSeek", "我是 Claude", "我是 GPT").
The assistant should present itself using its assigned persona name instead.

Reference: P0-4 requirement from the整改方案.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Model identity patterns that should be blocked
MODEL_IDENTITY_PATTERNS = [
    # DeepSeek variants
    re.compile(r"我是\s*DeepSeek", re.IGNORECASE),
    re.compile(r"DeepSeek\s*模型", re.IGNORECASE),
    re.compile(r"DeepSeek\s*V\d+", re.IGNORECASE),
    re.compile(r"DeepSeek-V\d+", re.IGNORECASE),

    # Claude variants
    re.compile(r"我是\s*Claude", re.IGNORECASE),
    re.compile(r"Claude\s*模型", re.IGNORECASE),
    re.compile(r"Claude\s*(Opus|Sonnet|Haiku)", re.IGNORECASE),
    re.compile(r"Anthropic\s*的\s*Claude", re.IGNORECASE),

    # GPT variants
    re.compile(r"我是\s*GPT", re.IGNORECASE),
    re.compile(r"GPT-\d+", re.IGNORECASE),
    re.compile(r"ChatGPT", re.IGNORECASE),
    re.compile(r"OpenAI\s*的\s*GPT", re.IGNORECASE),

    # Qwen variants
    re.compile(r"我是\s*通义千问", re.IGNORECASE),
    re.compile(r"我是\s*Qwen", re.IGNORECASE),
    re.compile(r"Qwen\s*模型", re.IGNORECASE),

    # Generic model disclosure
    re.compile(r"我是.*?大语言模型", re.IGNORECASE),
    re.compile(r"我是.*?AI\s*模型", re.IGNORECASE),
    re.compile(r"我是.*?人工智能模型", re.IGNORECASE),
    re.compile(r"基于.*?模型", re.IGNORECASE),
]

# Safe alternatives that should be allowed
ALLOWED_PATTERNS = [
    re.compile(r"我是.*?(助手|伙伴|陪伴者)"),  # "我是你的助手"
    re.compile(r"我是\s*[^A-Za-z]{2,10}"),  # "我是小明" (persona name)
]


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Result of content filtering."""

    passed: bool
    reason: str = ""
    matched_pattern: str = ""
    suggestion: str = ""


FilterAction = Literal["block", "rewrite", "warn"]


class ModelIdentityFilter:
    """Filter to prevent model identity disclosure in assistant responses."""

    def __init__(
        self,
        *,
        action: FilterAction = "block",
        persona_name: str | None = None,
    ) -> None:
        """Initialize the filter.

        Args:
            action: What to do when a violation is detected:
                - "block": Refuse to generate the response
                - "rewrite": Attempt to rewrite the response
                - "warn": Log a warning but allow the response
            persona_name: The persona name to use in rewrites
        """
        self.action = action
        self.persona_name = persona_name or "AI助手"

    def check(self, text: str) -> FilterResult:
        """Check if text contains model identity disclosure.

        Args:
            text: The assistant response text to check

        Returns:
            FilterResult indicating whether the text passed the filter
        """
        if not text or not text.strip():
            return FilterResult(passed=True)

        # Check if any disallowed pattern matches
        for pattern in MODEL_IDENTITY_PATTERNS:
            match = pattern.search(text)
            if match:
                # Check if it's in an allowed context
                for allowed in ALLOWED_PATTERNS:
                    if allowed.search(text):
                        # False positive - actually referring to persona
                        continue

                return FilterResult(
                    passed=False,
                    reason="model_identity_disclosure",
                    matched_pattern=match.group(0),
                    suggestion=f"请使用 '{self.persona_name}' 而不是具体的模型名称",
                )

        return FilterResult(passed=True)

    def filter(self, text: str) -> tuple[bool, str, str]:
        """Filter the text and take action based on the configured mode.

        Args:
            text: The assistant response text to filter

        Returns:
            Tuple of (should_emit, filtered_text, reason)
            - should_emit: Whether the response should be sent to the user
            - filtered_text: The (possibly rewritten) text
            - reason: Why the text was filtered/rewritten
        """
        result = self.check(text)

        if result.passed:
            return (True, text, "")

        if self.action == "block":
            return (
                False,
                "",
                f"Blocked due to {result.reason}: {result.matched_pattern}",
            )

        if self.action == "rewrite":
            rewritten = self._rewrite(text, result)
            # Verify the rewrite doesn't still contain violations
            recheck = self.check(rewritten)
            if recheck.passed:
                return (True, rewritten, f"Rewritten to remove {result.matched_pattern}")
            # If rewrite still fails, fall back to blocking
            return (
                False,
                "",
                f"Rewrite failed verification: {recheck.matched_pattern}",
            )

        if self.action == "warn":
            # Allow but log warning
            return (
                True,
                text,
                f"Warning: {result.reason} - {result.matched_pattern}",
            )

        # Default to blocking
        return (False, "", f"Unknown action {self.action}")

    def _rewrite(self, text: str, result: FilterResult) -> str:
        """Attempt to rewrite text to remove model identity disclosure.

        This is a simple replacement strategy. More sophisticated rewrites
        could use a separate LLM call or prompt engineering.

        Args:
            text: Original text
            result: The filter result with matched pattern

        Returns:
            Rewritten text
        """
        rewritten = text

        # Replace specific model mentions with persona name
        for pattern in MODEL_IDENTITY_PATTERNS:
            rewritten = pattern.sub(f"我是{self.persona_name}", rewritten, count=1)

        # Clean up any remaining patterns
        rewritten = re.sub(
            r"(DeepSeek|Claude|GPT-?\d*|Qwen|ChatGPT|通义千问)[\s模型]*",
            self.persona_name,
            rewritten,
            flags=re.IGNORECASE,
        )

        return rewritten


def create_default_filter(persona_name: str | None = None) -> ModelIdentityFilter:
    """Create a default model identity filter.

    Args:
        persona_name: Optional persona name to use in rewrites

    Returns:
        Configured ModelIdentityFilter
    """
    return ModelIdentityFilter(
        action="block",  # Default to blocking for safety
        persona_name=persona_name,
    )


__all__ = [
    "ModelIdentityFilter",
    "FilterResult",
    "FilterAction",
    "create_default_filter",
]
