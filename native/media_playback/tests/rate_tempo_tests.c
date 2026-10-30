/* Exercise the real FFmpeg tempo graph with short planar and packed input. */
#include "rate_tempo.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static int run_segment(mp_media_t *m, int speed, int samples, enum AVSampleFormat format, int64_t pts, bool unspecified)
{
	AVFrame *input = av_frame_alloc();
	if (!input)
		return 1;
	input->format = format;
	input->sample_rate = 48000;
	input->nb_samples = samples;
	av_channel_layout_default(&input->ch_layout, 2);
	if (av_frame_get_buffer(input, 0) < 0) {
		av_frame_free(&input);
		return 1;
	}
	for (int i = 0; i < samples; ++i) {
		float value = (float)(0.5 * sin(2.0 * 3.141592653589793 * 1000.0 * i / 48000.0));
		if (format == AV_SAMPLE_FMT_FLTP) {
			((float *)input->extended_data[0])[i] = value;
			((float *)input->extended_data[1])[i] = value;
		} else {
			((float *)input->extended_data[0])[2 * i] = value;
			((float *)input->extended_data[0])[2 * i + 1] = value;
		}
	}
	if (unspecified) {
		av_channel_layout_uninit(&input->ch_layout);
		input->ch_layout.order = AV_CHANNEL_ORDER_UNSPEC;
		input->ch_layout.nb_channels = 2;
	}
	m->speed = speed;
	bool ok = true;
	for (int offset = 0; ok && offset < samples;) {
		int count = mp_media_tempo_slice(input->sample_rate);
		if (count > samples - offset)
			count = samples - offset;
		ok = mp_media_tempo_process(m, input, offset, count,
					    pts + tempo_samples_to_ns(offset, input->sample_rate));
		offset += count;
	}
	av_frame_free(&input);
	if (ok)
		mp_media_tempo_drain(m);
	if (!ok || m->tempo_failed) {
		fprintf(stderr, "filter failed for %d samples at %d%%\n", samples, speed);
		return 1;
	}

	int64_t count = 0;
	int64_t nonzero = 0;
	while (m->tempo_queue.size) {
		struct mp_tempo_frame chunk;
		deque_pop_front(&m->tempo_queue, &chunk, sizeof(chunk));
		AVFrame *frame = chunk.frame;
		if ((frame->format != AV_SAMPLE_FMT_FLT && frame->format != AV_SAMPLE_FMT_FLTP) ||
		    frame->ch_layout.nb_channels != 2) {
			av_frame_free(&frame);
			fprintf(stderr, "unexpected filtered format\n");
			return 1;
		}
		for (int i = 0; i < frame->nb_samples; ++i) {
			float value = ((float *)frame->extended_data[0])[frame->format == AV_SAMPLE_FMT_FLTP ? i : 2 * i];
			if (!isfinite(value)) {
				av_frame_free(&frame);
				return 1;
			}
			if (fabsf(value) > 0.001f)
				++nonzero;
		}
		count += frame->nb_samples;
		av_frame_free(&frame);
	}
	int64_t expected = av_rescale(samples, 100, speed);
	if (count != expected || nonzero < count / 10) {
		fprintf(stderr, "%d samples at %d%%: got %lld, expected %lld, audible %lld\n", samples,
			speed, (long long)count, (long long)expected, (long long)nonzero);
		return 1;
	}
	return 0;
}

int main(void)
{
	const int speeds[] = {1, 25, 50, 150, 200};
	const int lengths[] = {960, 4800};
	const enum AVSampleFormat formats[] = {AV_SAMPLE_FMT_FLTP, AV_SAMPLE_FMT_FLT};
	for (size_t f = 0; f < sizeof(formats) / sizeof(*formats); ++f) {
		for (size_t i = 0; i < sizeof(speeds) / sizeof(*speeds); ++i) {
			for (size_t n = 0; n < sizeof(lengths) / sizeof(*lengths); ++n) {
				mp_media_t m = {0};
				m.has_audio = true;
				int result = run_segment(&m, speeds[i], lengths[n], formats[f], 0, false);
				mp_media_tempo_free(&m);
				if (result)
					return result;
			}
		}
	}

	/* Decoder layouts may specify the channel count without an ordering. */
	mp_media_t unspec = {0};
	unspec.has_audio = true;
	int unspec_result = run_segment(&unspec, 25, 960, AV_SAMPLE_FMT_FLTP, 0, true);
	mp_media_tempo_free(&unspec);
	if (unspec_result)
		return unspec_result;

	/* Drain several small segments without recreating the owning media object. */
	mp_media_t m = {0};
	m.has_audio = true;
	int64_t pts = 0;
	const int transitions[] = {150, 200, 150, 25, 200, 25};
	for (size_t i = 0; i < sizeof(transitions) / sizeof(*transitions); ++i) {
		if (run_segment(&m, transitions[i], 960, AV_SAMPLE_FMT_FLTP, pts, false)) {
			mp_media_tempo_free(&m);
			return 1;
		}
		pts += 20000000;
	}
	mp_media_tempo_free(&m);
	return 0;
}
