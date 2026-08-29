#include "solin/media_engine/program_recording.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <thread>
#include <vector>

#include <gst/gst.h>

namespace {

using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

class StaticProgramRenderer final : public solin::media_engine::SceneRenderer {
  public:
    StaticProgramRenderer() : pixels_(640U * 480U * 4U, 0U) {
        for (std::size_t offset = 0U; offset < pixels_.size(); offset += 4U) {
            pixels_[offset] = 32U;
            pixels_[offset + 1U] = 96U;
            pixels_[offset + 2U] = 192U;
            pixels_[offset + 3U] = 255U;
        }
    }

    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    prepare(const solin::media_engine::SceneRenderPreparation&) override {
        return {};
    }

    [[nodiscard]] solin::media_engine::SceneRenderTransitionPreparation
    prepare_transition(
        solin::media_engine::OutputBus,
        const std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>&,
        const solin::media_engine::SceneTransitionSpec& transition) override {
        return {.effective_transition = transition};
    }

    void commit_hydration(
        const std::array<std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>, 2U>&,
        const std::array<bool, 2U>&, std::uint64_t) noexcept override {}
    void commit_take(solin::media_engine::OutputBus,
                     std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>,
                     std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>,
                     std::uint64_t) noexcept override {}
    void set_output_enabled(solin::media_engine::OutputBus, bool,
                            std::uint64_t) noexcept override {}

    void set_system_memory_output_enabled(
        const solin::media_engine::OutputBus bus,
        const solin::media_engine::SystemMemoryOutputConsumer consumer,
        const bool enabled) noexcept override {
        if (bus == solin::media_engine::OutputBus::virtual_camera &&
            consumer == solin::media_engine::SystemMemoryOutputConsumer::program_recording) {
            requested_.store(enabled);
        }
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }
    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    [[nodiscard]] std::optional<solin::media_engine::SceneOutputFrameCursor>
    visit_latest_frame(
        const solin::media_engine::OutputBus bus,
        const solin::media_engine::SceneOutputFrameCursor after,
        const solin::media_engine::VideoFrameVisitor& visitor) const noexcept override {
        if (bus != solin::media_engine::OutputBus::virtual_camera || !requested_.load()) {
            return std::nullopt;
        }
        const auto failure_frame = fail_after_first_.load() && after.frame_sequence >= 1U;
        const auto frame_sequence = failure_frame ? 2U : frame_sequence_.load();
        if (after.frame_sequence >= frame_sequence) {
            return std::nullopt;
        }
        const auto frame_width = failure_frame ? 0U : frame_width_.load();
        const auto frame_height = frame_height_.load();
        const solin::media_engine::VideoFrameView frame{
            .sequence = frame_sequence,
            .width = frame_width,
            .height = frame_height,
            .pixel_format = solin::media_engine::VideoFramePixelFormat::bgra,
            .planes = {std::span<const std::uint8_t>{
                           pixels_.data(), frame_width * frame_height * 4U},
                       {}},
            .plane_strides = {static_cast<std::int32_t>(frame_width * 4U), 0},
        };
        visitor(frame);
        return solin::media_engine::SceneOutputFrameCursor{
            .route_generation = 1U,
            .frame_sequence = frame_sequence,
        };
    }

    void set_output_size(const std::uint32_t width, const std::uint32_t height) noexcept {
        frame_width_.store(width);
        frame_height_.store(height);
        frame_sequence_.fetch_add(1U);
    }

    void set_fail_after_first(const bool enabled) noexcept {
        fail_after_first_.store(enabled);
    }

    void shutdown() noexcept override {}

  private:
    std::vector<std::uint8_t> pixels_{};
    mutable std::atomic_bool requested_{false};
    mutable std::atomic_bool fail_after_first_{false};
    mutable std::atomic_uint32_t frame_width_{320U};
    mutable std::atomic_uint32_t frame_height_{240U};
    mutable std::atomic_uint64_t frame_sequence_{1U};
};

class TemporarilyRemovedFactory final {
  public:
    explicit TemporarilyRemovedFactory(const char* const name)
        : registry_(gst_registry_get()),
          feature_(GST_PLUGIN_FEATURE(gst_element_factory_find(name))) {
        if (feature_ != nullptr) {
            gst_registry_remove_feature(registry_, feature_);
        }
    }

