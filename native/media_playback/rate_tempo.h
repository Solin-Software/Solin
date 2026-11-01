/*
 * Copyright (c) 2023 Lain Bailey <lain@obsproject.com>
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#pragma once

#include "media-playback/media-playback.h"
#include "media-playback/media.h"

struct mp_tempo_frame {
	AVFrame *frame;
	int64_t pts;
};

static inline bool mp_media_use_tempo(mp_media_t *m)
{
	return m->speed != 100 && !m->tempo_failed;
}

static inline int64_t tempo_samples_to_ns(int64_t samples, int rate)
{
	return av_rescale(samples, 1000000000, rate);
}

static inline int64_t mp_media_tempo_fed_ns(mp_media_t *m, int64_t samples)
{
	return av_rescale(samples, 1000000000LL, m->tempo_sample_rate);
}

static inline int64_t tempo_output_media_ns(mp_media_t *m, int64_t samples, int rate)
{
	return av_rescale(samples, (int64_t)10000000 * m->speed, rate);
}

static inline int64_t mp_media_tempo_anchor(mp_media_t *m, int64_t pts)
{
	return pts - mp_media_tempo_fed_ns(m, m->tempo_in_samples);
}

int mp_media_tempo_slice(int rate);
void mp_media_tempo_reset(mp_media_t *m);
void mp_media_tempo_free(mp_media_t *m);
int64_t mp_media_tempo_update_lookahead(mp_media_t *m);
void mp_media_tempo_drain(mp_media_t *m);
bool mp_media_tempo_process(mp_media_t *m, const AVFrame *f, int sample_offset, int sample_count, int64_t pts);
