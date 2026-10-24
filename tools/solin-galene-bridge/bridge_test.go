package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"testing"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

func TestFrameReaderRoundTrip(t *testing.T) {
	var stream []byte
	stream = AppendFrame(stream, kindH264, 1500*time.Millisecond, []byte{0, 0, 0, 1, 0x65, 1, 2})
	stream = AppendFrame(stream, kindPCM, -20*time.Millisecond, make([]byte, 16))
	stream = AppendFrame(stream, kindOpus, 0, nil)
	fr := NewFrameReader(bytes.NewReader(stream))

	want := []struct {
		kind byte
		pts  time.Duration
		size int
	}{{kindH264, 1500 * time.Millisecond, 7}, {kindPCM, -20 * time.Millisecond, 16}, {kindOpus, 0, 0}}
	for i, w := range want {
		f, err := fr.Next()
		if err != nil {
			t.Fatalf("frame %d: %v", i, err)
		}
		if f.Kind != w.kind || f.PTS != w.pts || len(f.Data) != w.size {
			t.Fatalf("frame %d = kind %d pts %v len %d, want %+v", i, f.Kind, f.PTS, len(f.Data), w)
		}
	}
	if _, err := fr.Next(); err != io.EOF {
		t.Fatalf("after last frame: %v, want io.EOF", err)
	}
}

func TestFrameReaderHeaderBytes(t *testing.T) {
	raw := []byte{1, 0, 0, 0, 0, 0, 0, 0x03, 0xe8, 0, 0, 0, 2, 0xaa, 0xbb}
	f, err := NewFrameReader(bytes.NewReader(raw)).Next()
	if err != nil {
		t.Fatal(err)
	}
	if f.Kind != 1 || f.PTS != 1000 || !bytes.Equal(f.Data, []byte{0xaa, 0xbb}) {
		t.Fatalf("got %+v", f)
	}
}

func TestFrameReaderTruncated(t *testing.T) {
	stream := AppendFrame(nil, kindOpus, 0, []byte{1, 2, 3, 4})
	for _, cut := range []int{5, len(stream) - 1} {
		_, err := NewFrameReader(bytes.NewReader(stream[:cut])).Next()
		if !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("cut at %d: %v, want ErrUnexpectedEOF", cut, err)
		}
	}
}

func TestFrameReaderRejectsHugeLength(t *testing.T) {
	raw := []byte{1, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 0xff, 0xff}
	if _, err := NewFrameReader(bytes.NewReader(raw)).Next(); !errors.Is(err, errFrameTooLarge) {
		t.Fatalf("got %v, want errFrameTooLarge", err)
	}
}

func TestDecodeF32LE(t *testing.T) {
	b := []byte{0, 0, 0x80, 0x3f, 0, 0, 0x80, 0xbf, 0xff} // 1.0, -1.0, partial
	got := decodeF32LE(b)
	if len(got) != 2 || got[0] != 1 || got[1] != -1 {
		t.Fatalf("got %v", got)
	}
}

type emitted struct {
	first float32
	n     int
	pts   time.Duration
}

func collect(f *PCMFramer, pcm []float32, pts time.Duration, out *[]emitted) {
	f.Push(pcm, pts, func(frame []float32, pts time.Duration) {
		*out = append(*out, emitted{frame[0], len(frame), pts})
	})
}

// ramp returns interleaved stereo samples whose left channel counts from start.
func ramp(start, frames int) []float32 {
	s := make([]float32, 2*frames)
	for i := 0; i < frames; i++ {
		s[2*i] = float32(start + i)
		s[2*i+1] = -float32(start + i)
	}
	return s
}

func TestPCMFramerJoinsContiguousChunks(t *testing.T) {
	f := NewPCMFramer(48000, 2, 960)
	var out []emitted
	// 1024-frame chunks (libobs' audio buffer size) are not 20 ms aligned.
	for i := 0; i < 15; i++ {
		pts := time.Duration(i*1024) * time.Second / 48000
		collect(f, ramp(i*1024, 1024), pts, &out)
	}
	if len(out) != 15*1024/960 {
		t.Fatalf("%d frames, want %d", len(out), 15*1024/960)
	}
	for i, e := range out {
		if e.n != 1920 || e.first != float32(i*960) || e.pts != time.Duration(i)*20*time.Millisecond {
			t.Fatalf("frame %d = %+v", i, e)
		}
	}
	if f.Resets() != 0 {
		t.Fatalf("%d resets on contiguous input", f.Resets())
	}
}

