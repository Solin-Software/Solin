/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/cpu.h>

typedef struct {
	bool is_local_file;
} mp_media_t;

struct mp_decode {
	mp_media_t *m;
	AVStream *stream;
	bool audio;
	AVCodecContext *decoder;
	const AVCodec *codec;
	bool hw;
};

#define LOG_WARNING 0
#define blog(...) ((void)0)

static void init_hw_decoder(struct mp_decode *d, AVCodecContext *c)
{
	(void)d;
	(void)c;
}

#include "codec-under-test.h"

#define REQUIRE(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "Codec contract failed at line %d: %s\n", __LINE__, #condition); \
		exit(1); \
	} \
} while (0)

static AVCodecContext *open_decoder(enum AVCodecID id, bool audio, bool local)
{
	AVFormatContext *fmt = avformat_alloc_context();
	AVStream *stream = avformat_new_stream(fmt, NULL);
	REQUIRE(fmt && stream);
	stream->codecpar->codec_type = audio ? AVMEDIA_TYPE_AUDIO : AVMEDIA_TYPE_VIDEO;
	stream->codecpar->codec_id = id;
	if (audio) {
		stream->codecpar->sample_rate = 48000;
		av_channel_layout_default(&stream->codecpar->ch_layout, 2);
	} else {
		stream->codecpar->width = 1280;
		stream->codecpar->height = 720;
	}
	mp_media_t media = {.is_local_file = local};
	struct mp_decode d = {.m = &media, .stream = stream, .audio = audio,
			      .codec = avcodec_find_decoder(id)};
	REQUIRE(d.codec);
	REQUIRE(mp_open_codec(&d, false) >= 0);
	avformat_free_context(fmt);
	return d.decoder;
}

int main(void)
{
	/* Live video decodes without frame threading: each frame thread holds a
	 * frame back, a permanent delay behind a camera. Slice threads remain. */
	AVCodecContext *live = open_decoder(AV_CODEC_ID_H264, false, false);
	REQUIRE(live->flags & AV_CODEC_FLAG_LOW_DELAY);
	REQUIRE(live->active_thread_type != FF_THREAD_FRAME);
	avcodec_free_context(&live);

	/* Finite media keeps upstream threading for high-resolution playback. */
	AVCodecContext *file = open_decoder(AV_CODEC_ID_H264, false, true);
	REQUIRE(!(file->flags & AV_CODEC_FLAG_LOW_DELAY));
	if (av_cpu_count() > 1)
		REQUIRE(file->active_thread_type == FF_THREAD_FRAME);
	avcodec_free_context(&file);

	/* Live audio is not affected. */
	AVCodecContext *audio = open_decoder(AV_CODEC_ID_AAC, true, false);
	REQUIRE(!(audio->flags & AV_CODEC_FLAG_LOW_DELAY));
	avcodec_free_context(&audio);

	puts("Codec contract passed");
	return 0;
}
