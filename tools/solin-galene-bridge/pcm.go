package main

import "time"

const (
	opusRate        = 48000
	opusChannels    = 2
	opusFrame       = opusRate / 50 // 20 ms, samples per channel
	pcmGapTolerance = 5 * time.Millisecond
)

// PCMFramer cuts interleaved PCM chunks of arbitrary size into fixed frames,
// following the chunks' pts. Consecutive chunks are joined when a chunk's pts
// matches the end of the buffered audio (within pcmGapTolerance); otherwise
// the partial frame is dropped and framing restarts at the new pts, so frame
// timestamps never drift from the capture clock.
type PCMFramer struct {
	channels int
	frame    int // samples per channel per frame
	rate     int

	buf    []float32
	bufPTS time.Duration
	resets int // framing restarts caused by gaps or overlaps (not the first chunk)
}

func NewPCMFramer(rate, channels, frame int) *PCMFramer {
	return &PCMFramer{channels: channels, frame: frame, rate: rate}
}

// expectedPTS is the pts the next chunk should carry to be contiguous.
func (p *PCMFramer) expectedPTS() time.Duration {
	return p.bufPTS + samplesToDuration(len(p.buf)/p.channels, p.rate)
}

func samplesToDuration(samples, rate int) time.Duration {
	return time.Duration(int64(samples) * int64(time.Second) / int64(rate))
}

// Push appends a chunk that starts at pts and calls emit for every complete
// frame, in order. The frame slice is only valid during the call.
func (p *PCMFramer) Push(pcm []float32, pts time.Duration, emit func(frame []float32, pts time.Duration)) {
	pcm = pcm[:len(pcm)-len(pcm)%p.channels]
	if len(pcm) == 0 {
		return
	}
	if len(p.buf) == 0 {
		p.bufPTS = pts // nothing pending: restarting is lossless
	} else if (pts - p.expectedPTS()).Abs() > pcmGapTolerance {
		p.buf, p.bufPTS = p.buf[:0], pts
		p.resets++
	}
	p.buf = append(p.buf, pcm...)
	n := p.frame * p.channels
	consumed := 0
	for len(p.buf)-consumed >= n {
		emit(p.buf[consumed:consumed+n], p.bufPTS)
		consumed += n
		p.bufPTS += samplesToDuration(p.frame, p.rate)
	}
	if consumed > 0 {
		p.buf = append(p.buf[:0], p.buf[consumed:]...)
	}
}

// Resets returns how many times framing restarted because of a discontinuity.
func (p *PCMFramer) Resets() int { return p.resets }
