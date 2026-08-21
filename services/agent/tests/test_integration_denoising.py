"""Integration test for denoising and state management in MediaAudioIngress."""

import asyncio
import struct
import math
from unittest.mock import Mock, AsyncMock

from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngress
from services.agent.src.voice_core.media_protocol import AudioFrame, AudioFrameIdentity


def generate_test_audio(duration_ms: int = 100, frequency: int = 440, sample_rate: int = 16000) -> bytes:
    """Generate test audio PCM data."""
    num_samples = sample_rate * duration_ms // 1000
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * frequency * t))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


async def test_integration():
    """Test that MediaAudioIngress integrates denoising and state management."""

    print("Testing MediaAudioIngress integration...\n")

    # Create mock host
    mock_host = Mock()
    mock_host.metrics = Mock()
    mock_host.metrics.set_media_metric = Mock()
    mock_host.metrics.inc_media_metric = Mock()

    # Create ingress
    ingress = MediaAudioIngress(mock_host)

    # Test 1: Check initialization
    print("✓ Test 1: Initialization")
    assert ingress._denoiser is not None
    assert ingress._state_manager is not None
    print(f"  Denoiser Stage 1: {'available' if ingress._denoiser._stage1._rnnoise_available else 'fallback'}")
    print(f"  Denoiser Stage 2: {'available' if ingress._denoiser._stage2._model_available else 'unavailable'}")

    # Test 2: Check listening state management
    print("\n✓ Test 2: Listening state management")
    session_id = "test-session-001"

    initial_state = ingress.get_listening_state(session_id)
    print(f"  Initial state: {initial_state.value}")
    assert initial_state.value == "idle"

    # Test 3: Start listening
    print("\n✓ Test 3: Start listening")
    await ingress.start_listening(session_id)
    state = ingress.get_listening_state(session_id)
    print(f"  After start_listening: {state.value}")
    assert state.value == "listening"

    # Test 4: Stop listening
    print("\n✓ Test 4: Stop listening")
    await ingress.stop_listening(session_id, "test")
    state = ingress.get_listening_state(session_id)
    print(f"  After stop_listening: {state.value}")
    assert state.value == "idle"

    # Test 5: Auto-enter listening on session init
    print("\n✓ Test 5: Auto-enter listening")
    session_id2 = "test-session-002"
    await ingress.initialize_session_listening(session_id2)
    state = ingress.get_listening_state(session_id2)
    print(f"  After initialize_session_listening: {state.value}")
    assert state.value == "listening"

    # Test 6: Cleanup
    print("\n✓ Test 6: Session cleanup")
    ingress._state_manager.cleanup_session(session_id)
    ingress._state_manager.cleanup_session(session_id2)
    print(f"  Sessions cleaned up")

    # Test 7: Check denoising stats
    print("\n✓ Test 7: Denoising pipeline")
    test_audio = generate_test_audio(100, 440)
    denoised, vad_prob, stats = ingress._denoiser.process(test_audio)
    print(f"  Processed audio: {len(test_audio)} -> {len(denoised)} bytes")
    print(f"  VAD probability: {vad_prob:.2f}")
    print(f"  Stage 2 applied: {stats['stage2_applied']}")

    print("\n" + "="*50)
    print("✅ All integration tests passed!")
    print("="*50)

    print("\nNext steps:")
    print("1. Install dependencies: pip install rnnoise-python onnxruntime")
    print("2. Download DTLN model to services/agent/models/dtln_16k.onnx")
    print("3. Restart the agent service")
    print("4. Connect ESP32 board and test with real audio")


if __name__ == "__main__":
    asyncio.run(test_integration())
