package mediaedge

/*
#cgo pkg-config: opus
#include <opus.h>

static int memoria_opus_encoder_reset(OpusEncoder *encoder) {
	return opus_encoder_ctl(encoder, OPUS_RESET_STATE);
}
*/
import "C"

import (
	"fmt"
	"sync"
	"unsafe"
)

// opusEncoder is the smallest libopus surface required by the device WSS
// downlink. The mutex serializes encoding with destroy: the Voice Core
// receive goroutine can still be encoding a frame when the connection closes,
// and libopus state must not be freed under it (P2-04).
type opusEncoder struct {
	mu    sync.Mutex
	value *C.OpusEncoder
}

func newOpusEncoder(sampleRate, channels int) (*opusEncoder, error) {
	var code C.int
	value := C.opus_encoder_create(
		C.opus_int32(sampleRate), C.int(channels), C.OPUS_APPLICATION_VOIP, &code,
	)
	if value == nil || code != C.OPUS_OK {
		return nil, fmt.Errorf("create Opus encoder: %s", C.GoString(C.opus_strerror(code)))
	}
	return &opusEncoder{value: value}, nil
}

func (e *opusEncoder) Encode(pcm []int16, output []byte) (int, error) {
	if e == nil {
		return 0, fmt.Errorf("opus encoder input is empty")
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.value == nil || len(pcm) == 0 || len(output) == 0 {
		return 0, fmt.Errorf("opus encoder input is empty")
	}
	n := C.opus_encode(
		e.value,
		(*C.opus_int16)(unsafe.Pointer(&pcm[0])),
		C.int(len(pcm)),
		(*C.uchar)(unsafe.Pointer(&output[0])),
		C.opus_int32(len(output)),
	)
	if n < 0 {
		return 0, fmt.Errorf("encode Opus: %s", C.GoString(C.opus_strerror(n)))
	}
	return int(n), nil
}

func (e *opusEncoder) Reset() error {
	if e == nil {
		return fmt.Errorf("opus encoder is closed")
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.value == nil {
		return fmt.Errorf("opus encoder is closed")
	}
	if code := C.memoria_opus_encoder_reset(e.value); code != C.OPUS_OK {
		return fmt.Errorf("reset Opus encoder: %s", C.GoString(C.opus_strerror(code)))
	}
	return nil
}

func (e *opusEncoder) close() {
	if e == nil {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.value != nil {
		C.opus_encoder_destroy(e.value)
		e.value = nil
	}
}
