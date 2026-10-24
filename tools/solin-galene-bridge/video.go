package main

import (
	"fmt"
	"log"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/pion/rtcp"
	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

// Remote part-holders' video is forwarded, without decoding, as plain RTP to
// local UDP "slots" that Solin's libobs sidecar reads through an SDP file
// (solin.core.hosted_meeting.participant_video). Each slot is one fixed RTP
// stream: whoever holds it, the receiver sees one SSRC with continuous
// sequence numbers and timestamps, so a new part-holder taking a slot is just
// a new keyframe to the decoder.

const (
	slotPayloadType = 96
	slotGap         = 2 * time.Second  // a pause this long restarts the keyframe burst
	slotRefresh     = 10 * time.Second // keyframe interval once a slot is running
	slotTSStep      = 3000             // 90 kHz timestamp step across a hand-over (~33 ms)
)

// slotKeyframeBurst is when keyframes are requested after a slot starts or
// resumes. libobs' ffmpeg_source drops the first keyframe after it opens, so
// one request is not enough.
var slotKeyframeBurst = []time.Duration{0, 500 * time.Millisecond, 1500 * time.Millisecond, 4 * time.Second}

func parseVideoPorts(s string) ([]int, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil, nil
	}
	var ports []int
	for _, part := range strings.Split(s, ",") {
		port, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || port <= 0 || port > 65535 {
			return nil, fmt.Errorf("bad video port %q", part)
		}
		ports = append(ports, port)
	}
	return ports, nil
}

// slotRewriter maps the RTP packets of successive senders onto one continuous
// stream.
type slotRewriter struct {
	ssrc     uint32
	owner    string
	seqOff   uint16
	tsOff    uint32
	lastSeq  uint16
	lastTS   uint32
	anything bool
}

// rewrite rewrites p in place for the slot's stream; owner identifies the
// sender, and a new owner continues the stream where the last one stopped.
func (w *slotRewriter) rewrite(owner string, p *rtp.Packet) {
	if owner != w.owner {
		w.owner = owner
		if w.anything {
			w.seqOff = w.lastSeq + 1 - p.SequenceNumber
			w.tsOff = w.lastTS + slotTSStep - p.Timestamp
		} else {
			w.seqOff, w.tsOff = 0, 0
		}
	}
	p.SequenceNumber += w.seqOff
	p.Timestamp += w.tsOff
	p.SSRC = w.ssrc
	p.PayloadType = slotPayloadType
	if !w.anything || int16(p.SequenceNumber-w.lastSeq) > 0 {
		w.lastSeq, w.lastTS = p.SequenceNumber, p.Timestamp
	}
	w.anything = true
}

type videoSlot struct {
	index int // 1-based, as Solin names them
	port  int
	conn  *net.UDPConn

	mu     sync.Mutex
	holder string // stream id holding the slot, "" when free
	user   string
	rw     slotRewriter
}

// write forwards one packet if holder still holds the slot.
func (s *videoSlot) write(holder string, p *rtp.Packet) error {
	s.mu.Lock()
	if s.holder != holder {
		s.mu.Unlock()
		return nil
	}
	s.rw.rewrite(holder, p)
	buf, err := p.Marshal()
	s.mu.Unlock()
	if err != nil {
		return err
	}
	_, err = s.conn.Write(buf)
	return err
}

// videoSlots hands slots to part-holders' video tracks. A user who comes back
// (reconnects, or turns the camera off and on) gets their previous slot if it
// is free, so the hall's layout does not swap people around.
type videoSlots struct {
	events *emitter

	mu    sync.Mutex
	slots []*videoSlot
	last  map[string]int // user → slot index they last held
}