func TestPCMFramerToleratesJitterAndResetsOnGap(t *testing.T) {
	f := NewPCMFramer(48000, 2, 960)
	var out []emitted
	collect(f, ramp(0, 500), 0, &out)
	// 3 ms of pts jitter: still contiguous.
	collect(f, ramp(500, 500), time.Duration(500)*time.Second/48000+3*time.Millisecond, &out)
	if len(out) != 1 || f.Resets() != 0 {
		t.Fatalf("jitter: %d frames, %d resets", len(out), f.Resets())
	}
	// 40 frames pending; a 100 ms jump drops them and restarts at the new pts.
	gap := time.Second
	collect(f, ramp(10000, 960), gap, &out)
	if f.Resets() != 1 {
		t.Fatalf("resets = %d, want 1", f.Resets())
	}
	last := out[len(out)-1]
	if last.pts != gap || last.first != 10000 {
		t.Fatalf("after gap: %+v", last)
	}
	// Overlapping (earlier) pts also restarts.
	collect(f, ramp(0, 10), gap, &out)
	if f.Resets() != 1 { // buffer was empty: lossless restart, not counted
		t.Fatalf("resets = %d", f.Resets())
	}
	collect(f, ramp(0, 10), gap-10*time.Millisecond, &out)
	if f.Resets() != 2 {
		t.Fatalf("overlap resets = %d, want 2", f.Resets())
	}
}

func TestPCMFramerIgnoresPartialSample(t *testing.T) {
	f := NewPCMFramer(48000, 2, 960)
	var out []emitted
	collect(f, make([]float32, 1921), 0, &out)
	if len(out) != 1 || out[0].n != 1920 {
		t.Fatalf("got %+v", out)
	}
}

func TestRTPTimestamp(t *testing.T) {
	cases := []struct {
		base  uint32
		pts   time.Duration
		clock uint32
		want  uint32
	}{
		{0, time.Second, 90000, 90000},
		{100, 20 * time.Millisecond, 48000, 100 + 960},
		{1000, -6500 * time.Microsecond, 48000, 1000 - 312}, // Opus pre-skip
		{0xffffff00, time.Second, 48000, 48000 - 0x100},     // wraps
		{0, 11111111 * time.Nanosecond, 90000, 999},
	}
	for _, c := range cases {
		if got := rtpTimestamp(c.base, c.pts, c.clock); got != c.want {
			t.Errorf("rtpTimestamp(%d, %v, %d) = %d, want %d", c.base, c.pts, c.clock, got, c.want)
		}
	}
}

func TestRTPTimestampLargePTSDoesNotOverflow(t *testing.T) {
	// 40 days of uptime: pts*90000 overflows int64, the split math must not.
	pts := 40 * 24 * time.Hour
	a := rtpTimestamp(0, pts, 90000)
	b := rtpTimestamp(0, pts+time.Second, 90000)
	if b-a != 90000 {
		t.Fatalf("1 s at 40 days = %d ticks, want 90000", b-a)
	}
	want := uint32(uint64(pts/time.Second) * 90000)
	if a != want {
		t.Fatalf("got %d, want %d", a, want)
	}
}

func TestNTPTime(t *testing.T) {
	tm := time.Date(2026, 1, 1, 0, 0, 0, 500_000_000, time.UTC)
	n := ntpTime(tm)
	if secs := n >> 32; secs != uint64(tm.Unix())+2208988800 {
		t.Fatalf("seconds = %d", secs)
	}
	if frac := uint32(n); frac != 1<<31 {
		t.Fatalf("fraction = %#x, want 0x80000000", frac)
	}
}

func TestSeqGap(t *testing.T) {
	cases := []struct {
		prev, cur uint16
		lost      int
		late      bool
	}{
		{10, 11, 0, false},
		{10, 13, 2, false},
		{65535, 0, 0, false},
		{65534, 1, 2, false},
		{10, 10, 0, true},
		{10, 9, 0, true},
		{1, 65535, 0, true},
	}
	for _, c := range cases {
		lost, late := seqGap(c.prev, c.cur)
		if lost != c.lost || late != c.late {
			t.Errorf("seqGap(%d, %d) = %d, %v; want %d, %v", c.prev, c.cur, lost, late, c.lost, c.late)
		}
	}
}

