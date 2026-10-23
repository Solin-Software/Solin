package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/interceptor"
	"github.com/pion/webrtc/v4"
	"gopkg.in/hraban/opus.v2"
)

const (
	wsPingInterval = 20 * time.Second
	wsReadTimeout  = 75 * time.Second
	wsWriteTimeout = 5 * time.Second
	maxConcealed   = 5 // lost packets to conceal; larger gaps are left to the jitter buffer
)

type receiveConfig struct {
	ws         string
	group      string
	tokens     tokenSource
	ignoreUser string
	device     string
	gain       float64
	channels   int
	periodMs   int
	periods    int
	jitterMs   int
	maxDelayMs int
}

func runReceive(args []string) error {
	fs := flag.NewFlagSet("receive", flag.ExitOnError)
	var cfg receiveConfig
	fs.StringVar(&cfg.ws, "ws", "", "Galene WebSocket URL, e.g. wss://host/ws (required)")
	fs.StringVar(&cfg.group, "group", "", "group name (required)")
	fs.StringVar(&cfg.tokens.token, "token", "", "Galene join token")
	fs.StringVar(&cfg.tokens.file, "token-file", "", "read the token from this file on every (re)join (overrides --token)")
	fs.StringVar(&cfg.ignoreUser, "ignore-user", "", "refuse streams published by this username (the hall's own program)")
	fs.StringVar(&cfg.device, "device", "", "playback device id (from \"devices\") or exact name; default: system default")
	fs.Float64Var(&cfg.gain, "gain", 1.0, "linear playback gain")
	fs.IntVar(&cfg.channels, "channels", 2, "output channels: 1 (mono) or 2 (stereo)")
	fs.IntVar(&cfg.periodMs, "period-ms", 10, "audio device period, ms")
	fs.IntVar(&cfg.periods, "periods", 3, "audio device periods (buffer = periods x period)")
	fs.IntVar(&cfg.jitterMs, "jitter-ms", 40, "audio buffered per stream before playback starts, ms")
	fs.IntVar(&cfg.maxDelayMs, "max-delay-ms", 200, "per-stream buffer above which old audio is dropped, ms")
	fs.Parse(args)
	switch {
	case cfg.ws == "" || cfg.group == "":
		return withCode(exitUsage, errors.New("receive: --ws and --group are required"))
	case cfg.tokens.token == "" && cfg.tokens.file == "":
		return withCode(exitUsage, errors.New("receive: --token or --token-file is required"))
	case cfg.channels != 1 && cfg.channels != 2:
		return withCode(exitUsage, errors.New("receive: --channels must be 1 or 2"))
	case cfg.gain < 0 || cfg.periodMs <= 0 || cfg.periods <= 0 || cfg.jitterMs < 0 || cfg.maxDelayMs <= cfg.jitterMs:
		return withCode(exitUsage, errors.New("receive: bad gain or buffer sizes"))
	}
	if u, err := url.Parse(cfg.ws); err != nil || (u.Scheme != "ws" && u.Scheme != "wss") {
		return withCode(exitUsage, fmt.Errorf("receive: --ws must be a ws:// or wss:// URL"))
	}

	framesPerMs := opusRate / 1000
	mix := NewMixer(cfg.channels, cfg.jitterMs*framesPerMs, cfg.maxDelayMs*framesPerMs)
	events := newEmitter()
	pl, err := openPlayer(cfg.device, cfg.channels, cfg.periodMs, cfg.periods, mix)
	if err != nil {
		events.state("failed")
		return withCode(exitDevice, fmt.Errorf("audio: %w", err))
	}
	defer pl.close()

	r, err := newReceiver(cfg, events, mix)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	deviceLost := make(chan struct{})
	go func() {
		select {
		case <-pl.stopped:
			close(deviceLost)
			cancel()
		case <-ctx.Done():
		}
	}()
	go logMixerHealth(ctx, mix)
	err = r.run(ctx)
	select {
	case <-deviceLost:
		events.state("failed")
		return withCode(exitDevice, errors.New("audio device stopped"))
	default:
	}
	return err
}

// logMixerHealth reports jitter-buffer underruns and overflows, if any, every
// 30 s (a remote stream stalling, or sender/device clock drift).
func logMixerHealth(ctx context.Context, mix *Mixer) {
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			if u, o := mix.Counters(); u > 0 || o > 0 {
				log.Printf("playback: %d underruns, %d overflows in the last 30 s", u, o)
			}
		}
	}
}

