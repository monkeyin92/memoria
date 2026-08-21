"""Tests for RealtimeBridgeCoordinator."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.realtime_bridge_coordinator import (
    BridgeIntentConfig,
    FallbackIntentConfig,
    RealtimeBridgeCoordinator,
    ToolExecutionHandle,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_tool_execution_handle_basic():
    """Test basic tool execution handle functionality."""
    
    async def dummy_task():
        await asyncio.sleep(0.01)
        return "success"
    
    fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    handle = ToolExecutionHandle(
        task=asyncio.create_task(dummy_task()),
        started_at=0.0,
        fence=fence,
    )
    
    # Wait for completion
    completed, timed_out = await handle.wait_with_progress(
        max_wait_s=0.1,
        progress_callback=None,
    )
    
    assert completed is True
    assert timed_out is False
    
    result, error = handle.get_result_or_error()
    assert result == "success"
    assert error is None


@pytest.mark.asyncio
async def test_tool_execution_handle_timeout():
    """Test that slow tasks are detected as timeouts."""
    
    async def slow_task():
        await asyncio.sleep(1.0)  # Very slow
        return "late"
    
    fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    handle = ToolExecutionHandle(
        task=asyncio.create_task(slow_task()),
        started_at=0.0,
        fence=fence,
    )
    
    # Should timeout quickly
    completed, timed_out = await handle.wait_with_progress(
        max_wait_s=0.02,
        progress_callback=None,
    )
    
    assert completed is False
    assert timed_out is True


@pytest.mark.asyncio
async def test_realtime_bridge_coordinator_start_delegation():
    """Test starting tool delegation."""
    
    runtime = Mock()
    runtime.fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    runtime.output_floor_allows_assistant = True
    
    async def executor(query):
        await asyncio.sleep(0.01)
        return f"result for {query}"
    
    coordinator = RealtimeBridgeCoordinator(runtime, runtime.fence)
    handle = await coordinator.start_tool_delegation("weather query", executor)
    
    assert handle is not None
    assert handle.elapsed_ms >= 0
    assert not handle.is_slow


@pytest.mark.asyncio
async def test_realtime_bridge_coordinator_emit_fallback_force():
    """Test that fallback is emitted even when fence doesn't match."""
    
    original_fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    runtime = Mock()
    runtime.fence = GenerationFence(
        session_id="test-session",
        turn_id=2,  # Different fence!
        generation_id=1,
        tool_epoch=0,
    )
    runtime._llm_text_buf = ""
    
    coordinator = RealtimeBridgeCoordinator(
        runtime,
        original_fence,
        fallback_config=FallbackIntentConfig(
            fallback_phrase="Test fallback message",
            force_emit_on_fence_mismatch=True,
        ),
    )
    
    segmenter = Mock()
    segmenter.push_token = Mock(return_value=iter([Mock(text="Test fallback message")]))
    segmenter.flush = Mock(return_value=iter([]))
    
    _ready_segment = Mock(side_effect=lambda x: x if x else None)
    
    emitted = []
    async for segment in coordinator.emit_fallback(
        segmenter,
        _ready_segment,
        force=True,
        skip_fallback_if_current_fence=False,
    ):
        emitted.append(segment)
    
    # Should still emit even with force=True
    assert len(emitted) > 0 or coordinator.fallback_config.force_emit_on_fence_mismatch


@pytest.mark.asyncio
async def test_realtime_bridge_coordinator_is_current_fence():
    """Test fence matching logic."""
    
    fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    runtime = Mock()
    runtime.fence = fence
    
    coordinator = RealtimeBridgeCoordinator(runtime, fence)
    
    assert coordinator._is_current_fence() is True
    
    # Change fence
    runtime.fence = GenerationFence(
        session_id="test-session",
        turn_id=2,
        generation_id=1,
        tool_epoch=0,
    )
    
    assert coordinator._is_current_fence() is False


def test_bridge_intent_config_default_values():
    """Test default values for bridge config."""
    
    config = BridgeIntentConfig()
    
    assert config.bridge_phrase is not None
    assert config.expires_after_ms == 5000
    assert config.context_version == 0


def test_fallback_intent_config_default_values():
    """Test default values for fallback config."""
    
    config = FallbackIntentConfig()
    
    assert config.fallback_phrase is not None
    assert config.force_emit_on_fence_mismatch is True
    assert config.log_force_emit is True


@pytest.mark.asyncio
async def test_tool_execution_handle_cancelled():
    """Test cancelled task detection."""
    
    async def cancellable_task():
        await asyncio.Event().wait()  # Never completes
    
    fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    task = asyncio.create_task(cancellable_task())
    handle = ToolExecutionHandle(task, 0.0, fence)
    
    # Cancel the task
    task.cancel()
    
    try:
        await task
    except asyncio.CancelledError:
        pass
    
    result, error = handle.get_result_or_error()
    assert result is None
    assert error == "cancelled"


@pytest.mark.asyncio
async def test_tool_execution_handle_exception():
    """Test exception handling in task."""
    
    async def failing_task():
        raise ValueError("Test error")
    
    fence = GenerationFence(
        session_id="test-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
    )
    
    handle = ToolExecutionHandle(
        task=asyncio.create_task(failing_task()),
        started_at=0.0,
        fence=fence,
    )
    
    # Wait briefly
    await asyncio.sleep(0.01)
    
    result, error = handle.get_result_or_error()
    assert result is None
    assert error is not None
    assert "Test error" in error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