    ~TemporarilyRemovedFactory() {
        if (feature_ != nullptr) {
            static_cast<void>(gst_registry_add_feature(registry_, feature_));
            gst_object_unref(feature_);
        }
    }

    TemporarilyRemovedFactory(const TemporarilyRemovedFactory&) = delete;
    TemporarilyRemovedFactory& operator=(const TemporarilyRemovedFactory&) = delete;

    [[nodiscard]] bool removed() const noexcept { return feature_ != nullptr; }

  private:
    GstRegistry* registry_{nullptr};
    GstPluginFeature* feature_{nullptr};
};

[[nodiscard]] std::filesystem::path unique_test_directory() {
    auto result = std::filesystem::temp_directory_path() /
                  ("solin-recording-test-" + std::to_string(
                                                std::chrono::steady_clock::now()
                                                    .time_since_epoch()
                                                    .count()));
    std::filesystem::create_directories(result);
    return result;
}

[[nodiscard]] double p95_milliseconds(
    std::vector<std::chrono::steady_clock::duration> samples) {
    if (samples.empty()) {
        return 0.0;
    }
    std::ranges::sort(samples);
    const auto rank = static_cast<std::size_t>(
        (std::ceil)(static_cast<double>(samples.size()) * 0.95));
    const auto duration = samples[(std::max)(rank, std::size_t{1U}) - 1U];
    return std::chrono::duration<double, std::milli>{duration}.count();
}

void capture_first_pts(GstElement*, GstBuffer* buffer, GstPad*, gpointer user_data) {
    auto* const first_pts = static_cast<std::atomic<GstClockTime>*>(user_data);
    const auto pts = GST_BUFFER_PTS(buffer);
    auto unset = GST_CLOCK_TIME_NONE;
    if (GST_CLOCK_TIME_IS_VALID(pts)) {
        static_cast<void>(first_pts->compare_exchange_strong(unset, pts));
    }
}

[[nodiscard]] std::optional<double>
initial_av_offset_milliseconds(const std::filesystem::path& path) {
    const auto generic_path = path.generic_string();
    auto* const escaped_path = g_strescape(generic_path.c_str(), nullptr);
    const std::string description =
        "filesrc location=\"" + std::string{escaped_path} +
        "\" ! qtdemux name=demux "
        "demux.video_0 ! queue ! fakesink name=video_sink signal-handoffs=true sync=false "
        "demux.audio_0 ! queue ! fakesink name=audio_sink signal-handoffs=true sync=false";
    g_free(escaped_path);
    GError* parse_error = nullptr;
    auto* pipeline = gst_parse_launch(description.c_str(), &parse_error);
    if (pipeline == nullptr || parse_error != nullptr) {
        if (parse_error != nullptr) {
            g_error_free(parse_error);
        }
        if (pipeline != nullptr) {
            gst_object_unref(pipeline);
        }
        return std::nullopt;
    }
    auto* video_sink = gst_bin_get_by_name(GST_BIN(pipeline), "video_sink");
    auto* audio_sink = gst_bin_get_by_name(GST_BIN(pipeline), "audio_sink");
    if (video_sink == nullptr || audio_sink == nullptr) {
        if (video_sink != nullptr) {
            gst_object_unref(video_sink);
        }
        if (audio_sink != nullptr) {
            gst_object_unref(audio_sink);
        }
        gst_object_unref(pipeline);
        return std::nullopt;
    }
    std::atomic<GstClockTime> video_pts{GST_CLOCK_TIME_NONE};
    std::atomic<GstClockTime> audio_pts{GST_CLOCK_TIME_NONE};
    g_signal_connect(video_sink, "handoff", G_CALLBACK(capture_first_pts), &video_pts);
    g_signal_connect(audio_sink, "handoff", G_CALLBACK(capture_first_pts), &audio_pts);
    const auto playing = gst_element_set_state(pipeline, GST_STATE_PLAYING) !=
                         GST_STATE_CHANGE_FAILURE;
    auto* bus = gst_element_get_bus(pipeline);
    auto* message = playing && bus != nullptr
                        ? gst_bus_timed_pop_filtered(
                              bus, 5U * GST_SECOND,
                              static_cast<GstMessageType>(GST_MESSAGE_EOS | GST_MESSAGE_ERROR))
                        : nullptr;
    const auto completed = message != nullptr && GST_MESSAGE_TYPE(message) == GST_MESSAGE_EOS;
    if (message != nullptr) {
        gst_message_unref(message);
    }
    if (bus != nullptr) {
        gst_object_unref(bus);
    }
    static_cast<void>(gst_element_set_state(pipeline, GST_STATE_NULL));
    gst_object_unref(video_sink);
    gst_object_unref(audio_sink);
    gst_object_unref(pipeline);
    const auto first_video_pts = video_pts.load();
    const auto first_audio_pts = audio_pts.load();
    if (!completed || !GST_CLOCK_TIME_IS_VALID(first_video_pts) ||
        !GST_CLOCK_TIME_IS_VALID(first_audio_pts)) {
        return std::nullopt;
    }
    const auto offset = first_video_pts >= first_audio_pts
                            ? first_video_pts - first_audio_pts
                            : first_audio_pts - first_video_pts;
    return static_cast<double>(offset) / static_cast<double>(GST_MSECOND);
}

void test_configuration_and_staging_path_are_strict() {
    const auto final_path = std::filesystem::absolute("recording.mp4");
    const solin::media_engine::ProgramRecordingConfiguration valid{
        .path = final_path,
        .width = 1'920U,
        .height = 1'080U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
    };
    try {
        solin::media_engine::validate_program_recording_configuration(valid);
        expect(true, "valid recording configuration is accepted");
    } catch (const std::exception&) {
        expect(false, "valid recording configuration is accepted");
    }
    auto expected_staging = final_path;
    expected_staging += ".part";
    expect(solin::media_engine::program_recording_staging_path(final_path) == expected_staging,
           "staging path is derived without replacing the MP4 suffix");
    auto invalid = valid;
    invalid.path.replace_extension(".mkv");
    try {
        solin::media_engine::validate_program_recording_configuration(invalid);
        expect(false, "non-MP4 destinations are rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "non-MP4 destinations are rejected");
    }
}

void test_aac_encoder_selection_prefers_media_foundation_and_falls_back() {
    expect(solin::media_engine::program_recording_aac_encoder_factory(true, true) ==
               "mfaacenc",
           "Media Foundation AAC is preferred when both LGPL encoders are available");
    expect(solin::media_engine::program_recording_aac_encoder_factory(false, true) ==
               "avenc_aac",
           "libav AAC is selected when Media Foundation AAC is unavailable");
    expect(solin::media_engine::program_recording_aac_encoder_factory(false, false).empty(),
           "recording capability fails closed when no supported AAC encoder exists");
}

void test_video_bitrate_budget_tracks_resolution_and_frame_rate() {
    auto configuration = solin::media_engine::ProgramRecordingConfiguration{
        .width = 1'280U,
        .height = 720U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
    };
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 4'000U,
           "720p30 recording uses the high-quality minimum video bitrate");
    configuration.width = 1'920U;
    configuration.height = 1'080U;
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 7'465U,
           "1080p30 recording converts bits per second to kilobits per second");
    configuration.fps_numerator = 30'000U;
    configuration.fps_denominator = 1'001U;
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 7'458U,
           "fractional frame rates preserve their precise video budget");
    configuration.fps_numerator = 60U;
    configuration.fps_denominator = 1U;
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 14'930U,
           "1080p60 recording scales the video budget with frame rate");
    configuration.width = 3'840U;
    configuration.height = 2'160U;
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 24'000U,
           "4K60 recording stays inside the encoder safety ceiling");
    configuration.width = (std::numeric_limits<std::uint32_t>::max)();
    configuration.height = (std::numeric_limits<std::uint32_t>::max)();
    configuration.fps_numerator = (std::numeric_limits<std::uint32_t>::max)();
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 24'000U,
           "overflowing invalid formats saturate safely at the maximum budget");
    configuration = {};
    expect(solin::media_engine::program_recording_target_video_bitrate_kbps(
               configuration) == 4'000U,
           "invalid zero-valued formats fail closed to the minimum budget");
}

