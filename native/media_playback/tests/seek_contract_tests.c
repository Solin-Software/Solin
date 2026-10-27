/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct { int num; int den; } AVRational;
typedef struct { int index; AVRational time_base; } AVStream;
typedef struct { int64_t duration; AVStream **streams; } AVFormatContext;
typedef struct { AVStream *stream; unsigned packets; unsigned flushes; } decoder;
typedef struct {
	AVFormatContext *fmt;
	decoder v, a;
	bool has_video, has_audio, is_local_file, seek_next_ts, pause, eof;
	bool terminal_packet_corrupt, completed;
	int mutex;
	void *v_preload_cb;
} mp_media_t;

#define AV_NOPTS_VALUE INT64_MIN
#define AVSEEK_FLAG_FRAME 8
#define AVSEEK_FLAG_BACKWARD 1
#define AV_TIME_BASE_Q ((AVRational){1, 1000000})
#define LOG_WARNING 0
#define blog(...) ((void)0)

static int seek_result, selected_stream, prepare_calls, video_calls;
static int64_t selected_target;
static unsigned lock_calls;
static void pthread_mutex_lock(int *mutex) { (*mutex)++; lock_calls++; }
static void pthread_mutex_unlock(int *mutex) { (*mutex)--; }
static int av_seek_frame(AVFormatContext *fmt, int stream, int64_t target, int flags)
{
	(void)fmt;
	(void)flags;
	selected_stream = stream;
	selected_target = target;
	return seek_result;
}
static int64_t av_rescale_q(int64_t value, AVRational from, AVRational to)
{
	return value * from.num * to.den / (from.den * to.num);
}
static void mp_decode_flush(decoder *d)
{
	d->packets = 0;
	d->flushes++;
}
static bool mp_media_prepare_frames(mp_media_t *m)
{
	(void)m;
	prepare_calls++;
	return true;
}
static void mp_media_next_video(mp_media_t *m, bool preload)
{
	(void)m;
	(void)preload;
	video_calls++;
}

#include "seek-under-test.h"

#define REQUIRE(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "Seek contract failed at line %d: %s\n", __LINE__, #condition); \
		exit(1); \
	} \
} while (0)

int main(void)
{
	/* Stream zero can be artwork. Seek with the selected playback time base. */
	AVStream artwork = {0, {1, 90000}};
	AVStream audio = {1, {1, 48000}};
	AVStream video = {2, {1, 1000}};
	AVStream *streams[] = {&artwork, &audio, &video};
	AVFormatContext format = {8000000, streams};
	mp_media_t media = {
		.fmt = &format, .a = {&audio, 7, 0}, .v = {&video, 9, 0},
		.has_audio = true, .has_video = true, .is_local_file = true,
		.seek_next_ts = true, .pause = true, .eof = true,
		.terminal_packet_corrupt = true,
		.completed = true,
		.v_preload_cb = &format,
	};

	seek_result = -1;
	seek_to(&media, 4000000);
	REQUIRE(media.a.packets == 7 && media.v.packets == 9);
	REQUIRE(media.a.flushes == 0 && media.v.flushes == 0);
	REQUIRE(!media.seek_next_ts && media.eof && media.terminal_packet_corrupt && media.completed);
	REQUIRE(prepare_calls == 0 && video_calls == 0);

	seek_result = 0;
	media.seek_next_ts = true;
	seek_to(&media, 4000000);
	REQUIRE(!media.eof && !media.terminal_packet_corrupt && !media.completed);
	REQUIRE(media.mutex == 0 && lock_calls == 1);
	REQUIRE(media.a.packets == 0 && media.v.packets == 0);
	REQUIRE(media.a.flushes == 1 && media.v.flushes == 1);
	REQUIRE(selected_stream == video.index && selected_target == 4000);
	REQUIRE(prepare_calls == 1 && video_calls == 1);

	media.has_video = false;
	media.eof = true;
	seek_to(&media, 2000000);
	REQUIRE(!media.eof);
	REQUIRE(selected_stream == audio.index && selected_target == 96000);
	REQUIRE(media.a.flushes == 2 && media.v.flushes == 1);
	REQUIRE(prepare_calls == 1);

	format.duration = AV_NOPTS_VALUE;
	seek_to(&media, 123);
	REQUIRE(selected_target == 123);
	puts("Seek contract qualified");
	return 0;
}
