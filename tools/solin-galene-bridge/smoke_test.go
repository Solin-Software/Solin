package main

// End-to-end smoke test against a real Galene. Skipped unless
// SOLIN_BRIDGE_SMOKE=1. It needs the galene binary (SOLIN_GALENE, or
// build/hosted-meeting/<platform>/galene in the repository), ffmpeg with
// libx264 on PATH, and a working default playback device.
//
//	SOLIN_BRIDGE_SMOKE=1 go test -tags nolibopusfile -run Smoke -v ./...

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/webrtc/v4"
	"gopkg.in/hraban/opus.v2"
)

const smokeGroup = "smoke-hall"

func b64url(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// signToken makes a Galene HS256 join token, like solin.core.hosted_meeting.tokens.
func signToken(key []byte, user, audience string, perms []string, ttl time.Duration) string {
	if perms == nil {
		perms = []string{}
	}
	now := time.Now().Unix()
	header, _ := json.Marshal(map[string]string{"alg": "HS256", "typ": "JWT"})
	claims, _ := json.Marshal(map[string]any{
		"sub": user, "aud": audience, "permissions": perms,
		"iat": now, "exp": now + int64(ttl/time.Second),
	})
	input := b64url(header) + "." + b64url(claims)
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(input))
	return input + "." + b64url(mac.Sum(nil))
}

