// loadgen: joins N listen-only viewers to a Galene group and measures what they receive (G3).
//
// Each viewer speaks Galene's WebSocket protocol, requests the program and
// answers the server's offer with Pion. Every interval it prints one JSON line:
// total received Mbps (= what the hall uploads), per-viewer kbps, viewers with
// media, and RTP packets lost (sequence gaps).
//
//	go run . -n 50 -duration 120s [-audio-only]
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/webrtc/v4"
)

var (
	server    = flag.String("server", "http://127.0.0.1:8443", "Galene base URL")
	group     = flag.String("group", "g3", "group (wildcard observe users)")
	n         = flag.Int("n", 50, "number of viewers")
	audioOnly = flag.Bool("audio-only", false, "request audio only")
	duration  = flag.Duration("duration", 120*time.Second, "measurement time after all viewers joined")
	interval  = flag.Duration("interval", 10*time.Second, "report interval")
	ramp      = flag.Duration("ramp", 50*time.Millisecond, "delay between viewer joins")
)

type counters struct {
	bytes, packets, lost atomic.Int64
	videoBytes           atomic.Int64
}

type viewer struct {
	idx    int
	ws     *websocket.Conn
	wmu    sync.Mutex
	pcs    sync.Map
	c      *counters
	gotRTP atomic.Bool
}

func (v *viewer) send(m map[string]any) {
	v.wmu.Lock()
	defer v.wmu.Unlock()
	v.ws.WriteJSON(m)
}

func (v *viewer) run() error {
	u, _ := url.Parse(*server)
	u.Scheme = strings.Replace(u.Scheme, "http", "ws", 1)
	u.Path = "/ws"
	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		return err
	}
	v.ws = ws
	id := fmt.Sprintf("load-%d-%d", v.idx, time.Now().UnixNano())
	v.send(map[string]any{"type": "handshake", "version": []string{"2"}, "id": id})
	v.send(map[string]any{"type": "join", "kind": "join", "group": *group, "username": fmt.Sprintf("viewer%03d", v.idx), "password": "x"})
	req := []string{"audio", "video"}
	if *audioOnly {
		req = []string{"audio"}
	}
	go func() {
		for {
			var m map[string]any
			if err := ws.ReadJSON(&m); err != nil {
				return
			}
			s := func(k string) string { x, _ := m[k].(string); return x }
			switch s("type") {
			case "ping":
				v.send(map[string]any{"type": "pong"})
			case "joined":
				if s("kind") == "join" {
					v.send(map[string]any{"type": "request", "request": map[string]any{"": req}})
				}
			case "offer":
				go v.answer(s("id"), s("sdp"))
			case "ice":
				if pc, ok := v.pcs.Load(s("id")); ok {
					var cand webrtc.ICECandidateInit
					b, _ := json.Marshal(m["candidate"])
					if json.Unmarshal(b, &cand) == nil {
						pc.(*webrtc.PeerConnection).AddICECandidate(cand)
					}
				}
			}
		}
	}()
	return nil
}

func (v *viewer) answer(id, sdp string) {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Print(err)
		return
	}
	v.pcs.Store(id, pc)
	pc.OnTrack(func(t *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		video := t.Kind() == webrtc.RTPCodecTypeVideo
		var last uint16
		first := true
		buf := make([]byte, 1600)
		for {
			nb, _, err := t.Read(buf)
			if err != nil {
				return
			}
			seq := uint16(buf[2])<<8 | uint16(buf[3])
			if !first {
				if gap := int64(uint16(seq - last)); gap > 1 && gap < 1000 {
					v.c.lost.Add(gap - 1)
				}
			}
			first, last = false, seq
			v.c.bytes.Add(int64(nb))
			v.c.packets.Add(1)
			if video {
				v.c.videoBytes.Add(int64(nb))
			}
			v.gotRTP.Store(true)
		}
	})
	pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdp})
	ans, _ := pc.CreateAnswer(nil)
	done := webrtc.GatheringCompletePromise(pc)
	pc.SetLocalDescription(ans)
	<-done
	v.send(map[string]any{"type": "answer", "id": id, "sdp": pc.LocalDescription().SDP})
}

func main() {
	flag.Parse()
	c := &counters{}
	viewers := make([]*viewer, *n)
	t0 := time.Now()
	for i := range viewers {
		viewers[i] = &viewer{idx: i, c: c}
		if err := viewers[i].run(); err != nil {
			log.Fatalf("viewer %d: %v", i, err)
		}
		time.Sleep(*ramp)
	}
	// Wait until every viewer receives media (or 30 s).
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		ok := 0
		for _, v := range viewers {
			if v.gotRTP.Load() {
				ok++
			}
		}
		if ok == *n {
			break
		}
	}
	fmt.Fprintf(os.Stderr, "%d viewers joined in %.1f s\n", *n, time.Since(t0).Seconds())

	start := time.Now()
	lastB, lastV, lastP, lastL := c.bytes.Load(), c.videoBytes.Load(), c.packets.Load(), c.lost.Load()
	tick := time.NewTicker(*interval)
	for range tick.C {
		b, vb, p, l := c.bytes.Load(), c.videoBytes.Load(), c.packets.Load(), c.lost.Load()
		secs := interval.Seconds()
		withMedia := 0
		for _, v := range viewers {
			if v.gotRTP.Load() {
				withMedia++
			}
		}
		row := map[string]any{
			"t":             int(time.Since(start).Seconds()),
			"viewers":       *n,
			"with_media":    withMedia,
			"total_mbps":    float64(b-lastB) * 8 / secs / 1e6,
			"per_viewer_kb": float64(b-lastB) * 8 / secs / 1e3 / float64(*n),
			"video_mbps":    float64(vb-lastV) * 8 / secs / 1e6,
			"packets":       p - lastP,
			"lost":          l - lastL,
		}
		out, _ := json.Marshal(row)
		fmt.Println(string(out))
		lastB, lastV, lastP, lastL = b, vb, p, l
		if time.Since(start) >= *duration {
			return
		}
	}
}