func TestMixerSumsAndClips(t *testing.T) {
	m := NewMixer(2, 2, 8)
	a, b := m.NewInput(1), m.NewInput(0.5)
	a.Write([]float32{0.5, -0.5, 0.9, -0.9})
	b.Write([]float32{0.2, -0.2, 0.4, -0.4})
	out := make([]float32, 4)
	m.Read(out)
	want := []float32{0.6, -0.6, 1, -1} // 0.9+0.2 clipped
	for i := range want {
		if math.Abs(float64(out[i]-want[i])) > 1e-6 {
			t.Fatalf("out = %v, want %v", out, want)
		}
	}
}

func TestMixerPrebufferAndUnderrun(t *testing.T) {
	m := NewMixer(1, 4, 16)
	in := m.NewInput(1)
	in.Write([]float32{1, 1, 1})
	out := make([]float32, 2)
	m.Read(out)
	if out[0] != 0 || out[1] != 0 {
		t.Fatalf("played before the prebuffer filled: %v", out)
	}
	in.Write([]float32{1})
	m.Read(out)
	if out[0] != 1 || out[1] != 1 {
		t.Fatalf("not playing once primed: %v", out)
	}
	out3 := make([]float32, 3)
	m.Read(out3) // 2 left: underrun, plays them then silence
	if out3[0] != 1 || out3[1] != 1 || out3[2] != 0 {
		t.Fatalf("underrun read = %v", out3)
	}
	if u, _ := m.Counters(); u != 1 {
		t.Fatalf("underruns = %d", u)
	}
	in.Write([]float32{1, 1})
	m.Read(out)
	if out[0] != 0 {
		t.Fatalf("should re-prebuffer after an underrun: %v", out)
	}
}

func TestMixerBoundsLatency(t *testing.T) {
	m := NewMixer(1, 4, 10)
	in := m.NewInput(1)
	for i := 0; i < 12; i++ {
		in.Write([]float32{float32(i) / 100})
	}
	if got := in.Buffered(); got > 10 {
		t.Fatalf("buffered %d > max 10", got)
	}
	if _, o := m.Counters(); o == 0 {
		t.Fatal("overflow not counted")
	}
	out := make([]float32, in.Buffered())
	m.Read(out)
	if out[len(out)-1] != 0.11 {
		t.Fatalf("newest sample lost: %v", out)
	}
	// After an overflow only about the prebuffer amount is kept.
	if len(out) > 6 {
		t.Fatalf("kept %d frames after overflow, want about the prebuffer (4)", len(out))
	}
}

func TestMixerRemove(t *testing.T) {
	m := NewMixer(1, 1, 4)
	in := m.NewInput(1)
	in.Write([]float32{1, 1})
	m.Remove(in)
	out := make([]float32, 2)
	m.Read(out)
	if out[0] != 0 || m.Inputs() != 0 {
		t.Fatalf("removed input still mixed: %v", out)
	}
}

