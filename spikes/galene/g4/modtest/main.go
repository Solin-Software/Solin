// modtest: checks whether Galene's permissions cover hall-meeting moderation (G4).
//
// Scripted clients speak Galene's WebSocket protocol directly, with Pion for
// media: an operator, attendees and a viewer. Each check prints PASS/FAIL and,
// where relevant, a measured time.
//
//	go run . -server http://127.0.0.1:8443 -group g4 -tgroup g4t -key <base64url HS256 key>
package main

import (
	"encoding/base64"
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

	"github.com/golang-jwt/jwt/v5"
	"github.com/gorilla/websocket"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

var (
	server  = flag.String("server", "http://127.0.0.1:8443", "Galene base URL")
	grp     = flag.String("group", "g4", "group with wildcard observe users and an op user")
	tgrp    = flag.String("tgroup", "g4t", "group that only accepts signed tokens")
	keyB64  = flag.String("key", "", "HS256 key (base64url) configured in tgroup's authKeys")
	timeout = 5 * time.Second
)

type msg map[string]any

func (m msg) s(k string) string { v, _ := m[k].(string); return v }

func (m msg) perms() []string {
	var out []string
	if a, ok := m["permissions"].([]any); ok {
		for _, p := range a {
			out = append(out, fmt.Sprint(p))
		}
	}
	return out
}

// client is one Galene WebSocket session.
type client struct {
	name  string
	id    string
	ws    *websocket.Conn
	wmu   sync.Mutex
	inbox chan msg
	pcs   sync.Map // stream id -> *webrtc.PeerConnection
	// set by viewers: time of the last RTP packet per stream
	lastRTP sync.Map // stream id -> *atomic.Int64 (unix ns)
	closed  atomic.Bool
}

func dial(name string) *client {
	u, _ := url.Parse(*server)
	u.Scheme = strings.Replace(u.Scheme, "http", "ws", 1)
	u.Path = "/ws"
	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		log.Fatalf("%s: dial: %v", name, err)
	}
	c := &client{name: name, id: fmt.Sprintf("%s-%d", name, time.Now().UnixNano()), ws: ws, inbox: make(chan msg, 256)}
	go c.read()
	c.send(msg{"type": "handshake", "version": []string{"2"}, "id": c.id})
	return c
}

func (c *client) send(m msg) {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if err := c.ws.WriteJSON(m); err != nil && !c.closed.Load() {
		log.Printf("%s: write: %v", c.name, err)
	}
}

func (c *client) read() {
	defer close(c.inbox)
	for {
		var m msg
		if err := c.ws.ReadJSON(&m); err != nil {
			c.closed.Store(true)
			return
		}
		switch m.s("type") {
		case "ping":
			c.send(msg{"type": "pong"})
			continue
		case "ice":
			if pc, ok := c.pcs.Load(m.s("id")); ok {
				var cand webrtc.ICECandidateInit
				b, _ := json.Marshal(m["candidate"])
				if json.Unmarshal(b, &cand) == nil {
					pc.(*webrtc.PeerConnection).AddICECandidate(cand)
				}
			}
			continue
		case "offer":
			go c.answer(m) // server pushes a stream we requested
		case "answer":
			if pc, ok := c.pcs.Load(m.s("id")); ok {
				pc.(*webrtc.PeerConnection).SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: m.s("sdp")})
			}
		}
		c.inbox <- m
	}
}

// waitFor returns the first message matching pred within d.
func (c *client) waitFor(d time.Duration, pred func(msg) bool) (msg, bool) {
	deadline := time.After(d)
	for {
		select {
		case m, ok := <-c.inbox:
			if !ok {
				return msg{"type": "_closed"}, pred(msg{"type": "_closed"})
			}
			if pred(m) {
				return m, true
			}
		case <-deadline:
			return nil, false
		}
	}
}

func (c *client) drain() {
	for {
		select {
		case <-c.inbox:
		default:
			return
		}
	}
}

func (c *client) join(group, username, password, token string) msg {
	m := msg{"type": "join", "kind": "join", "group": group, "username": username}
	if token != "" {
		m["token"] = token
	} else {
		m["password"] = password
	}
	c.send(m)
	r, _ := c.waitFor(timeout, func(m msg) bool { return m.s("type") == "joined" && m.s("kind") != "change" })
	return r
}

