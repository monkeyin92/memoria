package mediaedge

import "testing"

func TestSessionRestoresActiveGenerationWithContinuedCoreClock(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "resume", AccountID: "a", DeviceID: "d", StreamEpoch: 2,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()
	fence := Fence{SessionID: "resume", TurnID: 3, GenerationID: 5, ToolEpoch: 1}
	if err := session.RestoreGeneration(fence, true); err != nil {
		t.Fatal(err)
	}
	continued := testFrame("resume", 2, 7, fence.GenerationID)
	continued.TurnID = fence.TurnID
	continued.ToolEpoch = fence.ToolEpoch
	continued.CaptureStartSample = 7 * continued.FrameSamples
	if err := session.AcceptDownlink(continued); err != nil {
		t.Fatalf("continued Core clock was rejected after reconnect: %v", err)
	}
	if err := session.RestoreGeneration(fence, true); err == nil {
		t.Fatal("reconnect snapshot was accepted after media had started")
	}
}

func TestSessionRestoresCancelledGenerationAsInactive(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "cancelled", AccountID: "a", DeviceID: "d", StreamEpoch: 2,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()
	fence := Fence{SessionID: "cancelled", TurnID: 3, GenerationID: 6, ToolEpoch: 1}
	if err := session.RestoreGeneration(fence, false); err != nil {
		t.Fatal(err)
	}
	frame := testFrame("cancelled", 2, 0, fence.GenerationID)
	frame.TurnID = fence.TurnID
	frame.ToolEpoch = fence.ToolEpoch
	if err := session.AcceptDownlink(frame); err != ErrStaleDownlinkGeneration {
		t.Fatalf("cancelled reconnect accepted audio: %v", err)
	}
}

// TestSessionDownlinkStrictContinuityBeforeLane proves the upstream gate the
// device lane relies on: within one generation the Session only admits
// strictly consecutive frames starting at sequence/sample zero, a forward
// gap is rejected, and AdvanceGeneration restarts the clock so generation 2
// begins again at zero on the same session (no reconnect).
func TestSessionDownlinkStrictContinuityBeforeLane(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "continuity", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()

	firstFence := Fence{SessionID: "continuity", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(firstFence); err != nil {
		t.Fatal(err)
	}
	first := testFrame("continuity", 1, 0, firstFence.GenerationID)
	first.TurnID = firstFence.TurnID
	if err := session.AcceptDownlink(first); err != nil {
		t.Fatalf("first generation frame at 0/0 was rejected: %v", err)
	}
	// A forward gap from sequence 0 to 2 must be rejected before the lane.
	gapped := testFrame("continuity", 1, 2, firstFence.GenerationID)
	gapped.TurnID = firstFence.TurnID
	if err := session.AcceptDownlink(gapped); err == nil {
		t.Fatal("downlink sequence gap was accepted by the session gate")
	}
	second := testFrame("continuity", 1, 1, firstFence.GenerationID)
	second.TurnID = firstFence.TurnID
	if err := session.AcceptDownlink(second); err != nil {
		t.Fatalf("consecutive frame was rejected: %v", err)
	}
	// Non-zero first frame of a generation is rejected (the sample clock
	// restarts at zero per generation).
	lateStart := testFrame("continuity", 1, 5, firstFence.GenerationID)
	lateStart.TurnID = firstFence.TurnID
	if err := session.AcceptDownlink(lateStart); err == nil {
		t.Fatal("generation restart at non-zero sequence was accepted")
	}

	// Generation 2 on the same session: the clock restarts at 0/0.
	secondFence := Fence{SessionID: "continuity", TurnID: 2, GenerationID: 2}
	if err := session.AdvanceGeneration(secondFence); err != nil {
		t.Fatal(err)
	}
	fresh := testFrame("continuity", 1, 0, secondFence.GenerationID)
	fresh.TurnID = secondFence.TurnID
	if err := session.AcceptDownlink(fresh); err != nil {
		t.Fatalf("generation 2 restart at 0/0 was rejected: %v", err)
	}
}

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
