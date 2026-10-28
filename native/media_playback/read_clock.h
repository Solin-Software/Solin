/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A blocked finite read suspends presentation time, not the media timeline.
 * The threshold suppresses normal per-packet I/O latency in the UI. */
#define SOLIN_READ_WAIT_NS UINT64_C(250000000)

struct solin_read_clock {
	uint64_t started_ns;
	uint64_t elapsed_ns;
	bool tracking;
};

static inline void solin_read_begin(struct solin_read_clock *clock, uint64_t now, bool tracking)
{
	clock->started_ns = now;
	clock->tracking = tracking;
}

static inline bool solin_read_waiting(const struct solin_read_clock *clock, uint64_t now)
{
	return clock->tracking && now - clock->started_ns >= SOLIN_READ_WAIT_NS;
}

static inline void solin_read_end(struct solin_read_clock *clock, uint64_t now)
{
	if (clock->tracking)
		clock->elapsed_ns += now - clock->started_ns;
	clock->tracking = false;
}

/* Settle once the next presentation is known. Per-packet estimates can change
 * while decoding; they must not leave an irreversible clock adjustment behind.
 * Decode/filter/scheduler work never contributes to the measured read time. */
static inline uint64_t solin_read_take_delay(struct solin_read_clock *clock, uint64_t budget)
{
	uint64_t elapsed = clock->elapsed_ns;
	clock->elapsed_ns = 0;
	return elapsed > budget ? elapsed - budget : 0;
}
