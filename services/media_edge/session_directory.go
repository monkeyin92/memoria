package mediaedge

import (
	"fmt"
	"sync"
)

type Directory struct {
	mu       sync.RWMutex
	sessions map[string]*Session
}

func NewDirectory() *Directory { return &Directory{sessions: make(map[string]*Session)} }

func (d *Directory) Put(session *Session) error {
	if session == nil || session.ID == "" {
		return fmt.Errorf("session is required")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, exists := d.sessions[session.ID]; exists {
		return fmt.Errorf("session already exists")
	}
	d.sessions[session.ID] = session
	return nil
}

// ReplaceNewer atomically installs a new transport epoch and returns the old
// session for cleanup. Equal/older epochs are rejected so a retried WHIP offer
// cannot displace an already-connected peer.
func (d *Directory) ReplaceNewer(session *Session) (*Session, error) {
	if session == nil || session.ID == "" {
		return nil, fmt.Errorf("session is required")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	old := d.sessions[session.ID]
	if old != nil && session.StreamEpoch <= old.Epoch() {
		return nil, fmt.Errorf("stream epoch must advance")
	}
	d.sessions[session.ID] = session
	return old, nil
}

func (d *Directory) Get(id string) (*Session, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	session, ok := d.sessions[id]
	return session, ok
}

func (d *Directory) Snapshots() []SessionStats {
	d.mu.RLock()
	sessions := make([]*Session, 0, len(d.sessions))
	for _, session := range d.sessions {
		sessions = append(sessions, session)
	}
	d.mu.RUnlock()
	stats := make([]SessionStats, 0, len(sessions))
	for _, session := range sessions {
		stats = append(stats, session.Stats())
	}
	return stats
}

func (d *Directory) Delete(id string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.sessions[id]; !ok {
		return false
	}
	delete(d.sessions, id)
	return true
}

// CloseAll releases every edge-owned session during process shutdown. The
// control-plane TTL still uses CloseSession for individual expiry; this sweep
// prevents a graceful server stop from retaining the directory map.
func (d *Directory) CloseAll() {
	for _, session := range d.TakeAll() {
		session.Stop()
	}
}

func (d *Directory) TakeAll() []*Session {
	d.mu.Lock()
	sessions := make([]*Session, 0, len(d.sessions))
	for id, session := range d.sessions {
		sessions = append(sessions, session)
		delete(d.sessions, id)
	}
	d.mu.Unlock()
	return sessions
}