void test_libav_aac_fallback_records_when_media_foundation_aac_is_absent() {
    auto* libav_factory = gst_element_factory_find("avenc_aac");
    if (libav_factory == nullptr) {
        std::cout << "SKIP: LGPL libav AAC encoder is unavailable\n";
        return;
    }
    gst_object_unref(libav_factory);
    TemporarilyRemovedFactory missing_media_foundation_aac{"mfaacenc"};
    if (!missing_media_foundation_aac.removed()) {
        return;
    }
    auto renderer = std::make_shared<StaticProgramRenderer>();
    solin::media_engine::ProgramRecordingController controller{renderer};
    expect(controller.supported(),
           "recording capability remains available with only the LGPL AAC fallback");
    if (!controller.supported()) {
        return;
    }
    const auto directory = unique_test_directory();
    const auto final_path = directory / "libav-aac.mp4";
    const solin::media_engine::ProgramRecordingConfiguration configuration{
        .path = final_path,
        .width = 320U,
        .height = 240U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
        .microphone = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
        .system_audio = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
    };
    const solin::media_engine::AudioDeviceSnapshot devices{
        .supported = true,
        .ready = true,
        .error_code = {},
    };
    const auto started = controller.start(configuration, devices);
    expect(started.applied, "recording starts with the LGPL AAC fallback");
    if (started.applied) {
        std::this_thread::sleep_for(100ms);
        expect(controller.stop().applied,
               "recording finalizes with the LGPL AAC fallback");
        expect(std::filesystem::is_regular_file(final_path),
               "the LGPL AAC fallback produces a finalized MP4");
    }
    std::error_code cleanup_error;
    std::filesystem::remove_all(directory, cleanup_error);
}

