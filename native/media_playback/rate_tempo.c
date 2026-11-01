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

/* Pitch-preserving audio filtering for the private media source. */
#include "rate_tempo.h"

#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/channel_layout.h>

#define TEMPO_LOOKAHEAD_MARGIN 2000000LL

static void mp_media_tempo_free_graph(mp_media_t *m)
{
	avfilter_graph_free(&m->tempo_graph);
	m->tempo_src = NULL;
	m->tempo_sink = NULL;
	av_frame_free(&m->tempo_frame);
	av_channel_layout_uninit(&m->tempo_ch_layout);
	m->tempo_format = AV_SAMPLE_FMT_NONE;
	m->tempo_sample_rate = 0;
	m->tempo_in_samples = 0;
	m->tempo_out_samples = 0;
	m->tempo_anchor_pts = 0;
	m->tempo_out_end_pts = 0;
}

static void mp_media_tempo_clear_queue(mp_media_t *m)
{
	struct mp_tempo_frame chunk = {0};

	while (m->tempo_queue.size) {
		deque_pop_front(&m->tempo_queue, &chunk, sizeof(chunk));
		av_frame_free(&chunk.frame);
	}
}

/* Drops the audio in progress, for when the timeline is interrupted. */
void mp_media_tempo_reset(mp_media_t *m)
{
	mp_media_tempo_free_graph(m);
	mp_media_tempo_clear_queue(m);
	m->tempo_sent = false;
	m->tempo_frame_offset = 0;
	m->tempo_lookahead_ns = 0;
	m->tempo_max_frame_samples = 0;
}

void mp_media_tempo_free(mp_media_t *m)
{
	mp_media_tempo_reset(m);
	deque_free(&m->tempo_queue);
}

/* Keep every stage in [0.5, 2]; atempo can skip samples above 2. */
static int mp_media_tempo_stages(int speed, double *last_factor)
{
	double factor = speed / 100.0;
	int stages = 1;
	while (factor < 0.5) {
		factor *= 2.0;
		stages++;
	}
	*last_factor = factor;
	return stages;
}

static void mp_media_tempo_build_chain(char *chain, size_t size, int speed)
{
	int numerator = speed;
	const int denominator = 100;
	size_t len = 0;
	chain[0] = '\0';
	while (numerator * 2 < denominator && len < size) {
		len += snprintf(chain + len, size - len, "atempo=1/2,");
		numerator *= 2;
	}
	if (len < size)
		snprintf(chain + len, size - len, "atempo=%d/%d", numerator, denominator);
}

/* atempo works on overlapping windows of rate / 24 samples, rounded up to a
 * power of two, and moves its output half a window at a time */
static inline int mp_media_tempo_window(int rate)
{
	int window = rate / 24;
	int power_of_two = 1 << av_log2(window);

	return power_of_two < window ? power_of_two * 2 : power_of_two;
}

/* Audio is fed to the filter at most this many samples at a time, as the
 * filter holds back more when given larger frames. */
int mp_media_tempo_slice(int rate)
{
	return FFMAX(mp_media_tempo_window(rate) / 4, 1);
}

/* Upper bound, in ns of media time, on how much audio the atempo chain holds
 * back (fed to it but not output yet). atempo moves its output W / 2 samples
 * at a time (W being its window size), and only passes output on once it has
 * filled a frame of round(input frame / tempo) samples. Each stage therefore
 * holds back less than W / tempo + W / 2 + output frame - 1 samples, and every
 * later stage stretches what the earlier ones hold back. */
static int64_t mp_media_tempo_max_delay_ns(int speed, int rate, int max_frame_samples)
{
	int window_samples = mp_media_tempo_window(rate);
	double last_factor;
	int stages = mp_media_tempo_stages(speed, &last_factor);
	double frame_samples = max_frame_samples;
	double delay_samples = 0.0;

	for (int i = 0; i < stages; i++) {
		double tempo = i + 1 < stages ? 0.5 : last_factor;

		frame_samples = (double)(int64_t)(0.5 + frame_samples / tempo);
		delay_samples =
			delay_samples / tempo + window_samples / tempo + window_samples / 2 + frame_samples - 1.0;
	}

	return (int64_t)(delay_samples * 1000000000.0 / rate) + 1;
}

static void mp_media_tempo_raise_lookahead(mp_media_t *m, int64_t lookahead_ns)
{
	/* the bound stays well below this at any speed; it only keeps a filter
	 * that stops producing output from having the whole file decoded */
	int64_t max_lookahead_ns = 2000000000LL;

	if (lookahead_ns > max_lookahead_ns)
		lookahead_ns = max_lookahead_ns;
	if (lookahead_ns > m->tempo_lookahead_ns)
		m->tempo_lookahead_ns = lookahead_ns;
}

