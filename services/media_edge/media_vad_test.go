package mediaedge

import "testing"

func TestMediaVADPublishesStableVoicedBoundaryAfterHangover(t *testing.T) {
	vad, err := newMediaVAD(MediaVADConfig{
		MinRMS: 100, StartFrames: 2, SilenceFrames: 2,
		NoiseMultiplier: 2, InitialNoiseFloor: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	voice := make([]int16, 320)
	for i := range voice {
		voice[i] = 1000
	}
	silence := make([]int16, 320)
	if event := vad.observe(0, voice); event != nil {
		t.Fatalf("VAD started before debounce: %+v", event)
	}
	started := vad.observe(320, voice)
	if started == nil || !started.Start || started.Sample != 0 {
		t.Fatalf("VAD start lost the candidate boundary: %+v", started)
	}
	if event := vad.observe(640, silence); event != nil {
		t.Fatalf("VAD ended before hangover: %+v", event)
	}
	ended := vad.observe(960, silence)
	if ended == nil || ended.Start || ended.Sample != 1280 || ended.VoicedEnd != 640 {
		t.Fatalf("VAD end did not preserve the voiced boundary: %+v", ended)
	}
}

func TestMediaVADFlushEndsOnlyAnActiveUtterance(t *testing.T) {
	vad, err := newMediaVAD(MediaVADConfig{
		MinRMS: 100, StartFrames: 1, SilenceFrames: 2,
		NoiseMultiplier: 2, InitialNoiseFloor: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	voice := []int16{1000, 1000}
	if event := vad.observe(0, voice); event == nil || !event.Start {
		t.Fatal("VAD did not start")
	}
	if event := vad.flush(2); event == nil || event.VoicedEnd != 2 || event.Sample != 3 {
		t.Fatalf("invalid flush event: %+v", event)
	}
	if event := vad.flush(4); event != nil {
		t.Fatalf("inactive VAD flushed twice: %+v", event)
	}
}
