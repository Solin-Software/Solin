// hallrx: the hall's receive side for remote comments (G5).
//
// Joins a Galene group, requests audio, decodes each remote Opus stream with
// libopus and writes 48 kHz mono s16le PCM to stdout, for the hall script to
// play on the hall speakers and feed to the echo canceller. Streams from
// -ignore (the hall's own WHIP publisher) are refused. Lost packets are
// concealed with Opus PLC. One active commenter at a time (the operator
// unmutes one person), so streams are written as they arrive, not mixed.
//
// Build: go build -tags nolibopusfile -o hallrx .   (cgo + libopus-dev)
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/pion/webrtc/v4"
	"gopkg.in/hraban/opus.v2"
)

var (
	server   = flag.String("server", "http://127.0.0.1:8443", "Galene base URL")
	group    = flag.String("group", "g5", "group")
	username = flag.String("username", "hall", "username")
	password = flag.String("password", "x", "password")
	ignore   = flag.String("ignore", "whip", "username whose streams are not wanted (the hall's own program)")
)

const rate = 48000

var (
	outMu sync.Mutex
	out   = bufio.NewWriterSize(os.Stdout, 1<<16)
)

func writePCM(pcm []int16) {
	outMu.Lock()
	defer outMu.Unlock()
	binary.Write(out, binary.LittleEndian, pcm)
	out.Flush()
}

func main() {
	flag.Parse()
	u, _ := url.Parse(*server)
	u.Scheme = strings.Replace(u.Scheme, "http", "ws", 1)
	u.Path = "/ws"
	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		log.Fatal(err)
	}
	var wmu sync.Mutex
	send := func(m map[string]any) {
		wmu.Lock()
		defer wmu.Unlock()
		ws.WriteJSON(m)
	}
	id := fmt.Sprintf("hallrx-%d", time.Now().UnixNano())
	send(map[string]any{"type": "handshake", "version": []string{"2"}, "id": id})
	send(map[string]any{"type": "join", "kind": "join", "group": *group, "username": *username, "password": *password})

	var pcs sync.Map
	for {
		var m map[string]any
		if err := ws.ReadJSON(&m); err != nil {
			log.Fatalf("websocket: %v", err)
		}
		s := func(k string) string { x, _ := m[k].(string); return x }
		switch s("type") {
		case "ping":
			send(map[string]any{"type": "pong"})
		case "joined":
			if s("kind") == "join" {
				send(map[string]any{"type": "request", "request": map[string]any{"": []string{"audio"}}})
				log.Printf("joined %s", *group)
			} else if s("kind") == "fail" {
				log.Fatalf("join failed: %v", m["value"])
			}
		case "offer":
			if s("username") == *ignore {
				send(map[string]any{"type": "abort", "id": s("id")})
				continue
			}
			go answer(s("id"), s("username"), s("sdp"), send, &pcs)
		case "ice":
			if pc, ok := pcs.Load(s("id")); ok {
				var cand webrtc.ICECandidateInit
				b, _ := json.Marshal(m["candidate"])
				if json.Unmarshal(b, &cand) == nil {
					pc.(*webrtc.PeerConnection).AddICECandidate(cand)
				}
			}
		case "close":
			if pc, ok := pcs.LoadAndDelete(s("id")); ok {
				pc.(*webrtc.PeerConnection).Close()
				log.Printf("stream %s closed", s("id"))
			}
		}
	}
}

func answer(id, user, sdp string, send func(map[string]any), pcs *sync.Map) {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Print(err)
		return
	}
	pcs.Store(id, pc)
	pc.OnTrack(func(t *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		if t.Kind() != webrtc.RTPCodecTypeAudio {
			return
		}
		log.Printf("receiving audio from %s", user)
		dec, err := opus.NewDecoder(rate, 1)
		if err != nil {
			log.Print(err)
			return
		}
		pcm := make([]int16, rate/10) // up to 100 ms per packet
		frame := make([]int16, rate/50)
		var last uint16
		first := true
		for {
			p, _, err := t.ReadRTP()
			if err != nil {
				return
			}
			if !first {
				if gap := int(uint16(p.SequenceNumber - last)); gap > 1 && gap <= 6 {
					for i := 1; i < gap; i++ { // conceal lost 20 ms frames
						if dec.DecodePLC(frame) == nil {
							writePCM(frame)
						}
					}
				}
			}
			first, last = false, p.SequenceNumber
			n, err := dec.Decode(p.Payload, pcm)
			if err != nil {
				continue
			}
			writePCM(pcm[:n])
		}
	})
	pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdp})
	ans, _ := pc.CreateAnswer(nil)
	done := webrtc.GatheringCompletePromise(pc)
	pc.SetLocalDescription(ans)
	<-done
	send(map[string]any{"type": "answer", "id": id, "sdp": pc.LocalDescription().SDP})
}
