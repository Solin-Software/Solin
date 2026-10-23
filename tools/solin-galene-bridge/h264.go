package main

import "bytes"

// NAL unit types used to gate video after a (re)connect.
const (
	nalIDR = 5
	nalSPS = 7
	nalPPS = 8
)

// splitNALs returns the NAL units of an Annex-B access unit, without start codes.
func splitNALs(au []byte) [][]byte {
	var nals [][]byte
	start := -1
	i := 0
	for i+2 < len(au) {
		if au[i] == 0 && au[i+1] == 0 && au[i+2] == 1 {
			if start >= 0 {
				end := i
				for end > start && au[end-1] == 0 { // 4-byte start code or trailing zeros
					end--
				}
				nals = append(nals, au[start:end])
			}
			i += 3
			start = i
			continue
		}
		i++
	}
	if start >= 0 && start < len(au) {
		nals = append(nals, au[start:])
	}
	return nals
}

// h264Params remembers the latest SPS and PPS so a keyframe that arrives
// without them can still start decoding at a receiver that just connected.
type h264Params struct {
	sps, pps []byte
}

// inspect records parameter sets found in au and reports whether au contains
// an IDR slice and whether it carries its own SPS and PPS.
func (h *h264Params) inspect(au []byte) (idr, hasParams bool) {
	var sps, pps bool
	for _, n := range splitNALs(au) {
		if len(n) == 0 {
			continue
		}
		switch n[0] & 0x1f {
		case nalIDR:
			idr = true
		case nalSPS:
			h.sps, sps = append(h.sps[:0], n...), true
		case nalPPS:
			h.pps, pps = append(h.pps[:0], n...), true
		}
	}
	return idr, sps && pps
}

// withParams prefixes au with the remembered SPS and PPS.
func (h *h264Params) withParams(au []byte) []byte {
	if h.sps == nil || h.pps == nil {
		return au
	}
	startCode := []byte{0, 0, 0, 1}
	return bytes.Join([][]byte{nil, h.sps, h.pps, au[skipStartCode(au):]}, startCode)
}

func skipStartCode(au []byte) int {
	switch {
	case len(au) >= 4 && au[0] == 0 && au[1] == 0 && au[2] == 0 && au[3] == 1:
		return 4
	case len(au) >= 3 && au[0] == 0 && au[1] == 0 && au[2] == 1:
		return 3
	}
	return 0
}
