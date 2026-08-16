package mediaedge

import (
	"encoding/hex"
	"testing"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/protobuf/proto"
)

func TestContinuousInteractionContractMatchesPythonWireVector(t *testing.T) {
	event := &mediav1.ContinuousInteractionEvent{
		EventId:            "e",
		SessionId:          "s",
		StreamEpoch:        1,
		Sequence:           2,
		CaptureStartSample: 3,
		CaptureEndSample:   4,
		SourceMonotonicMs:  5,
		EventKind:          mediav1.ContinuousEventKind_CONTINUOUS_EVENT_KIND_ASR_FINAL,
		TurnId:             6,
		GenerationId:       7,
		ToolEpoch:          8,
		TaskEpoch:          9,
		ContextVersion:     10,
		SpeakerEvidence: &mediav1.SpeakerEvidence{
			SpeakerClass:      "owner",
			Confidence:        0.5,
			ReasonCode:        "verified",
			AuthorityVerified: true,
		},
		Payload: []byte("x"),
	}
	wire, err := proto.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	const pythonWire = "0a01651201731801200228033004380540054806500758086009680a72180a056f776e6572150000003f1a08766572696669656420017a0178"
	if hex.EncodeToString(wire) != pythonWire {
		t.Fatalf("Python/Go media-v1 wire vector differs: %x", wire)
	}
	var decoded mediav1.ContinuousInteractionEvent
	if err := proto.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(event, &decoded) {
		t.Fatal("continuous event did not round trip")
	}
}

func TestOldSessionAuthorityDefaultsToPythonCompatibleUnspecified(t *testing.T) {
	hello := &mediav1.SessionHello{Identity: &mediav1.SessionIdentity{SessionId: "old"}}
	if hello.GetInteractionAuthority() != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_UNSPECIFIED {
		t.Fatalf("old hello selected a new authority: %v", hello.GetInteractionAuthority())
	}
}

func TestDeviceSessionIdentityAuthorityFenceMatchesPythonWire(t *testing.T) {
	identity := &mediav1.SessionIdentity{
		SessionId: "s", AccountId: "a", DeviceId: "d", ClientType: "device",
		StreamEpoch: 2, SubjectId: "subject", BindingId: "binding",
		BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	wire, err := proto.Marshal(identity)
	if err != nil {
		t.Fatal(err)
	}
	var decoded mediav1.SessionIdentity
	if err := proto.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(identity, &decoded) || decoded.GetRuntimeProfileVersion() != 27 {
		t.Fatalf("device authority fence did not round trip: %s", decoded.String())
	}
}