/* How far ahead of its pts decoded audio is fed to the tempo filter, so that
 * the filtered audio is ready by the time it's due. 0 if audio isn't fed
 * ahead. Updated for the frame that's waiting, as larger frames make the
 * filter hold more back. */
int64_t mp_media_tempo_update_lookahead(mp_media_t *m)
{
	const AVFrame *f = m->a.frame;

	if (!m->has_audio || !m->a.frame_ready || m->full_decode || !mp_media_use_tempo(m))
		return 0;

	if (f->sample_rate != m->tempo_lookahead_rate) {
		m->tempo_lookahead_rate = f->sample_rate;
		m->tempo_max_frame_samples = 0;
	}

	int frame_samples = FFMIN(f->nb_samples, mp_media_tempo_slice(f->sample_rate));

	if (frame_samples > m->tempo_max_frame_samples) {
		int64_t delay_ns = mp_media_tempo_max_delay_ns(m->speed, f->sample_rate, frame_samples);

		m->tempo_max_frame_samples = frame_samples;
		mp_media_tempo_raise_lookahead(m, av_rescale(delay_ns + TEMPO_LOOKAHEAD_MARGIN, m->speed, 100));
	}

	return m->tempo_lookahead_ns;
}

static bool mp_media_tempo_init(mp_media_t *m, const AVFrame *f)
{
	AVFilterInOut *outputs = avfilter_inout_alloc();
	AVFilterInOut *inputs = avfilter_inout_alloc();
	AVChannelLayout layout = {0};
	char layout_name[128];
	char chain[128];
	char args[256];
	int ret = AVERROR(ENOMEM);

	mp_media_tempo_free_graph(m);

	m->tempo_graph = avfilter_graph_alloc();
	m->tempo_frame = av_frame_alloc();
	if (!outputs || !inputs || !m->tempo_graph || !m->tempo_frame)
		goto fail;

	m->tempo_graph->nb_threads = 1;

	/* abuffer needs a concrete layout, so use the default one for the
	 * channel count when the decoder doesn't report an order */
	if (f->ch_layout.order == AV_CHANNEL_ORDER_UNSPEC)
		av_channel_layout_default(&layout, f->ch_layout.nb_channels);
	else if ((ret = av_channel_layout_copy(&layout, &f->ch_layout)) < 0)
		goto fail;
	av_channel_layout_describe(&layout, layout_name, sizeof(layout_name));

	snprintf(args, sizeof(args), "time_base=1/%d:sample_rate=%d:sample_fmt=%s:channel_layout=%s", f->sample_rate,
		 f->sample_rate, av_get_sample_fmt_name(f->format), layout_name);

	const AVFilter *source_filter = avfilter_get_by_name("abuffer");
	const AVFilter *sink_filter = avfilter_get_by_name("abuffersink");
	if (!source_filter || !sink_filter || !avfilter_get_by_name("atempo")) {
		ret = AVERROR_FILTER_NOT_FOUND;
		goto fail;
	}
	ret = avfilter_graph_create_filter(&m->tempo_src, source_filter, "in", args, NULL,
					   m->tempo_graph);
	if (ret < 0)
		goto fail;

	ret = avfilter_graph_create_filter(&m->tempo_sink, sink_filter, "out", NULL, NULL,
					   m->tempo_graph);
	if (ret < 0)
		goto fail;

	outputs->name = av_strdup("in");
	outputs->filter_ctx = m->tempo_src;
	outputs->pad_idx = 0;
	outputs->next = NULL;

	inputs->name = av_strdup("out");
	inputs->filter_ctx = m->tempo_sink;
	inputs->pad_idx = 0;
	inputs->next = NULL;

	mp_media_tempo_build_chain(chain, sizeof(chain), m->speed);

	ret = avfilter_graph_parse_ptr(m->tempo_graph, chain, &inputs, &outputs, NULL);
	if (ret < 0)
		goto fail;

	ret = avfilter_graph_config(m->tempo_graph, NULL);
	if (ret < 0)
		goto fail;

	ret = av_channel_layout_copy(&m->tempo_ch_layout, &f->ch_layout);
	if (ret < 0)
		goto fail;

	m->tempo_format = f->format;
	m->tempo_sample_rate = f->sample_rate;

	av_channel_layout_uninit(&layout);
	avfilter_inout_free(&inputs);
	avfilter_inout_free(&outputs);
	return true;

fail:
	blog(LOG_ERROR, "MP: Failed to create tempo filter: %s", av_err2str(ret));
	av_channel_layout_uninit(&layout);
	avfilter_inout_free(&inputs);
	avfilter_inout_free(&outputs);
	mp_media_tempo_free_graph(m);
	return false;
}

