package main

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
	"time"
)

// Frame kinds on the publish subcommand's stdin.
const (
	kindOpus = 0 // one Opus packet (48 kHz)
	kindH264 = 1 // one Annex-B H.264 access unit
	kindPCM  = 2 // raw PCM: float32 little-endian, interleaved stereo, 48 kHz
)

const (
	frameHeaderSize = 13       // 1 byte kind + 8 bytes pts + 4 bytes length
	maxFrameSize    = 32 << 20 // sanity bound: a corrupt length must not allocate gigabytes
)

// Frame is one framed packet read from stdin.
type Frame struct {
	Kind byte
	PTS  time.Duration // capture clock, nanoseconds, may be negative
	Data []byte
}

// errFrameTooLarge reports a length field above maxFrameSize (a desynchronised stream).
var errFrameTooLarge = errors.New("frame larger than the 32 MiB limit")

// FrameReader parses the stdin framing: kind (1 byte), pts (int64 ns, big
// endian), length (uint32, big endian), then length bytes of payload.
type FrameReader struct {
	r   *bufio.Reader
	hdr [frameHeaderSize]byte
}

func NewFrameReader(r io.Reader) *FrameReader {
	return &FrameReader{r: bufio.NewReaderSize(r, 1<<20)}
}

// Next returns the next frame. It returns io.EOF when the input ends cleanly
// between frames and io.ErrUnexpectedEOF when it ends inside one.
func (fr *FrameReader) Next() (Frame, error) {
	if _, err := io.ReadFull(fr.r, fr.hdr[:]); err != nil {
		return Frame{}, err
	}
	size := binary.BigEndian.Uint32(fr.hdr[9:13])
	if size > maxFrameSize {
		return Frame{}, fmt.Errorf("%w (%d bytes)", errFrameTooLarge, size)
	}
	f := Frame{
		Kind: fr.hdr[0],
		PTS:  time.Duration(int64(binary.BigEndian.Uint64(fr.hdr[1:9]))),
		Data: make([]byte, size),
	}
	if _, err := io.ReadFull(fr.r, f.Data); err != nil {
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		return Frame{}, err
	}
	return f, nil
}

// AppendFrame encodes one frame in the stdin format (used by tests and the
// smoke-test feeder).
func AppendFrame(dst []byte, kind byte, pts time.Duration, data []byte) []byte {
	var hdr [frameHeaderSize]byte
	hdr[0] = kind
	binary.BigEndian.PutUint64(hdr[1:9], uint64(int64(pts)))
	binary.BigEndian.PutUint32(hdr[9:13], uint32(len(data)))
	return append(append(dst, hdr[:]...), data...)
}

// decodeF32LE converts little-endian float32 bytes to samples; a trailing
// partial sample is ignored.
func decodeF32LE(b []byte) []float32 {
	out := make([]float32, len(b)/4)
	for i := range out {
		out[i] = math.Float32frombits(binary.LittleEndian.Uint32(b[4*i:]))
	}
	return out
}
