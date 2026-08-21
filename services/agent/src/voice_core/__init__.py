"""Provider-neutral contracts for the Memoria media and voice boundary.

The package intentionally contains only standard-library data models.  It can
be used by the existing LiveKit path while the future media edge is built.
"""

from .adaptive_vad import AdaptiveEnergyVAD, AdaptiveVADConfig, VADEngine, VADEvent
from .asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from .device_client import (
    LinuxMediaDeviceClient,
    MediaDeviceConfig,
    MediaDeviceTLS,
)
from .device_protocol import (
    DEVICE_EVENT_TYPES,
    DEVICE_TOPICS,
    DeviceCommand,
    DeviceCommandAck,
    DeviceEvent,
)
from .device_runtime import (
    AudioDeviceConfig,
    DevicePcmFrame,
    LinuxAudioPipeline,
    NLMSAcousticEchoCanceller,
)
from .device_security import (
    DeviceIdentity,
    OtaManifest,
    ProvisionedDevice,
    SignedChallenge,
    provision_device,
    sign_challenge,
    sign_ota_manifest,
    verify_artifact_digest,
    verify_challenge,
    verify_ota_manifest,
)
from .generation_controller import GenerationController, ResponseLease
from .grpc_bridge import (
    AudioFrameHandler,
    ClientEventHandler,
    MediaBridgeGrpcServer,
    MediaBridgeTLS,
    PlaybackProgressHandler,
    SessionClosedHandler,
    SpeechSegmentHandler,
)
from .keyword_spotter import (
    ControlKeywordSpotter,
    KeywordHit,
    KeywordSpotter,
)
from .media_bridge_server import MediaBridgeServer, MediaBridgeSession, PCMFrame
from .media_protocol import (
    AudioEncoding,
    AudioFormat,
    AudioFrame,
    MediaEnvelope,
    PlaybackProgress,
    SessionIdentity,
)
from .ota import OtaSlot, OtaUpdateManager
from .playback_ledger import PlaybackLedger, PlaybackSpan
from .replay_harness import (
    AudioReplayHarness,
    ChaosEvent,
    ChaosReport,
    ChaosRunner,
    FixtureMetadata,
    LoadReport,
    LoadScenario,
    ReplayResult,
    SyntheticFixture,
    estimate_load,
)
from .reply_delivery import (
    ReplyDelivery,
    ReplyDeliveryEvent,
    ReplyDeliveryKey,
    ReplyDeliveryLedger,
)
from .slo import MediaSLO, SLOReport, evaluate_slo
from .slo_reporter import MediaSLOReporter, MediaSLOReporterConfig
from .speech_timeline import (
    ASRLogicalVersion,
    ASRResult,
    ASRTimingCoverage,
    ASRTimingEvidence,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
    asr_result_to_segment,
)
from .telemetry import (
    MEDIA_METRIC_NAMES,
    MEDIA_SPAN_NAMES,
    MediaTelemetry,
    TraceContext,
    TraceEvent,
    TurnTimeline,
)

__all__ = [
    "ASRResult",
    "ASRAcceptDecision",
    "ASRDecisionReason",
    "ASRLogicalVersion",
    "ASRTimingCoverage",
    "ASRTimingEvidence",
    "ASRWordTiming",
    "ASRStreamSupervisor",
    "DEVICE_TOPICS",
    "DeviceCommand",
    "DeviceCommandAck",
    "DeviceEvent",
    "DEVICE_EVENT_TYPES",
    "LinuxMediaDeviceClient",
    "MediaDeviceConfig",
    "MediaDeviceTLS",
    "AdaptiveEnergyVAD",
    "AdaptiveVADConfig",
    "AudioDeviceConfig",
    "AudioReplayHarness",
    "AudioEncoding",
    "AudioFrame",
    "AudioFormat",
    "ChaosEvent",
    "ChaosReport",
    "ChaosRunner",
    "GenerationController",
    "MediaBridgeGrpcServer",
    "MediaBridgeTLS",
    "AudioFrameHandler",
    "ClientEventHandler",
    "SessionClosedHandler",
    "PlaybackProgressHandler",
    "SpeechSegmentHandler",
    "ControlKeywordSpotter",
    "KeywordHit",
    "KeywordSpotter",
    "DeviceIdentity",
    "DevicePcmFrame",
    "LinuxAudioPipeline",
    "LoadReport",
    "LoadScenario",
    "MEDIA_METRIC_NAMES",
    "MEDIA_SPAN_NAMES",
    "MediaEnvelope",
    "MediaBridgeServer",
    "MediaBridgeSession",
    "MediaTelemetry",
    "MediaSLO",
    "NLMSAcousticEchoCanceller",
    "OtaManifest",
    "OtaSlot",
    "OtaUpdateManager",
    "PCMFrame",
    "PlaybackLedger",
    "PlaybackProgress",
    "PlaybackSpan",
    "ReplyDelivery",
    "ReplyDeliveryEvent",
    "ReplyDeliveryKey",
    "ReplyDeliveryLedger",
    "ResponseLease",
    "ProvisionedDevice",
    "ReplayResult",
    "SegmentKind",
    "SessionIdentity",
    "SignedChallenge",
    "SpeechSegment",
    "SpeechTimeline",
    "SLOReport",
    "SyntheticFixture",
    "TraceContext",
    "TraceEvent",
    "TurnTimeline",
    "VADEngine",
    "VADEvent",
    "asr_result_to_segment",
    "FixtureMetadata",
    "estimate_load",
    "provision_device",
    "sign_challenge",
    "sign_ota_manifest",
    "verify_artifact_digest",
    "verify_challenge",
    "verify_ota_manifest",
    "evaluate_slo",
    "MediaSLOReporter",
    "MediaSLOReporterConfig",
]