void test_static_program_is_duplicated_and_finalized() {
    auto renderer = std::make_shared<StaticProgramRenderer>();
    solin::media_engine::ProgramRecordingController controller{renderer};
    if (!controller.supported()) {
        std::cout << "SKIP: supported Media Foundation recording pipeline is unavailable\n";
        return;
    }
    const auto directory = unique_test_directory();
    const auto final_path = directory / "program.mp4";
    const solin::media_engine::ProgramRecordingConfiguration configuration{
        .path = final_path,
        .width = 320U,
        .height = 240U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
        .microphone = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
        .system_audio = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
    };
    const solin::media_engine::AudioDeviceSnapshot devices{
        .supported = true,
        .ready = true,
        .error_code = {},
    };
    const auto started = controller.start(configuration, devices);
    expect(started.applied, "recording starts after the first Program frame is accepted");
    if (started.applied) {
        std::this_thread::sleep_for(250ms);
        const auto recording = controller.state();
        expect(recording.status == solin::media_engine::ProgramRecordingStatus::recording,
               "controller exposes the recording state");
        expect(recording.frames_duplicated > 0U,
               "static Program frames are duplicated at output cadence");
        renderer->set_output_size(640U, 360U);
        std::this_thread::sleep_for(250ms);
        expect(controller.state().status ==
                   solin::media_engine::ProgramRecordingStatus::recording,
               "a Program resolution change is scaled into the fixed recording track");
        const auto stopped = controller.stop();
        expect(stopped.applied, "recording finalizes successfully");
        expect(std::filesystem::is_regular_file(final_path),
               "finalized MP4 is atomically exposed");
        expect(!std::filesystem::exists(
                   solin::media_engine::program_recording_staging_path(final_path)),
               "staging file is removed by atomic rename");
        std::error_code error;
        expect(std::filesystem::file_size(final_path, error) > 0U && !error,
               "finalized MP4 contains encoded media");
        const auto initial_av_offset_ms = initial_av_offset_milliseconds(final_path);
        if (initial_av_offset_ms.has_value()) {
            std::cout << "PERF program-recording initial-av-offset-ms="
                      << initial_av_offset_ms.value() << '\n';
        }
        expect(initial_av_offset_ms.has_value() && initial_av_offset_ms.value() <= 50.0,
               "the finalized MP4 starts audio and video within 50 ms");
    }
    std::error_code cleanup_error;
    std::filesystem::remove_all(directory, cleanup_error);
}

