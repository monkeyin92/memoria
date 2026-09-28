package mediaedge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The same vectors drive services/agent/tests/unit/test_generation_fence_vectors.py.
type fenceVectors struct {
	SessionID  string `json:"session_id"`
	OrderCases []struct {
		Name      string    `json:"name"`
		Current   [4]uint64 `json:"current"`
		Candidate [4]uint64 `json:"candidate"`
		Equal     bool      `json:"equal"`
		Order     string    `json:"order"`
	} `json:"order_cases"`
	CancelCases []struct {
		Name      string    `json:"name"`
		Current   [4]uint64 `json:"current"`
		Cancelled [4]uint64 `json:"cancelled"`
		Accepted  bool      `json:"accepted"`
	} `json:"cancel_cases"`
}

func loadFenceVectors(t *testing.T) fenceVectors {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "packages", "contracts", "generation-fence-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors fenceVectors
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors.OrderCases) == 0 || len(vectors.CancelCases) == 0 {
		t.Fatal("fence vectors are empty")
	}
	return vectors
}

// fields are ordered (session_epoch, turn_id, generation_id, tool_epoch).
func vectorFence(sessionID string, fields [4]uint64) Fence {
	return Fence{
		SessionID:    sessionID,
		SessionEpoch: fields[0],
		TurnID:       fields[1],
		GenerationID: fields[2],
		ToolEpoch:    fields[3],
	}
}

func vectorSession(t *testing.T, sessionID string, current Fence) *Session {
	t.Helper()
	session, err := NewSession(OpenSessionRequest{
		SessionID: sessionID, AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatalf("install current fence: %v", err)
	}
	return session
}

func TestGenerationFenceOrderMatchesSharedVectors(t *testing.T) {
	vectors := loadFenceVectors(t)
	for _, vector := range vectors.OrderCases {
		t.Run(vector.Name, func(t *testing.T) {
			current := vectorFence(vectors.SessionID, vector.Current)
			candidate := vectorFence(vectors.SessionID, vector.Candidate)
			if got := current.Equal(candidate); got != vector.Equal {
				t.Fatalf("Equal = %v, want %v", got, vector.Equal)
			}
			err := vectorSession(t, vectors.SessionID, current).AdvanceGeneration(candidate)
			if (err != nil) != (vector.Order == "before") {
				t.Fatalf("AdvanceGeneration error = %v for order %q", err, vector.Order)
			}
		})
	}
}

func TestGenerationCancelMatchesSharedVectors(t *testing.T) {
	vectors := loadFenceVectors(t)
	for _, vector := range vectors.CancelCases {
		t.Run(vector.Name, func(t *testing.T) {
			current := vectorFence(vectors.SessionID, vector.Current)
			cancelled := vectorFence(vectors.SessionID, vector.Cancelled)
			err := vectorSession(t, vectors.SessionID, current).ApplyCancelledGeneration(cancelled)
			if (err == nil) != vector.Accepted {
				t.Fatalf("ApplyCancelledGeneration error = %v, want accepted=%v", err, vector.Accepted)
			}
		})
	}
}