// galeneMessage is the subset of Galene's protocol messages the receiver uses.
type galeneMessage struct {
	Type             string                   `json:"type"`
	Kind             string                   `json:"kind,omitempty"`
	ID               string                   `json:"id,omitempty"`
	Username         *string                  `json:"username,omitempty"`
	Error            string                   `json:"error,omitempty"`
	Value            json.RawMessage          `json:"value,omitempty"`
	SDP              string                   `json:"sdp,omitempty"`
	Candidate        *webrtc.ICECandidateInit `json:"candidate,omitempty"`
	RTCConfiguration json.RawMessage          `json:"rtcConfiguration,omitempty"`
}

func (m *galeneMessage) username() string {
	if m.Username == nil {
		return ""
	}
	return *m.Username
}

func (m *galeneMessage) valueString() string {
	var s string
	if json.Unmarshal(m.Value, &s) == nil {
		return s
	}
	return string(m.Value)
}

// joinFailure classifies a "joined" message of kind "fail": fatal ones
// (token refused, group missing) make the process exit with exitAuth.
func joinFailure(m *galeneMessage) error {
	reason := m.valueString()
	switch {
	case m.Error == "duplicate-username", reason == "internal server error":
		return fmt.Errorf("join failed: %s", reason) // transient
	default:
		return fmt.Errorf("join failed: %s: %w", reason, errAuth)
	}
}

type receiver struct {
	cfg    receiveConfig
	events *emitter
	mix    *Mixer
	api    *webrtc.API
	gain   float32

	rtc atomic.Pointer[webrtc.Configuration]
}

func newReceiver(cfg receiveConfig, events *emitter, mix *Mixer) (*receiver, error) {
	m := &webrtc.MediaEngine{}
	if err := m.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{
			MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2,
			SDPFmtpLine: "minptime=10;useinbandfec=1;stereo=1",
		},
		PayloadType: 111,
	}, webrtc.RTPCodecTypeAudio); err != nil {
		return nil, err
	}
	ir := &interceptor.Registry{}
	if err := webrtc.RegisterDefaultInterceptors(m, ir); err != nil {
		return nil, err
	}
	r := &receiver{
		cfg: cfg, events: events, mix: mix, gain: float32(cfg.gain),
		api: webrtc.NewAPI(webrtc.WithMediaEngine(m), webrtc.WithInterceptorRegistry(ir)),
	}
	r.rtc.Store(&webrtc.Configuration{})
	return r, nil
}

func (r *receiver) run(ctx context.Context) error {
	bo := newBackoff(time.Second, 10*time.Second)
	state := "connecting"
	for {
		r.events.state(state)
		joined, err := r.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		log.Printf("galene: %v", err)
		if errors.Is(err, errAuth) {
			r.events.state("failed")
			return withCode(exitAuth, err)
		}
		if joined {
			bo.Reset()
		}
		state = "reconnecting"
		r.events.state(state)
		delay := bo.Next()
		log.Printf("reconnecting in %v", delay)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
	}
}

// wsConn serialises writes to the WebSocket.
type wsConn struct {
	mu sync.Mutex
	ws *websocket.Conn
}

func (c *wsConn) send(v any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	_ = c.ws.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
	return c.ws.WriteJSON(v)
}

