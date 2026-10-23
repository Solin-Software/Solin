package main

import "time"

// rtpTimestamp maps a capture pts to an RTP timestamp at the given clock
// rate. Seconds and the sub-second remainder are scaled separately so large
// pts values (libobs uses a monotonic clock that can be days old) cannot
// overflow int64; the result wraps modulo 2^32 as RTP expects. Negative pts
// (Opus pre-skip) are allowed.
func rtpTimestamp(base uint32, pts time.Duration, clockRate uint32) uint32 {
	sec := int64(pts / time.Second)
	rem := int64(pts % time.Second)
	ticks := sec*int64(clockRate) + rem*int64(clockRate)/int64(time.Second)
	return base + uint32(ticks)
}

// ntpTime converts a wall-clock time to the 64-bit NTP format used in RTCP
// sender reports.
func ntpTime(t time.Time) uint64 {
	secs := uint64(t.Unix()) + 2208988800
	frac := uint64(t.Nanosecond()) << 32 / uint64(time.Second)
	return secs<<32 | frac
}

// seqGap compares an RTP sequence number with the previous one. lost is the
// number of packets missing in between (0 when contiguous); late is true for
// a duplicate or reordered packet that arrived after a newer one.
func seqGap(prev, cur uint16) (lost int, late bool) {
	d := cur - prev // modulo 2^16
	if d == 0 || d >= 0x8000 {
		return 0, true
	}
	return int(d) - 1, false
}
