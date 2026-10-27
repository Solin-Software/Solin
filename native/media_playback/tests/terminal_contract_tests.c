/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "../read_clock.h"

#define REQUIRE(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "Terminal contract failed at line %d: %s\n", __LINE__, #condition); \
		exit(1); \
	} \
} while (0)

#define AVERROR_EOF (-1000)
#define AVERROR_EXIT (-1001)
#define AVERROR_INVALIDDATA (-1002)
#define AV_PKT_FLAG_CORRUPT 2
#define IO_ERROR (-5)
#define LOG_WARNING 0
#define blog(...) ((void)0)

enum obs_media_state {
	OBS_MEDIA_STATE_PLAYING, OBS_MEDIA_STATE_PAUSED, OBS_MEDIA_STATE_STOPPED,
	OBS_MEDIA_STATE_ENDED, OBS_MEDIA_STATE_ERROR, OBS_MEDIA_STATE_BUFFERING,
};
typedef struct { int size, flags; } AVPacket;
typedef struct { int error; } AVIOContext;
typedef struct { AVIOContext *pb; } AVFormatContext;
typedef struct mp_decode { bool frame_ready, eof; } decoder;
typedef void (*mp_stop_cb)(void *, bool);
typedef struct {
	AVFormatContext *fmt;
	struct { AVPacket **array; unsigned num; } packet_pool;
	bool kill, stopping, terminal_packet_corrupt;
	bool has_audio, has_video, looping, active, completed, reset, reset_ts, reconnecting;
	bool is_local_file, full_decode, pause;
	bool tempo_failed, tempo_rate_pending;
	struct { unsigned size; } tempo_queue;
	uint64_t next_ns;
	int64_t play_sys_ts;
	struct solin_read_clock read_clock;
	decoder v, a;
	void *sem;
	void *wake_event;
	int mutex;
	mp_stop_cb stop_cb;
	void *opaque;
} mp_media_t;
typedef mp_media_t mp_cache_t;
typedef struct { int id; } media_playback_t;
struct ffmpeg_source {
	media_playback_t *media;
	bool destroy_media, is_clear_on_media_end, is_track_matte;
	bool close_when_inactive, is_local_file, is_looping, reconnecting;
	void *source;
	volatile long state;
};

static int read_result, callbacks, ended, started, destroyed, opened, locked;
static int open_failure;
static int resets, wakes;
static uint64_t os_gettime_ns(void) { return 0; }
static bool thread_result, stop_callback;
static media_playback_t initial_media = {1}, replacement_media = {2};
static AVPacket packet;

static void media_stopped(void *opaque, bool error);
static void ffmpeg_source_open(struct ffmpeg_source *s);
static void pthread_mutex_lock(int *mutex) { (void)mutex; locked++; }
static void pthread_mutex_unlock(int *mutex) { (void)mutex; locked--; }
static int av_read_frame(AVFormatContext *fmt, AVPacket *pkt)
{
	(void)fmt; (void)pkt;
	return read_result;
}
static AVPacket *av_packet_alloc(void) { return &packet; }
#define da_end(pool) ((pool).num ? &(pool).array[(pool).num - 1] : NULL)
#define da_pop_back(pool) ((pool).num--)
static decoder *get_packet_decoder(mp_media_t *m, AVPacket *pkt)
{
	(void)m; (void)pkt;
	return NULL;
}
static void mp_decode_push_packet(decoder *d, AVPacket *pkt) { (void)d; (void)pkt; }
static void mp_media_free_packet(mp_media_t *m, AVPacket *pkt) { (void)m; (void)pkt; }
static bool mp_media_thread(mp_media_t *m) { (void)m; return thread_result; }
static bool mp_cache_thread(mp_cache_t *c) { (void)c; return thread_result; }
static bool mp_media_reset(mp_media_t *m) { (void)m; resets++; return false; }
static void os_sem_post(void *sem) { (void)sem; wakes++; }
static void os_event_signal(void *event) { (void)event; wakes++; }
static enum obs_media_state get_media_state(struct ffmpeg_source *s)
{
	return (enum obs_media_state)s->state;
}
static long os_atomic_load_long(const volatile long *state) { return *state; }
static int interleaved_state = -1;
static bool os_atomic_compare_swap_long(volatile long *state, long expected, long desired)
{
	/* An explicit transport command can arrive immediately before the CAS. */
	if (interleaved_state >= 0) {
		*state = interleaved_state;
		interleaved_state = -1;
	}
	if (*state != expected)
		return false;
	*state = desired;
	return true;
}
static void mp_media_tempo_drain(mp_media_t *m) { (void)m; }
static void set_media_state(void *data, enum obs_media_state state)
{
	((struct ffmpeg_source *)data)->state = state;
}
static void obs_source_output_video(void *source, void *frame) { (void)source; (void)frame; }
static void obs_source_show_preloaded_video(void *source) { (void)source; }
static bool obs_source_showing(void *source) { (void)source; return true; }
static void obs_source_media_ended(void *source) { (void)source; ended++; }
static void obs_source_media_started(void *source) { (void)source; started++; }
static void media_playback_destroy(media_playback_t *media)
{
	REQUIRE(media != NULL);
	REQUIRE(locked == 0);
	destroyed++;
}
static void media_playback_play(media_playback_t *media, bool loop, bool reconnect)
{
	(void)media; (void)loop; (void)reconnect;
}
static bool media_playback_has_video(media_playback_t *media) { (void)media; return false; }
static void media_playback_play_pause(media_playback_t *media, bool pause) { (void)media; (void)pause; }
static struct ffmpeg_source *stopping_source;
static void media_playback_stop(media_playback_t *media)
{
	(void)media;
	REQUIRE(stopping_source->state == OBS_MEDIA_STATE_STOPPED);
	if (stop_callback)
		media_stopped(stopping_source, true);
}

