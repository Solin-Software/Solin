/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A blocked finite read suspends presentation time, not the media timeline.
 * The threshold suppresses normal per-packet I/O latency in the UI. */
#define SOLIN_READ_WAIT_NS UINT64_C(250000000)

struct solin_read_clock {
	uint64_t started_ns;
	bool tracking;
	bool pending;
};

static inline void solin_read_begin(struct solin_read_clock *clock, uint64_t now, bool tracking)
{
	clock->started_ns = now;
	clock->tracking = tracking;
	clock->pending |= tracking;
}

static inline bool solin_read_waiting(const struct solin_read_clock *clock, uint64_t now)
{
	return clock->tracking && now - clock->started_ns >= SOLIN_READ_WAIT_NS;
}

static inline void solin_read_end(struct solin_read_clock *clock)
{
	clock->tracking = false;
}

/* A successful read may fit entirely inside the next frame's normal budget.
 * Rebase only an overdue deadline, independently of the UI debounce. */
static inline uint64_t solin_read_deadline(struct solin_read_clock *clock,
					 uint64_t deadline, uint64_t now)
{
	bool pending = clock->pending;
	clock->pending = false;
	return pending && deadline && deadline < now ? now : deadline;
}
