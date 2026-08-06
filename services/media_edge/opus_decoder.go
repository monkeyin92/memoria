package mediaedge

/*
#cgo pkg-config: opus
#include <opus.h>

static int memoria_opus_decoder_reset(OpusDecoder *decoder) {
	return opus_decoder_ctl(decoder, OPUS_RESET_STATE);
}
*/
import "C"

import (
	"fmt"
	"unsafe"
)

// opusDecoder owns the small libopus surface needed for normal decoding and
// packet-loss concealment. An empty packet invokes the codec's PLC state.
type opusDecoder struct {
	value *C.OpusDecoder
}

func newOpusDecoder(sampleRate, channels int) (*opusDecoder, error) {
	var code C.int
	value := C.opus_decoder_create(C.opus_int32(sampleRate), C.int(channels), &code)
	if value == nil || code != C.OPUS_OK {
		return nil, fmt.Errorf("create Opus decoder: %s", C.GoString(C.opus_strerror(code)))
	}
	return &opusDecoder{value: value}, nil
}

func (d *opusDecoder) Decode(packet []byte, output []int16, fec bool) (int, error) {
	if d == nil || d.value == nil || len(output) == 0 {
		return 0, fmt.Errorf("opus decoder input is empty")
	}
	var input *C.uchar
	if len(packet) > 0 {
		input = (*C.uchar)(unsafe.Pointer(&packet[0]))
	}
	decoded := C.opus_decode(
		d.value,
		input,
		C.opus_int32(len(packet)),
		(*C.opus_int16)(unsafe.Pointer(&output[0])),
		C.int(len(output)),
		C.int(boolToInt(fec)),
	)
	if decoded < 0 {
		return 0, fmt.Errorf("decode Opus: %s", C.GoString(C.opus_strerror(decoded)))
	}
	return int(decoded), nil
}

func (d *opusDecoder) Reset() error {
	if d == nil || d.value == nil {
		return fmt.Errorf("opus decoder is closed")
	}
	if code := C.memoria_opus_decoder_reset(d.value); code != C.OPUS_OK {
		return fmt.Errorf("reset Opus decoder: %s", C.GoString(C.opus_strerror(code)))
	}
	return nil
}

func (d *opusDecoder) close() {
	if d != nil && d.value != nil {
		C.opus_decoder_destroy(d.value)
		d.value = nil
	}
}

func boolToInt(value bool) C.int {
	if value {
		return 1
	}
	return 0
}
