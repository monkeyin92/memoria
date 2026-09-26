from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2


def test_continuous_interaction_contract_round_trip_and_wire_vector() -> None:
    event = media_pb2.ContinuousInteractionEvent(
        event_id="e",
        session_id="s",
        stream_epoch=1,
        sequence=2,
        capture_start_sample=3,
        capture_end_sample=4,
        source_monotonic_ms=5,
        event_kind=media_pb2.CONTINUOUS_EVENT_KIND_ASR_FINAL,
        turn_id=6,
        generation_id=7,
        tool_epoch=8,
        task_epoch=9,
        context_version=10,
        speaker_evidence=media_pb2.SpeakerEvidence(
            speaker_class="owner",
            confidence=0.5,
            reason_code="verified",
            authority_verified=True,
        ),
        payload=b"x",
    )
    wire = event.SerializeToString()
    assert media_pb2.ContinuousInteractionEvent.FromString(wire) == event
    # The Go contract test constructs the same message and asserts this exact
    # wire vector, catching field-number or generated-binding divergence.
    assert wire.hex() == (
        "0a01651201731801200228033004380540054806500758086009680a"
        "72180a056f776e6572150000003f1a08766572696669656420017a0178"
    )


def test_session_authority_fields_are_additive_and_default_safe() -> None:
    old_hello = media_pb2.SessionHello(identity=media_pb2.SessionIdentity(session_id="old"))
    assert old_hello.interaction_authority == media_pb2.INTERACTION_AUTHORITY_UNSPECIFIED

    accepted = media_pb2.SessionAccepted(
        identity=media_pb2.SessionIdentity(session_id="new"),
        interaction_authority=media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
    )
    assert media_pb2.SessionAccepted.FromString(accepted.SerializeToString()) == accepted


def test_old_binding_ignores_and_preserves_new_session_authority_field() -> None:
    schema = descriptor_pb2.FileDescriptorProto(name="media_before_a0.proto", package="compat")
    identity = schema.message_type.add(name="SessionIdentity")
    identity.field.add(
        name="session_id",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    hello = schema.message_type.add(name="SessionHello")
    hello.field.add(
        name="identity",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".compat.SessionIdentity",
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    old_hello_type = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("compat.SessionHello")
    )

    new_hello = media_pb2.SessionHello(
        identity=media_pb2.SessionIdentity(session_id="compatible"),
        interaction_authority=media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
    )
    old_hello = old_hello_type.FromString(new_hello.SerializeToString())
    assert old_hello.identity.session_id == "compatible"

    # Python protobuf preserves unknown fields on parse/serialize, so an old
    # intermediary neither crashes nor silently rewrites the selected mode.
    recovered = media_pb2.SessionHello.FromString(old_hello.SerializeToString())
    assert recovered.interaction_authority == media_pb2.INTERACTION_AUTHORITY_GO_SHADOW
