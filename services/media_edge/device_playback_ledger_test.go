package mediaedge

import (
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func TestDevicePlaybackLedgerUsesServerSendClockAndKeepsFencesIndependent(
	t *testing.T,
) {
	base := time.Unix(2_000_000_000, 0)
	now := base
	var observedLag int64 = -1
	ledger := newDevicePlaybackLedger(16_000, func(lag int64) {
		observedLag = lag
	})
	ledger.now = func() time.Time { return now }
	firstFence := deviceFence{TurnID: 1, GenerationID: 1, SessionEpoch: 7}
	secondFence := deviceFence{TurnID: 2, GenerationID: 2}
	ledger.startTransportFence(firstFence, false)
	ledger.recordSent(firstFence, 7, 320)
	now = base.Add(45 * time.Millisecond)
	progress, ok := ledger.record(devicePlaybackReceipt{
		Fence: firstFence, ReceivedSequence: 7, RenderedSampleEnd: 320,
	}, 9_999_999_999)
	if !ok || observedLag != 45 {
		t.Fatalf("receipt ok=%v lag=%d, want true/45", ok, observedLag)
	}
	if progress.RenderedSampleEnd != 480 || progress.ClientMonotonicMS != 9_999_999_999 {
		t.Fatalf("playback projection mismatch: %+v", progress)
	}
	if progress.SessionEpoch != 7 {
		t.Fatalf("playback session epoch = %d, want 7", progress.SessionEpoch)
	}
	// Sequence/sample clocks may restart on a replacement fence.
	ledger.startTransportFence(secondFence, false)
	ledger.recordSent(secondFence, 0, 320)
	if _, ok := ledger.record(devicePlaybackReceipt{
		Fence: secondFence, ReceivedSequence: 0, RenderedSampleEnd: 160,
	}, 1); !ok {
		t.Fatal("replacement generation receipt was rejected")
	}
	// Progress may advance while the highest received frame stays unchanged.
	if _, ok := ledger.record(devicePlaybackReceipt{
		Fence: secondFence, ReceivedSequence: 0, RenderedSampleEnd: 240,
	}, 2); !ok {
		t.Fatal("same-sequence render progress was rejected")
	}
	if _, ok := ledger.record(devicePlaybackReceipt{
		Fence: secondFence, ReceivedSequence: 0, RenderedSampleEnd: 200,
	}, 3); ok {
		t.Fatal("rendered sample regression was accepted")
	}
	if _, ok := ledger.record(devicePlaybackReceipt{
		Fence:            deviceFence{TurnID: 3, GenerationID: 3},
		ReceivedSequence: 1, RenderedSampleEnd: 321,
	}, 4); ok {
		t.Fatal("unaligned 16 kHz watermark was accepted")
	}
}

func TestResumedTransportRebasesWireClockAndMapsReceiptBackToCore(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	fence := deviceFence{TurnID: 3, GenerationID: 5, ToolEpoch: 1}
	connection := &DeviceConnection{
		lane:   lane,
		ledger: newDevicePlaybackLedger(16_000, nil),
	}
	connection.beginDownlinkClock(fence, true)
	wireSequence, wireSample, ok := connection.projectDownlinkClock(fence, 7, 3_360)
	if !ok || wireSequence != 0 || wireSample != 0 {
		t.Fatalf("wire rebase = %d/%d ok=%v, want 0/0 true", wireSequence, wireSample, ok)
	}
	connection.ledger.recordSent(fence, wireSequence, 320)
	progress, ok := connection.ledger.record(devicePlaybackReceipt{
		Fence: fence, ReceivedSequence: 0, RenderedSampleEnd: 320,
	}, 42)
	if !ok {
		t.Fatal("resumed receipt was rejected")
	}
	if progress.ReceivedSequence != 7 || progress.RenderedSampleEnd != 3_840 {
		t.Fatalf("Core mapping mismatch: %+v", progress)
	}
	if !progress.Approximate {
		t.Fatal("resumed transport receipt was not conservatively downgraded")
	}
}

func TestDevicePlaybackLedgerRejectsProgressAfterTerminalReceipt(t *testing.T) {
	ledger := newDevicePlaybackLedger(24_000, nil)
	fence := deviceFence{TurnID: 4, GenerationID: 7}
	ledger.startTransportFence(fence, false)
	ledger.recordSent(fence, 0, 480)
	progress, ok := ledger.record(devicePlaybackReceipt{
		deviceEventBase:   deviceEventBase{Type: "playback.ended"},
		Fence:             fence,
		ReceivedSequence:  0,
		RenderedSampleEnd: 480,
	}, 1)
	if !ok {
		t.Fatal("terminal playback receipt was rejected")
	}
	if progress.EventType != mediav1.PlaybackEventType_PLAYBACK_EVENT_TYPE_ENDED {
		t.Fatalf("terminal event type = %s, want ENDED", progress.EventType)
	}
	ledger.recordSent(fence, 1, 960)
	if _, ok := ledger.record(devicePlaybackReceipt{
		deviceEventBase:   deviceEventBase{Type: "playback.progress"},
		Fence:             fence,
		ReceivedSequence:  1,
		RenderedSampleEnd: 960,
	}, 2); ok {
		t.Fatal("playback progress extended a terminal generation")
	}
}