func TestRTCConfigurationFromGalene(t *testing.T) {
	raw := json.RawMessage(`{
		"iceServers": [
			{"urls": "stun:stun.example.org:3478"},
			{"urls": ["turn:turn.example.org:3478?transport=udp", "turns:turn.example.org:5349", "http://bogus"],
			 "username": "1700000000:galene", "credential": "c2VjcmV0", "credentialType": "password"},
			{"urls": ["turn:noauth.example.org"]},
			{"urls": "turn:oauth.example.org", "username": "u", "credential": {"macKey": "x"}, "credentialType": "oauth"}
		],
		"iceTransportPolicy": "relay"
	}`)
	conf, err := rtcConfigurationFromGalene(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(conf.ICEServers) != 2 {
		t.Fatalf("ICE servers = %+v", conf.ICEServers)
	}
	if got := conf.ICEServers[0].URLs; len(got) != 1 || got[0] != "stun:stun.example.org:3478" {
		t.Fatalf("stun = %v", got)
	}
	turn := conf.ICEServers[1]
	if len(turn.URLs) != 2 || turn.Username != "1700000000:galene" || turn.Credential != "c2VjcmV0" ||
		turn.CredentialType != webrtc.ICECredentialTypePassword {
		t.Fatalf("turn = %+v", turn)
	}
	if conf.ICETransportPolicy != webrtc.ICETransportPolicyRelay {
		t.Fatalf("policy = %v", conf.ICETransportPolicy)
	}
	// Pion must accept the result.
	pc, err := webrtc.NewPeerConnection(conf)
	if err != nil {
		t.Fatalf("pion rejected the configuration: %v", err)
	}
	pc.Close()
}

func TestRTCConfigurationFromGaleneEmpty(t *testing.T) {
	for _, raw := range []string{"", "null", "{}", `{"iceServers": []}`} {
		conf, err := rtcConfigurationFromGalene(json.RawMessage(raw))
		if err != nil || len(conf.ICEServers) != 0 {
			t.Fatalf("%q: %+v, %v", raw, conf, err)
		}
	}
	if _, err := rtcConfigurationFromGalene(json.RawMessage(`[1,2]`)); err == nil {
		t.Fatal("malformed configuration accepted")
	}
}

func TestICEServersFromLinks(t *testing.T) {
	headers := []string{
		`<stun:stun.example.org>; rel="ice-server"`,
		`<turn:turn.example.org:3478>; rel="ice-server"; username="a,b"; credential="p\"w;x"; credential-type="password", <https://x/y>; rel="next"`,
		`<turn:no-cred.example.org>; rel="ice-server"`,
	}
	got := iceServersFromLinks(headers)
	if len(got) != 2 {
		t.Fatalf("got %+v", got)
	}
	if got[0].URLs[0] != "stun:stun.example.org" || got[0].Username != "" {
		t.Fatalf("stun = %+v", got[0])
	}
	if got[1].URLs[0] != "turn:turn.example.org:3478" || got[1].Username != "a,b" || got[1].Credential != `p"w;x` {
		t.Fatalf("turn = %+v", got[1])
	}
}

func TestSplitNALsAndKeyframes(t *testing.T) {
	sps := []byte{0x67, 0x42, 0xe0, 0x1f}
	pps := []byte{0x68, 0xce}
	idr := []byte{0x65, 0x88, 0x00}
	au := bytes.Join([][]byte{nil, sps, pps, idr}, []byte{0, 0, 0, 1})
	nals := splitNALs(au)
	if len(nals) != 3 || !bytes.Equal(nals[0], sps) || !bytes.Equal(nals[1], pps) || !bytes.Equal(nals[2], idr) {
		t.Fatalf("nals = %x", nals)
	}
	var p h264Params
	if isIDR, has := p.inspect(au); !isIDR || !has {
		t.Fatalf("inspect = %v, %v", isIDR, has)
	}
	bare := append([]byte{0, 0, 1}, idr...)
	isIDR, has := p.inspect(bare)
	if !isIDR || has {
		t.Fatalf("bare IDR inspect = %v, %v", isIDR, has)
	}
	fixed := p.withParams(bare)
	if !bytes.Equal(fixed, au) {
		t.Fatalf("withParams = %x, want %x", fixed, au)
	}
	if isIDR, _ := p.inspect([]byte{0, 0, 0, 1, 0x41, 0x9a}); isIDR {
		t.Fatal("P slice reported as IDR")
	}
}

func TestJoinFailure(t *testing.T) {
	s := func(v string) json.RawMessage { b, _ := json.Marshal(v); return b }
	if err := joinFailure(&galeneMessage{Value: s("not authorised")}); !errors.Is(err, errAuth) {
		t.Fatalf("not authorised: %v", err)
	}
	if err := joinFailure(&galeneMessage{Error: "duplicate-username", Value: s("duplicate username")}); errors.Is(err, errAuth) {
		t.Fatalf("duplicate username treated as fatal: %v", err)
	}
}

func TestBackoff(t *testing.T) {
	b := newBackoff(time.Second, 10*time.Second)
	var got []time.Duration
	for i := 0; i < 6; i++ {
		got = append(got, b.Next())
	}
	want := []time.Duration{1, 2, 4, 8, 10, 10}
	for i := range want {
		if got[i] != want[i]*time.Second {
			t.Fatalf("delays = %v", got)
		}
	}
	b.Reset()
	if b.Next() != time.Second {
		t.Fatal("reset did not restart at min")
	}
}

func TestParseVideoPorts(t *testing.T) {
	ports, err := parseVideoPorts(" 47110, 47112 ")
	if err != nil || len(ports) != 2 || ports[0] != 47110 || ports[1] != 47112 {
		t.Fatalf("got %v, %v", ports, err)
	}
	if ports, err := parseVideoPorts(""); err != nil || ports != nil {
		t.Fatalf("empty: %v, %v", ports, err)
	}
	for _, bad := range []string{"x", "0", "70000", "47110,"} {
		if _, err := parseVideoPorts(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestSlotRewriterContinuesAcrossSenders(t *testing.T) {
	w := slotRewriter{ssrc: 0xabc}
	pkt := func(seq uint16, ts uint32) *rtp.Packet {
		return &rtp.Packet{Header: rtp.Header{SequenceNumber: seq, Timestamp: ts, SSRC: 1, PayloadType: 102}}
	}
	// The first sender passes through, with the slot's SSRC and payload type.
	a := pkt(65534, 1000)
	w.rewrite("a", a)
	if a.SequenceNumber != 65534 || a.Timestamp != 1000 || a.SSRC != 0xabc || a.PayloadType != slotPayloadType {
		t.Fatalf("first: %+v", a.Header)
	}
	w.rewrite("a", pkt(65535, 4000))
	w.rewrite("a", pkt(0, 7000)) // sequence wraps
	late := pkt(65535, 4000)     // a late duplicate does not move the stream's end
	w.rewrite("a", late)
	// The next sender continues right after the last packet.
	b := pkt(100, 900000)
	w.rewrite("b", b)
	if b.SequenceNumber != 1 || b.Timestamp != 7000+slotTSStep {
		t.Fatalf("hand-over: seq %d ts %d", b.SequenceNumber, b.Timestamp)
	}
	b2 := pkt(101, 903000)
	w.rewrite("b", b2)
	if b2.SequenceNumber != 2 || b2.Timestamp != 7000+slotTSStep+3000 {
		t.Fatalf("second packet: seq %d ts %d", b2.SequenceNumber, b2.Timestamp)
	}
}

func TestVideoSlotsAssignment(t *testing.T) {
	var out bytes.Buffer
	ev := &emitter{enc: json.NewEncoder(&out)}
	v, err := newVideoSlots([]int{47990, 47992}, ev)
	if err != nil {
		t.Fatal(err)
	}
	defer v.close()
	s1 := v.acquire("s1", "Ana")
	s2 := v.acquire("s2", "Bruno")
	if s1 == nil || s2 == nil || s1.index != 1 || s2.index != 2 {
		t.Fatalf("slots %v %v", s1, s2)
	}
	if v.acquire("s3", "Carla") != nil {
		t.Fatal("a third track got a slot")
	}
	// Ana leaves and Bruno reconnects: Bruno keeps slot 2, slot 1 stays free for Ana.
	v.release(s1, "s1")
	v.release(s2, "s2")
	v.release(s2, "s2") // twice is harmless
	if got := v.acquire("s4", "Bruno"); got == nil || got.index != 2 {
		t.Fatalf("Bruno got %v", got)
	}
	if got := v.acquire("s5", "Ana"); got == nil || got.index != 1 {
		t.Fatalf("Ana got %v", got)
	}
	var events []map[string]any
	dec := json.NewDecoder(&out)
	for dec.More() {
		var e map[string]any
		if err := dec.Decode(&e); err != nil {
			t.Fatal(err)
		}
		events = append(events, e)
	}
	if len(events) != 6 || events[0]["slot"] != float64(1) || events[0]["user"] != "Ana" || events[2]["state"] != "stopped" {
		t.Fatalf("events %v", events)
	}
}

func TestVideoSlotForwardsOnlyTheHolder(t *testing.T) {
	ln, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	v, err := newVideoSlots([]int{ln.LocalAddr().(*net.UDPAddr).Port}, &emitter{enc: json.NewEncoder(io.Discard)})
	if err != nil {
		t.Fatal(err)
	}
	defer v.close()
	s := v.acquire("s1", "Ana")
	p := &rtp.Packet{Header: rtp.Header{Version: 2, SequenceNumber: 7, Timestamp: 9, SSRC: 1, PayloadType: 102}, Payload: []byte{0x65, 1, 2}}
	if err := s.write("other", p); err != nil {
		t.Fatal(err)
	}
	if err := s.write("s1", p); err != nil {
		t.Fatal(err)
	}
	_ = ln.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 1500)
	n, err := ln.Read(buf)
	if err != nil {
		t.Fatal(err)
	}
	var got rtp.Packet
	if err := got.Unmarshal(buf[:n]); err != nil {
		t.Fatal(err)
	}
	if got.PayloadType != slotPayloadType || got.SSRC != s.rw.ssrc || !bytes.Equal(got.Payload, []byte{0x65, 1, 2}) {
		t.Fatalf("got %+v", got.Header)
	}
	_ = ln.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
	if _, err := ln.Read(buf); err == nil {
		t.Fatal("a packet from a non-holder was forwarded")
	}
}
