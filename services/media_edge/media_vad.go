package mediaedge

import "math"

type MediaVADConfig struct {
	MinRMS            float64
	StartFrames       int
	SilenceFrames     int
	NoiseMultiplier   float64
	InitialNoiseFloor float64
}

func DefaultMediaVADConfig() MediaVADConfig {
	return MediaVADConfig{
		MinRMS: 180, StartFrames: 2, SilenceFrames: 10,
		NoiseMultiplier: 3.0, InitialNoiseFloor: 60,
	}
}

func (c MediaVADConfig) validate() error {
	if c.MinRMS <= 0 || c.StartFrames <= 0 || c.SilenceFrames <= 0 ||
		c.NoiseMultiplier <= 1 || c.InitialNoiseFloor < 0 {
		return errInvalidMediaVADConfig
	}
	return nil
}

type mediaVADError string

func (e mediaVADError) Error() string { return string(e) }

const errInvalidMediaVADConfig = mediaVADError("invalid media VAD configuration")

type mediaVADEvent struct {
	Start       bool
	Sample      uint64
	VoicedEnd   uint64
	Probability float32
	RMS         float32
	NoiseFloor  float32
}

type mediaVAD struct {
	config         MediaVADConfig
	active         bool
	voicedRun      int
	silenceRun     int
	candidateStart uint64
	lastVoicedEnd  uint64
	noiseFloor     float64
}

func newMediaVAD(config MediaVADConfig) (*mediaVAD, error) {
	if err := config.validate(); err != nil {
		return nil, err
	}
	return &mediaVAD{config: config, noiseFloor: config.InitialNoiseFloor}, nil
}

func pcmRMS(samples []int16) float64 {
	if len(samples) == 0 {
		return 0
	}
	var sum float64
	for _, sample := range samples {
		value := float64(sample)
		sum += value * value
	}
	return math.Sqrt(sum / float64(len(samples)))
}

func (v *mediaVAD) observe(startSample uint64, samples []int16) *mediaVADEvent {
	if len(samples) == 0 {
		return nil
	}
	rms := pcmRMS(samples)
	threshold := math.Max(v.config.MinRMS, v.noiseFloor*v.config.NoiseMultiplier)
	voiced := rms >= threshold
	endSample := startSample + uint64(len(samples))
	probability := float32(math.Min(1, rms/math.Max(threshold, 1)))
	if voiced {
		v.silenceRun = 0
		v.lastVoicedEnd = endSample
		if !v.active {
			if v.voicedRun == 0 {
				v.candidateStart = startSample
			}
			v.voicedRun++
			if v.voicedRun >= v.config.StartFrames {
				v.active = true
				return &mediaVADEvent{
					Start: true, Sample: v.candidateStart, VoicedEnd: v.candidateStart,
					Probability: probability, RMS: float32(rms), NoiseFloor: float32(v.noiseFloor),
				}
			}
		}
		return nil
	}

	v.voicedRun = 0
	if !v.active {
		// Update only from non-speech frames so active speech cannot inflate the
		// threshold and silence the rest of the same utterance.
		v.noiseFloor = 0.95*v.noiseFloor + 0.05*rms
		return nil
	}
	v.silenceRun++
	if v.silenceRun < v.config.SilenceFrames {
		return nil
	}
	event := &mediaVADEvent{
		Start: false, Sample: endSample, VoicedEnd: v.lastVoicedEnd,
		Probability: probability, RMS: float32(rms), NoiseFloor: float32(v.noiseFloor),
	}
	v.active = false
	v.silenceRun = 0
	v.candidateStart = 0
	return event
}

func (v *mediaVAD) flush(sample uint64) *mediaVADEvent {
	if !v.active {
		return nil
	}
	voicedEnd := v.lastVoicedEnd
	if sample <= voicedEnd {
		sample = voicedEnd + 1
	}
	v.active = false
	v.voicedRun = 0
	v.silenceRun = 0
	return &mediaVADEvent{
		Sample: sample, VoicedEnd: voicedEnd, Probability: 0,
		NoiseFloor: float32(v.noiseFloor),
	}
}
