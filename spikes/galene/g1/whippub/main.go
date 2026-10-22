// whippub: publishes H.264 + Opus to a WHIP endpoint.
//
// Default mode (G1 option 1): FFmpeg encodes and sends RTP to 127.0.0.1:5004
// (video) and :5006 (audio); both are forwarded as-is.
//
// -stdin mode (G1 path D): reads encoded frames from stdin, each framed as
// 1 byte kind (0 audio, 1 video), 8 bytes pts in ns, 4 bytes length (big
// endian), then the frame (Annex-B H.264 access unit or one Opus packet).
// RTP timestamps follow the pts, and sender reports map them to the capture
// clock (not the send time), so the browser can lip-sync audio and video.
package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/interceptor"
	"github.com/pion/rtcp"
	"github.com/pion/rtp"
	"github.com/pion/rtp/codecs"
	"github.com/pion/webrtc/v4"
)

// ptsTrack packetizes frames with RTP timestamps derived from their pts.
type ptsTrack struct {
	track      *webrtc.TrackLocalStaticRTP
	packetizer rtp.Packetizer
	clock      uint64
	base       uint32
	ssrc       uint32

	mu      sync.Mutex
	lastRTP uint32
	lastPTS time.Duration
	packets uint32
	octets  uint32
	started bool
}

func newPTSTrack(c webrtc.RTPCodecCapability, id string, payloader rtp.Payloader) *ptsTrack {
	track, err := webrtc.NewTrackLocalStaticRTP(c, id, "solin")
	if err != nil {
		log.Fatal(err)
	}
	// Payload type and SSRC are rewritten by the track's binding.
	return &ptsTrack{
		track:      track,
		packetizer: rtp.NewPacketizer(1200, 0, 0, payloader, rtp.NewRandomSequencer(), uint32(c.ClockRate)),
		clock:      uint64(c.ClockRate),
		base:       uint32(time.Now().UnixNano()),
	}
}

func (t *ptsTrack) write(data []byte, pts time.Duration) {
	ts := t.base + uint32(int64(pts)*int64(t.clock)/int64(time.Second)) // pts may start negative (Opus pre-skip)
	packets := t.packetizer.Packetize(data, 0)
	for _, p := range packets {
		p.Timestamp = ts
		if err := t.track.WriteRTP(p); err != nil {
			log.Printf("write: %v", err)
		}
	}
	t.mu.Lock()
	t.lastRTP, t.lastPTS, t.started = ts, pts, true
	t.packets += uint32(len(packets))
	t.octets += uint32(len(data))
	t.mu.Unlock()
}

// senderReport maps the last packet's RTP timestamp to wall0 + its pts.
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

func ntpTime(t time.Time) uint64 {
	secs := uint64(t.Unix()) + 2208988800
	frac := uint64(t.Nanosecond()) << 32 / uint64(time.Second)
	return secs<<32 | frac
}

// readFrames feeds framed samples from stdin to the pts tracks and sends
// sender reports every second.
func readFrames(pc *webrtc.PeerConnection, video, audio *ptsTrack, ptsSR bool, vbytes, abytes *atomic.Int64) {
	r := bufio.NewReaderSize(os.Stdin, 1<<20)
	var hdr [13]byte
	var wall0 time.Time
	lastSR := time.Now()
	for {
		if _, err := io.ReadFull(r, hdr[:]); err != nil {
			log.Fatalf("stdin: %v", err)
		}
		pts := time.Duration(binary.BigEndian.Uint64(hdr[1:9]))
		data := make([]byte, binary.BigEndian.Uint32(hdr[9:13]))
		if _, err := io.ReadFull(r, data); err != nil {
			log.Fatalf("stdin: %v", err)
		}
		if wall0.IsZero() {
			// Audio and video pts share libobs' clock; anchor it once.
			wall0 = time.Now().Add(-pts)
		}
		t, counter := audio, abytes
		if hdr[0] == 1 {
			t, counter = video, vbytes
		}
		t.write(data, pts)
		counter.Add(int64(len(data)))
		if ptsSR && time.Since(lastSR) >= time.Second {
			lastSR = time.Now()
			var reports []rtcp.Packet
			for _, tr := range []*ptsTrack{video, audio} {
				if sr := tr.senderReport(wall0); sr != nil {
					reports = append(reports, sr)
				}
			}
			if err := pc.WriteRTCP(reports); err != nil {
				log.Printf("rtcp: %v", err)
			}
		}
	}
}

func forward(port int, track *webrtc.TrackLocalStaticRTP, bytesSent *atomic.Int64) {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		log.Fatal(err)
	}
	buf := make([]byte, 1600)
	for {
		n, _, err := conn.ReadFrom(buf)
		if err != nil {
			log.Fatal(err)
		}
		if _, err := track.Write(buf[:n]); err != nil {
			log.Printf("write: %v", err)
		}
		bytesSent.Add(int64(n))
	}
}