// session runs one WebSocket connection; it returns whether the group was
// joined and why the session ended.
func (r *receiver) session(ctx context.Context) (joined bool, err error) {
	token, err := r.cfg.tokens.get()
	if err != nil {
		return false, err
	}
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second, Proxy: http.ProxyFromEnvironment}
	ws, resp, err := dialer.DialContext(ctx, r.cfg.ws, nil)
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return false, fmt.Errorf("websocket: %s: %w", resp.Status, errAuth)
		}
		return false, fmt.Errorf("websocket: %w", err)
	}
	conn := &wsConn{ws: ws}
	streams := map[string]*downStream{}
	defer func() {
		ws.Close()
		for _, s := range streams {
			s.close()
		}
	}()
	// Close the socket when ctx ends so the blocking read returns.
	sessionDone := make(chan struct{})
	defer close(sessionDone)
	go func() {
		ping := time.NewTicker(wsPingInterval)
		defer ping.Stop()
		for {
			select {
			case <-ctx.Done():
				conn.mu.Lock()
				_ = ws.WriteControl(websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""), time.Now().Add(time.Second))
				conn.mu.Unlock()
				ws.Close()
				return
			case <-sessionDone:
				return
			case <-ping.C:
				_ = conn.send(galeneMessage{Type: "ping"})
			}
		}
	}()

	id := fmt.Sprintf("solin-bridge-%d", time.Now().UnixNano())
	if err := conn.send(map[string]any{"type": "handshake", "version": []string{"2"}, "id": id}); err != nil {
		return false, err
	}
	if err := conn.send(map[string]any{"type": "join", "kind": "join", "group": r.cfg.group, "token": token}); err != nil {
		return false, err
	}

	for {
		_ = ws.SetReadDeadline(time.Now().Add(wsReadTimeout))
		var m galeneMessage
		if err := ws.ReadJSON(&m); err != nil {
			var ce *websocket.CloseError
			if errors.As(err, &ce) && (ce.Code == 4401 || ce.Code == 4403) {
				return joined, fmt.Errorf("websocket closed: %d %s: %w", ce.Code, ce.Text, errAuth)
			}
			return joined, fmt.Errorf("websocket: %w", err)
		}
		switch m.Type {
		case "ping":
			_ = conn.send(galeneMessage{Type: "pong"})
		case "joined":
			switch m.Kind {
			case "join", "change":
				if conf, err := rtcConfigurationFromGalene(m.RTCConfiguration); err != nil {
					log.Printf("rtcConfiguration: %v", err)
				} else {
					r.rtc.Store(&conf)
				}
				if m.Kind == "join" {
					joined = true
					log.Printf("joined %s as %q", r.cfg.group, m.username())
					if err := conn.send(map[string]any{"type": "request", "request": map[string][]string{"": {"audio"}}}); err != nil {
						return joined, err
					}
					r.events.state("connected")
				}
			case "fail":
				return joined, joinFailure(&m)
			case "leave":
				return joined, errors.New("left the group (kicked or group closed)")
			}
		case "offer":
			if s, ok := streams[m.ID]; ok {
				if err := s.renegotiate(m.SDP); err != nil {
					log.Printf("stream %s: renegotiate: %v", m.ID, err)
					_ = conn.send(galeneMessage{Type: "abort", ID: m.ID})
				}
				continue
			}
			user := m.username()
			if r.cfg.ignoreUser != "" && user == r.cfg.ignoreUser {
				_ = conn.send(galeneMessage{Type: "abort", ID: m.ID})
				continue
			}
			s, err := r.newDownStream(conn, m.ID, user, m.SDP)
			if err != nil {
				log.Printf("stream %s from %q: %v", m.ID, user, err)
				_ = conn.send(galeneMessage{Type: "abort", ID: m.ID})
				continue
			}
			streams[m.ID] = s
		case "ice":
			if s, ok := streams[m.ID]; ok && m.Candidate != nil {
				if err := s.pc.AddICECandidate(*m.Candidate); err != nil {
					log.Printf("stream %s: ICE candidate: %v", m.ID, err)
				}
			}
		case "close":
			if s, ok := streams[m.ID]; ok {
				delete(streams, m.ID)
				s.close()
			}
		case "usermessage":
			if m.Kind == "error" || m.Kind == "warning" {
				log.Printf("galene %s: %s", m.Kind, m.valueString())
			}
		}
	}
}

// downStream is one stream offered by Galene (one remote participant).
type downStream struct {
	r    *receiver
	conn *wsConn
	id   string
	user string
	pc   *webrtc.PeerConnection

	mu       sync.Mutex
	answered bool
	pending  []webrtc.ICECandidateInit

	started bool // guarded by mu
	closed  bool // guarded by mu
}

