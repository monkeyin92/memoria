package mediaedge

import "testing"

func TestDescribeRejectedControlNamesTheFrameWithoutItsContent(t *testing.T) {
	cases := map[string]string{
		`{"type":"playback.ended","control_sequence":12,"fence":{"turn_id":2,"generation_id":3},"note":"secret"}`: "type=playback.ended control_sequence=12 fence.turn=2 fence.generation=3",
		`{"type":"keyword.detected","control_sequence":7,"expected_fence":{"turn_id":1,"generation_id":2}}`:       "type=keyword.detected control_sequence=7 expected_fence.turn=1 expected_fence.generation=2",
		`{"type":"device.telemetry","control_sequence":9}`:                                                        "type=device.telemetry control_sequence=9",
		`{"x":1}`:      "unknown",
		`not json`:     "unparsable",
		`{"type":123}`: "unknown",
	}
	for input, want := range cases {
		if got := describeRejectedControl([]byte(input)); got != want {
			t.Errorf("describeRejectedControl(%s) = %q, want %q", input, got, want)
		}
	}
}