#include "terminal-under-test.h"

static void ffmpeg_source_open(struct ffmpeg_source *s)
{
	opened++;
	s->media = open_failure == 2 ? NULL : &replacement_media;
	if (open_failure)
		media_stopped(s, true);
}
static void failed(void *opaque, bool error)
{
	REQUIRE(locked == 0 && error);
	callbacks++;
	media_stopped(opaque, error);
}
static void completed(void *opaque, bool error)
{
	REQUIRE(locked == 0 && !error);
	callbacks++;
	media_stopped(opaque, error);
}
static struct ffmpeg_source fresh_source(void)
{
	return (struct ffmpeg_source){
		.media = &initial_media, .is_local_file = true,
		.state = OBS_MEDIA_STATE_PLAYING,
	};
}

int main(void)
{
	for (int waiting = 0; waiting <= 1; waiting++) {
		struct ffmpeg_source buffering = fresh_source();
		buffering.state = waiting ? OBS_MEDIA_STATE_PLAYING : OBS_MEDIA_STATE_BUFFERING;
		media_buffering(&buffering, waiting != 0);
		REQUIRE(buffering.state == (waiting ? OBS_MEDIA_STATE_BUFFERING : OBS_MEDIA_STATE_PLAYING));
		for (int intent = OBS_MEDIA_STATE_PAUSED; intent <= OBS_MEDIA_STATE_ERROR; intent++) {
			buffering.state = waiting ? OBS_MEDIA_STATE_PLAYING : OBS_MEDIA_STATE_BUFFERING;
			interleaved_state = intent;
			media_buffering(&buffering, waiting != 0);
			REQUIRE(buffering.state == intent);
		}
	}
	struct ffmpeg_source completing = fresh_source();
	interleaved_state = OBS_MEDIA_STATE_STOPPED;
	media_stopped(&completing, false);
	REQUIRE(completing.state == OBS_MEDIA_STATE_STOPPED && ended == 0);
	AVIOContext io = {0};
	AVFormatContext format = {&io};
	mp_media_t media = {.fmt = &format};
	read_result = AVERROR_EOF;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_EOF);
	io.error = AVERROR_EOF;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_EOF);
	io.error = IO_ERROR;
	REQUIRE(mp_media_next_packet(&media) == IO_ERROR);
	io.error = AVERROR_EXIT;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_EXIT);
	read_result = 0;
	REQUIRE(mp_media_next_packet(&media) == 0);
	format.pb = NULL;
	read_result = AVERROR_EOF;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_EOF);
	/* WAV can return a short corrupt packet followed by EOF with no pb error. */
	read_result = 0;
	packet.flags = AV_PKT_FLAG_CORRUPT;
	REQUIRE(mp_media_next_packet(&media) == 0);
	read_result = AVERROR_EOF;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_INVALIDDATA);
	/* Recoverable corruption followed by valid data is not a terminal error. */
	read_result = 0;
	packet.flags = 0;
	REQUIRE(mp_media_next_packet(&media) == 0);
	read_result = AVERROR_EOF;
	REQUIRE(mp_media_next_packet(&media) == AVERROR_EOF);

	/* Both worker implementations distinguish failure from stop/destruction. */
	for (int cached = 0; cached <= 1; cached++) {
		for (int cancellation = 0; cancellation <= 2; cancellation++) {
			struct ffmpeg_source source = fresh_source();
			media.stop_cb = failed;
			media.opaque = &source;
			media.kill = cancellation == 1;
			media.stopping = cancellation == 2;
			callbacks = ended = 0;
			thread_result = false;
			if (cached)
				mp_cache_thread_start(&media);
			else
				mp_media_thread_start(&media);
			REQUIRE(callbacks == (cancellation ? 0 : 1));
			REQUIRE(ended == 0);
			REQUIRE(source.state == (cancellation ? OBS_MEDIA_STATE_PLAYING : OBS_MEDIA_STATE_ERROR));
			REQUIRE(source.destroy_media == (cancellation == 0));
			thread_result = true;
			callbacks = 0;
			if (cached)
				mp_cache_thread_start(&media);
			else
				mp_media_thread_start(&media);
			REQUIRE(callbacks == 0);
		}
	}

	struct ffmpeg_source source = fresh_source();
	ended = 0;
	media_stopped(&source, false);
	REQUIRE(source.state == OBS_MEDIA_STATE_ENDED && ended == 1 && !source.destroy_media);
	for (int error = 0; error <= 1; error++) {
		source = fresh_source();
		source.state = OBS_MEDIA_STATE_STOPPED;
		ended = 0;
		media_stopped(&source, error != 0);
		REQUIRE(source.state == OBS_MEDIA_STATE_STOPPED && ended == 0);
	}
	source = fresh_source();
	stopping_source = &source;
	stop_callback = true;
	ffmpeg_source_stop(&source);
	REQUIRE(source.state == OBS_MEDIA_STATE_STOPPED && ended == 0);

	/* First activation keeps the decoder's initial clock and seek contract. */
	media = (mp_media_t){0};
	mp_media_play(&media, false, false);
	REQUIRE(media.active && !media.reset && !media.reset_ts);

	/* Natural completion must not read or rewind an unavailable origin. */
	source = fresh_source();
	media = (mp_media_t){.stop_cb = completed, .opaque = &source, .active = true};
	callbacks = ended = resets = wakes = 0;
	REQUIRE(mp_media_eof(&media));
	REQUIRE(source.state == OBS_MEDIA_STATE_ENDED && ended == 1 && callbacks == 1);
	REQUIRE(media.completed && !media.active && resets == 0);
	REQUIRE(mp_media_eof(&media));
	REQUIRE(ended == 1 && callbacks == 1 && resets == 0);
	mp_media_play(&media, false, false);
	REQUIRE(media.active && media.reset && media.reset_ts && !media.completed && wakes == 1);
	REQUIRE(resets == 0); /* Reset is deferred to the decoder's next iteration. */
	media = (mp_media_t){.stop_cb = completed, .opaque = &source, .active = true, .looping = true};
	callbacks = resets = 0;
	REQUIRE(mp_media_eof(&media));
	REQUIRE(resets == 1 && callbacks == 0 && media.active && !media.completed);
	/* A completed decoder accepts Play through the same start path as Restart. */
	source = fresh_source();
	source.state = OBS_MEDIA_STATE_ENDED;
	open_failure = 0;
	started = 0;
	ffmpeg_source_play_pause(&source, false);
	REQUIRE(source.state == OBS_MEDIA_STATE_PLAYING && started == 1);

	/* Retry cannot depend on the next video tick consuming destroy_media. */
	for (int play = 0; play <= 1; play++) {
		for (int failure = 0; failure <= 2; failure++) {
			source = fresh_source();
			media_stopped(&source, true);
			open_failure = failure;
			destroyed = opened = started = ended = 0;
			if (play)
				ffmpeg_source_play_pause(&source, false);
			else
				ffmpeg_source_restart(&source);
			REQUIRE(destroyed == 1 && opened == 1 && ended == 0);
			REQUIRE(source.state == (failure ? OBS_MEDIA_STATE_ERROR : OBS_MEDIA_STATE_PLAYING));
			REQUIRE(source.destroy_media == (failure != 0));
			REQUIRE(started == (failure ? 0 : 1));
		}
	}
	puts("Terminal EOF, failure, cancellation and explicit retry qualified");
	return 0;
}