/* Queue filtered audio in original media time. */
static void mp_media_tempo_push(mp_media_t *m, AVFrame *f)
{
	struct mp_tempo_frame chunk = {0};

	chunk.frame = f;
	chunk.pts = m->tempo_anchor_pts + tempo_output_media_ns(m, m->tempo_out_samples, f->sample_rate);
	deque_push_back(&m->tempo_queue, &chunk, sizeof(chunk));

	m->tempo_out_samples += f->nb_samples;
	m->tempo_out_end_pts = m->tempo_anchor_pts + tempo_output_media_ns(m, m->tempo_out_samples, f->sample_rate);
}

/* Moves the filter's output to the queue. */
static int mp_media_tempo_receive(mp_media_t *m)
{
	for (;;) {
		AVFrame *out = av_frame_alloc();
		int ret;

		if (!out)
			return AVERROR(ENOMEM);

		ret = av_buffersink_get_frame(m->tempo_sink, out);
		if (ret < 0) {
			av_frame_free(&out);
			return ret == AVERROR(EAGAIN) || ret == AVERROR_EOF ? 0 : ret;
		}

		mp_media_tempo_push(m, out);
	}
}

/* Trim the drained output to the duration of the real input. The drain feeds
 * bounded silence into the filter as context, so samples beyond that duration
 * belong to the synthetic extension. A remaining deficit is an error. */
static void mp_media_tempo_fix_length(mp_media_t *m)
{
	int rate = av_buffersink_get_sample_rate(m->tempo_sink);
	int64_t wanted_samples =
		av_rescale(m->tempo_in_samples, 100LL * rate, (int64_t)m->speed * m->tempo_sample_rate);
	int64_t diff_samples = wanted_samples - m->tempo_out_samples;


	/* too long: shorten what's still queued */
	while (diff_samples < 0 && m->tempo_queue.size) {
		struct mp_tempo_frame chunk = {0};

		deque_peek_back(&m->tempo_queue, &chunk, sizeof(chunk));
		if (-diff_samples < chunk.frame->nb_samples) {
			chunk.frame->nb_samples += (int)diff_samples;
			diff_samples = 0;
		} else {
			deque_pop_back(&m->tempo_queue, NULL, sizeof(chunk));
			diff_samples += chunk.frame->nb_samples;
			av_frame_free(&chunk.frame);
		}
	}

	if (diff_samples < 0) {
		blog(LOG_ERROR, "MP: Tempo filter output exceeds the available audio queue");
		m->tempo_failed = true;
		return;
	}

	if (diff_samples > 0) {
		blog(LOG_ERROR, "MP: Tempo filter could not produce the requested audio duration");
		m->tempo_failed = true;
	}

}

/* Once the audio has run out, moves what the filter still holds to the queue
 * and releases the filter. */
void mp_media_tempo_drain(mp_media_t *m)
{
	if (!m->tempo_graph)
		return;

	/* Give the overlap windows silence beyond the real input so the filter
	 * can render short final fragments. The extension is clipped below and
	 * does not advance the media timeline. */
	int rate = m->tempo_sample_rate;
	int64_t wanted = av_rescale(m->tempo_in_samples, 100LL * rate, (int64_t)m->speed * rate);
	int64_t delay_ns = mp_media_tempo_max_delay_ns(m->speed, rate, mp_media_tempo_slice(rate));
	int64_t padding_limit = av_rescale(delay_ns, (int64_t)rate * m->speed, 100000000000LL) +
				mp_media_tempo_window(rate) * 2;
	AVFrame *padding = av_frame_alloc();
	int ret = padding ? 0 : AVERROR(ENOMEM);
	if (padding) {
		padding->format = m->tempo_format;
		padding->sample_rate = rate;
		padding->nb_samples = mp_media_tempo_slice(rate);
		ret = av_channel_layout_copy(&padding->ch_layout, &m->tempo_ch_layout);
		if (ret >= 0 && padding->ch_layout.order == AV_CHANNEL_ORDER_UNSPEC) {
			int channels = padding->ch_layout.nb_channels;
			av_channel_layout_uninit(&padding->ch_layout);
			av_channel_layout_default(&padding->ch_layout, channels);
		}
		if (ret >= 0)
			ret = av_frame_get_buffer(padding, 0);
		if (ret >= 0)
			ret = av_samples_set_silence(padding->extended_data, 0, padding->nb_samples,
						     padding->ch_layout.nb_channels, padding->format);
	}
	for (int64_t padded = 0; ret >= 0 && m->tempo_out_samples < wanted && padded < padding_limit;
	     padded += padding->nb_samples) {
		ret = av_buffersrc_add_frame_flags(m->tempo_src, padding, AV_BUFFERSRC_FLAG_KEEP_REF);
		if (ret >= 0)
			ret = mp_media_tempo_receive(m);
	}
	av_frame_free(&padding);
	if (ret >= 0)
		ret = av_buffersrc_add_frame_flags(m->tempo_src, NULL, 0);
	if (ret >= 0)
		ret = mp_media_tempo_receive(m);
	if (ret >= 0)
		mp_media_tempo_fix_length(m);
	else {
		blog(LOG_ERROR, "MP: Failed to drain tempo filter: %s", av_err2str(ret));
		m->tempo_failed = true;
	}

	mp_media_tempo_free_graph(m);
}