void test_audio_pair_update_rolls_back_when_second_branch_fails() {
    auto renderer = std::make_shared<StaticProgramRenderer>();
    solin::media_engine::ProgramRecordingController controller{renderer};
    if (!controller.supported()) {
        return;
    }
    const auto directory = unique_test_directory();
    const solin::media_engine::AudioDeviceSnapshot devices{
        .supported = true,
        .ready = true,
        .error_code = {},
    };
    const solin::media_engine::ProgramRecordingConfiguration configuration{
        .path = directory / "atomic-audio.mp4",
        .width = 320U,
        .height = 240U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
        .microphone = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
        .system_audio = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
    };
    const auto started = controller.start(configuration, devices);
    expect(started.applied, "atomic audio update test recording starts");
    if (started.applied) {
        const auto before = controller.state();
        {
            TemporarilyRemovedFactory missing_wasapi{"wasapi2src"};
            if (!missing_wasapi.removed()) {
                std::cout << "SKIP: wasapi2src factory could not be isolated\n";
            } else {
                const auto updated = controller.set_audio(
                    {.mode = solin::media_engine::RecordingAudioSelectionMode::device,
                     .device_id = "missing-microphone"},
                    {.mode =
                         solin::media_engine::RecordingAudioSelectionMode::system_default},
                    devices);
                const auto after = controller.state();
                expect(!updated.applied &&
                           updated.error_code == "recording_audio_update_failed",
                       "audio pair update rejects a failure in the second prepared branch");
                expect(after.generation == before.generation &&
                           after.microphone_warning == before.microphone_warning &&
                           after.system_audio_warning == before.system_audio_warning,
                       "failed pair preparation leaves the previous audio state authoritative");
                expect(after.status ==
                           solin::media_engine::ProgramRecordingStatus::recording,
                       "failed audio pair preparation does not interrupt video recording");
            }
        }
        expect(controller.stop().applied,
               "recording finalizes after an atomic audio update rollback");
    }
    std::error_code cleanup_error;
    std::filesystem::remove_all(directory, cleanup_error);
}