func newPC() *webrtc.PeerConnection {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Fatal(err)
	}
	return pc
}

// publish offers an audio (and optionally video) stream; returns its id.
func (c *client) publish(withVideo bool) string {
	id := fmt.Sprintf("up-%s-%d", c.name, time.Now().UnixNano())
	pc := newPC()
	c.pcs.Store(id, pc)
	audio, _ := webrtc.NewTrackLocalStaticSample(webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2}, "audio", id)
	pc.AddTrack(audio)
	if withVideo {
		video, _ := webrtc.NewTrackLocalStaticSample(webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000}, "video", id)
		pc.AddTrack(video)
	}
	go func() { // 20 ms Opus silence frames
		frame := []byte{0xf8, 0xff, 0xfe}
		for pc.ConnectionState() != webrtc.PeerConnectionStateClosed {
			audio.WriteSample(media.Sample{Data: frame, Duration: 20 * time.Millisecond})
			time.Sleep(20 * time.Millisecond)
		}
	}()
	offer, _ := pc.CreateOffer(nil)
	done := webrtc.GatheringCompletePromise(pc)
	pc.SetLocalDescription(offer)
	<-done
	c.send(msg{"type": "offer", "id": id, "label": "camera", "source": c.id, "sdp": pc.LocalDescription().SDP})
	return id
}

func (c *client) answer(m msg) {
	id := m.s("id")
	pc := newPC()
	c.pcs.Store(id, pc)
	last := &atomic.Int64{}
	c.lastRTP.Store(id, last)
	pc.OnTrack(func(t *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		for {
			if _, _, err := t.ReadRTP(); err != nil {
				return
			}
			last.Store(time.Now().UnixNano())
		}
	})
	pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: m.s("sdp")})
	ans, _ := pc.CreateAnswer(nil)
	done := webrtc.GatheringCompletePromise(pc)
	pc.SetLocalDescription(ans)
	<-done
	c.send(msg{"type": "answer", "id": id, "sdp": pc.LocalDescription().SDP})
}

// mediaFrom reports whether the viewer received RTP from any stream of username within d.
func (c *client) streamFrom(username string, d time.Duration) (string, bool) {
	m, ok := c.waitFor(d, func(m msg) bool { return m.s("type") == "offer" && m.s("username") == username })
	if !ok {
		return "", false
	}
	return m.s("id"), true
}

func (c *client) rtpFlowing(stream string, within time.Duration) bool {
	v, ok := c.lastRTP.Load(stream)
	if !ok {
		return false
	}
	return time.Since(time.Unix(0, v.(*atomic.Int64).Load())) < within
}

// --- results ---

var failures int

func check(name string, ok bool, detail string) {
	status := "PASS"
	if !ok {
		status = "FAIL"
		failures++
	}
	fmt.Printf("%-4s  %-58s %s\n", status, name, detail)
}

func mint(sub string, perms []string, aud string, exp time.Duration) string {
	key, err := base64.RawURLEncoding.DecodeString(*keyB64)
	if err != nil {
		log.Fatalf("key: %v", err)
	}
	claims := jwt.MapClaims{"sub": sub, "aud": aud, "permissions": perms, "iat": time.Now().Unix(), "exp": time.Now().Add(exp).Unix()}
	s, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString(key)
	if err != nil {
		log.Fatal(err)
	}
	return s
}

