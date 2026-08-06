from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.interaction_authority import (
    InteractionAuthority,
    InteractionAuthorityState,
    InteractionRuntime,
    can_execute_realtime_effect,
    interaction_authority_from_proto,
    interaction_authority_to_proto,
)


def test_unspecified_and_future_authority_values_fail_closed_to_python() -> None:
    assert interaction_authority_from_proto(0) is InteractionAuthority.PYTHON_AUTHORITATIVE
    assert interaction_authority_from_proto(99) is InteractionAuthority.PYTHON_AUTHORITATIVE
    assert (
        interaction_authority_to_proto(InteractionAuthority.PYTHON_AUTHORITATIVE)
        == media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
    )


def test_authority_requires_shadow_parity_and_always_supports_rollback() -> None:
    python = InteractionAuthorityState()
    shadow = python.transition(InteractionAuthority.GO_SHADOW)

    try:
        python.transition(InteractionAuthority.GO_AUTHORITATIVE)
    except ValueError:
        pass
    else:
        raise AssertionError("direct Python-to-Go promotion bypassed shadow")

    try:
        shadow.transition(InteractionAuthority.GO_AUTHORITATIVE)
    except ValueError:
        pass
    else:
        raise AssertionError("Go promotion bypassed parity")

    authoritative = shadow.transition(
        InteractionAuthority.GO_AUTHORITATIVE,
        shadow_parity_met=True,
    )
    assert authoritative.transition(InteractionAuthority.GO_SHADOW) == shadow
    assert authoritative.transition(InteractionAuthority.PYTHON_AUTHORITATIVE) == python


def test_go_shadow_can_compute_but_cannot_execute_effects() -> None:
    assert can_execute_realtime_effect(
        InteractionAuthority.GO_SHADOW,
        producer=InteractionRuntime.PYTHON,
        candidate_only=False,
    )
    assert not can_execute_realtime_effect(
        InteractionAuthority.GO_SHADOW,
        producer=InteractionRuntime.GO,
        candidate_only=True,
    )
    assert not can_execute_realtime_effect(
        InteractionAuthority.GO_SHADOW,
        producer=InteractionRuntime.GO,
        candidate_only=False,
    )
    assert can_execute_realtime_effect(
        InteractionAuthority.GO_AUTHORITATIVE,
        producer=InteractionRuntime.GO,
        candidate_only=False,
    )


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
    assert (
        interaction_authority_from_proto(old_hello.interaction_authority)
        is InteractionAuthority.PYTHON_AUTHORITATIVE
    )

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
