// whippub: publishes H.264 + Opus to a WHIP endpoint.
//
// Default mode (G1 option 1): FFmpeg encodes and sends RTP to 127.0.0.1:5004
// (video) and :5006 (audio); both are forwarded as-is.
//
// -stdin mode (G1 path D): reads encoded frames from stdin, each framed as
// 1 byte kind (0 audio, 1 video), 8 bytes duration in ns, 4 bytes length
// (big endian), then the frame (Annex-B H.264 access unit or one Opus packet).
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
	"sync/atomic"
	"time"

	"github.com/pion/interceptor"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

// readFrames feeds framed samples from stdin to the sample tracks.
func readFrames(video, audio *webrtc.TrackLocalStaticSample, vbytes, abytes *atomic.Int64) {
	r := bufio.NewReaderSize(os.Stdin, 1<<20)
	var hdr [13]byte
	for {
		if _, err := io.ReadFull(r, hdr[:]); err != nil {
			log.Fatalf("stdin: %v", err)
		}
		dur := time.Duration(binary.BigEndian.Uint64(hdr[1:9]))
		data := make([]byte, binary.BigEndian.Uint32(hdr[9:13]))
		if _, err := io.ReadFull(r, data); err != nil {
			log.Fatalf("stdin: %v", err)
		}
		t, counter := audio, abytes
		if hdr[0] == 1 {
			t, counter = video, vbytes
		}
		if err := t.WriteSample(media.Sample{Data: data, Duration: dur}); err != nil {
			log.Printf("write: %v", err)
		}
		counter.Add(int64(len(data)))
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
	flag.Parse()

	m := &webrtc.MediaEngine{}
	if err := m.RegisterDefaultCodecs(); err != nil {
		log.Fatal(err)
	}
	// NACK responder, sender reports (lip sync) and TWCC, as a browser would send.
	ir := &interceptor.Registry{}
	if err := webrtc.RegisterDefaultInterceptors(m, ir); err != nil {
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
	var videoSample, audioSample *webrtc.TrackLocalStaticSample
	var tracks []webrtc.TrackLocal
	if *stdin {
		videoSample, _ = webrtc.NewTrackLocalStaticSample(videoCap, "video", "solin")
		audioSample, _ = webrtc.NewTrackLocalStaticSample(audioCap, "audio", "solin")
		tracks = []webrtc.TrackLocal{videoSample, audioSample}
	} else {
		videoRTP, _ = webrtc.NewTrackLocalStaticRTP(videoCap, "video", "solin")
		audioRTP, _ = webrtc.NewTrackLocalStaticRTP(audioCap, "audio", "solin")
		tracks = []webrtc.TrackLocal{videoRTP, audioRTP}
	}
	for _, t := range tracks {
		if _, err := pc.AddTransceiverFromTrack(t, webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionSendonly}); err != nil {
			log.Fatal(err)
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
		go readFrames(videoSample, audioSample, &vbytes, &abytes)
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
