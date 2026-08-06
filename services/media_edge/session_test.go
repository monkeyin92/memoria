package mediaedge

import "testing"

func TestSessionPlayoutBufferRestartsAtZeroAfterGenerationChange(t *testing.T) {
	for _, test := range []struct {
		name       string
		transition func(*testing.T, *Session, Fence) Fence
	}{
		{
			name: "advance",
			transition: func(t *testing.T, session *Session, _ Fence) Fence {
				next := Fence{SessionID: "playout", TurnID: 2}
				if err := session.AdvanceGeneration(next); err != nil {
					t.Fatal(err)
				}
				return next
			},
		},
		{
			name: "cancel then advance",
			transition: func(t *testing.T, session *Session, current Fence) Fence {
				if _, _, _, err := session.CancelGeneration("stop", &current); err != nil {
					t.Fatal(err)
				}
				next := Fence{SessionID: "playout", TurnID: 2}
				if err := session.AdvanceGeneration(next); err != nil {
					t.Fatal(err)
				}
				return next
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			session, err := NewSession(OpenSessionRequest{
				SessionID: "playout", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
			}, 2)
			if err != nil {
				t.Fatal(err)
			}
			defer session.Stop()

			current := Fence{SessionID: "playout", TurnID: 1, GenerationID: 1}
			if err := session.AdvanceGeneration(current); err != nil {
				t.Fatal(err)
			}
			old := testFrame("playout", 1, 0, current.GenerationID)
			old.TurnID = current.TurnID
			if err := session.AcceptDownlink(old); err != nil {
				t.Fatal(err)
			}
			session.MirrorPlayback(old.FrameSamples, current)

			next := test.transition(t, session, current)
			fresh := testFrame("playout", 1, 0, next.GenerationID)
			fresh.TurnID = next.TurnID
			if err := session.AcceptDownlink(fresh); err != nil {
				t.Fatal(err)
			}

			if got, want := session.Stats().PlayoutBufferMS, 20.0/3.0; got != want {
				t.Fatalf("playout buffer = %vms, want %vms", got, want)
			}
		})
	}
}

func TestSessionCountsRealPlayoutUnderrunTransitions(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "underrun", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()

	fence := Fence{SessionID: "underrun", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	first := testFrame("underrun", 1, 0, fence.GenerationID)
	first.TurnID = fence.TurnID
	if err := session.AcceptDownlink(first); err != nil {
		t.Fatal(err)
	}
	session.MirrorPlayback(first.FrameSamples, fence)
	session.MirrorPlayback(first.FrameSamples, fence)
	if got := session.Stats().PlayoutUnderruns; got != 1 {
		t.Fatalf("repeated empty-buffer ACK counted %d underruns, want 1", got)
	}

	second := testFrame("underrun", 1, 1, fence.GenerationID)
	second.TurnID = fence.TurnID
	if err := session.AcceptDownlink(second); err != nil {
		t.Fatal(err)
	}
	session.MirrorPlayback(second.CaptureStartSample+second.FrameSamples, fence)
	if got := session.Stats().PlayoutUnderruns; got != 2 {
		t.Fatalf("buffer refill did not re-arm underrun detection: got %d", got)
	}
}

func TestSessionDoesNotCountCompletedFinalFrameAsPlayoutUnderrun(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "complete", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()

	fence := Fence{SessionID: "complete", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	final := testFrame("complete", 1, 0, fence.GenerationID)
	final.TurnID = fence.TurnID
	final.Final = true
	if err := session.AcceptDownlink(final); err != nil {
		t.Fatal(err)
	}
	session.MirrorPlayback(final.FrameSamples, fence)

	if got := session.Stats().PlayoutUnderruns; got != 0 {
		t.Fatalf("natural final playout counted %d underruns", got)
	}
}
