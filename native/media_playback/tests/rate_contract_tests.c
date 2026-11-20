/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <util/platform.h>
#include <util/threading.h>

static uint64_t clock_now(void);
static int wait_for_clock(os_event_t *event, unsigned long milliseconds);

/* Compile the patched worker with a controlled clock and wait. FFmpeg decoding,
 * command handling, position reporting and frame output all run as shipped. */
#define os_gettime_ns clock_now
#define os_event_timedwait wait_for_clock
#include <media-playback/media.c>
#undef os_event_timedwait
#undef os_gettime_ns

#define NS_PER_MS UINT64_C(1000000)

enum { CHANGE_AFTER = 3, FRAME_COUNT = 10, MEDIA_FRAME_MS = 100 };

struct rate_change {
	int from, to;
	uint64_t old_interval_ns, new_interval_ns;
};

struct video_sample {
	uint64_t deadline_ns, arrival_ns;
	int64_t position_ms;
};

static struct {
	mp_media_t media;
	struct video_sample samples[FRAME_COUNT];
	struct rate_change rate;
	uint64_t now_ns;
	size_t count;
	bool late, requested;
} run;

#define REQUIRE(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "Rate contract %d%% to %d%% (%s), %zu frames received, line %d: %s\n", \
			run.rate.from, run.rate.to, run.late ? "late wake" : "on-time wake", \
			run.count, __LINE__, #condition); \
		exit(1); \
	} \
} while (0)

static uint64_t clock_now(void)
{
	return run.now_ns;
}

static int wait_for_clock(os_event_t *event, unsigned long milliseconds)
{
	if (os_event_try(event) == 0)
		return 0;

	if (run.count == FRAME_COUNT) {
		pthread_mutex_lock(&run.media.mutex);
		run.media.kill = true;
		pthread_mutex_unlock(&run.media.mutex);
		return 0;
	}

	uint64_t remaining_ns = milliseconds * NS_PER_MS;
	if (run.count == CHANGE_AFTER && !run.requested) {
		run.requested = true;
		/* Post the command during the pending interval. A late worker sees it
		 * only after enough time has elapsed for several frames to be due. */
		run.now_ns += remaining_ns / 2;
		mp_media_set_speed(&run.media, run.rate.to);
		if (run.late)
			run.now_ns += remaining_ns / 2 + run.rate.new_interval_ns * 3;
		REQUIRE(os_event_try(event) == 0);
		return 0;
	}

	run.now_ns += remaining_ns;
	return ETIMEDOUT;
}

static void record_video(void *opaque, struct obs_source_frame *frame)
{
	mp_media_t *media = opaque;
	REQUIRE(run.count < FRAME_COUNT);
	run.samples[run.count++] = (struct video_sample){
		.deadline_ns = frame->timestamp,
		.arrival_ns = clock_now(),
		.position_ms = mp_media_get_current_time(media),
	};
}

static void test_rate_change(const char *path, struct rate_change rate, bool late)
{
	run.rate = rate;
	run.now_ns = 1000 * NS_PER_MS;
	run.count = 0;
	run.late = late;
	run.requested = false;
	const struct mp_media_info info = {
		.opaque = &run.media,
		.v_cb = record_video,
		.path = path,
		.speed = rate.from,
		.buffering = 1,
		.is_local_file = true,
	};
	REQUIRE(mp_media_init(&run.media, &info));
	mp_media_play(&run.media, false, false);
	REQUIRE(pthread_join(run.media.thread, NULL) == 0);
	run.media.thread_valid = false;
	mp_media_free(&run.media);
	REQUIRE(run.requested && run.count == FRAME_COUNT);

	/* Anchor the frame timestamps to the test clock at the first output.
	 * Native timed waits round to the nearest millisecond. */
	const uint64_t clock_offset_ns = run.samples[0].arrival_ns - run.samples[0].deadline_ns;
	const uint64_t rounding_ns = NS_PER_MS / 2;
	for (size_t index = 1; index < FRAME_COUNT; index++) {
		struct video_sample before = run.samples[index - 1];
		struct video_sample after = run.samples[index];
		/* The frame already scheduled before the command retains its deadline. */
		uint64_t interval = index <= CHANGE_AFTER ? rate.old_interval_ns : rate.new_interval_ns;
		REQUIRE(after.deadline_ns == before.deadline_ns + interval);
		uint64_t deadline_ns = after.deadline_ns + clock_offset_ns;
		REQUIRE(after.arrival_ns + rounding_ns >= deadline_ns);
		if (!late)
			REQUIRE(after.arrival_ns <= deadline_ns + rounding_ns);
		/* The first callback precedes the initial publication of position. */
		if (index > 1)
			REQUIRE(after.position_ms - before.position_ms == MEDIA_FRAME_MS);
	}
	const uint64_t first_arrival = run.samples[CHANGE_AFTER].arrival_ns;
	REQUIRE((first_arrival == run.samples[CHANGE_AFTER + 1].arrival_ns) == late);
}

int main(int argc, char **argv)
{
	REQUIRE(argc == 2);
	/* The fixture is video-only with a 100 ms media interval. */
	const struct rate_change rates[] = {
		{100, 150, 100 * NS_PER_MS, UINT64_C(66666667)},
		{150, 200, UINT64_C(66666667), 50 * NS_PER_MS},
		{200, 25, 50 * NS_PER_MS, 400 * NS_PER_MS},
	};
	for (size_t index = 0; index < sizeof(rates) / sizeof(rates[0]); index++) {
		test_rate_change(argv[1], rates[index], false);
		test_rate_change(argv[1], rates[index], true);
	}
	puts("Rate transition continuity qualified");
	return 0;
}
