/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../read_clock.h"

#define REQUIRE(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "Interrupt contract failed at line %d: %s\n", __LINE__, #condition); \
		exit(1); \
	} \
} while (0)

typedef struct { int unused; } AVInputFormat;
typedef struct { int unused; } AVDictionary;
typedef struct {
	int flags;
	struct { int (*callback)(void *); void *opaque; } interrupt_callback;
} AVFormatContext;
typedef struct {
	bool frame_ready, eof;
	int64_t frame_pts, next_pts, last_duration;
} mock_decode_t;
typedef struct {
	AVFormatContext *fmt;
	const char *path, *format_name, *ffmpeg_options;
	int buffering, mutex;
	uint64_t interrupt_poll_ts;
	bool is_local_file, reconnecting, hw, has_audio, has_video, kill, stopping;
	bool seek_next_ts, audio_pending;
	int speed;
	uint64_t next_ns;
	int64_t next_pts_ns, pending_audio_pts;
	mock_decode_t a, v;
	struct { size_t size; } tempo_queue;
	struct solin_read_clock read_clock;
	void (*buffer_cb)(void *, bool);
	void *opaque;
} mp_media_t;

#define LOG_INFO 0
#define LOG_WARNING 1
#define AVFMT_FLAG_NOBUFFER 1
#define AVMEDIA_TYPE_VIDEO 0
#define AVMEDIA_TYPE_AUDIO 1
#define FFMAX(a, b) ((a) > (b) ? (a) : (b))
#define FFMIN(a, b) ((a) < (b) ? (a) : (b))
#define blog(...) ((void)0)

static uint64_t clock_ns;
static int buffering_notifications;
static void buffer_wait(void *opaque, bool waiting)
{
	(void)opaque;
	REQUIRE(waiting);
	buffering_notifications++;
}
static int timeout_options, cancellation, probes;
static AVFormatContext format_context;
static uint64_t os_gettime_ns(void) { return clock_ns; }
static void pthread_mutex_lock(int *mutex) { (void)mutex; }
static void pthread_mutex_unlock(int *mutex) { (void)mutex; }
static const AVInputFormat *av_find_input_format(const char *name) { (void)name; return NULL; }
static int av_dict_set(AVDictionary **opts, const char *key, const char *value, int flags)
{
	(void)opts; (void)value; (void)flags;
	if (strcmp(key, "stimeout") == 0)
		timeout_options++;
	return 0;
}
static int av_dict_set_int(AVDictionary **opts, const char *key, int value, int flags)
{
	(void)opts; (void)key; (void)value; (void)flags;
	return 0;
}
static int av_dict_parse_string(AVDictionary **opts, const char *text,
			       const char *equals, const char *space, int flags)
{
	(void)opts; (void)text; (void)equals; (void)space; (void)flags;
	return 0;
}
static void av_dict_free(AVDictionary **opts) { (void)opts; }
static AVFormatContext *avformat_alloc_context(void)
{
	memset(&format_context, 0, sizeof(format_context));
	return &format_context;
}
static int avformat_open_input(AVFormatContext **context, const char *path,
			      const AVInputFormat *format, AVDictionary **opts)
{
	(void)path; (void)format; (void)opts;
	AVFormatContext *fmt = *context;
	REQUIRE(fmt->interrupt_callback.callback != NULL);
	mp_media_t *media = fmt->interrupt_callback.opaque;
	REQUIRE(media != NULL);
	if (!cancellation)
		return 0;

	/* A retrying input must observe both stop and destruction requests. */
	while (++probes <= 30) {
		clock_ns += 1000000;
		if (probes == 3) {
			media->kill = cancellation == 1;
			media->stopping = cancellation == 2;
		}
		if (fmt->interrupt_callback.callback(fmt->interrupt_callback.opaque))
			return -1;
	}
	REQUIRE(false);
	return 0;
}
static int avformat_find_stream_info(AVFormatContext *fmt, void *opts)
{
	(void)fmt; (void)opts;
	return 0;
}
static bool mp_decode_init(mp_media_t *m, int type, bool hw)
{
	(void)m; (void)hw;
	return type == AVMEDIA_TYPE_AUDIO;
}

