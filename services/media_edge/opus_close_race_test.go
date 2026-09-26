package mediaedge

import (
	"sync"
	"testing"
)

// Encoding and decoding can still be in flight when the connection closes the
// codec; destroy must wait for them and later calls must fail, not touch freed
// libopus state (SIGBUS in opus_encode before the fix).
func TestOpusCodecsSurviveConcurrentClose(t *testing.T) {
	for round := 0; round < 50; round++ {
		encoder, err := newOpusEncoder(16_000, 1)
		if err != nil {
			t.Fatal(err)
		}
		decoder, err := newOpusDecoder(16_000, 1)
		if err != nil {
			t.Fatal(err)
		}
		pcm := make([]int16, DeviceUplinkFrameSamples)
		var wg sync.WaitGroup
		wg.Add(3)
		go func() {
			defer wg.Done()
			encoded := make([]byte, 1500)
			for index := 0; index < 20; index++ {
				_, _ = encoder.Encode(pcm, encoded)
			}
		}()
		go func() {
			defer wg.Done()
			output := make([]int16, DeviceUplinkFrameSamples)
			for index := 0; index < 20; index++ {
				_, _ = decoder.Decode(nil, output, false)
			}
		}()
		go func() {
			defer wg.Done()
			encoder.close()
			decoder.close()
		}()
		wg.Wait()
		if _, err := encoder.Encode(pcm, make([]byte, 1500)); err == nil {
			t.Fatal("encode after close succeeded")
		}
		if _, err := decoder.Decode(nil, make([]int16, DeviceUplinkFrameSamples), false); err == nil {
			t.Fatal("decode after close succeeded")
		}
	}
}
