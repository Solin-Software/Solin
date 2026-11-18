package main

import (
	"encoding/json"
	"net"
	"net/url"
	"strings"

	"github.com/pion/webrtc/v4"
)

// galeneICEServer is one entry of the rtcConfiguration Galene sends in its
// "joined" message (a browser RTCIceServer): urls may be a string or a list.
type galeneICEServer struct {
	URLs           json.RawMessage `json:"urls"`
	Username       string          `json:"username"`
	Credential     any             `json:"credential"`
	CredentialType string          `json:"credentialType"`
}

type galeneRTCConfiguration struct {
	ICEServers         []galeneICEServer `json:"iceServers"`
	ICETransportPolicy string            `json:"iceTransportPolicy"`
}

// localEndpoint reports whether Galene is reached on this computer (a loopback
// address). Solin runs the bridge next to Galene: STUN/TURN servers are for remote
// attendees and only slow ICE down here. With a relay and the many interfaces a
// hall PC can have (Docker, LXD, IPv6), the checks outlast the connection timeout.
func localEndpoint(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// settingEngine for a peer connection to Galene at endpoint. Next to Galene the
// bridge offers a single candidate, its IPv4 loopback address: with a TURN relay
// configured, Galene asks the relay for a permission per remote candidate before
// it answers the nomination, and a hall PC's many interfaces (Docker, LXD, Wi-Fi,
// IPv6) queued enough of those to outlast the connection timeout.
func settingEngine(endpoint string) webrtc.SettingEngine {
	var se webrtc.SettingEngine
	if localEndpoint(endpoint) {
		se.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
		se.SetIncludeLoopbackCandidate(true)
		se.SetIPFilter(func(ip net.IP) bool { return ip.IsLoopback() })
	}
	return se
}

func validICEURL(u string) bool {
	l := strings.ToLower(u)
	for _, p := range []string{"stun:", "stuns:", "turn:", "turns:"} {
		if strings.HasPrefix(l, p) {
			return true
		}
	}
	return false
}

func isTURN(u string) bool {
	l := strings.ToLower(u)
	return strings.HasPrefix(l, "turn:") || strings.HasPrefix(l, "turns:")
}

// rtcConfigurationFromGalene converts Galene's rtcConfiguration JSON to a
// Pion configuration. Entries Pion cannot use (unknown schemes, TURN without
// a password credential) are skipped rather than failing the whole
// configuration. A null or empty input yields an empty configuration.
func rtcConfigurationFromGalene(raw json.RawMessage) (webrtc.Configuration, error) {
	var conf webrtc.Configuration
	if len(raw) == 0 || string(raw) == "null" {
		return conf, nil
	}
	var g galeneRTCConfiguration
	if err := json.Unmarshal(raw, &g); err != nil {
		return conf, err
	}
	for _, s := range g.ICEServers {
		var urls []string
		if len(s.URLs) > 0 {
			var one string
			if json.Unmarshal(s.URLs, &one) == nil {
				urls = []string{one}
			} else if err := json.Unmarshal(s.URLs, &urls); err != nil {
				continue
			}
		}
		credential, _ := s.Credential.(string)
		passwordType := s.CredentialType == "" || strings.EqualFold(s.CredentialType, "password")
		var keep []string
		for _, u := range urls {
			if !validICEURL(u) {
				continue
			}
			if isTURN(u) && (!passwordType || credential == "") {
				continue
			}
			keep = append(keep, u)
		}
		if len(keep) == 0 {
			continue
		}
		server := webrtc.ICEServer{URLs: keep, Username: s.Username}
		if credential != "" {
			server.Credential = credential
			server.CredentialType = webrtc.ICECredentialTypePassword
		}
		conf.ICEServers = append(conf.ICEServers, server)
	}
	if strings.EqualFold(g.ICETransportPolicy, "relay") {
		conf.ICETransportPolicy = webrtc.ICETransportPolicyRelay
	}
	return conf, nil
}

// iceServersFromLinks parses WHIP "Link" headers (RFC 9725 section 4.6):
//
//	<turn:host:3478>; rel="ice-server"; username="u"; credential="p"; credential-type="password"
//
// Several links may share one header, separated by commas.
func iceServersFromLinks(headers []string) []webrtc.ICEServer {
	var servers []webrtc.ICEServer
	for _, h := range headers {
		for _, link := range splitOutsideQuotes(h, ',') {
			link = strings.TrimSpace(link)
			if !strings.HasPrefix(link, "<") {
				continue
			}
			end := strings.Index(link, ">")
			if end < 0 {
				continue
			}
			u := link[1:end]
			params := map[string]string{}
			for _, p := range splitOutsideQuotes(link[end+1:], ';') {
				k, v, ok := strings.Cut(strings.TrimSpace(p), "=")
				if !ok {
					continue
				}
				params[strings.ToLower(strings.TrimSpace(k))] = unquote(strings.TrimSpace(v))
			}
			if !strings.EqualFold(params["rel"], "ice-server") || !validICEURL(u) {
				continue
			}
			s := webrtc.ICEServer{URLs: []string{u}}
			if isTURN(u) {
				ct := params["credential-type"]
				if params["credential"] == "" || (ct != "" && !strings.EqualFold(ct, "password")) {
					continue
				}
				s.Username = params["username"]
				s.Credential = params["credential"]
				s.CredentialType = webrtc.ICECredentialTypePassword
			}
			servers = append(servers, s)
		}
	}
	return servers
}

// splitOutsideQuotes splits s on sep, ignoring separators inside double
// quotes or angle brackets.
func splitOutsideQuotes(s string, sep byte) []string {
	var parts []string
	inQuote, inAngle, escaped := false, false, false
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case escaped:
			escaped = false
		case inQuote && c == '\\':
			escaped = true
		case c == '"' && !inAngle:
			inQuote = !inQuote
		case c == '<' && !inQuote:
			inAngle = true
		case c == '>' && !inQuote:
			inAngle = false
		case c == sep && !inQuote && !inAngle:
			parts = append(parts, s[start:i])
			start = i + 1
		}
	}
	return append(parts, s[start:])
}

func unquote(v string) string {
	if len(v) >= 2 && v[0] == '"' && v[len(v)-1] == '"' {
		v = v[1 : len(v)-1]
		var b strings.Builder
		for i := 0; i < len(v); i++ {
			if v[i] == '\\' && i+1 < len(v) {
				i++
			}
			b.WriteByte(v[i])
		}
		return b.String()
	}
	return v
}