static int64_t playback_time(int64_t media_time, int speed)
{
	return media_time * 100 / speed;
}

static bool mp_media_audio_pending_pts(mp_media_t *m, int64_t *pts)
{
	if (!m->audio_pending)
		return false;
	*pts = m->pending_audio_pts;
	return true;
}

static int64_t mp_media_audio_frame_pts(mp_media_t *m)
{
	return m->a.frame_pts;
}

#include "interrupt-under-test.h"

static void test_read_budget(void)
{
	mp_media_t m = {.next_ns = 10000, .next_pts_ns = 1000, .speed = 200};

	/* Missing frames use the next predicted presentation, scaled by rate. */
	m.has_audio = true;
	m.a.next_pts = 1400;
	REQUIRE(mp_media_read_budget(&m) == 200);
	m.has_audio = false;
	m.has_video = true;
	m.v.next_pts = 1600;
	REQUIRE(mp_media_read_budget(&m) == 300);

	/* A ready video precedes the next filtered audio block. */
	m.has_audio = true;
	m.v.frame_ready = true;
	m.v.frame_pts = 1200;
	m.audio_pending = true;
	m.tempo_queue.size = 1;
	m.pending_audio_pts = 1400;
	m.a.next_pts = 1800;
	REQUIRE(mp_media_read_budget(&m) == 100);

	/* Filtered output wins over later decoder input and video. */
	m.v.frame_pts = 1600;
	m.pending_audio_pts = 1200;
	REQUIRE(mp_media_read_budget(&m) == 100);

	/* A ready decoder frame sets the horizon when no output is queued. */
	m.tempo_queue.size = 0;
	m.a.frame_ready = true;
	m.a.frame_pts = 1200;
	m.pending_audio_pts = 1050;
	REQUIRE(mp_media_read_budget(&m) == 100);
	m.a.frame_ready = false;
	m.a.next_pts = 1400;
	REQUIRE(mp_media_read_budget(&m) == 200);

	/* Near-coincident A/V events do not turn sub-frame demux work into a
	 * clock suspension. The earliest decoder cadence remains the budget. */
	m.v.frame_pts = 1001;
	m.v.last_duration = 400;
	m.a.last_duration = 100;
	REQUIRE(mp_media_read_budget(&m) == 50);
	m.speed = 50;
	REQUIRE(mp_media_read_budget(&m) == 200);
	m.speed = 200;
	m.v.frame_pts = 1600;
	m.v.last_duration = 0;
	m.a.last_duration = 0;

	/* A decoder can finish while filtered output is still queued. */
	m.a.eof = true;
	m.tempo_queue.size = 1;
	m.pending_audio_pts = 1200;
	REQUIRE(mp_media_read_budget(&m) == 100);
	m.tempo_queue.size = 0;
	/* Finished streams with no pending audio do not set the horizon. */
	m.audio_pending = false;
	m.a.eof = true;
	REQUIRE(mp_media_read_budget(&m) == 300);
	m.v.eof = true;
	REQUIRE(mp_media_read_budget(&m) == UINT64_MAX);

	/* Seek and reset leave no valid running-clock horizon. */
	m.v.eof = false;
	m.seek_next_ts = true;
	REQUIRE(mp_media_read_budget(&m) == UINT64_MAX);
	m.seek_next_ts = false;
	m.next_ns = 0;
	REQUIRE(mp_media_read_budget(&m) == UINT64_MAX);
}

