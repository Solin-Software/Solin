package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/pion/interceptor"
	"github.com/pion/rtcp"
	"github.com/pion/rtp"
	"github.com/pion/rtp/codecs"
	"github.com/pion/webrtc/v4"
	"gopkg.in/hraban/opus.v2"
)

const (
	disconnectGrace   = 5 * time.Second  // a disconnected PeerConnection gets this long to recover
	connectTimeout    = 15 * time.Second // WHIP answer to ICE+DTLS connected
	gatherTimeout     = 5 * time.Second
	httpTimeout       = 10 * time.Second
	statsInterval     = 5 * time.Second
	srInterval        = time.Second
	keyframeReqMinGap = time.Second
)

var (
	videoCapability = webrtc.RTPCodecCapability{
		MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
		SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		RTCPFeedback: []webrtc.RTCPFeedback{
			{Type: "nack"}, {Type: "nack", Parameter: "pli"}, {Type: "ccm", Parameter: "fir"},
		},
	}
	audioCapability = webrtc.RTPCodecCapability{
		MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2,
		SDPFmtpLine: "minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1",
	}
)

// errAuth marks a refused token (HTTP 401/403 from WHIP, a failed join).
var errAuth = errors.New("not authorised")

type publishConfig struct {
	whip        string
	tokens      tokenSource
	opusBitrate int
	opusLoss    int
	fec         bool
	voip        bool
}

func runPublish(args []string) error {
	fs := flag.NewFlagSet("publish", flag.ExitOnError)
	var cfg publishConfig
	fs.StringVar(&cfg.whip, "whip", "", "WHIP endpoint, e.g. https://host/group/NAME/.whip (required)")
	fs.StringVar(&cfg.tokens.token, "token", "", "Galene token sent as the WHIP bearer token")
	fs.StringVar(&cfg.tokens.file, "token-file", "", "read the token from this file on every connection attempt (overrides --token)")
	fs.IntVar(&cfg.opusBitrate, "opus-bitrate", 64000, "Opus bitrate for PCM input, bits/s")
	fs.IntVar(&cfg.opusLoss, "opus-loss", 10, "expected packet loss % for PCM input (sizes the in-band FEC)")
	noFEC := fs.Bool("no-fec", false, "disable Opus in-band FEC for PCM input")
	fs.BoolVar(&cfg.voip, "opus-voip", false, "encode PCM input with OPUS_APPLICATION_VOIP instead of AUDIO")
	fs.Parse(args)
	cfg.fec = !*noFEC
	if cfg.whip == "" {
		return withCode(exitUsage, errors.New("publish: --whip is required"))
	}
	if _, err := url.Parse(cfg.whip); err != nil {
		return withCode(exitUsage, fmt.Errorf("publish: bad --whip: %w", err))
	}
	if cfg.tokens.token == "" && cfg.tokens.file == "" {
		return withCode(exitUsage, errors.New("publish: --token or --token-file is required"))
	}

	p, err := newPublisher(cfg, newEmitter())
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var mediaErr atomic.Pointer[error]
	go func() {
		err := p.readMedia(os.Stdin)
		if errors.Is(err, io.EOF) {
			log.Printf("stdin closed, shutting down")
		} else {
			mediaErr.Store(&err)
		}
		cancel()
	}()
	go p.reportStats(ctx)
	if err := p.run(ctx); err != nil {
		return err
	}
	if err := mediaErr.Load(); err != nil {
		p.events.state("failed")
		return fmt.Errorf("stdin: %w", *err)
	}
	return nil
}

// ptsTrack packetizes frames with RTP timestamps derived from their pts and
// keeps what the RTCP sender reports need. One per track per session.
type ptsTrack struct {
	track      *webrtc.TrackLocalStaticRTP
	packetizer rtp.Packetizer
	clock      uint32
	base       uint32
	ssrc       uint32

	mu      sync.Mutex
	lastRTP uint32
	lastPTS time.Duration
	packets uint32
	octets  uint32
	started bool
}

func newPTSTrack(c webrtc.RTPCodecCapability, id string, payloader rtp.Payloader, base uint32) (*ptsTrack, error) {
	track, err := webrtc.NewTrackLocalStaticRTP(c, id, "solin")
	if err != nil {
		return nil, err
	}
	// Payload type and SSRC are rewritten by the track's binding.
	return &ptsTrack{
		track:      track,
		packetizer: rtp.NewPacketizer(1200, 0, 0, payloader, rtp.NewRandomSequencer(), c.ClockRate),
		clock:      c.ClockRate,
		base:       base,
	}, nil
}