func freePort(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

func platformKey() string {
	return runtime.GOOS + "-" + runtime.GOARCH
}

type galeneProc struct {
	t    *testing.T
	bin  string
	root string
	port int
	cmd  *exec.Cmd
	log  *lockedBuffer
}

func (g *galeneProc) start() {
	g.t.Helper()
	g.cmd = exec.Command(g.bin, "-insecure", "-http", fmt.Sprintf("127.0.0.1:%d", g.port), "-turn", "",
		"-data", filepath.Join(g.root, "data"), "-groups", filepath.Join(g.root, "groups"),
		"-static", filepath.Join(g.root, "static"))
	g.cmd.Stdout, g.cmd.Stderr = g.log, g.log
	if err := g.cmd.Start(); err != nil {
		g.t.Fatal(err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/group/%s/.status", g.port, smokeGroup))
		if err == nil {
			resp.Body.Close()
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	g.t.Fatal("galene did not start")
}

func (g *galeneProc) stop() {
	if g.cmd == nil {
		return
	}
	g.cmd.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() { g.cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		g.cmd.Process.Kill()
		<-done
	}
	g.cmd = nil
}

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

// bridgeProc is a running solin-galene-bridge with its JSON events.
type bridgeProc struct {
	name   string
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stderr *lockedBuffer
	events chan map[string]any
	exited chan struct{}
	code   int
}

func startBridge(t *testing.T, bin, name string, args ...string) *bridgeProc {
	t.Helper()
	b := &bridgeProc{name: name, stderr: &lockedBuffer{}, events: make(chan map[string]any, 256), exited: make(chan struct{})}
	b.cmd = exec.Command(bin, args...)
	b.cmd.Stderr = b.stderr
	var err error
	if b.stdin, err = b.cmd.StdinPipe(); err != nil {
		t.Fatal(err)
	}
	stdout, err := b.cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := b.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() {
		sc := bufio.NewScanner(stdout)
		for sc.Scan() {
			var ev map[string]any
			if json.Unmarshal(sc.Bytes(), &ev) == nil {
				select {
				case b.events <- ev:
				default:
				}
			}
		}
		b.cmd.Wait()
		b.code = b.cmd.ProcessState.ExitCode()
		close(b.exited)
	}()
	t.Cleanup(func() {
		select {
		case <-b.exited:
		default:
			b.cmd.Process.Kill()
			<-b.exited
		}
		if t.Failed() || testing.Verbose() {
			t.Logf("--- %s stderr ---\n%s", b.name, b.stderr.String())
		}
	})
	return b
}

// waitEvent waits for an event matching all key/values, skipping others.
func (b *bridgeProc) waitEvent(t *testing.T, timeout time.Duration, kv ...string) map[string]any {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case ev := <-b.events:
			match := true
			for i := 0; i+1 < len(kv); i += 2 {
				if fmt.Sprint(ev[kv[i]]) != kv[i+1] {
					match = false
				}
			}
			if match {
				return ev
			}
		case <-b.exited:
			t.Fatalf("%s exited (code %d) while waiting for %v", b.name, b.code, kv)
		case <-deadline:
			t.Fatalf("%s: no event %v within %v", b.name, kv, timeout)
		}
	}
}

func (b *bridgeProc) waitExit(t *testing.T, timeout time.Duration) int {
	t.Helper()
	select {
	case <-b.exited:
		return b.code
	case <-time.After(timeout):
		t.Fatalf("%s did not exit within %v", b.name, timeout)
		return -1
	}
}

// splitAccessUnits cuts an Annex-B stream at access unit delimiters (NAL 9).
func splitAccessUnits(stream []byte) [][]byte {
	var starts []int
	for i := 0; i+4 < len(stream); i++ {
		if stream[i] == 0 && stream[i+1] == 0 && stream[i+2] == 0 && stream[i+3] == 1 && stream[i+4]&0x1f == 9 {
			starts = append(starts, i)
		}
	}
	var aus [][]byte
	for i, s := range starts {
		end := len(stream)
		if i+1 < len(starts) {
			end = starts[i+1]
		}
		aus = append(aus, stream[s:end])
	}
	return aus
}

// feed writes framed media to w in real time until ctx ends or w fails:
// H.264 access units at 30 fps (looped) when aus is not nil, and 440 Hz
// stereo PCM in 1024-frame chunks like libobs.
func feed(ctx context.Context, w io.WriteCloser, aus [][]byte) {
	defer w.Close()
	start := time.Now()
	var vIdx, aFrames int
	const chunk = 1024
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
	var buf []byte
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		now := time.Since(start)
		buf = buf[:0]
		for aus != nil && time.Duration(vIdx)*time.Second/30 <= now {
			buf = AppendFrame(buf, kindH264, time.Duration(vIdx)*time.Second/30, aus[vIdx%len(aus)])
			vIdx++
		}
		for time.Duration(aFrames)*time.Second/48000 <= now {
			pcm := make([]byte, chunk*2*4)
			for i := 0; i < chunk; i++ {
				v := float32(0.3 * math.Sin(2*math.Pi*440*float64(aFrames+i)/48000))
				bits := math.Float32bits(v)
				for c := 0; c < 2; c++ {
					o := (i*2 + c) * 4
					pcm[o], pcm[o+1], pcm[o+2], pcm[o+3] = byte(bits), byte(bits>>8), byte(bits>>16), byte(bits>>24)
				}
			}
			buf = AppendFrame(buf, kindPCM, time.Duration(aFrames)*time.Second/48000, pcm)
			aFrames += chunk
		}
		if len(buf) > 0 {
			if _, err := w.Write(buf); err != nil {
				return
			}
		}
	}
}

// observer joins the group like a browser and counts what it receives.
type observer struct {
	videoPackets, audioPackets atomic.Int64
	audioRMS                   atomic.Uint64 // float64 bits, RMS of the latest decoded frame
	users                      sync.Map
}

func runObserver(ctx context.Context, t *testing.T, wsURL, token string) *observer {
	ob := &observer{}
	ws, _, err := websocket.DefaultDialer.DialContext(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("observer: %v", err)
	}
	var mu sync.Mutex
	send := func(v any) { mu.Lock(); defer mu.Unlock(); ws.WriteJSON(v) }
	send(map[string]any{"type": "handshake", "version": []string{"2"}, "id": "observer"})
	send(map[string]any{"type": "join", "kind": "join", "group": smokeGroup, "token": token})
	pcs := map[string]*webrtc.PeerConnection{}
	go func() { <-ctx.Done(); ws.Close() }()
	go func() {
		defer func() {
			for _, pc := range pcs {
				pc.Close()
			}
		}()
		for {
			var m galeneMessage
			if err := ws.ReadJSON(&m); err != nil {
				return
			}
			switch m.Type {
			case "ping":
				send(map[string]any{"type": "pong"})
			case "joined":
				if m.Kind == "join" {
					send(map[string]any{"type": "request", "request": map[string][]string{"": {"audio", "video"}}})
				}
			case "offer":
				ob.users.Store(m.username(), true)
				pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
				if err != nil {
					continue
				}
				pcs[m.ID] = pc
				pc.OnTrack(func(tr *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
					var dec *opus.Decoder
					if tr.Kind() == webrtc.RTPCodecTypeAudio {
						dec, _ = opus.NewDecoder(48000, 2)
					}
					pcm := make([]float32, 5760*2)
					for {
						p, _, err := tr.ReadRTP()
						if err != nil {
							return
						}
						if dec == nil {
							ob.videoPackets.Add(1)
							continue
						}
						ob.audioPackets.Add(1)
						if n, err := dec.DecodeFloat32(p.Payload, pcm); err == nil && n > 0 {
							var sum float64
							for _, v := range pcm[:2*n] {
								sum += float64(v) * float64(v)
							}
							ob.audioRMS.Store(math.Float64bits(math.Sqrt(sum / float64(2*n))))
						}
					}
				})
				pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: m.SDP})
				ans, _ := pc.CreateAnswer(nil)
				gathered := webrtc.GatheringCompletePromise(pc)
				pc.SetLocalDescription(ans)
				<-gathered
				send(map[string]any{"type": "answer", "id": m.ID, "sdp": pc.LocalDescription().SDP})
			case "ice":
				if pc, ok := pcs[m.ID]; ok && m.Candidate != nil {
					pc.AddICECandidate(*m.Candidate)
				}
			case "close":
				if pc, ok := pcs[m.ID]; ok {
					pc.Close()
					delete(pcs, m.ID)
				}
			}
		}
	}()
	return ob
}