int main(void)
{
	test_read_budget();
	/* Budget is a media interval, independent of absolute scheduler time. */
	struct solin_read_clock read_clock = {0};
	solin_read_begin(&read_clock, 100, true);
	solin_read_end(&read_clock, 900);
	clock_ns = UINT64_C(9000000000);
	REQUIRE(solin_read_take_delay(&read_clock, 1000) == 0);

	/* A blocked read consumes the interval before suspending presentation. */
	solin_read_begin(&read_clock, 900, true);
	solin_read_end(&read_clock, 2300);
	REQUIRE(solin_read_take_delay(&read_clock, 1000) == 400);
	REQUIRE(solin_read_take_delay(&read_clock, 1000) == 0);

	/* Late wake-ups and CPU work between fast reads cannot accumulate gaps. */
	for (uint64_t tick = 0; tick < 10000; tick++) {
		uint64_t now = UINT64_C(9000000000) + tick * 10000;
		solin_read_begin(&read_clock, now, true);
		solin_read_end(&read_clock, now + 100);
		solin_read_begin(&read_clock, now + 5000, true);
		solin_read_end(&read_clock, now + 5100);
		REQUIRE(solin_read_take_delay(&read_clock, 1000) == 0);
	}

	/* Several reads share the final budget once the next frame is known. */
	solin_read_begin(&read_clock, 100, true);
	solin_read_end(&read_clock, 700);
	solin_read_begin(&read_clock, 800, true);
	solin_read_end(&read_clock, 1300);
	solin_read_begin(&read_clock, 2000, true);
	solin_read_end(&read_clock, 2300);
	REQUIRE(solin_read_take_delay(&read_clock, 1200) == 200);
	REQUIRE(solin_read_take_delay(&read_clock, 0) == 0);
	/* An early estimate must not survive a later, longer presentation interval. */
	solin_read_begin(&read_clock, 0, true);
	solin_read_end(&read_clock, 5000);
	REQUIRE(solin_read_take_delay(&read_clock, 10000) == 0);

	/* No running timeline and untracked reads do not suspend presentation. */
	solin_read_begin(&read_clock, 100, true);
	solin_read_end(&read_clock, 5000);
	REQUIRE(solin_read_take_delay(&read_clock, UINT64_MAX) == 0);
	solin_read_begin(&read_clock, 100, false);
	solin_read_end(&read_clock, 5000);
	REQUIRE(solin_read_take_delay(&read_clock, 1000) == 0);

	/* The UI debounce is independent of presentation accounting. */
	solin_read_begin(&read_clock, 100, true);
	REQUIRE(!solin_read_waiting(&read_clock, 100 + SOLIN_READ_WAIT_NS - 1));
	REQUIRE(solin_read_waiting(&read_clock, 100 + SOLIN_READ_WAIT_NS));
	solin_read_end(&read_clock, 100 + SOLIN_READ_WAIT_NS);
	REQUIRE(solin_read_take_delay(&read_clock, 1000) == SOLIN_READ_WAIT_NS - 1000);
	REQUIRE(!solin_read_waiting(&read_clock, UINT64_C(9000000000)));
	mp_media_t blocked = {.buffer_cb = buffer_wait};
	solin_read_begin(&blocked.read_clock, 100, true);
	clock_ns = SOLIN_READ_WAIT_NS + 100;
	REQUIRE(!interrupt_callback(&blocked));
	REQUIRE(buffering_notifications == 1);
	blocked.stopping = true;
	clock_ns += 21000000;
	REQUIRE(interrupt_callback(&blocked));
	REQUIRE(buffering_notifications == 1);

	for (int local = 0; local <= 1; local++) {
		for (int request = 0; request <= 2; request++) {
			mp_media_t media = {
				.path = "http://127.0.0.1/finite-input",
				.is_local_file = local != 0,
			};
			clock_ns = 0;
			timeout_options = probes = 0;
			cancellation = request;
			REQUIRE(init_avformat(&media) == (request == 0));
			REQUIRE(timeout_options == (local ? 0 : 1));
			if (request)
				REQUIRE(probes <= 24);
			else
				REQUIRE(media.has_audio && !media.has_video);
		}
	}
	puts("Finite and live input cancellation qualified");
	return 0;
}
