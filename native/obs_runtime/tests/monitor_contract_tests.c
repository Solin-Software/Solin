/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <windows.h>
#include <audioclient.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <util/util_uint64.h>

#define REQUIRE(condition)                                                                                        \
	do {                                                                                                      \
		if (!(condition)) {                                                                               \
			fprintf(stderr, "Monitor contract failed at line %d: %s\n", __LINE__, #condition);           \
			exit(1);                                                                                  \
		}                                                                                                 \
	} while (0)

struct audio_monitor {
	IAudioClient *client;
	IAudioRenderClient *render;
	uint32_t sample_rate;
	uint32_t channels;
	uint32_t buffer_frames;
	uint32_t preroll_frames;
	bool started;
	uint64_t last_submit_time;
	uint64_t last_submit_duration;
	SRWLOCK playback_mutex;
	void *resampler;
	bool source_has_video;
};

static uint64_t clock_ns;
static uint64_t mock_gettime_ns(void)
{
	return clock_ns;
}

/* Generated from the production source, without substituting helper bodies. */
#define os_gettime_ns mock_gettime_ns
#define warn(...) fprintf(stderr, __VA_ARGS__)
#include "monitor-contract.h"
#undef warn
#undef os_gettime_ns

struct fake_device {
	IAudioClient client;
	IAudioRenderClient render;
	uint32_t capacity;
	uint32_t channels;
	uint32_t padding;
	uint32_t read_pos;
	uint32_t write_pos;
	uint32_t acquired_frames;
	uint32_t last_get_frames;
	DWORD last_flags;
	float *queue;
	float *buffer;
	float *played;
	size_t played_frames;
	size_t capture_capacity;
	uint64_t submitted_frames;
	uint64_t underflow_frames;
	uint64_t last_clock_ns;
	uint64_t clock_remainder;
	bool running;
	bool acquired;
	HRESULT get_result;
	HRESULT release_result;
	HRESULT start_result;
	HRESULT stop_result;
	HRESULT reset_result;
	unsigned get_count;
	unsigned release_count;
	unsigned start_count;
	unsigned stop_count;
	unsigned reset_count;
	char calls[1024];
	size_t call_count;
};

static void record_call(struct fake_device *device, char call)
{
	REQUIRE(device->call_count + 1 < sizeof(device->calls));
	device->calls[device->call_count++] = call;
	device->calls[device->call_count] = '\0';
}

static struct fake_device *from_client(IAudioClient *client)
{
	return (struct fake_device *)((char *)client - offsetof(struct fake_device, client));
}

static struct fake_device *from_render(IAudioRenderClient *render)
{
	return (struct fake_device *)((char *)render - offsetof(struct fake_device, render));
}

static HRESULT STDMETHODCALLTYPE fake_get_buffer(IAudioRenderClient *render, UINT32 frames, BYTE **output)
{
	struct fake_device *device = from_render(render);
	record_call(device, 'G');
	device->get_count++;
	device->last_get_frames = frames;
	REQUIRE(frames != 0);
	REQUIRE(!device->acquired);
	if (FAILED(device->get_result))
		return device->get_result;
	REQUIRE(frames <= device->capacity - device->padding);
	device->acquired = true;
	device->acquired_frames = frames;
	memset(device->buffer, 0xcd, (size_t)frames * device->channels * sizeof(float));
	*output = (BYTE *)device->buffer;
	return S_OK;
}

static HRESULT STDMETHODCALLTYPE fake_release_buffer(IAudioRenderClient *render, UINT32 frames, DWORD flags)
{
	struct fake_device *device = from_render(render);
	record_call(device, 'L');
	device->release_count++;
	device->last_flags = flags;
	REQUIRE(device->acquired);
	REQUIRE(frames == device->acquired_frames);
	REQUIRE(flags == 0 || flags == AUDCLNT_BUFFERFLAGS_SILENT);
	device->acquired = false;
	if (FAILED(device->release_result))
		return device->release_result;
	for (uint32_t frame = 0; frame < frames; frame++) {
		float *dest = device->queue + (size_t)device->write_pos * device->channels;
		if (flags == AUDCLNT_BUFFERFLAGS_SILENT)
			memset(dest, 0, device->channels * sizeof(float));
		else
			memcpy(dest, device->buffer + (size_t)frame * device->channels,
			       device->channels * sizeof(float));
		device->write_pos = (device->write_pos + 1) % device->capacity;
	}
	device->padding += frames;
	device->submitted_frames += frames;
	return S_OK;
}

static HRESULT STDMETHODCALLTYPE fake_start(IAudioClient *client)
{
	struct fake_device *device = from_client(client);
	record_call(device, 'S');
	device->start_count++;
	REQUIRE(!device->acquired);
	REQUIRE(!device->running);
	REQUIRE(device->padding != 0);
	if (FAILED(device->start_result))
		return device->start_result;
	device->running = true;
	device->last_clock_ns = clock_ns;
	device->clock_remainder = 0;
	return S_OK;
}

static HRESULT STDMETHODCALLTYPE fake_stop(IAudioClient *client)
{
	struct fake_device *device = from_client(client);
	record_call(device, 'T');
	device->stop_count++;
	if (FAILED(device->stop_result))
		return device->stop_result;
	device->running = false;
	return S_OK;
}

static HRESULT STDMETHODCALLTYPE fake_reset(IAudioClient *client)
{
	struct fake_device *device = from_client(client);
	record_call(device, 'R');
	device->reset_count++;
	REQUIRE(!device->running);
	REQUIRE(!device->acquired);
	if (FAILED(device->reset_result))
		return device->reset_result;
	device->padding = 0;
	device->read_pos = 0;
	device->write_pos = 0;
	device->clock_remainder = 0;
	return S_OK;
}

static HRESULT STDMETHODCALLTYPE fake_get_padding(IAudioClient *client, UINT32 *padding)
{
	*padding = from_client(client)->padding;
	return S_OK;
}

static IAudioClientVtbl client_vtable = {
	.GetCurrentPadding = fake_get_padding,
	.Start = fake_start,
	.Stop = fake_stop,
	.Reset = fake_reset,
};

static IAudioRenderClientVtbl render_vtable = {
	.GetBuffer = fake_get_buffer,
	.ReleaseBuffer = fake_release_buffer,
};

static void init_device(struct fake_device *device, struct audio_monitor *monitor, uint32_t capacity, uint32_t reserve)
{
	memset(device, 0, sizeof(*device));
	device->client.lpVtbl = &client_vtable;
	device->render.lpVtbl = &render_vtable;
	device->capacity = capacity;
	device->channels = 2;
	device->queue = calloc((size_t)capacity * device->channels, sizeof(float));
	device->buffer = malloc((size_t)capacity * device->channels * sizeof(float));
	REQUIRE(device->queue && device->buffer);
	*monitor = (struct audio_monitor){
		.client = &device->client,
		.render = &device->render,
		.sample_rate = 48000,
		.channels = device->channels,
		.buffer_frames = capacity,
		.preroll_frames = reserve,
	};
	clock_ns = UINT64_C(1000000000);
}

static void free_device(struct fake_device *device)
{
	free(device->played);
	free(device->buffer);
	free(device->queue);
}

/* Nonzero, exactly representable samples reveal loss, duplication and reordering. */
static float sample_at(uint32_t frame, uint32_t channel)
{
	float value = 0.125f + (float)(frame + 1) / 1048576.0f;
	return channel ? -value : value;
}

static void fill_pcm(float *pcm, uint32_t first, uint32_t frames)
{
	for (uint32_t frame = 0; frame < frames; frame++) {
		pcm[frame * 2] = sample_at(first + frame, 0);
		pcm[frame * 2 + 1] = sample_at(first + frame, 1);
	}
}

static void require_zero(const float *data, size_t samples)
{
	for (size_t i = 0; i < samples; i++)
		REQUIRE(data[i] == 0.0f);
}

static void consume_frames(struct fake_device *device, uint64_t frames)
{
	REQUIRE(device->running);
	for (uint64_t frame = 0; frame < frames; frame++) {
		float *output = NULL;
		if (device->played) {
			REQUIRE(device->played_frames < device->capture_capacity);
			output = device->played + device->played_frames * device->channels;
		}
		if (device->padding) {
			if (output)
				memcpy(output, device->queue + (size_t)device->read_pos * device->channels,
				       device->channels * sizeof(float));
			device->read_pos = (device->read_pos + 1) % device->capacity;
			device->padding--;
		} else {
			if (output)
				memset(output, 0, device->channels * sizeof(float));
			device->underflow_frames++;
		}
		device->played_frames++;
	}
}

static void advance_to(struct fake_device *device, uint64_t now, uint32_t rate)
{
	REQUIRE(now >= device->last_clock_ns);
	if (device->running) {
		uint64_t ticks = (now - device->last_clock_ns) * rate + device->clock_remainder;
		consume_frames(device, ticks / UINT64_C(1000000000));
		device->clock_remainder = ticks % UINT64_C(1000000000);
	}
	device->last_clock_ns = now;
	clock_ns = now;
}

/* Exercise the real capture callback: OBS 33 output muting must never silence
 * monitoring, including the monitor-only route. Resampling and A/V buffering
 * are controlled boundaries; the production submission path is unchanged. */
#define MAX_AV_PLANES 8
#define EPSILON 0.00001f
#define UNUSED_PARAMETER(value) ((void)(value))
typedef struct {
	float user_volume;
	volatile long activate_refs;
	bool async_unbuffered, async_decoupled;
} obs_source_t;
struct audio_data {
	uint8_t *data[MAX_AV_PLANES];
	uint32_t frames;
	uint64_t timestamp;
};

static long os_atomic_load_long(volatile long *value) { return *value; }
static bool close_float(float left, float right, float epsilon)
{
	return left - right < epsilon && right - left < epsilon;
}
static bool audio_monitor_init_wasapi(struct audio_monitor *monitor)
{
	(void)monitor;
	REQUIRE(false);
	return false;
}
static void audio_monitor_free_for_reconnect(struct audio_monitor *monitor)
{
	(void)monitor;
	REQUIRE(false);
}
static bool audio_resampler_resample(void *resampler, uint8_t **output, uint32_t *frames,
				    uint64_t *offset, const uint8_t *const *input, uint32_t input_frames)
{
	(void)resampler;
	output[0] = (uint8_t *)input[0];
	*frames = input_frames;
	*offset = 0;
	return true;
}
static bool process_audio_delay(struct audio_monitor *monitor, float **data, uint32_t *frames,
			       uint64_t timestamp, uint32_t padding)
{
	(void)monitor; (void)data; (void)frames; (void)timestamp; (void)padding;
	REQUIRE(false);
	return false;
}
#define os_gettime_ns mock_gettime_ns
#include "monitor-callback-contract.h"
#undef os_gettime_ns

static void test_monitoring_ignores_output_mute(void)
{
	const float volumes[] = {1.0f, 0.5f};
	for (unsigned muted = 0; muted < 2; muted++) {
		for (unsigned volume = 0; volume < 2; volume++) {
			struct fake_device device;
			struct audio_monitor monitor;
			float pcm[8];
			fill_pcm(pcm, 0, 4);
			init_device(&device, &monitor, 32, 8);
			InitializeSRWLock(&monitor.playback_mutex);
			obs_source_t source = {.user_volume = volumes[volume], .activate_refs = 1};
			struct audio_data audio = {.data = {(uint8_t *)pcm}, .frames = 4};
			on_audio_playback(&monitor, &source, &audio, muted != 0);
			REQUIRE(monitor.started && device.padding == 12 && device.last_flags == 0);
			require_zero(device.queue, 8 * monitor.channels);
			for (unsigned frame = 0; frame < 4; frame++)
				for (unsigned channel = 0; channel < 2; channel++)
					REQUIRE(device.queue[(8 + frame) * 2 + channel] ==
						sample_at(frame, channel) * volumes[volume]);
			free_device(&device);
		}
	}
}

static void test_preroll_and_pcm(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[18];
	fill_pcm(pcm, 0, 9);
	init_device(&device, &monitor, 64, 8);
	REQUIRE(write_audio(&monitor, pcm, 5, 0) == S_OK);
	REQUIRE(strcmp(device.calls, "GLS") == 0);
	REQUIRE(device.padding == 13 && device.last_get_frames == 13);
	require_zero(device.queue, 8 * monitor.channels);
	REQUIRE(memcmp(device.queue + 8 * monitor.channels, pcm, 5 * monitor.channels * sizeof(float)) == 0);
	REQUIRE(monitor.started && monitor.last_submit_time == clock_ns);
	REQUIRE(monitor.last_submit_duration == util_mul_div64(13, 1000000000, monitor.sample_rate));
	REQUIRE(write_audio(&monitor, pcm + 10, 4, device.padding) == S_OK);
	REQUIRE(device.start_count == 1 && device.last_get_frames == 4 && device.padding == 17);
	REQUIRE(memcmp(device.queue + 13 * monitor.channels, pcm + 10, 4 * monitor.channels * sizeof(float)) == 0);
	free_device(&device);
}

static void test_empty(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[10];
	fill_pcm(pcm, 0, 5);
	init_device(&device, &monitor, 32, 8);
	REQUIRE(write_audio(&monitor, NULL, 0, 0) == S_OK);
	REQUIRE(!monitor.started && device.call_count == 0 && monitor.last_submit_time == 0);
	REQUIRE(write_audio(&monitor, pcm, 5, 0) == S_OK);
	uint64_t submitted_at = monitor.last_submit_time;
	uint64_t duration = monitor.last_submit_duration;
	clock_ns++;
	REQUIRE(write_audio(&monitor, NULL, 0, device.padding) == S_OK);
	REQUIRE(strcmp(device.calls, "GLS") == 0 && monitor.started);
	REQUIRE(monitor.last_submit_time == submitted_at && monitor.last_submit_duration == duration);
	free_device(&device);
}

static void test_capacity(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[34];
	fill_pcm(pcm, 0, 17);
	for (uint32_t frames = 12; frames <= 16; frames += 4) {
		init_device(&device, &monitor, 16, 8);
		REQUIRE(write_audio(&monitor, pcm, frames, 0) == S_OK);
		REQUIRE(device.last_get_frames == 16 && device.padding == 16);
		uint32_t reserve = 16 - frames;
		require_zero(device.queue, reserve * monitor.channels);
		REQUIRE(memcmp(device.queue + reserve * monitor.channels, pcm,
			       frames * monitor.channels * sizeof(float)) == 0);
		REQUIRE(write_audio(&monitor, pcm, 1, device.padding) == AUDCLNT_E_BUFFER_TOO_LARGE);
		REQUIRE(device.get_count == 1 && device.start_count == 1);
		free_device(&device);
	}
	init_device(&device, &monitor, 16, 8);
	REQUIRE(write_audio(&monitor, pcm, 17, 0) == AUDCLNT_E_BUFFER_TOO_LARGE);
	REQUIRE(write_audio(&monitor, pcm, UINT32_MAX, 0) == AUDCLNT_E_BUFFER_TOO_LARGE);
	REQUIRE(write_audio(&monitor, pcm, 2, 15) == AUDCLNT_E_BUFFER_TOO_LARGE);
	REQUIRE(write_audio(&monitor, pcm, 1, UINT32_MAX) == AUDCLNT_E_BUFFER_TOO_LARGE);
	REQUIRE(device.call_count == 0 && !monitor.started);
	free_device(&device);
}

static void test_write_failures(void)
{
	const char *expected[] = {"G", "GL", "GLS"};
	float pcm[8];
	fill_pcm(pcm, 0, 4);
	for (unsigned failure = 0; failure < 3; failure++) {
		struct fake_device device;
		struct audio_monitor monitor;
		init_device(&device, &monitor, 32, 8);
		if (failure == 0)
			device.get_result = E_FAIL;
		else if (failure == 1)
			device.release_result = E_FAIL;
		else
			device.start_result = E_FAIL;
		REQUIRE(write_audio(&monitor, pcm, 4, 0) == E_FAIL);
		REQUIRE(strcmp(device.calls, expected[failure]) == 0);
		REQUIRE(!monitor.started && !device.running);
		REQUIRE(monitor.last_submit_time == 0 && monitor.last_submit_duration == 0);
		REQUIRE(device.submitted_frames == (failure == 2 ? 12 : 0));
		free_device(&device);
	}
}

static void test_reset_rearms(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[8];
	fill_pcm(pcm, 0, 4);
	init_device(&device, &monitor, 32, 8);
	REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
	REQUIRE(reset_playback(&monitor) == S_OK);
	REQUIRE(!monitor.started && !device.running && device.padding == 0);
	REQUIRE(monitor.last_submit_time == 0 && monitor.last_submit_duration == 0);
	clock_ns++;
	REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
	REQUIRE(strcmp(device.calls, "GLSTRGLS") == 0);
	REQUIRE(device.padding == 12 && device.start_count == 2);
	require_zero(device.queue, 8 * monitor.channels);
	REQUIRE(memcmp(device.queue + 8 * monitor.channels, pcm, sizeof(pcm)) == 0);
	free_device(&device);
}

static void test_reset_failures(void)
{
	float pcm[8];
	fill_pcm(pcm, 0, 4);
	for (unsigned failure = 0; failure < 2; failure++) {
		struct fake_device device;
		struct audio_monitor monitor;
		init_device(&device, &monitor, 32, 8);
		REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
		uint64_t submitted_at = monitor.last_submit_time;
		if (failure == 0)
			device.stop_result = E_FAIL;
		else
			device.reset_result = E_FAIL;
		REQUIRE(reset_playback(&monitor) == E_FAIL);
		REQUIRE(device.stop_count == 1 && device.reset_count == failure);
		REQUIRE(monitor.started && monitor.last_submit_time == submitted_at);
		REQUIRE(device.padding == 12 && device.start_count == 1);
		free_device(&device);
	}
}

static void test_idle_requires_empty_and_expired(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[8];
	fill_pcm(pcm, 0, 4);
	init_device(&device, &monitor, 32, 8);
	REQUIRE(reset_if_idle(&monitor, 0, clock_ns) == S_OK);
	REQUIRE(device.stop_count == 0);
	REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
	uint64_t expiry = monitor.last_submit_time + monitor.last_submit_duration +
			  util_mul_div64(monitor.preroll_frames, 1000000000, monitor.sample_rate);
	REQUIRE(reset_if_idle(&monitor, device.padding, expiry + 1) == S_OK);
	REQUIRE(device.stop_count == 0 && device.padding == 12);
	consume_frames(&device, device.padding);
	REQUIRE(reset_if_idle(&monitor, 0, expiry - 1) == S_OK);
	REQUIRE(reset_if_idle(&monitor, 0, expiry) == S_OK);
	REQUIRE(device.stop_count == 0 && monitor.started);
	REQUIRE(reset_if_idle(&monitor, 0, expiry + 1) == S_OK);
	REQUIRE(device.stop_count == 1 && device.reset_count == 1 && !monitor.started);
	REQUIRE(reset_if_idle(&monitor, 0, expiry + 2) == S_OK);
	REQUIRE(device.stop_count == 1);
	clock_ns = expiry + 2;
	REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
	REQUIRE(device.padding == 12 && device.start_count == 2);
	free_device(&device);
}

static void test_idle_propagates_failure(void)
{
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[8];
	fill_pcm(pcm, 0, 4);
	init_device(&device, &monitor, 32, 8);
	REQUIRE(write_audio(&monitor, pcm, 4, 0) == S_OK);
	consume_frames(&device, device.padding);
	device.reset_result = E_FAIL;
	REQUIRE(reset_if_idle(&monitor, 0, clock_ns + 1000000000) == E_FAIL);
	REQUIRE(device.stop_count == 1 && device.reset_count == 1);
	free_device(&device);
}

static void test_preroll_budget(void)
{
	const uint32_t rates[] = {44100, 48000, 96000};
	const REFERENCE_TIME periods[] = {30000, 100000, 200000}; /* 100 ns units */
	const uint64_t limit_ns = 75000000;
	for (size_t r = 0; r < sizeof(rates) / sizeof(rates[0]); r++) {
		for (size_t p = 0; p < sizeof(periods) / sizeof(periods[0]); p++) {
			uint64_t budget_ns = limit_ns - (uint64_t)periods[p] * 100;
			uint32_t frames = monitor_preroll_frames(rates[r], periods[p]);
			uint64_t scaled_budget = budget_ns * rates[r];
			uint64_t scaled_frames = (uint64_t)frames * 1000000000;
			REQUIRE(scaled_frames <= scaled_budget);
			REQUIRE(scaled_budget - scaled_frames < 1000000000);
		}
		REQUIRE(monitor_preroll_frames(rates[r], -1) == 0);
		REQUIRE(monitor_preroll_frames(rates[r], 0) == 0);
		REQUIRE(monitor_preroll_frames(rates[r], 750000) == 0);
		REQUIRE(monitor_preroll_frames(rates[r], 750001) == 0);
	}
}

static void test_small_packet_jitter(uint32_t reserve, bool expect_underflow)
{
	enum { rate = 48000, packet_frames = 256, packets = 200 };
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[packet_frames * 2];
	init_device(&device, &monitor, rate, reserve);
	device.capture_capacity = (size_t)packet_frames * packets + rate;
	device.played = malloc(device.capture_capacity * monitor.channels * sizeof(float));
	REQUIRE(device.played);
	uint64_t origin = clock_ns;
	uint64_t previous = origin;
	for (uint32_t packet = 0; packet < packets; packet++) {
		uint64_t nominal = ((uint64_t)packet * packet_frames * 1000000000 + rate - 1) / rate;
		uint64_t jitter = packet && packet % 40 == 0 ? UINT64_C(54000000) : 0;
		uint64_t arrival = origin + nominal + jitter;
		if (arrival < previous)
			arrival = previous;
		advance_to(&device, arrival, rate);
		previous = arrival;
		fill_pcm(pcm, packet * packet_frames, packet_frames);
		REQUIRE(write_audio(&monitor, pcm, packet_frames, device.padding) == S_OK);
	}
	REQUIRE(device.padding != 0);
	while (device.padding)
		consume_frames(&device, min(device.padding, rate / 100));
	REQUIRE(device.start_count == 1 && device.stop_count == 0 && device.reset_count == 0);
	REQUIRE(device.submitted_frames == (uint64_t)packets * packet_frames + reserve);
	REQUIRE(device.played_frames == device.submitted_frames + device.underflow_frames);
	REQUIRE(expect_underflow ? device.underflow_frames > 0 : device.underflow_frames == 0);
	require_zero(device.played, (size_t)reserve * monitor.channels);
	uint32_t real_frame = 0;
	uint64_t gap_frames = 0;
	for (size_t frame = reserve; frame < device.played_frames; frame++) {
		const float *sample = device.played + frame * monitor.channels;
		if (sample[0] == 0.0f && sample[1] == 0.0f) {
			gap_frames++;
			continue;
		}
		REQUIRE(sample[0] == sample_at(real_frame, 0));
		REQUIRE(sample[1] == sample_at(real_frame, 1));
		real_frame++;
	}
	REQUIRE(real_frame == packets * packet_frames && gap_frames == device.underflow_frames);
	printf("Small-packet jitter: reserve=%u, underrun=%llu frames, PCM=%u frames, EOF drained\n", reserve,
	       (unsigned long long)device.underflow_frames, real_frame);
	free_device(&device);
}

static void test_dac_jitter(uint32_t reserve)
{
	enum { rate = 48000, packet_frames = 4096, packets = 120 };
	const uint32_t jitter_ms[] = {5, 12, 7, 10, 6, 11};
	struct fake_device device;
	struct audio_monitor monitor;
	float pcm[packet_frames * 2];
	init_device(&device, &monitor, rate, reserve);
	device.capture_capacity = (size_t)packet_frames * packets + rate;
	device.played = malloc(device.capture_capacity * monitor.channels * sizeof(float));
	REQUIRE(device.played);
	uint64_t origin = clock_ns;
	for (uint32_t packet = 0; packet < packets; packet++) {
		/* Absolute deadlines prevent accumulated rounding or relative-sleep drift. */
		uint64_t nominal = ((uint64_t)packet * packet_frames * 1000000000 + rate - 1) / rate;
		uint64_t jitter = packet ? (uint64_t)jitter_ms[(packet - 1) % 6] * 1000000 : 0;
		advance_to(&device, origin + nominal + jitter, rate);
		/* Isolate startup reserve: idle recovery has its own boundary tests. */
		fill_pcm(pcm, packet * packet_frames, packet_frames);
		REQUIRE(write_audio(&monitor, pcm, packet_frames, device.padding) == S_OK);
	}
	REQUIRE(device.padding != 0);
	/* EOF sends no new callback: the DAC must still consume every queued frame. */
	while (device.padding)
		consume_frames(&device, min(device.padding, rate / 100));
	REQUIRE(device.start_count == 1 && device.stop_count == 0 && device.reset_count == 0);
	REQUIRE(device.submitted_frames == (uint64_t)packets * packet_frames + reserve);
	REQUIRE(device.played_frames == device.submitted_frames + device.underflow_frames);
	if (reserve)
		REQUIRE(device.underflow_frames == 0);
	else
		REQUIRE(device.underflow_frames >= rate * 5 / 1000);
	require_zero(device.played, (size_t)reserve * monitor.channels);
	uint32_t real_frame = 0;
	uint64_t gap_frames = 0;
	for (size_t frame = reserve; frame < device.played_frames; frame++) {
		const float *sample = device.played + frame * monitor.channels;
		if (sample[0] == 0.0f && sample[1] == 0.0f) {
			gap_frames++;
			continue;
		}
		REQUIRE(sample[0] == sample_at(real_frame, 0));
		REQUIRE(sample[1] == sample_at(real_frame, 1));
		real_frame++;
	}
	REQUIRE(real_frame == packets * packet_frames && gap_frames == device.underflow_frames);
	printf("DAC jitter: reserve=%u, underrun=%llu frames, PCM=%u frames, EOF drained\n", reserve,
	       (unsigned long long)device.underflow_frames, real_frame);
	free_device(&device);
}

int main(void)
{
	test_preroll_and_pcm();
	test_empty();
	test_monitoring_ignores_output_mute();
	test_capacity();
	test_write_failures();
	test_reset_rearms();
	test_reset_failures();
	test_idle_requires_empty_and_expired();
	test_idle_propagates_failure();
	test_preroll_budget();
	test_small_packet_jitter(48000 * 20 / 1000, true);
	test_small_packet_jitter(monitor_preroll_frames(48000, 100000), false);
	test_dac_jitter(0);
	test_dac_jitter(2 * 48000 / 100);
	puts("Monitor contracts passed");
	return 0;
}
