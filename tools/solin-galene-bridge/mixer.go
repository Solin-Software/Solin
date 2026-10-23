package main

import "sync"

// Mixer sums the decoded remote streams for the playback device. Each input
// owns a small ring buffer that absorbs network jitter: an input starts
// playing once it holds `prebuffer` frames, goes back to prebuffering after an
// underrun, and drops its oldest audio when it holds more than `maxBuffer`
// frames (sender/device clock drift or a burst after a stall), so latency
// stays bounded. Samples are interleaved float32 with `channels` channels.
type Mixer struct {
	mu        sync.Mutex
	channels  int
	prebuffer int // frames
	maxBuffer int // frames
	inputs    map[*MixInput]struct{}

	underruns int
	overflows int
}

// MixInput is one stream feeding the mixer.
type MixInput struct {
	m      *Mixer
	gain   float32
	ring   []float32
	head   int // read position, in samples
	size   int // buffered samples
	primed bool
}

func NewMixer(channels, prebufferFrames, maxBufferFrames int) *Mixer {
	if maxBufferFrames < prebufferFrames {
		maxBufferFrames = prebufferFrames
	}
	return &Mixer{
		channels:  channels,
		prebuffer: prebufferFrames,
		maxBuffer: maxBufferFrames,
		inputs:    map[*MixInput]struct{}{},
	}
}

// NewInput registers a stream with the given linear gain.
func (m *Mixer) NewInput(gain float32) *MixInput {
	in := &MixInput{m: m, gain: gain, ring: make([]float32, m.maxBuffer*m.channels)}
	m.mu.Lock()
	m.inputs[in] = struct{}{}
	m.mu.Unlock()
	return in
}

// Remove unregisters a stream; its buffered audio is discarded.
func (m *Mixer) Remove(in *MixInput) {
	m.mu.Lock()
	delete(m.inputs, in)
	m.mu.Unlock()
}

// Inputs returns the number of registered streams.
func (m *Mixer) Inputs() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.inputs)
}

// Counters returns and resets the underrun and overflow counts.
func (m *Mixer) Counters() (underruns, overflows int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	underruns, overflows = m.underruns, m.overflows
	m.underruns, m.overflows = 0, 0
	return
}

// Buffered returns the frames waiting in the input.
func (in *MixInput) Buffered() int {
	in.m.mu.Lock()
	defer in.m.mu.Unlock()
	return in.size / in.m.channels
}

// Write queues interleaved samples (a partial trailing frame is ignored).
func (in *MixInput) Write(samples []float32) {
	m := in.m
	samples = samples[:len(samples)-len(samples)%m.channels]
	m.mu.Lock()
	defer m.mu.Unlock()
	capacity := len(in.ring)
	if len(samples) >= capacity {
		samples = samples[len(samples)-capacity:]
	}
	if in.size+len(samples) > capacity {
		// Too much latency: keep only the newest `prebuffer` frames worth
		// (counting what is being written), then append.
		keep := m.prebuffer*m.channels - len(samples)
		if keep < 0 {
			keep = 0
		}
		if keep < in.size {
			drop := in.size - keep
			in.head = (in.head + drop) % capacity
			in.size = keep
		}
		m.overflows++
	}
	tail := (in.head + in.size) % capacity
	n := copy(in.ring[tail:], samples)
	copy(in.ring, samples[n:])
	in.size += len(samples)
}

// Read fills out with the mix of all primed inputs, hard-clipped to [-1, 1].
func (m *Mixer) Read(out []float32) {
	for i := range out {
		out[i] = 0
	}
	m.mu.Lock()
	for in := range m.inputs {
		if !in.primed {
			if in.size < m.prebuffer*m.channels || in.size == 0 {
				continue
			}
			in.primed = true
		}
		n := len(out)
		if in.size < n {
			n = in.size
			in.primed = false // underrun: rebuild the jitter buffer
			m.underruns++
		}
		capacity := len(in.ring)
		for i := 0; i < n; i++ {
			out[i] += in.ring[(in.head+i)%capacity] * in.gain
		}
		in.head = (in.head + n) % capacity
		in.size -= n
	}
	m.mu.Unlock()
	clip(out)
}

func clip(s []float32) {
	for i, v := range s {
		if v > 1 {
			s[i] = 1
		} else if v < -1 {
			s[i] = -1
		}
	}
}
