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
	AVFormatContext *fmt;
	const char *path, *format_name, *ffmpeg_options;
	int buffering, mutex;
	uint64_t interrupt_poll_ts;
	bool is_local_file, reconnecting, hw, has_audio, has_video, kill, stopping;
	struct solin_read_clock read_clock;
	void (*buffer_cb)(void *, bool);
	void *opaque;
} mp_media_t;

#define LOG_INFO 0
#define LOG_WARNING 1
#define AVFMT_FLAG_NOBUFFER 1
#define AVMEDIA_TYPE_VIDEO 0
#define AVMEDIA_TYPE_AUDIO 1
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

#include "interrupt-under-test.h"

int main(void)
{
	/* Normal I/O does not suspend presentation. Real starvation suspends the
	 * full blocked interval; live/preload reads do not participate. */
	struct solin_read_clock read_clock = {0};
	solin_read_begin(&read_clock, 100, true);
	REQUIRE(!solin_read_waiting(&read_clock, SOLIN_READ_WAIT_NS));
	solin_read_end(&read_clock);
	REQUIRE(solin_read_deadline(&read_clock, 1000, 500) == 1000);
	solin_read_begin(&read_clock, 100, true);
	REQUIRE(solin_read_waiting(&read_clock, SOLIN_READ_WAIT_NS + 100));
	solin_read_end(&read_clock);
	REQUIRE(solin_read_deadline(&read_clock, 1000, 5000) == 5000);
	REQUIRE(solin_read_deadline(&read_clock, 1000, 5000) == 1000);
	REQUIRE(!solin_read_waiting(&read_clock, UINT64_C(9000000000)));
	solin_read_begin(&read_clock, 100, false);
	solin_read_end(&read_clock);
	REQUIRE(solin_read_deadline(&read_clock, 1000, UINT64_C(9000000000)) == 1000);
	solin_read_begin(&read_clock, 100, true);
	solin_read_end(&read_clock);
	REQUIRE(solin_read_deadline(&read_clock, 200, 300) == 300);
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