func newVideoSlots(ports []int, events *emitter) (*videoSlots, error) {
	v := &videoSlots{events: events, last: map[string]int{}}
	for i, port := range ports {
		conn, err := net.DialUDP("udp", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
		if err != nil {
			v.close()
			return nil, fmt.Errorf("video slot %d: %w", i+1, err)
		}
		v.slots = append(v.slots, &videoSlot{
			index: i + 1, port: port, conn: conn,
			rw: slotRewriter{ssrc: 0x5011a000 + uint32(i+1)},
		})
	}
	return v, nil
}

func (v *videoSlots) close() {
	for _, s := range v.slots {
		s.conn.Close()
	}
}

// acquire gives holder (a stream id) a free slot, or nil when all are taken.
func (v *videoSlots) acquire(holder, user string) *videoSlot {
	v.mu.Lock()
	defer v.mu.Unlock()
	var chosen *videoSlot
	if i, ok := v.last[user]; ok && v.slots[i-1].free() {
		chosen = v.slots[i-1]
	}
	for _, s := range v.slots {
		if chosen != nil {
			break
		}
		if s.free() {
			chosen = s
		}
	}
	if chosen == nil {
		return nil
	}
	chosen.mu.Lock()
	chosen.holder, chosen.user = holder, user
	chosen.mu.Unlock()
	v.last[user] = chosen.index
	v.events.emit(map[string]any{"event": "video", "slot": chosen.index, "id": holder, "user": user, "state": "started"})
	return chosen
}

func (v *videoSlots) release(s *videoSlot, holder string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	s.mu.Lock()
	if s.holder != holder {
		s.mu.Unlock()
		return
	}
	user := s.user
	s.holder, s.user = "", ""
	s.mu.Unlock()
	v.events.emit(map[string]any{"event": "video", "slot": s.index, "id": holder, "user": user, "state": "stopped"})
}

func (s *videoSlot) free() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.holder == ""
}

// forwardVideo copies one part-holder's video track into a slot until the
// track ends, asking the sender for keyframes when the slot starts, after a
// pause, and every slotRefresh (a libobs source that reopens needs one).
func (s *downStream) forwardVideo(t *webrtc.TrackRemote) {
	slots := s.r.video
	if slots == nil {
		return
	}
	mime := t.Codec().MimeType
	if !strings.EqualFold(mime, webrtc.MimeTypeH264) {
		log.Printf("stream %s from %q: video %s not forwarded (H.264 only)", s.id, s.user, mime)
		return
	}
	slot := slots.acquire(s.id, s.user)
	if slot == nil {
		log.Printf("stream %s from %q: no free video slot", s.id, s.user)
		return
	}
	log.Printf("stream %s from %q: video to slot %d (udp %d)", s.id, s.user, slot.index, slot.port)
	defer slots.release(slot, s.id)

	media := uint32(t.SSRC())
	keyframe := func() {
		_ = s.pc.WriteRTCP([]rtcp.Packet{&rtcp.PictureLossIndication{MediaSSRC: media}})
	}
	done := make(chan struct{})
	defer close(done)
	burst := make(chan struct{}, 1)
	go func() {
		var timers []*time.Timer
		stopTimers := func() {
			for _, tm := range timers {
				tm.Stop()
			}
			timers = timers[:0]
		}
		defer stopTimers()
		refresh := time.NewTicker(slotRefresh)
		defer refresh.Stop()
		for {
			select {
			case <-done:
				return
			case <-burst:
				stopTimers()
				for _, d := range slotKeyframeBurst {
					timers = append(timers, time.AfterFunc(d, keyframe))
				}
			case <-refresh.C:
				keyframe()
			}
		}
	}()
	startBurst := func() {
		select {
		case burst <- struct{}{}:
		default:
		}
	}
	startBurst()

	var last time.Time
	var writeErrors int
	for {
		p, _, err := t.ReadRTP()
		if err != nil {
			return
		}
		now := time.Now()
		if !last.IsZero() && now.Sub(last) > slotGap {
			startBurst()
		}
		last = now
		if err := slot.write(s.id, p); err != nil {
			// Nothing listening yet (ICMP port unreachable on loopback) is normal.
			writeErrors++
			if writeErrors == 1 {
				log.Printf("video slot %d: %v", slot.index, err)
			}
		}
	}
}