// write sends one frame and returns its payload size.
func (t *ptsTrack) write(data []byte, pts time.Duration) int {
	ts := rtpTimestamp(t.base, pts, t.clock)
	packets := t.packetizer.Packetize(data, 0)
	for _, p := range packets {
		p.Timestamp = ts
		// Errors only mean the transport is not (or no longer) writable;
		// the connection supervisor deals with that.
		_ = t.track.WriteRTP(p)
	}
	t.mu.Lock()
	t.lastRTP, t.lastPTS, t.started = ts, pts, true
	t.packets += uint32(len(packets))
	t.octets += uint32(len(data))
	t.mu.Unlock()
	return len(data)
}

// senderReport maps the last packet's RTP timestamp to wall0 + its pts, so
// receivers can lip-sync audio and video on the capture clock.
func (t *ptsTrack) senderReport(wall0 time.Time) *rtcp.SenderReport {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.started {
		return nil
	}
	return &rtcp.SenderReport{
		SSRC: t.ssrc, NTPTime: ntpTime(wall0.Add(t.lastPTS)), RTPTime: t.lastRTP,
		PacketCount: t.packets, OctetCount: t.octets,
	}
}

// pubSession is one WHIP session (one PeerConnection).
type pubSession struct {
	pc           *webrtc.PeerConnection
	video, audio *ptsTrack
	resource     string
	token        string
	states       chan webrtc.PeerConnectionState

	live    atomic.Bool // connected: media is written
	needKey atomic.Bool // drop video until the next keyframe
}

type publisher struct {
	cfg    publishConfig
	events *emitter
	api    *webrtc.API
	http   *http.Client
	base   uint32 // RTP timestamp offset, random per process

	cur atomic.Pointer[pubSession]

	vbytes, abytes atomic.Int64
	lastKeyReq     atomic.Int64 // unix ns of the last keyframe_request event
}

func newPublisher(cfg publishConfig, events *emitter) (*publisher, error) {
	m := &webrtc.MediaEngine{}
	if err := m.RegisterCodec(webrtc.RTPCodecParameters{RTPCodecCapability: videoCapability, PayloadType: 102}, webrtc.RTPCodecTypeVideo); err != nil {
		return nil, err
	}
	if err := m.RegisterCodec(webrtc.RTPCodecParameters{RTPCodecCapability: audioCapability, PayloadType: 111}, webrtc.RTPCodecTypeAudio); err != nil {
		return nil, err
	}
	ir := &interceptor.Registry{}
	// NACK responder and TWCC; sender reports are sent by readMedia on the
	// capture clock rather than by Pion on the send clock.
	if err := webrtc.ConfigureNack(m, ir); err != nil {
		return nil, err
	}
	if err := webrtc.ConfigureTWCCHeaderExtensionSender(m, ir); err != nil {
		return nil, err
	}
	return &publisher{
		cfg:    cfg,
		events: events,
		api:    webrtc.NewAPI(webrtc.WithMediaEngine(m), webrtc.WithInterceptorRegistry(ir)),
		http:   &http.Client{Timeout: httpTimeout},
		base:   uint32(time.Now().UnixNano()),
	}, nil
}