func main() {
	endpoint := flag.String("url", "http://127.0.0.1:8443/group/g1/.whip", "WHIP endpoint")
	token := flag.String("token", "", "bearer token")
	stdin := flag.Bool("stdin", false, "read framed encoded samples from stdin instead of RTP")
	srMode := flag.String("sr", "pts", "-stdin sender reports: pts (capture clock) or send (Pion, send time)")
	flag.Parse()

	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		log.Fatal(err)
	}
	ir := &interceptor.Registry{}
	if *stdin {
		// NACK responder and TWCC; sender reports come from readFrames.
		if err := webrtc.ConfigureNack(m, ir); err != nil {
			log.Fatal(err)
		}
		if err := webrtc.ConfigureTWCCHeaderExtensionSender(m, ir); err != nil {
			log.Fatal(err)
		}
		if *srMode == "send" {
			if err := webrtc.ConfigureRTCPReports(ir); err != nil {
				log.Fatal(err)
			}
		}
	} else if err := webrtc.RegisterDefaultInterceptors(m, ir); err != nil {
		// NACK responder, sender reports (lip sync) and TWCC, as a browser would send.
		log.Fatal(err)
	}
	api := webrtc.NewAPI(webrtc.WithMediaEngine(m), webrtc.WithInterceptorRegistry(ir))
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		log.Fatal(err)
	}

	videoCap := webrtc.RTPCodecCapability{
		MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
		SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
	}
	audioCap := webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2}
	var videoRTP, audioRTP *webrtc.TrackLocalStaticRTP
	var videoPTS, audioPTS *ptsTrack
	if *stdin {
		videoPTS = newPTSTrack(videoCap, "video", &codecs.H264Payloader{})
		audioPTS = newPTSTrack(audioCap, "audio", &codecs.OpusPayloader{})
		videoRTP, audioRTP = videoPTS.track, audioPTS.track
	} else {
		videoRTP, _ = webrtc.NewTrackLocalStaticRTP(videoCap, "video", "solin")
		audioRTP, _ = webrtc.NewTrackLocalStaticRTP(audioCap, "audio", "solin")
	}
	for _, t := range []*webrtc.TrackLocalStaticRTP{videoRTP, audioRTP} {
		tr, err := pc.AddTransceiverFromTrack(t, webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionSendonly})
		if err != nil {
			log.Fatal(err)
		}
		// Incoming RTCP (NACK, PLI, receiver reports) only reaches the
		// interceptors if something reads it.
		go func(sender *webrtc.RTPSender) {
			buf := make([]byte, 1500)
			for {
				if _, _, err := sender.Read(buf); err != nil {
					return
				}
			}
		}(tr.Sender())
		if *stdin {
			ssrc := uint32(tr.Sender().GetParameters().Encodings[0].SSRC)
			if t == videoRTP {
				videoPTS.ssrc = ssrc
			} else {
				audioPTS.ssrc = ssrc
			}
		}
	}
	pc.OnConnectionStateChange(func(s webrtc.PeerConnectionState) { log.Printf("connection: %s", s) })

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		log.Fatal(err)
	}
	gathered := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		log.Fatal(err)
	}
	<-gathered

	req, _ := http.NewRequest("POST", *endpoint, bytes.NewBufferString(pc.LocalDescription().SDP))
	req.Header.Set("Content-Type", "application/sdp")
	if *token != "" {
		req.Header.Set("Authorization", "Bearer "+*token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		log.Fatalf("WHIP POST: %s: %s", resp.Status, body)
	}
	resource, _ := url.Parse(*endpoint)
	resource, _ = resource.Parse(resp.Header.Get("Location"))
	log.Printf("WHIP resource: %s", resource)
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: string(body)}); err != nil {
		log.Fatal(err)
	}

	var vbytes, abytes atomic.Int64
	if *stdin {
		go readFrames(pc, videoPTS, audioPTS, *srMode == "pts", &vbytes, &abytes)
	} else {
		go forward(5004, videoRTP, &vbytes)
		go forward(5006, audioRTP, &abytes)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt)
	tick := time.NewTicker(5 * time.Second)
	var lastV, lastA int64
	for {
		select {
		case <-tick.C:
			v, a := vbytes.Load(), abytes.Load()
			fmt.Printf("video %.0f kbps, audio %.0f kbps\n", float64(v-lastV)*8/5000, float64(a-lastA)*8/5000)
			lastV, lastA = v, a
		case <-sig:
			del, _ := http.NewRequest("DELETE", resource.String(), nil)
			if *token != "" {
				del.Header.Set("Authorization", "Bearer "+*token)
			}
			http.DefaultClient.Do(del)
			pc.Close()
			return
		}
	}
}
