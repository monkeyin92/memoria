package mediaedge

// Downlink buffering policy for the hardware device path. The target keeps
// 80-200 ms of audio ahead of the device; anything beyond the ceiling is
// dropped from the oldest frames instead of increasing playout latency, and
// stale-generation audio is discarded at arrival.

import "time"

type DeviceBackpressureConfig struct {
	TargetMinMS    int64
	TargetMaxMS    int64
	MaxAudioFrames int
}

func DefaultDeviceBackpressureConfig() DeviceBackpressureConfig {
	return DeviceBackpressureConfig{
		TargetMinMS:    80,
		TargetMaxMS:    200,
		MaxAudioFrames: 32,
	}
}

func (c DeviceBackpressureConfig) validate() error {
	if c.TargetMinMS < 0 || c.TargetMaxMS <= c.TargetMinMS || c.MaxAudioFrames <= 0 {
		return errInvalidDeviceBackpressure
	}
	return nil
}

type deviceBackpressureError string

func (e deviceBackpressureError) Error() string { return string(e) }

const errInvalidDeviceBackpressure = deviceBackpressureError("invalid device backpressure configuration")

// frameAgeMS returns the age of one 20 ms downlink frame in milliseconds.
func frameAgeMS(queuedAt time.Time, now time.Time) int64 {
	age := now.Sub(queuedAt)
	if age <= 0 {
		return 0
	}
	return age.Milliseconds()
}