void test_recording_latency_budgets_and_live_metrics() {
    auto renderer = std::make_shared<StaticProgramRenderer>();
    solin::media_engine::ProgramRecordingController controller{renderer};
    if (!controller.supported()) {
        return;
    }
    const auto directory = unique_test_directory();
    const solin::media_engine::AudioDeviceSnapshot devices{
        .supported = true,
        .ready = true,
        .error_code = {},
    };
    std::vector<std::chrono::steady_clock::duration> hot_swap_samples;
    std::vector<std::chrono::steady_clock::duration> finalization_samples;
    constexpr std::size_t session_count = 8U;
    for (std::size_t session = 0U; session < session_count; ++session) {
        const solin::media_engine::ProgramRecordingConfiguration configuration{
            .path = directory / ("budget-" + std::to_string(session) + ".mp4"),
            .width = 320U,
            .height = 240U,
            .fps_numerator = 30U,
            .fps_denominator = 1U,
            .microphone = {.mode =
                               solin::media_engine::RecordingAudioSelectionMode::none},
            .system_audio = {.mode =
                                 solin::media_engine::RecordingAudioSelectionMode::none},
        };
        const auto started = controller.start(configuration, devices);
        expect(started.applied, "recording latency sample starts");
        if (!started.applied) {
            break;
        }
        const auto hot_swap_begin = std::chrono::steady_clock::now();
        const auto updated = controller.set_audio(
            {.mode = solin::media_engine::RecordingAudioSelectionMode::system_default},
            {.mode = solin::media_engine::RecordingAudioSelectionMode::none}, devices);
        hot_swap_samples.push_back(std::chrono::steady_clock::now() - hot_swap_begin);
        expect(updated.applied, "recording audio hot-swap sample applies");
        if (session == 0U) {
            const auto before_metrics = controller.state();
            std::this_thread::sleep_for(1'100ms);
            const auto live_metrics = controller.state();
            const auto frame_feed_p95_ms =
                static_cast<double>(live_metrics.frame_feed_p95_ns) / 1'000'000.0;
            std::cout << "PERF program-recording frame-feed-p95-ms="
                      << frame_feed_p95_ms << '\n';
            expect(live_metrics.frames_written > before_metrics.frames_written &&
                       live_metrics.generation > before_metrics.generation,
                    "recording metrics publish a bounded live generation update");
            expect(frame_feed_p95_ms > 0.0 && frame_feed_p95_ms < (1'000.0 / 30.0),
                   "Program frame feed P95 remains below one frame interval");
        }
        const auto finalization_begin = std::chrono::steady_clock::now();
        const auto stopped = controller.stop();
        finalization_samples.push_back(std::chrono::steady_clock::now() -
                                       finalization_begin);
        expect(stopped.applied, "recording latency sample finalizes");
    }
    const auto hot_swap_p95_ms = p95_milliseconds(std::move(hot_swap_samples));
    const auto finalization_p95_ms = p95_milliseconds(std::move(finalization_samples));
    std::cout << "PERF program-recording audio-hot-swap-p95-ms=" << hot_swap_p95_ms
              << " finalization-p95-ms=" << finalization_p95_ms << '\n';
    expect(hot_swap_p95_ms > 0.0 && hot_swap_p95_ms < 250.0,
           "audio hot-swap P95 remains below 250 ms");
    expect(finalization_p95_ms > 0.0 && finalization_p95_ms < 2'000.0,
           "normal recording finalization P95 remains below 2 seconds");
    std::error_code cleanup_error;
    std::filesystem::remove_all(directory, cleanup_error);
}

void test_asynchronous_failure_is_reaped_before_retry() {
    auto renderer = std::make_shared<StaticProgramRenderer>();
    solin::media_engine::ProgramRecordingController controller{renderer};
    if (!controller.supported()) {
        return;
    }
    const auto directory = unique_test_directory();
    const solin::media_engine::AudioDeviceSnapshot devices{
        .supported = true,
        .ready = true,
        .error_code = {},
    };
    solin::media_engine::ProgramRecordingConfiguration configuration{
        .path = directory / "failed.mp4",
        .width = 320U,
        .height = 240U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
        .microphone = {
            .mode = solin::media_engine::RecordingAudioSelectionMode::device,
            .device_id = "missing-microphone",
        },
        .system_audio = {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
    };
    const auto started = controller.start(configuration, devices);
    expect(started.applied, "failure test recording starts");
    if (started.applied) {
        expect(controller.state().microphone_warning == "microphone_device_unavailable",
               "missing explicit microphone degrades to silence with a typed warning");
        const auto warning_generation = controller.state().generation;
        const auto audio_updated = controller.set_audio(
            {.mode = solin::media_engine::RecordingAudioSelectionMode::none},
            {.mode = solin::media_engine::RecordingAudioSelectionMode::none}, devices);
        expect(audio_updated.applied && controller.state().microphone_warning.empty() &&
                   controller.state().generation > warning_generation,
               "audio warning recovery publishes a new native state generation");
        renderer->set_fail_after_first(true);
        std::this_thread::sleep_for(100ms);
        controller.refresh_audio_devices(devices);
        expect(controller.state().status == solin::media_engine::ProgramRecordingStatus::failed,
               "asynchronous Program failure is surfaced after control-thread reap");
        expect(!std::filesystem::exists(configuration.path) &&
                   std::filesystem::is_regular_file(
                       solin::media_engine::program_recording_staging_path(
                           configuration.path)),
               "an asynchronously failed recording preserves its finalized partial file");
        renderer->set_fail_after_first(false);
        configuration.path = directory / "retry.mp4";
        const auto retried = controller.start(configuration, devices);
        expect(retried.applied,
               "retry starts only after the failed pipeline and consumer are fully reaped");
        if (retried.applied) {
            std::this_thread::sleep_for(100ms);
            expect(controller.stop().applied, "retried recording finalizes");
        }
    }
    std::error_code cleanup_error;
    std::filesystem::remove_all(directory, cleanup_error);
}

} // namespace

int main() {
    GError* error = nullptr;
    if (gst_init_check(nullptr, nullptr, &error) == FALSE) {
        if (error != nullptr) {
            g_error_free(error);
        }
        std::cerr << "GStreamer initialization failed\n";
        return 1;
    }
    test_configuration_and_staging_path_are_strict();
    test_aac_encoder_selection_prefers_media_foundation_and_falls_back();
    test_video_bitrate_budget_tracks_resolution_and_frame_rate();
    test_static_program_is_duplicated_and_finalized();
    test_audio_pair_update_rolls_back_when_second_branch_fails();
    test_recording_latency_budgets_and_live_metrics();
    test_asynchronous_failure_is_reaped_before_retry();
    test_libav_aac_fallback_records_when_media_foundation_aac_is_absent();
    if (failures != 0) {
        std::cerr << failures << " Program recording test(s) failed\n";
        return 1;
    }
    return 0;
}