// readMedia consumes stdin until EOF or a framing error. It never blocks on
// the network: media is dropped while no session is connected.
func (p *publisher) readMedia(r io.Reader) error {
	app := opus.AppAudio
	if p.cfg.voip {
		app = opus.AppVoIP
	}
	enc, err := opus.NewEncoder(opusRate, opusChannels, app)
	if err != nil {
		return err
	}
	for _, err := range []error{enc.SetBitrate(p.cfg.opusBitrate), enc.SetInBandFEC(p.cfg.fec), enc.SetPacketLossPerc(p.cfg.opusLoss)} {
		if err != nil {
			return fmt.Errorf("opus encoder: %w", err)
		}
	}
	framer := NewPCMFramer(opusRate, opusChannels, opusFrame)
	opusBuf := make([]byte, 4000)
	var params h264Params
	var wall0 time.Time
	lastSR := time.Now()
	unknown := map[byte]bool{}
	fr := NewFrameReader(r)
	for {
		f, err := fr.Next()
		if err != nil {
			if errors.Is(err, io.ErrUnexpectedEOF) {
				log.Printf("stdin ended inside a frame")
				return io.EOF
			}
			return err
		}
		if wall0.IsZero() {
			// Audio and video pts share one capture clock; anchor it once.
			wall0 = time.Now().Add(-f.PTS)
		}
		s := p.cur.Load()
		live := s != nil && s.live.Load()
		switch f.Kind {
		case kindOpus:
			if live {
				p.abytes.Add(int64(s.audio.write(f.Data, f.PTS)))
			}
		case kindH264:
			idr, hasParams := params.inspect(f.Data)
			if !live {
				break
			}
			if s.needKey.Load() {
				if !idr {
					break // a receiver cannot start decoding before a keyframe
				}
				s.needKey.Store(false)
			}
			au := f.Data
			if idr && !hasParams {
				au = params.withParams(au)
			}
			p.vbytes.Add(int64(s.video.write(au, f.PTS)))
		case kindPCM:
			framer.Push(decodeF32LE(f.Data), f.PTS, func(frame []float32, pts time.Duration) {
				n, err := enc.EncodeFloat32(frame, opusBuf)
				if err != nil {
					log.Printf("opus encode: %v", err)
					return
				}
				if live {
					p.abytes.Add(int64(s.audio.write(append([]byte(nil), opusBuf[:n]...), pts)))
				}
			})
		default:
			if !unknown[f.Kind] {
				unknown[f.Kind] = true
				log.Printf("ignoring frames of unknown kind %d", f.Kind)
			}
		}
		if live && time.Since(lastSR) >= srInterval {
			lastSR = time.Now()
			var reports []rtcp.Packet
			for _, tr := range []*ptsTrack{s.video, s.audio} {
				if sr := tr.senderReport(wall0); sr != nil {
					reports = append(reports, sr)
				}
			}
			if len(reports) > 0 {
				_ = s.pc.WriteRTCP(reports)
			}
		}
	}
}

func (p *publisher) reportStats(ctx context.Context) {
	tick := time.NewTicker(statsInterval)
	defer tick.Stop()
	var lastV, lastA int64
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			v, a := p.vbytes.Load(), p.abytes.Load()
			secs := statsInterval.Seconds()
			p.events.emit(map[string]any{
				"event":      "stats",
				"video_kbps": int64(float64(v-lastV) * 8 / 1000 / secs),
				"audio_kbps": int64(float64(a-lastA) * 8 / 1000 / secs),
			})
			lastV, lastA = v, a
		}
	}
}

// run keeps a WHIP session up until ctx ends.
func (p *publisher) run(ctx context.Context) error {
	bo := newBackoff(time.Second, 10*time.Second)
	state := "connecting"
	for {
		p.events.state(state)
		s, err := p.connect(ctx)
		if err == nil {
			everConnected := p.supervise(ctx, s)
			p.teardown(s)
			if everConnected {
				bo.Reset()
			}
		} else if ctx.Err() == nil {
			log.Printf("connect: %v", err)
			if errors.Is(err, errAuth) && !p.cfg.tokens.refreshable() {
				p.events.state("failed")
				return withCode(exitAuth, err)
			}
		}
		if ctx.Err() != nil {
			return nil
		}
		state = "reconnecting"
		p.events.state(state)
		delay := bo.Next()
		log.Printf("retrying in %v", delay)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
	}
}

// supervise waits for the session to connect and then for it to fail. It
// returns whether the session ever connected.
func (p *publisher) supervise(ctx context.Context, s *pubSession) bool {
	connected := false
	deadline := time.NewTimer(connectTimeout)
	defer deadline.Stop()
	for {
		select {
		case <-ctx.Done():
			return connected
		case <-deadline.C:
			if connected {
				log.Printf("connection did not recover within %v", disconnectGrace)
			} else {
				log.Printf("connection not established within %v", connectTimeout)
			}
			return connected
		case st := <-s.states:
			log.Printf("peer connection: %s", st)
			switch st {
			case webrtc.PeerConnectionStateConnected:
				connected = true
				deadline.Stop()
				s.needKey.Store(true)
				s.live.Store(true)
				p.events.state("connected")
			case webrtc.PeerConnectionStateDisconnected:
				s.live.Store(false)
				if connected {
					p.events.state("reconnecting")
					deadline.Reset(disconnectGrace)
				}
			case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
				return connected
			}
		}
	}
}

func (p *publisher) teardown(s *pubSession) {
	s.live.Store(false)
	p.cur.CompareAndSwap(s, nil)
	if s.resource != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		req, err := http.NewRequestWithContext(ctx, http.MethodDelete, s.resource, nil)
		if err == nil {
			req.Header.Set("Authorization", "Bearer "+s.token)
			if resp, err := p.http.Do(req); err != nil {
				log.Printf("WHIP DELETE: %v", err)
			} else {
				resp.Body.Close()
				log.Printf("WHIP DELETE: %s", resp.Status)
			}
		}
		cancel()
	}
	if err := s.pc.Close(); err != nil {
		log.Printf("close: %v", err)
	}
}