func (r *receiver) newDownStream(conn *wsConn, id, user, sdp string) (*downStream, error) {
	pc, err := r.api.NewPeerConnection(*r.rtc.Load())
	if err != nil {
		return nil, err
	}
	s := &downStream{r: r, conn: conn, id: id, user: user, pc: pc}
	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		init := c.ToJSON()
		s.mu.Lock()
		if !s.answered {
			s.pending = append(s.pending, init)
			s.mu.Unlock()
			return
		}
		s.mu.Unlock()
		_ = conn.send(galeneMessage{Type: "ice", ID: id, Candidate: &init})
	})
	pc.OnConnectionStateChange(func(st webrtc.PeerConnectionState) {
		log.Printf("stream %s from %q: %s", id, user, st)
		if st == webrtc.PeerConnectionStateFailed {
			// Ask Galene to close it; the matching "close" ends the stream.
			_ = conn.send(galeneMessage{Type: "abort", ID: id})
		}
	})
	pc.OnTrack(func(t *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		if t.Kind() != webrtc.RTPCodecTypeAudio {
			return
		}
		go s.play(t)
	})
	if err := s.answer(sdp); err != nil {
		pc.Close()
		return nil, err
	}
	return s, nil
}

func (s *downStream) answer(sdp string) error {
	if err := s.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdp}); err != nil {
		return err
	}
	ans, err := s.pc.CreateAnswer(nil)
	if err != nil {
		return err
	}
	if err := s.pc.SetLocalDescription(ans); err != nil {
		return err
	}
	if err := s.conn.send(galeneMessage{Type: "answer", ID: s.id, SDP: s.pc.LocalDescription().SDP}); err != nil {
		return err
	}
	s.mu.Lock()
	s.answered = true
	pending := s.pending
	s.pending = nil
	s.mu.Unlock()
	for i := range pending {
		_ = s.conn.send(galeneMessage{Type: "ice", ID: s.id, Candidate: &pending[i]})
	}
	return nil
}

func (s *downStream) renegotiate(sdp string) error { return s.answer(sdp) }

func (s *downStream) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	if s.started {
		log.Printf("stream %s from %q stopped", s.id, s.user)
		s.r.events.emit(map[string]string{"event": "stream", "id": s.id, "user": s.user, "state": "stopped"})
	}
	s.mu.Unlock()
	_ = s.pc.Close()
}

// markStarted emits the "started" event once, unless the stream is closed.
func (s *downStream) markStarted(mime string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return false
	}
	if !s.started {
		s.started = true
		log.Printf("stream %s from %q started (%s)", s.id, s.user, mime)
		s.r.events.emit(map[string]string{"event": "stream", "id": s.id, "user": s.user, "state": "started"})
	}
	return true
}

// play decodes one remote Opus track into the mixer, concealing small losses
// with in-band FEC (the packet after a loss carries a low-bitrate copy of the
// lost one) and packet-loss concealment.
func (s *downStream) play(t *webrtc.TrackRemote) {
	channels := s.r.cfg.channels
	dec, err := opus.NewDecoder(opusRate, channels)
	if err != nil {
		log.Printf("stream %s: opus decoder: %v", s.id, err)
		return
	}
	if !s.markStarted(t.Codec().MimeType) {
		return
	}
	in := s.r.mix.NewInput(s.r.gain)
	defer s.r.mix.Remove(in)
	pcm := make([]float32, opusRate*120/1000*channels) // the longest Opus packet
	frame := opusFrame                                 // samples per channel of the last packet
	var last uint16
	first := true
	var concealed, decodeErrors int
	for {
		p, _, err := t.ReadRTP()
		if err != nil {
			if concealed > 0 || decodeErrors > 0 {
				log.Printf("stream %s: %d packets concealed, %d undecodable", s.id, concealed, decodeErrors)
			}
			return
		}
		if len(p.Payload) == 0 {
			continue
		}
		if !first {
			lost, late := seqGap(last, p.SequenceNumber)
			if late {
				continue
			}
			if lost > 0 && lost <= maxConcealed {
				buf := pcm[:frame*channels]
				for i := 0; i < lost-1; i++ {
					if dec.DecodePLCFloat32(buf) == nil {
						in.Write(buf)
					}
				}
				if dec.DecodeFECFloat32(p.Payload, buf) == nil || dec.DecodePLCFloat32(buf) == nil {
					in.Write(buf)
				}
				concealed += lost
			}
		}
		first, last = false, p.SequenceNumber
		n, err := dec.DecodeFloat32(p.Payload, pcm)
		if err != nil {
			decodeErrors++
			continue
		}
		frame = n
		in.Write(pcm[:n*channels])
	}
}
