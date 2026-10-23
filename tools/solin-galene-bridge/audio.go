package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"unsafe"

	"github.com/gen2brain/malgo"
)

type deviceEntry struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Default bool   `json:"default"`
}

// newAudioContext initialises miniaudio with its default backend order
// (WASAPI on Windows, Core Audio on macOS, PulseAudio/PipeWire then ALSA on
// Linux). miniaudio's chatty log is shown only with SOLIN_BRIDGE_AUDIO_DEBUG=1.
func newAudioContext() (*malgo.AllocatedContext, error) {
	var logProc malgo.LogProc
	if os.Getenv("SOLIN_BRIDGE_AUDIO_DEBUG") == "1" {
		logProc = func(msg string) { log.Printf("miniaudio: %s", strings.TrimSpace(msg)) }
	}
	return malgo.InitContext(nil, malgo.ContextConfig{}, logProc)
}

func playbackDevices(ctx *malgo.AllocatedContext) ([]malgo.DeviceInfo, error) {
	return ctx.Devices(malgo.Playback)
}

func runDevices(args []string) error {
	if len(args) > 0 {
		return withCode(exitUsage, fmt.Errorf("devices: unexpected arguments %q", args))
	}
	ctx, err := newAudioContext()
	if err != nil {
		return withCode(exitDevice, fmt.Errorf("audio: %w", err))
	}
	defer func() {
		_ = ctx.Uninit()
		ctx.Free()
	}()
	infos, err := playbackDevices(ctx)
	if err != nil {
		return withCode(exitDevice, fmt.Errorf("audio: %w", err))
	}
	out := make([]deviceEntry, 0, len(infos))
	for _, d := range infos {
		out = append(out, deviceEntry{ID: d.ID.String(), Name: d.Name(), Default: d.IsDefault != 0})
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

// findDevice resolves --device: a malgo ID as printed by "devices" (hex) or
// an exact device name. Empty selects the system default (nil ID).
func findDevice(infos []malgo.DeviceInfo, want string) (*malgo.DeviceID, string, error) {
	if want == "" {
		return nil, "default", nil
	}
	for i := range infos {
		if strings.EqualFold(infos[i].ID.String(), want) {
			return &infos[i].ID, infos[i].Name(), nil
		}
	}
	for i := range infos {
		if infos[i].Name() == want {
			return &infos[i].ID, infos[i].Name(), nil
		}
	}
	// An ID that is valid hex but not enumerated may still open (some
	// backends hide devices); let miniaudio decide.
	if raw, err := hex.DecodeString(want); err == nil && len(raw) > 0 {
		var id malgo.DeviceID
		if len(raw) <= len(id) {
			copy(id[:], raw)
			return &id, want, nil
		}
	}
	return nil, "", fmt.Errorf("no playback device with id or name %q", want)
}

// player plays the mixer on a miniaudio playback device.
type player struct {
	ctx      *malgo.AllocatedContext
	dev      *malgo.Device
	stopped  chan struct{}
	stopOnce sync.Once
	closing  atomic.Bool
}

// close stops the device and releases the audio context.
func (p *player) close() {
	p.closing.Store(true)
	if p.dev != nil {
		p.dev.Uninit()
		p.dev = nil
	}
	_ = p.ctx.Uninit()
	p.ctx.Free()
}

// openPlayer opens the device with a short period (low latency) and starts
// pulling from the mixer. Device stops not requested by close are signalled
// on the stopped channel (unplugged device, audio server restart).
func openPlayer(device string, channels, periodMs, periods int, mix *Mixer) (*player, error) {
	ctx, err := newAudioContext()
	if err != nil {
		return nil, err
	}
	p := &player{ctx: ctx, stopped: make(chan struct{})}
	fail := func(err error) (*player, error) {
		_ = ctx.Uninit()
		ctx.Free()
		return nil, err
	}
	infos, err := playbackDevices(ctx)
	if err != nil {
		return fail(err)
	}
	id, name, err := findDevice(infos, device)
	if err != nil {
		return fail(err)
	}
	cfg := malgo.DefaultDeviceConfig(malgo.Playback)
	cfg.Playback.Format = malgo.FormatF32
	cfg.Playback.Channels = uint32(channels)
	cfg.SampleRate = opusRate
	cfg.PeriodSizeInMilliseconds = uint32(periodMs)
	cfg.Periods = uint32(periods)
	cfg.PerformanceProfile = malgo.LowLatency
	cfg.Pulse.StreamNamePlayback = "Solin remote audio"
	if id != nil {
		ptr := id.Pointer() // C copy, freed after InitDevice
		defer freeC(ptr)
		cfg.Playback.DeviceID = ptr
	}
	var scratch []float32
	onData := func(out, _ []byte, frames uint32) {
		n := int(frames) * channels
		if cap(scratch) < n {
			scratch = make([]float32, n)
		}
		buf := scratch[:n]
		mix.Read(buf)
		if len(out) >= n*4 {
			copy(out, unsafe.Slice((*byte)(unsafe.Pointer(&buf[0])), n*4))
		}
	}
	onStop := func() {
		if !p.closing.Load() {
			p.stopOnce.Do(func() { close(p.stopped) })
		}
	}
	dev, err := malgo.InitDevice(ctx.Context, cfg, malgo.DeviceCallbacks{Data: onData, Stop: onStop})
	if err != nil {
		return fail(fmt.Errorf("open %s: %w", name, err))
	}
	if err := dev.Start(); err != nil {
		dev.Uninit()
		return fail(fmt.Errorf("start %s: %w", name, err))
	}
	p.dev = dev
	log.Printf("playing on %s: %d ch, %d Hz, %d x %d ms periods", name, dev.PlaybackChannels(), dev.SampleRate(), periods, periodMs)
	return p, nil
}