func main() {
	flag.Parse()

	op := dial("op")
	r := op.join(*grp, "operator", "op", "")
	check("operator joins with op", r.s("kind") == "join" && contains(r.perms(), "op"), fmt.Sprint(r.perms()))

	// Viewer stands in for the hall/other attendees and receives everything.
	viewer := dial("viewer")
	viewer.join(*grp, "viewer", "x", "")
	viewer.send(msg{"type": "request", "request": msg{"": []string{"audio", "video"}}})

	alice := dial("alice")
	r = alice.join(*grp, "alice", "x", "")
	check("attendee joins with observe (no permissions)", r.s("kind") == "join" && len(r.perms()) == 0, fmt.Sprint(r.perms()))

	// 1. Attendees can't publish on their own (no self-unmute).
	id := alice.publish(false)
	m, ok := alice.waitFor(timeout, func(m msg) bool {
		return (m.s("type") == "abort" && m.s("id") == id) || (m.s("type") == "usermessage" && m.s("kind") == "error")
	})
	check("attendee offer refused without present", ok, fmt.Sprintf("%s %v", m.s("type"), m["value"]))
	_, leaked := viewer.streamFrom("alice", 2*time.Second)
	check("refused offer never reaches other clients", !leaked, "")
	alice.drain()

	// 2. Raised hand reaches the operator (and, by design, everyone).
	alice.send(msg{"type": "useraction", "kind": "setdata", "source": alice.id, "dest": alice.id, "value": msg{"raisehand": true}})
	t0 := time.Now()
	hand := func(m msg) bool {
		d, _ := m["data"].(map[string]any)
		return m.s("type") == "user" && m.s("kind") == "change" && m.s("username") == "alice" && d["raisehand"] == true
	}
	_, ok = op.waitFor(timeout, hand)
	check("raised hand reaches operator", ok, fmt.Sprintf("%d ms", time.Since(t0).Milliseconds()))
	_, ok = viewer.waitFor(timeout, hand)
	check("raised hand is visible to other attendees (by design)", ok, "")

	// 3. Operator calls on alice: grant present, alice publishes, media reaches others.
	t0 = time.Now()
	op.send(msg{"type": "useraction", "kind": "present", "source": op.id, "dest": alice.id})
	_, ok = alice.waitFor(timeout, func(m msg) bool {
		return m.s("type") == "joined" && m.s("kind") == "change" && contains(m.perms(), "present")
	})
	check("operator grants present (unmute)", ok, fmt.Sprintf("%d ms to permission event", time.Since(t0).Milliseconds()))
	alice.publish(false)
	stream, ok := viewer.streamFrom("alice", timeout)
	if ok {
		deadline := time.Now().Add(timeout)
		for !viewer.rtpFlowing(stream, 200*time.Millisecond) && time.Now().Before(deadline) {
			time.Sleep(20 * time.Millisecond)
		}
		ok = viewer.rtpFlowing(stream, 200*time.Millisecond)
	}
	check("commenter's audio reaches other clients", ok, fmt.Sprintf("%d ms from grant to media", time.Since(t0).Milliseconds()))

	// 4. Operator ends the comment: unpresent cuts the live stream.
	t0 = time.Now()
	op.send(msg{"type": "useraction", "kind": "unpresent", "source": op.id, "dest": alice.id})
	_, gotClose := viewer.waitFor(timeout, func(m msg) bool { return m.s("type") == "close" && m.s("id") == stream })
	closeMs := time.Since(t0).Milliseconds()
	deadline := time.Now().Add(timeout)
	for viewer.rtpFlowing(stream, 100*time.Millisecond) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	check("unpresent cuts the live stream for everyone", gotClose && !viewer.rtpFlowing(stream, 100*time.Millisecond),
		fmt.Sprintf("close after %d ms, media stopped after %d ms", closeMs, time.Since(t0).Milliseconds()))
	_, ok = alice.waitFor(timeout, func(m msg) bool { return m.s("type") == "abort" })
	check("commenter is told its stream was aborted", ok, "")
	alice.drain()

	// 5. present also allows camera: the server can't restrict to audio.
	op.send(msg{"type": "useraction", "kind": "present", "source": op.id, "dest": alice.id})
	alice.waitFor(timeout, func(m msg) bool { return m.s("type") == "joined" && m.s("kind") == "change" })
	vid := alice.publish(true)
	m, refused := alice.waitFor(3*time.Second, func(m msg) bool { return m.s("type") == "abort" && m.s("id") == vid })
	check("present can be limited to audio (server-side)", refused, "present also accepts video: limit in our client")
	op.send(msg{"type": "useraction", "kind": "unpresent", "source": op.id, "dest": alice.id})
	alice.drain()

	// 6. No recording.
	op.send(msg{"type": "groupaction", "kind": "record", "source": op.id})
	m, ok = op.waitFor(timeout, func(m msg) bool { return m.s("type") == "usermessage" && m.s("kind") == "error" })
	check("recording refused (allow-recording false)", ok, fmt.Sprint(m["value"]))

	// 7. Lock: nobody new gets in, the operator still can.
	op.send(msg{"type": "groupaction", "kind": "lock", "source": op.id, "value": "meeting in progress"})
	time.Sleep(300 * time.Millisecond)
	late := dial("late")
	r = late.join(*grp, "late", "x", "")
	check("locked group refuses new attendees", r.s("kind") == "fail", fmt.Sprintf("%s: %v", r.s("kind"), r["value"]))
	op2 := dial("op2")
	r = op2.join(*grp, "operator", "op", "")
	check("locked group still admits an operator (e.g. remote attendant)", r.s("kind") == "join", r.s("kind"))
	op.send(msg{"type": "groupaction", "kind": "unlock", "source": op.id})
	time.Sleep(300 * time.Millisecond)
	late2 := dial("late2")
	r = late2.join(*grp, "late2", "x", "")
	check("unlocked group admits attendees again", r.s("kind") == "join", r.s("kind"))

	// 8. Attendee list is visible to every attendee.
	_, ok = late2.waitFor(timeout, func(m msg) bool { return m.s("type") == "user" && m.s("kind") == "add" && m.s("username") == "alice" })
	check("attendees can be hidden from each other", !ok, "every client receives every user (names + data)")

	// 9. Kick.
	t0 = time.Now()
	op.send(msg{"type": "useraction", "kind": "kick", "source": op.id, "dest": late2.id, "value": "test"})
	_, ok = late2.waitFor(timeout, func(m msg) bool {
		return (m.s("type") == "usermessage" && m.s("kind") == "kicked") || (m.s("type") == "joined" && m.s("kind") == "leave") || m.s("type") == "_closed"
	})
	check("operator kicks an attendee", ok, fmt.Sprintf("%d ms", time.Since(t0).Milliseconds()))

	// 10. Renaming another user.
	op.send(msg{"type": "useraction", "kind": "rename", "source": op.id, "dest": alice.id, "value": "Alice B."})
	m, _ = op.waitFor(timeout, func(m msg) bool { return m.s("type") == "usermessage" && m.s("kind") == "error" })
	check("operator renames an attendee", m == nil, fmt.Sprintf("%v", m["value"]))

	// 11. Lobby via signed tokens (hub mints a token only on admit).
	if *keyB64 != "" {
		aud := strings.TrimRight(*server, "/") + "/group/" + *tgrp + "/"
		nt := dial("notoken")
		r = nt.join(*tgrp, "intruder", "guess", "")
		check("token-only group refuses password joins", r.s("kind") == "fail", fmt.Sprint(r["value"]))
		tk := dial("tok")
		r = tk.join(*tgrp, "", "", mint("Bob Hall", []string{}, aud, 3*time.Hour))
		check("admitted attendee joins with a hub-minted token", r.s("kind") == "join" && r.s("username") == "Bob Hall", fmt.Sprintf("username %q perms %v", r.s("username"), r.perms()))
		ex := dial("expired")
		r = ex.join(*tgrp, "", "", mint("Eve", []string{}, aud, -time.Minute))
		check("expired token refused", r.s("kind") == "fail", fmt.Sprint(r["value"]))
		wg := dial("wronggroup")
		r = wg.join(*tgrp, "", "", mint("Eve", []string{}, strings.TrimRight(*server, "/")+"/group/other/", time.Hour))
		check("token for another group refused", r.s("kind") == "fail", fmt.Sprint(r["value"]))
		pt := dial("parttoken")
		r = pt.join(*tgrp, "", "", mint("Part Holder", []string{"present", "message"}, aud, time.Hour))
		check("part-holder token can carry present", r.s("kind") == "join" && contains(r.perms(), "present"), fmt.Sprint(r.perms()))
	}

	if failures > 0 {
		fmt.Printf("\n%d check(s) failed (some failures are expected Galene limitations)\n", failures)
		os.Exit(1)
	}
}

func contains(a []string, s string) bool {
	for _, x := range a {
		if x == s {
			return true
		}
	}
	return false
}
