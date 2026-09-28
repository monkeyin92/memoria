package mediaedge

import (
	"context"
	"testing"
	"time"
)

func TestAcquireOpenSlotGivesUpWhenRequestContextEnds(t *testing.T) {
	server := NewServer(JWTVerifier{}, 0)
	releases := make([]func(), 0, cap(server.openSemaphore))
	for range cap(server.openSemaphore) {
		release, ok := server.acquireOpenSlot(context.Background())
		if !ok {
			t.Fatal("free slot was not granted")
		}
		releases = append(releases, release)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	started := time.Now()
	if release, ok := server.acquireOpenSlot(ctx); ok || release != nil {
		t.Fatal("full semaphore granted a slot")
	}
	if waited := time.Since(started); waited > time.Second {
		t.Fatalf("acquire outlived its request context: %s", waited)
	}

	releases[0]()
	release, ok := server.acquireOpenSlot(context.Background())
	if !ok {
		t.Fatal("released slot was not reusable")
	}
	release()
	for _, release := range releases[1:] {
		release()
	}
}
