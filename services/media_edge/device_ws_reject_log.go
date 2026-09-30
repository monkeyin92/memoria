package mediaedge

import (
	"encoding/json"
	"fmt"
	"strings"
)

// describeRejectedControl names a control frame the handler refused, so a
// closed session can be traced to the message that closed it. Only the type,
// sequence and generation numbers are read, never message content; anything
// that does not parse is reported as such.
func describeRejectedControl(data []byte) string {
	var frame map[string]json.RawMessage
	if err := json.Unmarshal(data, &frame); err != nil {
		return "unparsable"
	}
	var parts []string
	var messageType string
	if raw, ok := frame["type"]; ok && json.Unmarshal(raw, &messageType) == nil && len(messageType) <= 64 {
		parts = append(parts, "type="+messageType)
	}
	var sequence uint64
	if raw, ok := frame["control_sequence"]; ok && json.Unmarshal(raw, &sequence) == nil {
		parts = append(parts, fmt.Sprintf("control_sequence=%d", sequence))
	}
	for _, key := range []string{"fence", "expected_fence"} {
		raw, ok := frame[key]
		if !ok {
			continue
		}
		var fence struct {
			TurnID       uint64 `json:"turn_id"`
			GenerationID uint64 `json:"generation_id"`
		}
		if json.Unmarshal(raw, &fence) == nil {
			parts = append(parts, fmt.Sprintf("%s.turn=%d %s.generation=%d", key, fence.TurnID, key, fence.GenerationID))
		}
	}
	if len(parts) == 0 {
		return "unknown"
	}
	return strings.Join(parts, " ")
}