func waitFor(t *testing.T, timeout time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestSmoke(t *testing.T) {
	if os.Getenv("SOLIN_BRIDGE_SMOKE") != "1" {
		t.Skip("set SOLIN_BRIDGE_SMOKE=1 to run the end-to-end smoke test")
	}
	galeneBin := os.Getenv("SOLIN_GALENE")
	if galeneBin == "" {
		galeneBin, _ = filepath.Abs(filepath.Join("..", "..", "build", "hosted-meeting", platformKey(), "galene"))
	}
	if _, err := os.Stat(galeneBin); err != nil {
		t.Fatalf("galene binary: %v", err)
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		t.Fatal("ffmpeg not found")
	}
	tmp := t.TempDir()

	// The bridge under test.
	bin := filepath.Join(tmp, "solin-galene-bridge")
	if out, err := exec.Command("go", "build", "-tags", "nolibopusfile", "-o", bin, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}

	// Synthetic H.264: baseline, 1 s GOP, with access unit delimiters.
	h264 := filepath.Join(tmp, "test.h264")
	ff := exec.Command("ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
		"-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1", "-pix_fmt", "yuv420p", "-g", "30",
		"-x264-params", "aud=1:repeat-headers=1", "-tune", "zerolatency", "-b:v", "800k", "-f", "h264", h264)
	if out, err := ff.CombinedOutput(); err != nil {
		t.Fatalf("ffmpeg: %v\n%s", err, out)
	}
	raw, err := os.ReadFile(h264)
	if err != nil {
		t.Fatal(err)
	}
	aus := splitAccessUnits(raw)
	if len(aus) < 100 {
		t.Fatalf("only %d access units", len(aus))
	}

	// Galene with one token-only group.
	key := make([]byte, 32)
	rand.Read(key)
	root := filepath.Join(tmp, "galene")
	for _, d := range []string{"data", "groups", "static"} {
		os.MkdirAll(filepath.Join(root, d), 0o755)
	}
	os.WriteFile(filepath.Join(root, "data", "config.json"), []byte("{}"), 0o644)
	group, _ := json.Marshal(map[string]any{
		"codecs":   []string{"h264", "opus"},
		"authKeys": []map[string]string{{"kty": "oct", "alg": "HS256", "k": b64url(key)}},
	})
	os.WriteFile(filepath.Join(root, "groups", smokeGroup+".json"), group, 0o644)
	g := &galeneProc{t: t, bin: galeneBin, root: root, port: freePort(t), log: &lockedBuffer{}}
	g.start()
	t.Cleanup(func() {
		g.stop()
		if t.Failed() {
			t.Logf("--- galene ---\n%s", g.log.String())
		}
	})
	base := fmt.Sprintf("http://127.0.0.1:%d", g.port)
	aud := base + "/group/" + smokeGroup + "/"
	whip := base + "/group/" + smokeGroup + "/.whip"
	wsURL := fmt.Sprintf("ws://127.0.0.1:%d/ws", g.port)
	tok := func(user string, perms ...string) string { return signToken(key, user, aud, perms, time.Hour) }

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 1. The hall program: video + PCM through publish.
	program := startBridge(t, bin, "publish(program)", "publish", "--whip", whip, "--token", tok("hall-program", "present"))
	go feed(ctx, program.stdin, aus)
	program.waitEvent(t, 20*time.Second, "event", "state", "state", "connected")
	t.Log("program publisher connected")

	// 2. A browser-like observer receives both tracks; the audio is the sine.
	ob := runObserver(ctx, t, wsURL, tok("observer"))
	waitFor(t, 20*time.Second, "observer media", func() bool {
		return ob.videoPackets.Load() > 100 && ob.audioPackets.Load() > 100
	})
	rms := math.Float64frombits(ob.audioRMS.Load())
	if rms < 0.1 || rms > 0.4 { // a 0.3 sine has RMS 0.21
		t.Fatalf("observer audio RMS %.3f, want about 0.21", rms)
	}
	t.Logf("observer: %d video, %d audio packets, audio RMS %.3f", ob.videoPackets.Load(), ob.audioPackets.Load(), rms)
	stats := program.waitEvent(t, 12*time.Second, "event", "stats")
	if stats["video_kbps"].(float64) <= 0 || stats["audio_kbps"].(float64) <= 0 {
		t.Fatalf("stats = %v", stats)
	}
	t.Logf("program stats: %v", stats)

	// 3. The hall receiver ignores the program and plays a commenter.
	rx := startBridge(t, bin, "receive", "receive", "--ws", wsURL, "--group", smokeGroup, "--token", tok("hall-rx"),
		"--ignore-user", "hall-program", "--gain", "0")
	rx.waitEvent(t, 10*time.Second, "event", "state", "state", "connected")
	commenter := startBridge(t, bin, "publish(commenter)", "publish", "--whip", whip, "--token", tok("Ana Souza", "present"))
	commenterCtx, stopCommenter := context.WithCancel(ctx)
	go feed(commenterCtx, commenter.stdin, nil)
	commenter.waitEvent(t, 20*time.Second, "event", "state", "state", "connected")
	ev := rx.waitEvent(t, 15*time.Second, "event", "stream", "state", "started")
	if ev["user"] != "Ana Souza" {
		t.Fatalf("receive started a stream from %v, want only the commenter", ev["user"])
	}
	time.Sleep(3 * time.Second) // play a little
	stopCommenter()             // closes the commenter's stdin
	if code := commenter.waitExit(t, 10*time.Second); code != 0 {
		t.Fatalf("commenter exit code %d", code)
	}
	ev = rx.waitEvent(t, 10*time.Second, "event", "stream", "state", "stopped")
	if ev["user"] != "Ana Souza" {
		t.Fatalf("stopped event for %v", ev["user"])
	}
	if strings.Contains(rx.stderr.String(), "hall-program\" started") {
		t.Fatal("receive played the hall's own program")
	}
	t.Log("receive played and released the commenter, ignored the program")

	// 4. Galene restarts: both bridges reconnect on their own.
	g.stop()
	program.waitEvent(t, 15*time.Second, "event", "state", "state", "reconnecting")
	rx.waitEvent(t, 15*time.Second, "event", "state", "state", "reconnecting")
	time.Sleep(2 * time.Second)
	g.start()
	program.waitEvent(t, 30*time.Second, "event", "state", "state", "connected")
	rx.waitEvent(t, 30*time.Second, "event", "state", "state", "connected")
	t.Log("both bridges reconnected after a Galene restart")

	// 5. Refused tokens exit with code 3.
	bad := signToken([]byte("wrong key"), "intruder", aud, []string{"present"}, time.Hour)
	badPub := startBridge(t, bin, "publish(bad)", "publish", "--whip", whip, "--token", bad)
	if code := badPub.waitExit(t, 15*time.Second); code != exitAuth {
		t.Fatalf("publish with a bad token exited %d, want %d", code, exitAuth)
	}
	badRx := startBridge(t, bin, "receive(bad)", "receive", "--ws", wsURL, "--group", smokeGroup, "--token", bad, "--gain", "0")
	if code := badRx.waitExit(t, 15*time.Second); code != exitAuth {
		t.Fatalf("receive with a bad token exited %d, want %d", code, exitAuth)
	}

	// 6. Clean shutdown on SIGINT.
	for _, b := range []*bridgeProc{program, rx} {
		b.cmd.Process.Signal(os.Interrupt)
		if code := b.waitExit(t, 10*time.Second); code != 0 {
			t.Fatalf("%s exit code %d after SIGINT", b.name, code)
		}
	}
	if !strings.Contains(program.stderr.String(), "WHIP DELETE: 200") {
		t.Fatalf("program did not delete its WHIP resource on shutdown")
	}
	cancel()
}