static int mp_media_tempo_feed(mp_media_t *m, const AVFrame *f, int sample_offset, int sample_count)
{
	AVFrame *in = m->tempo_frame;
	int ret;

	if (sample_offset == 0 && sample_count == f->nb_samples) {
		ret = av_frame_ref(in, f);
	} else {
		in->format = f->format;
		in->sample_rate = f->sample_rate;
		in->nb_samples = sample_count;
		ret = av_channel_layout_copy(&in->ch_layout, &f->ch_layout);
		if (ret >= 0)
			ret = av_frame_get_buffer(in, 0);
		if (ret >= 0)
			ret = av_samples_copy(in->extended_data, f->extended_data, 0, sample_offset, sample_count,
					      f->ch_layout.nb_channels, f->format);
	}

	if (ret >= 0) {
		in->pts = m->tempo_in_samples;
		if (in->ch_layout.order == AV_CHANNEL_ORDER_UNSPEC) {
			int channels = in->ch_layout.nb_channels;
			av_channel_layout_uninit(&in->ch_layout);
			av_channel_layout_default(&in->ch_layout, channels);
		}
		ret = av_buffersrc_add_frame_flags(m->tempo_src, in, 0);
	}

	av_frame_unref(in);
	return ret;
}

/* Feed decoded samples into the worker-owned filter. */
bool mp_media_tempo_process(mp_media_t *m, const AVFrame *f, int sample_offset, int sample_count, int64_t pts)
{
	bool needs_new_graph = !m->tempo_graph || f->format != m->tempo_format ||
			       f->sample_rate != m->tempo_sample_rate ||
			       av_channel_layout_compare(&f->ch_layout, &m->tempo_ch_layout) != 0;
	int ret = 0;

	if (needs_new_graph) {
		/* the filter is set up for the previous format, so finish
		 * what it holds, laid out against this frame like any other
		 * frame would, before starting over with the new one */
		if (m->tempo_graph)
			m->tempo_anchor_pts = mp_media_tempo_anchor(m, pts);
		mp_media_tempo_drain(m);
		if (m->tempo_failed)
			return false;

		if (!mp_media_tempo_init(m, f)) {
			m->tempo_failed = true;
			return false;
		}
	}

	m->tempo_anchor_pts = mp_media_tempo_anchor(m, pts);

	ret = mp_media_tempo_feed(m, f, sample_offset, sample_count);
	if (ret >= 0)
		ret = mp_media_tempo_receive(m);

	if (ret < 0) {
		blog(LOG_ERROR, "MP: Failed to filter audio: %s", av_err2str(ret));
		mp_media_tempo_free_graph(m);
		m->tempo_failed = true;
		return false;
	}

	m->tempo_in_samples += sample_count;

	int rate = av_buffersink_get_sample_rate(m->tempo_sink);
	int64_t fed_end_pts = m->tempo_anchor_pts + mp_media_tempo_fed_ns(m, m->tempo_in_samples);
	m->tempo_out_end_pts = m->tempo_anchor_pts + tempo_output_media_ns(m, m->tempo_out_samples, rate);

	/* the look-ahead comes from atempo's worst case, but make sure it
	 * always covers what the filter actually holds back */
	mp_media_tempo_raise_lookahead(m, fed_end_pts - m->tempo_out_end_pts + TEMPO_LOOKAHEAD_MARGIN);
	return true;
}

