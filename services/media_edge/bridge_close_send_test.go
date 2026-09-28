package mediaedge

// grpc-go forbids CloseSend concurrently with SendMsg on the same stream.
// Close must cancel the stream context first (which unblocks an in-flight
// SendMsg) and then close the send side under sendMu.

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

type overlapDetectingStream struct {
	grpc.BidiStreamingClient[mediav1.MediaToCore, mediav1.CoreToMedia]
	ctx       context.Context
	entered   chan struct{}
	inFlight  atomic.Bool
	overlaps  atomic.Int32
	closeSent atomic.Int32
}

func (s *overlapDetectingStream) Send(*mediav1.MediaToCore) error {
	s.inFlight.Store(true)
	defer s.inFlight.Store(false)
	close(s.entered)
	// Like a flow-control-blocked SendMsg: returns only when the stream
	// context is cancelled.
	<-s.ctx.Done()
	// Give a racing CloseSend a window to observe the in-flight send.
	time.Sleep(20 * time.Millisecond)
	return s.ctx.Err()
}

func (s *overlapDetectingStream) CloseSend() error {
	if s.inFlight.Load() {
		s.overlaps.Add(1)
	}
	s.closeSent.Add(1)
	return nil
}

func TestVoiceCoreSessionCloseDoesNotRaceInFlightSend(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	stream := &overlapDetectingStream{ctx: ctx, entered: make(chan struct{})}
	session := &VoiceCoreSession{stream: stream, cancel: cancel}

	sendDone := make(chan error, 1)
	go func() { sendDone <- session.send(&mediav1.MediaToCore{}) }()
	<-stream.entered

	closeDone := make(chan error, 1)
	go func() { closeDone <- session.Close() }()
	select {
	case <-closeDone:
	case <-time.After(2 * time.Second):
		t.Fatal("Close blocked behind an in-flight send")
	}
	<-sendDone
	if got := stream.overlaps.Load(); got != 0 {
		t.Fatalf("CloseSend ran while SendMsg was in flight %d time(s)", got)
	}
	if got := stream.closeSent.Load(); got != 1 {
		t.Fatalf("CloseSend calls = %d, want 1", got)
	}
}