// fetchICEServers asks the WHIP endpoint for its ICE servers (OPTIONS + Link
// headers). Failures are not fatal: host candidates may be enough.
func (p *publisher) fetchICEServers(ctx context.Context, token string) []webrtc.ICEServer {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodOptions, p.cfg.whip, nil)
	if err != nil {
		return nil
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := p.http.Do(req)
	if err != nil {
		log.Printf("WHIP OPTIONS: %v", err)
		return nil
	}
	resp.Body.Close()
	return iceServersFromLinks(resp.Header.Values("Link"))
}

func (p *publisher) connect(ctx context.Context) (*pubSession, error) {
	token, err := p.cfg.tokens.get()
	if err != nil {
		return nil, err
	}
	ice := p.fetchICEServers(ctx, token)
	pc, err := p.api.NewPeerConnection(webrtc.Configuration{ICEServers: ice})
	if err != nil {
		return nil, err
	}
	s := &pubSession{pc: pc, token: token, states: make(chan webrtc.PeerConnectionState, 16)}
	ok := false
	defer func() {
		if !ok {
			pc.Close()
		}
	}()
	pc.OnConnectionStateChange(func(st webrtc.PeerConnectionState) {
		select {
		case s.states <- st:
		default:
		}
	})
	if s.video, err = newPTSTrack(videoCapability, "video", &codecs.H264Payloader{}, p.base); err != nil {
		return nil, err
	}
	if s.audio, err = newPTSTrack(audioCapability, "audio", &codecs.OpusPayloader{}, p.base); err != nil {
		return nil, err
	}
	for _, t := range []*ptsTrack{s.video, s.audio} {
		tr, err := pc.AddTransceiverFromTrack(t.track, webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionSendonly})
		if err != nil {
			return nil, err
		}
		t.ssrc = uint32(tr.Sender().GetParameters().Encodings[0].SSRC)
		go p.readRTCP(tr.Sender(), t == s.video)
	}

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return nil, err
	}
	gathered := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		return nil, err
	}
	select {
	case <-gathered:
	case <-time.After(gatherTimeout):
		log.Printf("ICE gathering incomplete after %v, sending the candidates found so far", gatherTimeout)
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	reqCtx, cancel := context.WithTimeout(ctx, httpTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, p.cfg.whip, bytes.NewBufferString(pc.LocalDescription().SDP))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/sdp")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := p.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("WHIP POST: %w", err)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	if err != nil {
		return nil, fmt.Errorf("WHIP POST: %w", err)
	}
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return nil, fmt.Errorf("WHIP POST: %s: %w", resp.Status, errAuth)
	case resp.StatusCode != http.StatusCreated:
		return nil, fmt.Errorf("WHIP POST: %s: %s", resp.Status, bytes.TrimSpace(body))
	}
	location := resp.Header.Get("Location")
	if location == "" {
		return nil, errors.New("WHIP POST: no Location header")
	}
	resource, err := url.Parse(p.cfg.whip)
	if err != nil {
		return nil, err
	}
	if resource, err = resource.Parse(location); err != nil {
		return nil, fmt.Errorf("WHIP POST: bad Location: %w", err)
	}
	s.resource = resource.String()
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: string(body)}); err != nil {
		p.teardown(s)
		ok = true // teardown closed it
		return nil, fmt.Errorf("WHIP answer: %w", err)
	}
	log.Printf("WHIP resource: %s", s.resource)
	p.cur.Store(s)
	ok = true
	return s, nil
}

// readRTCP drains a sender's incoming RTCP (NACKs only reach the NACK
// responder if something reads) and reports keyframe requests on video.
func (p *publisher) readRTCP(sender *webrtc.RTPSender, video bool) {
	for {
		packets, _, err := sender.ReadRTCP()
		if err != nil {
			return
		}
		if !video {
			continue
		}
		for _, pkt := range packets {
			switch pkt.(type) {
			case *rtcp.PictureLossIndication, *rtcp.FullIntraRequest:
				now := time.Now().UnixNano()
				last := p.lastKeyReq.Load()
				if now-last >= int64(keyframeReqMinGap) && p.lastKeyReq.CompareAndSwap(last, now) {
					p.events.emit(map[string]string{"event": "keyframe_request"})
				}
			}
		}
	}
}
