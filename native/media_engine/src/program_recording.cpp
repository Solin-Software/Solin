#include "solin/media_engine/program_recording.hpp"

#include "solin/media_engine/video_frame.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include "gstreamer_source_runtime.hpp"

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#endif

namespace solin::media_engine {
namespace {

using namespace std::chrono_literals;

constexpr auto kFirstFrameTimeout = 3s;
constexpr auto kFinalizationTimeout = 8s;
constexpr std::uintmax_t kMinimumRecordingFreeSpaceBytes = 64ULL * 1024ULL * 1024ULL;

[[nodiscard]] std::uint64_t monotonic_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                          std::chrono::steady_clock::now().time_since_epoch())
                                          .count());
}

template <std::size_t Size>
[[nodiscard]] std::uint64_t p95_nanoseconds(
    const std::array<std::uint64_t, Size>& samples, const std::size_t count) noexcept {
    if (count == 0U) {
        return 0U;
    }
    auto ordered = samples;
    std::ranges::sort(ordered.begin(), ordered.begin() + static_cast<std::ptrdiff_t>(count));
    const auto rank = (count * 95U + 99U) / 100U;
    return ordered[rank - 1U];
}

[[nodiscard]] bool valid_device_id(const std::string_view value) noexcept {
    return !value.empty() && value.size() <= 1'024U &&
           std::ranges::none_of(value, [](const unsigned char character) {
               return character < 32U || character == 127U;
           });
}

[[nodiscard]] ProgramRecordingOperationResult rejected(std::string code,
                                                        std::string message) {
    return {
        .applied = false,
        .error_code = std::move(code),
        .error_message = std::move(message),
    };
}

[[nodiscard]] ProgramRecordingOperationResult applied() { return {.applied = true}; }

struct ResolvedAudioSelection final {
    std::optional<std::string> device_id{};
    bool silence{false};
    std::string warning{};

    [[nodiscard]] std::string key(const AudioDeviceDirection direction) const {
        return std::string{direction == AudioDeviceDirection::input ? "input:" : "output:"} +
               (silence ? "silence:" + (warning.empty() ? std::string{"none"} : warning)
                        : device_id.value_or("default"));
    }
};

[[nodiscard]] ResolvedAudioSelection resolve_audio_selection(
    const RecordingAudioSelection& selection, const AudioDeviceDirection direction,
    const AudioDeviceSnapshot& devices) {
    if (selection.mode == RecordingAudioSelectionMode::none) {
        return {.silence = true};
    }
    if (selection.mode == RecordingAudioSelectionMode::system_default) {
        const auto match = std::ranges::find_if(
            devices.devices, [direction](const AudioDevice& device) {
                return device.direction == direction && device.is_default;
            });
        return match == devices.devices.end()
                   ? ResolvedAudioSelection{}
                   : ResolvedAudioSelection{.device_id = match->device_id};
    }
    const auto match = std::ranges::find_if(
        devices.devices, [&selection, direction](const AudioDevice& device) {
            return device.direction == direction && device.device_id == selection.device_id;
        });
    if (match == devices.devices.end()) {
        return {
            .silence = true,
            .warning = direction == AudioDeviceDirection::input
                           ? "microphone_device_unavailable"
                           : "system_audio_device_unavailable",
        };
    }
    return {.device_id = match->device_id};
}

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER

class GstElementOwner final {
  public:
    explicit GstElementOwner(GstElement* value = nullptr) noexcept : value_(value) {}
    ~GstElementOwner() {
        if (value_ != nullptr) {
            gst_object_unref(value_);
        }
    }
    GstElementOwner(const GstElementOwner&) = delete;
    GstElementOwner& operator=(const GstElementOwner&) = delete;
    GstElementOwner(GstElementOwner&& other) noexcept
        : value_(std::exchange(other.value_, nullptr)) {}
    [[nodiscard]] GstElement* get() const noexcept { return value_; }
    [[nodiscard]] GstElement* release() noexcept { return std::exchange(value_, nullptr); }

  private:
    GstElement* value_{nullptr};
};

class GstCapsOwner final {
  public:
    explicit GstCapsOwner(GstCaps* value = nullptr) noexcept : value_(value) {}
    ~GstCapsOwner() {
        if (value_ != nullptr) {
            gst_caps_unref(value_);
        }
    }
    GstCapsOwner(const GstCapsOwner&) = delete;
    GstCapsOwner& operator=(const GstCapsOwner&) = delete;

    [[nodiscard]] GstCaps* get() const noexcept { return value_; }
    void reset(GstCaps* value) noexcept {
        if (value_ != nullptr) {
            gst_caps_unref(value_);
        }
        value_ = value;
    }

  private:
    GstCaps* value_{nullptr};
};

[[nodiscard]] GstElement* make_element(const char* factory, const char* name = nullptr) {
    auto* element = gst_element_factory_make(factory, name);
    if (element == nullptr) {
        throw std::runtime_error(std::string{"missing GStreamer element: "} + factory);
    }
    return element;
}

void require_link(GstElement* left, GstElement* right) {
    if (gst_element_link(left, right) == FALSE) {
        throw std::runtime_error("recording pipeline link failed");
    }
}

[[nodiscard]] std::string path_utf8(const std::filesystem::path& path) {
    const auto encoded = path.u8string();
    return {reinterpret_cast<const char*>(encoded.data()), encoded.size()};
}

[[nodiscard]] bool has_factory(const char* name) noexcept {
    auto* factory = gst_element_factory_find(name);
    if (factory == nullptr) {
        return false;
    }
    gst_object_unref(factory);
    return true;
}

[[nodiscard]] bool recording_factories_available() noexcept {
    constexpr const char* factories[]{
        "appsrc",      "videoconvert", "videoscale",  "capsfilter",  "mfh264enc",
        "h264parse",   "queue",        "audiomixer", "audioconvert",
        "audioresample", "aacparse",    "mp4mux",
        "filesink",    "audiotestsrc", "volume",      "wasapi2src",
    };
    return std::ranges::all_of(factories, [](const char* name) { return has_factory(name); }) &&
           !program_recording_aac_encoder_factory(has_factory("mfaacenc"),
                                                  has_factory("avenc_aac"))
                .empty();
}

#endif

} // namespace

std::string_view program_recording_status_text(const ProgramRecordingStatus status) noexcept {
    switch (status) {
    case ProgramRecordingStatus::idle:
        return "idle";
    case ProgramRecordingStatus::starting:
        return "starting";
    case ProgramRecordingStatus::recording:
        return "recording";
    case ProgramRecordingStatus::stopping:
        return "stopping";
    case ProgramRecordingStatus::failed:
        return "failed";
    }
    return "failed";
}

std::filesystem::path
program_recording_staging_path(const std::filesystem::path& final_path) {
    auto result = final_path;
    result += ".part";
    return result;
}

bool program_recording_runtime_supported() noexcept {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    return recording_factories_available();
#else
    return false;
#endif
}

std::string_view program_recording_aac_encoder_factory(
    const bool media_foundation_available, const bool libav_available) noexcept {
    if (media_foundation_available) {
        return "mfaacenc";
    }
    return libav_available ? std::string_view{"avenc_aac"} : std::string_view{};
}

void validate_program_recording_configuration(
    const ProgramRecordingConfiguration& configuration) {
    if (configuration.path.empty() || !configuration.path.is_absolute() ||
        configuration.path.extension() != ".mp4" ||
        configuration.path.native().size() > 4'096U) {
        throw std::invalid_argument("invalid recording path");
    }
    if (configuration.width == 0U || configuration.height == 0U ||
        configuration.width > kMaximumProgramRecordingDimension ||
        configuration.height > kMaximumProgramRecordingDimension ||
        static_cast<std::uint64_t>(configuration.width) * configuration.height >
            kMaximumProgramRecordingPixels ||
        configuration.fps_numerator == 0U || configuration.fps_denominator == 0U ||
        static_cast<std::uint64_t>(configuration.fps_numerator) >
            static_cast<std::uint64_t>(kMaximumProgramRecordingFramesPerSecond) *
                configuration.fps_denominator) {
        throw std::invalid_argument("invalid recording video format");
    }
    const auto validate_selection = [](const RecordingAudioSelection& selection) {
        const bool explicit_device =
            selection.mode == RecordingAudioSelectionMode::device;
        if (explicit_device != !selection.device_id.empty() ||
            (explicit_device && !valid_device_id(selection.device_id))) {
            throw std::invalid_argument("invalid recording audio selection");
        }
    };
    validate_selection(configuration.microphone);
    validate_selection(configuration.system_audio);
}

class ProgramRecordingController::Impl final {
  public:
    explicit Impl(std::shared_ptr<SceneRenderer> renderer) : renderer_(std::move(renderer)) {}
    ~Impl() { shutdown(); }

    [[nodiscard]] bool supported() const noexcept {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        return renderer_ != nullptr && program_recording_runtime_supported();
#else
        return false;
#endif
    }

    [[nodiscard]] ProgramRecordingOperationResult
    start(const ProgramRecordingConfiguration& configuration,
          const AudioDeviceSnapshot& devices) {
        std::unique_lock lock{mutex_};
        if (state_.status == ProgramRecordingStatus::starting ||
            state_.status == ProgramRecordingStatus::recording ||
            state_.status == ProgramRecordingStatus::stopping) {
            return rejected("recording_busy", "A Program recording is already active");
        }
        try {
            validate_program_recording_configuration(configuration);
        } catch (const std::invalid_argument& error) {
            return rejected("invalid_recording_configuration", error.what());
        }
        if (!supported()) {
            return rejected("recording_encoder_unavailable",
                            "A supported H.264/AAC recording pipeline is unavailable");
        }
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        if (pipeline_ != nullptr || pump_.joinable() || system_memory_enabled_) {
            const auto reaped = cleanup_locked(
                true, state_.status != ProgramRecordingStatus::failed && !pump_failed_);
            if (!reaped.applied) {
                return rejected("recording_previous_session_cleanup_failed",
                                reaped.error_message);
            }
        }
#endif
        std::error_code filesystem_error;
        if (!std::filesystem::is_directory(configuration.path.parent_path(), filesystem_error) ||
            filesystem_error || std::filesystem::exists(configuration.path, filesystem_error) ||
            filesystem_error) {
            return rejected("recording_path_unavailable",
                            "The recording destination is not available");
        }
        const auto destination_space =
            std::filesystem::space(configuration.path.parent_path(), filesystem_error);
        if (filesystem_error ||
            destination_space.available < kMinimumRecordingFreeSpaceBytes) {
            return rejected("recording_disk_space_low",
                            "The recording destination has insufficient free space");
        }
        const auto staging = program_recording_staging_path(configuration.path);
        if (std::filesystem::exists(staging, filesystem_error) || filesystem_error) {
            return rejected("recording_staging_path_exists",
                            "A partial recording already uses this destination");
        }
        state_ = {
            .generation = state_.generation + 1U,
            .status = ProgramRecordingStatus::starting,
            .path = configuration.path,
        };
        configuration_ = configuration;
        microphone_ = configuration.microphone;
        system_audio_ = configuration.system_audio;
        try {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
            build_pipeline_locked(devices);
            if (!gpu_path_enabled_) {
                renderer_->set_system_memory_output_enabled(
                    OutputBus::virtual_camera, SystemMemoryOutputConsumer::program_recording,
                    true);
                system_memory_enabled_ = true;
            }
            start_pump_locked();
            const auto first_frame_ready = first_frame_changed_.wait_for(
                lock, kFirstFrameTimeout,
                [this] { return first_frame_accepted_ || pump_failed_; });
            if (!first_frame_ready || !first_frame_accepted_) {
                const auto failure = pump_failed_ ? "recording_video_failed"
                                                  : "recording_program_frame_timeout";
                static_cast<void>(cleanup_locked(false));
                fail_locked(failure, "The first Program frame could not be recorded");
                return rejected(state_.error_code, state_.error_message);
            }
            state_.status = ProgramRecordingStatus::recording;
            state_.started_at_monotonic_ns = recording_started_at_ns_;
            ++state_.generation;
            return applied();
#else
            static_cast<void>(devices);
#endif
        } catch (const std::exception& error) {
            static_cast<void>(cleanup_locked(false));
            fail_locked("recording_pipeline_start_failed", error.what());
            return rejected(state_.error_code, state_.error_message);
        }
        fail_locked("recording_unavailable", "Program recording is unavailable");
        return rejected(state_.error_code, state_.error_message);
    }

    [[nodiscard]] ProgramRecordingOperationResult
    set_audio(const RecordingAudioSelection& microphone,
              const RecordingAudioSelection& system_audio,
              const AudioDeviceSnapshot& devices) {
        std::scoped_lock lock{mutex_};
        try {
            ProgramRecordingConfiguration candidate = configuration_;
            candidate.microphone = microphone;
            candidate.system_audio = system_audio;
            if (!candidate.path.empty()) {
                validate_program_recording_configuration(candidate);
            } else {
                const auto validate = [](const RecordingAudioSelection& selection) {
                    if ((selection.mode == RecordingAudioSelectionMode::device) !=
                        !selection.device_id.empty()) {
                        throw std::invalid_argument("invalid recording audio selection");
                    }
                };
                validate(microphone);
                validate(system_audio);
            }
        } catch (const std::invalid_argument& error) {
            return rejected("invalid_recording_audio_selection", error.what());
        }
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        if (pipeline_ != nullptr &&
            (state_.status == ProgramRecordingStatus::recording ||
             state_.status == ProgramRecordingStatus::starting)) {
            try {
                refresh_audio_locked(microphone, system_audio, devices, true);
            } catch (const std::exception& error) {
                return rejected("recording_audio_update_failed", error.what());
            }
        }
#else
        static_cast<void>(devices);
#endif
        microphone_ = microphone;
        system_audio_ = system_audio;
        configuration_.microphone = microphone;
        configuration_.system_audio = system_audio;
        ++state_.generation;
        return applied();
    }

    [[nodiscard]] ProgramRecordingOperationResult stop() {
        std::scoped_lock lock{mutex_};
        if (state_.status == ProgramRecordingStatus::idle) {
            return applied();
        }
        if (state_.status == ProgramRecordingStatus::stopping) {
            return applied();
        }
        if (state_.path.empty()) {
            static_cast<void>(cleanup_locked(false));
            state_.status = ProgramRecordingStatus::idle;
            ++state_.generation;
            return applied();
        }
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        const auto publish_final = state_.status != ProgramRecordingStatus::failed;
#endif
        state_.status = ProgramRecordingStatus::stopping;
        ++state_.generation;
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        const auto finalized = cleanup_locked(true, publish_final);
        if (!finalized.applied) {
            fail_locked(finalized.error_code, finalized.error_message);
            return finalized;
        }
#else
        static_cast<void>(cleanup_locked(false));
#endif
        state_.status = ProgramRecordingStatus::idle;
        state_.started_at_monotonic_ns = 0U;
        state_.error_code.clear();
        state_.error_message.clear();
        state_.microphone_warning.clear();
        state_.system_audio_warning.clear();
        ++state_.generation;
        return applied();
    }

    void refresh_audio_devices(const AudioDeviceSnapshot& devices) {
        std::scoped_lock lock{mutex_};
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        if (pump_failed_ && (pipeline_ != nullptr || pump_.joinable())) {
            const auto original_code = state_.error_code;
            const auto original_message = state_.error_message;
            const auto finalized = cleanup_locked(true, false);
            state_.status = ProgramRecordingStatus::failed;
            state_.error_code = original_code.empty() ? "recording_video_failed" : original_code;
            state_.error_message = original_message.empty()
                                       ? "The Program video stream failed"
                                       : original_message;
            if (!finalized.applied) {
                state_.error_message += "; " + finalized.error_message;
            }
            ++state_.generation;
            return;
        }
        if (pipeline_ != nullptr && state_.status == ProgramRecordingStatus::recording) {
            try {
                refresh_audio_locked(microphone_, system_audio_, devices, true);
            } catch (const std::exception&) {
                // A transient device graph failure must not abort the video. The old
                // branch remains authoritative until a later refresh succeeds.
            }
        }
#else
        static_cast<void>(devices);
#endif
    }

    [[nodiscard]] ProgramRecordingState state() const {
        std::scoped_lock lock{mutex_};
        return state_;
    }

    void shutdown() noexcept {
        try {
            std::scoped_lock lock{mutex_};
            static_cast<void>(cleanup_locked(
                true, state_.status != ProgramRecordingStatus::failed && !pump_failed_));
        } catch (...) {
        }
    }

  private:
    void fail_locked(std::string code, std::string message) {
        state_.status = ProgramRecordingStatus::failed;
        state_.error_code = std::move(code);
        state_.error_message = std::move(message);
        ++state_.generation;
    }

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    struct AudioBranch final {
        std::string key{};
        GstElement* source{nullptr};
        GstElement* queue{nullptr};
        GstElement* convert{nullptr};
        GstElement* resample{nullptr};
        GstElement* caps_filter{nullptr};
        GstElement* volume{nullptr};
        GstPad* mixer_pad{nullptr};

        [[nodiscard]] std::array<GstElement*, 6U> elements() const noexcept {
            return {source, queue, convert, resample, caps_filter, volume};
        }
    };

    [[nodiscard]] AudioBranch create_audio_branch_locked(
        const ResolvedAudioSelection& selection, const AudioDeviceDirection direction,
        const bool initial) {
        AudioBranch branch{.key = selection.key(direction)};
        branch.source = make_element(selection.silence ? "audiotestsrc" : "wasapi2src");
        branch.queue = make_element("queue");
        branch.convert = make_element("audioconvert");
        branch.resample = make_element("audioresample");
        branch.caps_filter = make_element("capsfilter");
        branch.volume = make_element("volume");
        if (selection.silence) {
            g_object_set(branch.source, "is-live", TRUE, "wave", 4, nullptr);
        } else {
            g_object_set(branch.source, "loopback",
                         direction == AudioDeviceDirection::output ? TRUE : FALSE,
                         "continue-on-error", TRUE, "do-timestamp", TRUE, nullptr);
            if (selection.device_id.has_value()) {
                g_object_set(branch.source, "device", selection.device_id->c_str(), nullptr);
            }
        }
        g_object_set(branch.queue, "max-size-time", static_cast<guint64>(250U * GST_MSECOND),
                     "max-size-buffers", 0U, "max-size-bytes", 0U, "leaky", 2, nullptr);
        g_object_set(branch.volume, "volume", initial ? 1.0 : 0.0, nullptr);
        GstElementOwner source_owner{branch.source};
        GstElementOwner queue_owner{branch.queue};
        GstElementOwner convert_owner{branch.convert};
        GstElementOwner resample_owner{branch.resample};
        GstElementOwner caps_owner{branch.caps_filter};
        GstElementOwner volume_owner{branch.volume};
        const auto add = [this](GstElementOwner& owner) {
            if (gst_bin_add(GST_BIN(pipeline_), owner.get()) == FALSE) {
                throw std::runtime_error("recording audio branch could not be added");
            }
            static_cast<void>(owner.release());
        };
        try {
            add(source_owner);
            add(queue_owner);
            add(convert_owner);
            add(resample_owner);
            add(caps_owner);
            add(volume_owner);
            GstCaps* audio_caps = gst_caps_new_simple(
                "audio/x-raw", "format", G_TYPE_STRING, "S16LE", "rate", G_TYPE_INT,
                48'000, "channels", G_TYPE_INT, 2, "layout", G_TYPE_STRING,
                "interleaved", nullptr);
            g_object_set(branch.caps_filter, "caps", audio_caps, nullptr);
            gst_caps_unref(audio_caps);
            require_link(branch.source, branch.queue);
            require_link(branch.queue, branch.convert);
            require_link(branch.convert, branch.resample);
            require_link(branch.resample, branch.caps_filter);
            require_link(branch.caps_filter, branch.volume);
            auto* source_pad = gst_element_get_static_pad(branch.volume, "src");
            branch.mixer_pad = gst_element_request_pad_simple(audio_mixer_, "sink_%u");
            if (source_pad == nullptr || branch.mixer_pad == nullptr ||
                gst_pad_link(source_pad, branch.mixer_pad) != GST_PAD_LINK_OK) {
                if (source_pad != nullptr) {
                    gst_object_unref(source_pad);
                }
                throw std::runtime_error("recording audio mixer link failed");
            }
            gst_object_unref(source_pad);
            if (!initial) {
                for (auto* element : branch.elements()) {
                    if (gst_element_sync_state_with_parent(element) == FALSE) {
                        throw std::runtime_error(
                            "recording audio branch could not be started");
                    }
                }
            }
        } catch (...) {
            auto* source_pad = branch.volume == nullptr
                                   ? nullptr
                                   : gst_element_get_static_pad(branch.volume, "src");
            if (source_pad != nullptr && branch.mixer_pad != nullptr) {
                static_cast<void>(gst_pad_unlink(source_pad, branch.mixer_pad));
            }
            if (source_pad != nullptr) {
                gst_object_unref(source_pad);
            }
            if (branch.mixer_pad != nullptr) {
                gst_element_release_request_pad(audio_mixer_, branch.mixer_pad);
                gst_object_unref(branch.mixer_pad);
                branch.mixer_pad = nullptr;
            }
            for (auto* element : branch.elements()) {
                if (element != nullptr &&
                    gst_object_has_as_parent(GST_OBJECT(element), GST_OBJECT(pipeline_)) !=
                        FALSE) {
                    static_cast<void>(gst_element_set_state(element, GST_STATE_NULL));
                    static_cast<void>(gst_bin_remove(GST_BIN(pipeline_), element));
                }
            }
            throw;
        }
        return branch;
    }

    void remove_audio_branch_locked(AudioBranch& branch) noexcept {
        if (branch.source == nullptr) {
            return;
        }
        for (auto* element : branch.elements()) {
            gst_element_set_state(element, GST_STATE_NULL);
        }
        auto* source_pad = gst_element_get_static_pad(branch.volume, "src");
        if (source_pad != nullptr && branch.mixer_pad != nullptr) {
            gst_pad_unlink(source_pad, branch.mixer_pad);
        }
        if (source_pad != nullptr) {
            gst_object_unref(source_pad);
        }
        if (branch.mixer_pad != nullptr) {
            gst_element_release_request_pad(audio_mixer_, branch.mixer_pad);
            gst_object_unref(branch.mixer_pad);
            branch.mixer_pad = nullptr;
        }
        for (auto* element : branch.elements()) {
            gst_bin_remove(GST_BIN(pipeline_), element);
        }
        branch = {};
    }

    void refresh_audio_locked(const RecordingAudioSelection& desired_microphone,
                              const RecordingAudioSelection& desired_system_audio,
                              const AudioDeviceSnapshot& devices, const bool crossfade) {
        const auto microphone = resolve_audio_selection(
            desired_microphone, AudioDeviceDirection::input, devices);
        const auto system_audio = resolve_audio_selection(
            desired_system_audio, AudioDeviceDirection::output, devices);
        const auto warnings_changed = state_.microphone_warning != microphone.warning ||
                                      state_.system_audio_warning != system_audio.warning;
        const auto microphone_changed =
            microphone_branch_.key != microphone.key(AudioDeviceDirection::input);
        const auto system_audio_changed =
            system_audio_branch_.key != system_audio.key(AudioDeviceDirection::output);
        std::optional<AudioBranch> next_microphone;
        std::optional<AudioBranch> next_system_audio;
        try {
            if (microphone_changed) {
                next_microphone.emplace(create_audio_branch_locked(
                    microphone, AudioDeviceDirection::input, !crossfade));
            }
            if (system_audio_changed) {
                next_system_audio.emplace(create_audio_branch_locked(
                    system_audio, AudioDeviceDirection::output, !crossfade));
            }
        } catch (...) {
            if (next_microphone.has_value()) {
                remove_audio_branch_locked(next_microphone.value());
            }
            if (next_system_audio.has_value()) {
                remove_audio_branch_locked(next_system_audio.value());
            }
            throw;
        }
        if (crossfade && (next_microphone.has_value() || next_system_audio.has_value())) {
            constexpr int steps = 5;
            for (int step = 1; step <= steps; ++step) {
                const auto progress = static_cast<double>(step) / steps;
                if (next_microphone.has_value()) {
                    g_object_set(next_microphone->volume, "volume", progress, nullptr);
                    if (microphone_branch_.volume != nullptr) {
                        g_object_set(microphone_branch_.volume, "volume", 1.0 - progress,
                                     nullptr);
                    }
                }
                if (next_system_audio.has_value()) {
                    g_object_set(next_system_audio->volume, "volume", progress, nullptr);
                    if (system_audio_branch_.volume != nullptr) {
                        g_object_set(system_audio_branch_.volume, "volume", 1.0 - progress,
                                     nullptr);
                    }
                }
                std::this_thread::sleep_for(20ms);
            }
        }
        if (next_microphone.has_value()) {
            remove_audio_branch_locked(microphone_branch_);
            microphone_branch_ = std::move(next_microphone).value();
        }
        if (next_system_audio.has_value()) {
            remove_audio_branch_locked(system_audio_branch_);
            system_audio_branch_ = std::move(next_system_audio).value();
        }
        state_.microphone_warning = microphone.warning;
        state_.system_audio_warning = system_audio.warning;
        if (warnings_changed) {
            ++state_.generation;
        }
    }

    void build_pipeline_locked(const AudioDeviceSnapshot& devices) {
        staging_path_ = program_recording_staging_path(configuration_.path);
        pipeline_ = gst_pipeline_new("program-recording");
        if (pipeline_ == nullptr) {
            throw std::runtime_error("recording pipeline could not be created");
        }
        video_source_ = make_element("appsrc", "program-video-source");
        auto* video_encoder = make_element("mfh264enc");
        gboolean encoder_d3d11_aware = FALSE;
        if (g_object_class_find_property(G_OBJECT_GET_CLASS(video_encoder), "d3d11-aware") !=
            nullptr) {
            g_object_get(video_encoder, "d3d11-aware", &encoder_d3d11_aware, nullptr);
        }
        const auto gpu_frame = renderer_->latest_gpu_frame(OutputBus::virtual_camera);
        const auto gpu_payload =
            gpu_frame == nullptr
                ? std::shared_ptr<const GStreamerSamplePayload>{}
                : std::dynamic_pointer_cast<const GStreamerSamplePayload>(gpu_frame->payload);
        const auto* gpu_caps = gpu_payload == nullptr
                                   ? nullptr
                                   : gst_sample_get_caps(gpu_payload->sample());
        const auto* gpu_features =
            gpu_caps == nullptr || gst_caps_is_empty(gpu_caps)
                ? nullptr
                : gst_caps_get_features(gpu_caps, 0U);
        gpu_path_enabled_ = encoder_d3d11_aware != FALSE && has_factory("d3d11convert") &&
                            gpu_features != nullptr &&
                            gst_caps_features_contains(gpu_features,
                                                       "memory:D3D11Memory") != FALSE;
        auto* video_convert =
            make_element(gpu_path_enabled_ ? "d3d11convert" : "videoconvert");
        auto* video_scale = gpu_path_enabled_ ? nullptr : make_element("videoscale");
        auto* video_caps_filter = make_element("capsfilter");
        auto* video_parser = make_element("h264parse");
        auto* video_queue = make_element("queue");
        audio_mixer_ = make_element("audiomixer");
        auto* audio_convert = make_element("audioconvert");
        auto* audio_caps_filter = make_element("capsfilter");
        const auto audio_encoder_factory = program_recording_aac_encoder_factory(
            has_factory("mfaacenc"), has_factory("avenc_aac"));
        if (audio_encoder_factory.empty()) {
            throw std::runtime_error("recording AAC encoder is unavailable");
        }
        auto* audio_encoder =
            make_element(audio_encoder_factory.data());
        auto* audio_parser = make_element("aacparse");
        auto* audio_queue = make_element("queue");
        auto* muxer = make_element("mp4mux");
        auto* sink = make_element("filesink");
        const std::array<GstElement*, 13U> owned_elements{
            video_convert, video_scale, video_caps_filter, video_encoder, video_parser,
            video_queue,   audio_convert, audio_caps_filter, audio_encoder, audio_parser,
            audio_queue,   muxer, sink,
        };
        if (gst_bin_add(GST_BIN(pipeline_), video_source_) == FALSE) {
            throw std::runtime_error("recording video source could not be added");
        }
        for (auto* element : owned_elements) {
            if (element != nullptr && gst_bin_add(GST_BIN(pipeline_), element) == FALSE) {
                throw std::runtime_error("recording element could not be added");
            }
        }
        if (gst_bin_add(GST_BIN(pipeline_), audio_mixer_) == FALSE) {
            throw std::runtime_error("recording audio mixer could not be added");
        }

        g_object_set(video_source_, "is-live", TRUE, "format", GST_FORMAT_TIME,
                     "block", FALSE, "max-buffers", static_cast<guint64>(3U),
                     "max-bytes", static_cast<guint64>(0U), "max-time",
                     static_cast<guint64>(0U), "leaky-type", 2, nullptr);
        const auto bitrate_kbps = static_cast<guint>((std::clamp)(
            static_cast<std::uint64_t>(configuration_.width) * configuration_.height *
                configuration_.fps_numerator /
                configuration_.fps_denominator * 12U / 100U,
            4'000ULL, 24'000ULL));
        g_object_set(video_encoder, "bitrate", bitrate_kbps, nullptr);
        GstCaps* encoded_video_caps = gst_caps_new_simple(
            "video/x-raw", "format", G_TYPE_STRING, "NV12", "width", G_TYPE_INT,
            static_cast<int>(configuration_.width), "height", G_TYPE_INT,
            static_cast<int>(configuration_.height), "framerate", GST_TYPE_FRACTION,
            static_cast<int>(configuration_.fps_numerator),
            static_cast<int>(configuration_.fps_denominator), "pixel-aspect-ratio",
            GST_TYPE_FRACTION, 1, 1, nullptr);
        if (gpu_path_enabled_) {
            gst_caps_set_features(encoded_video_caps, 0U,
                                  gst_caps_features_new("memory:D3D11Memory", nullptr));
        }
        g_object_set(video_caps_filter, "caps", encoded_video_caps, nullptr);
        gst_caps_unref(encoded_video_caps);
        g_object_set(audio_mixer_, "latency", static_cast<guint64>(40U * GST_MSECOND), nullptr);
        const auto* audio_sample_format =
            audio_encoder_factory == "mfaacenc" ? "S16LE" : "F32LE";
        GstCaps* encoded_audio_caps = gst_caps_new_simple(
            "audio/x-raw", "format", G_TYPE_STRING, audio_sample_format, "rate", G_TYPE_INT, 48'000,
            "channels", G_TYPE_INT, 2, "layout", G_TYPE_STRING, "interleaved", nullptr);
        g_object_set(audio_caps_filter, "caps", encoded_audio_caps, nullptr);
        gst_caps_unref(encoded_audio_caps);
        g_object_set(audio_encoder, "bitrate", 192'000U, nullptr);
        g_object_set(muxer, "fragment-duration", 1'000U, "fragment-mode", 1,
                     "faststart", TRUE, nullptr);
        const auto staging_utf8 = path_utf8(staging_path_);
        g_object_set(sink, "location", staging_utf8.c_str(), "sync", FALSE, nullptr);

        require_link(video_source_, video_convert);
        if (video_scale != nullptr) {
            require_link(video_convert, video_scale);
            require_link(video_scale, video_caps_filter);
        } else {
            require_link(video_convert, video_caps_filter);
        }
        require_link(video_caps_filter, video_encoder);
        require_link(video_encoder, video_parser);
        require_link(video_parser, video_queue);
        require_link(video_queue, muxer);
        require_link(audio_mixer_, audio_convert);
        require_link(audio_convert, audio_caps_filter);
        require_link(audio_caps_filter, audio_encoder);
        require_link(audio_encoder, audio_parser);
        require_link(audio_parser, audio_queue);
        require_link(audio_queue, muxer);
        require_link(muxer, sink);

        refresh_audio_locked(microphone_, system_audio_, devices, false);
        if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
            throw std::runtime_error("recording pipeline could not enter PLAYING");
        }
    }

    void start_pump_locked() {
        first_frame_accepted_ = false;
        pump_failed_ = false;
        recording_started_at_ns_ = monotonic_nanoseconds();
        pump_ = std::jthread([this](const std::stop_token stop_token) {
            const auto frame_duration = std::chrono::nanoseconds{
                static_cast<std::int64_t>(1'000'000'000ULL * configuration_.fps_denominator /
                                          configuration_.fps_numerator)};
            auto next_frame_at = std::chrono::steady_clock::now();
            SceneOutputFrameCursor cursor{};
            std::optional<PackedVideoFrame> latest;
            std::shared_ptr<const SourceFrame> latest_gpu;
            GstCapsOwner current_source_caps{};
            std::uint64_t gpu_sequence = 0U;
            std::uint64_t output_sequence = 0U;
            GstClockTime video_pts_base = GST_CLOCK_TIME_NONE;
            std::array<std::uint64_t, 128U> frame_feed_samples{};
            std::size_t frame_feed_sample_count = 0U;
            std::size_t frame_feed_sample_cursor = 0U;
            auto next_metrics_publication = std::chrono::steady_clock::now() + 1s;
            while (!stop_token.stop_requested()) {
                if (const auto pipeline_error = poll_pipeline_error_locked();
                    pipeline_error.has_value()) {
                    signal_pump_failure(std::move(pipeline_error).value());
                    return;
                }
                bool fresh = false;
                if (gpu_path_enabled_) {
                    const auto candidate =
                        renderer_->latest_gpu_frame(OutputBus::virtual_camera);
                    if (candidate != nullptr && candidate->sequence != gpu_sequence) {
                        latest_gpu = candidate;
                        gpu_sequence = candidate->sequence;
                        fresh = true;
                    }
                } else {
                    bool frame_invalid = false;
                    const auto sequence = renderer_->visit_latest_frame(
                        OutputBus::virtual_camera, cursor,
                        [&latest, &frame_invalid](const VideoFrameView& frame) noexcept {
                            try {
                                latest = copy_video_frame(frame);
                            } catch (const std::exception&) {
                                frame_invalid = true;
                            }
                        });
                    if (frame_invalid) {
                        signal_pump_failure("recording_program_frame_invalid");
                        return;
                    }
                    fresh = sequence.has_value();
                    if (fresh) {
                        cursor = sequence.value();
                    }
                }
                if ((gpu_path_enabled_ && latest_gpu != nullptr) ||
                    (!gpu_path_enabled_ && latest.has_value())) {
                    const auto frame_feed_started_at = std::chrono::steady_clock::now();
                    const auto frame_width = gpu_path_enabled_ ? latest_gpu->width : latest->width;
                    const auto frame_height =
                        gpu_path_enabled_ ? latest_gpu->height : latest->height;
                    if (fresh) {
                        GstCaps* source_caps = nullptr;
                        if (gpu_path_enabled_) {
                            const auto payload = std::dynamic_pointer_cast<
                                const GStreamerSamplePayload>(latest_gpu->payload);
                            const auto* sample_caps =
                                payload == nullptr ? nullptr
                                                   : gst_sample_get_caps(payload->sample());
                            source_caps = sample_caps == nullptr ? nullptr
                                                                 : gst_caps_copy(sample_caps);
                        } else {
                            const auto* pixel_format =
                                latest->pixel_format == VideoFramePixelFormat::bgra
                                    ? "BGRA"
                                    : latest->pixel_format == VideoFramePixelFormat::nv12 ? "NV12"
                                                                                          : "YUY2";
                            source_caps = gst_caps_new_simple(
                                "video/x-raw", "format", G_TYPE_STRING, pixel_format, "width",
                                G_TYPE_INT, static_cast<int>(frame_width), "height",
                                G_TYPE_INT, static_cast<int>(frame_height), "framerate",
                                GST_TYPE_FRACTION,
                                static_cast<int>(configuration_.fps_numerator),
                                static_cast<int>(configuration_.fps_denominator), nullptr);
                        }
                        if (source_caps == nullptr) {
                            signal_pump_failure("recording_gpu_sample_unavailable");
                            return;
                        }
                        if (current_source_caps.get() == nullptr ||
                            gst_caps_is_equal(current_source_caps.get(), source_caps) == FALSE) {
                            gst_app_src_set_caps(GST_APP_SRC(video_source_), source_caps);
                            current_source_caps.reset(source_caps);
                        } else {
                            gst_caps_unref(source_caps);
                        }
                    }
                    GstBuffer* buffer = nullptr;
                    if (gpu_path_enabled_) {
                        const auto payload = std::dynamic_pointer_cast<
                            const GStreamerSamplePayload>(latest_gpu->payload);
                        auto* sample_buffer =
                            payload == nullptr ? nullptr
                                               : gst_sample_get_buffer(payload->sample());
                        buffer = sample_buffer == nullptr ? nullptr
                                                          : gst_buffer_copy(sample_buffer);
                    } else {
                        buffer = gst_buffer_new_allocate(nullptr, latest->bytes.size(), nullptr);
                        if (buffer != nullptr &&
                            gst_buffer_fill(buffer, 0U, latest->bytes.data(),
                                            latest->bytes.size()) != latest->bytes.size()) {
                            gst_buffer_unref(buffer);
                            buffer = nullptr;
                        }
                    }
                    if (buffer == nullptr) {
                        if (buffer != nullptr) {
                            gst_buffer_unref(buffer);
                        }
                        signal_pump_failure("recording_video_buffer_failed");
                        return;
                    }
                    if (!GST_CLOCK_TIME_IS_VALID(video_pts_base)) {
                        auto* clock = gst_element_get_clock(pipeline_);
                        const auto base_time = gst_element_get_base_time(pipeline_);
                        const auto clock_time =
                            clock == nullptr ? GST_CLOCK_TIME_NONE : gst_clock_get_time(clock);
                        if (clock != nullptr) {
                            gst_object_unref(clock);
                        }
                        video_pts_base = GST_CLOCK_TIME_IS_VALID(clock_time) &&
                                                 GST_CLOCK_TIME_IS_VALID(base_time) &&
                                                 clock_time >= base_time
                                             ? clock_time - base_time
                                             : 0U;
                    }
                    const auto pts = video_pts_base + gst_util_uint64_scale(
                        output_sequence, GST_SECOND * configuration_.fps_denominator,
                        configuration_.fps_numerator);
                    const auto duration = gst_util_uint64_scale(
                        1U, GST_SECOND * configuration_.fps_denominator,
                        configuration_.fps_numerator);
                    GST_BUFFER_PTS(buffer) = pts;
                    GST_BUFFER_DTS(buffer) = pts;
                    GST_BUFFER_DURATION(buffer) = duration;
                    const auto flow = gst_app_src_push_buffer(GST_APP_SRC(video_source_), buffer);
                    const auto frame_feed_elapsed =
                        std::chrono::steady_clock::now() - frame_feed_started_at;
                    frame_feed_samples[frame_feed_sample_cursor] =
                        static_cast<std::uint64_t>(
                            std::chrono::duration_cast<std::chrono::nanoseconds>(
                                frame_feed_elapsed)
                                .count());
                    frame_feed_sample_cursor =
                        (frame_feed_sample_cursor + 1U) % frame_feed_samples.size();
                    frame_feed_sample_count =
                        (std::min)(frame_feed_sample_count + 1U, frame_feed_samples.size());
                    {
                        std::scoped_lock state_lock{mutex_};
                        if (flow == GST_FLOW_OK) {
                            ++state_.frames_written;
                            if (!fresh && output_sequence > 0U) {
                                ++state_.frames_duplicated;
                            }
                            if (!first_frame_accepted_) {
                                first_frame_accepted_ = true;
                                first_frame_changed_.notify_all();
                            }
                        } else if (flow == GST_FLOW_FLUSHING && stop_token.stop_requested()) {
                            return;
                        } else {
                            ++state_.frames_dropped;
                            pump_failed_ = true;
                            pump_error_code_ = "recording_video_push_failed";
                            first_frame_changed_.notify_all();
                            return;
                        }
                        const auto metrics_now = std::chrono::steady_clock::now();
                        if (metrics_now >= next_metrics_publication) {
                            state_.frame_feed_p95_ns =
                                p95_nanoseconds(frame_feed_samples, frame_feed_sample_count);
                            ++state_.generation;
                            next_metrics_publication = metrics_now + 1s;
                        }
                    }
                    ++output_sequence;
                }
                next_frame_at += frame_duration;
                const auto now = std::chrono::steady_clock::now();
                if (next_frame_at <= now) {
                    next_frame_at = now + frame_duration;
                    std::scoped_lock state_lock{mutex_};
                    ++state_.frames_dropped;
                    if (now >= next_metrics_publication) {
                        ++state_.generation;
                        next_metrics_publication = now + 1s;
                    }
                } else {
                    std::this_thread::sleep_until(next_frame_at);
                }
            }
        });
    }

    [[nodiscard]] std::optional<std::string> poll_pipeline_error_locked() const noexcept {
        if (pipeline_ == nullptr) {
            return std::nullopt;
        }
        auto* bus = gst_element_get_bus(pipeline_);
        if (bus == nullptr) {
            return std::nullopt;
        }
        auto* message = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR);
        gst_object_unref(bus);
        if (message == nullptr) {
            return std::nullopt;
        }
        GError* error = nullptr;
        gchar* debug = nullptr;
        gst_message_parse_error(message, &error, &debug);
        std::string code = "recording_pipeline_failed";
        if (error != nullptr && error->domain == GST_RESOURCE_ERROR &&
            error->code == GST_RESOURCE_ERROR_NO_SPACE_LEFT) {
            code = "recording_disk_full";
        } else if (error != nullptr && error->domain == GST_RESOURCE_ERROR) {
            code = "recording_output_failed";
        } else if (error != nullptr && error->domain == GST_STREAM_ERROR) {
            code = "recording_encoder_failed";
        }
        if (error != nullptr) {
            g_error_free(error);
        }
        g_free(debug);
        gst_message_unref(message);
        return code;
    }

    void signal_pump_failure(std::string code) {
        std::scoped_lock lock{mutex_};
        pump_failed_ = true;
        pump_error_code_ = std::move(code);
        if (state_.status == ProgramRecordingStatus::recording) {
            fail_locked(pump_error_code_, "The Program video stream failed");
        }
        first_frame_changed_.notify_all();
    }

    [[nodiscard]] ProgramRecordingOperationResult
    cleanup_locked(const bool finalize, const bool publish_final = true) noexcept {
        if (pump_.joinable()) {
            pump_.request_stop();
            // Do not join while holding the state mutex: the pump may be publishing
            // its final counters. Temporarily release through a dedicated stop path.
            mutex_.unlock();
            pump_.join();
            mutex_.lock();
        }
        if (system_memory_enabled_) {
            renderer_->set_system_memory_output_enabled(
                OutputBus::virtual_camera, SystemMemoryOutputConsumer::program_recording, false);
            system_memory_enabled_ = false;
        }
        bool eos_received = !finalize;
        if (pipeline_ != nullptr && finalize) {
            gst_element_send_event(pipeline_, gst_event_new_eos());
            auto* bus = gst_element_get_bus(pipeline_);
            if (bus != nullptr) {
                auto* message = gst_bus_timed_pop_filtered(
                    bus, static_cast<GstClockTime>(kFinalizationTimeout.count()) * GST_SECOND,
                    static_cast<GstMessageType>(GST_MESSAGE_EOS | GST_MESSAGE_ERROR));
                eos_received = message != nullptr && GST_MESSAGE_TYPE(message) == GST_MESSAGE_EOS;
                if (message != nullptr) {
                    gst_message_unref(message);
                }
                gst_object_unref(bus);
            }
        }
        if (pipeline_ != nullptr) {
            gst_element_set_state(pipeline_, GST_STATE_NULL);
            gst_object_unref(pipeline_);
            pipeline_ = nullptr;
        }
        video_source_ = nullptr;
        audio_mixer_ = nullptr;
        microphone_branch_ = {};
        system_audio_branch_ = {};
        if (finalize && !eos_received) {
            return rejected("recording_finalization_timeout",
                            "The partial MP4 could not be finalized in time");
        }
        if (finalize && !staging_path_.empty()) {
            std::error_code error;
            if (!std::filesystem::exists(staging_path_, error) || error) {
                return rejected("recording_output_missing",
                                "The recording output was not created");
            }
            if (publish_final) {
                std::filesystem::rename(staging_path_, configuration_.path, error);
                if (error) {
                    return rejected("recording_output_rename_failed",
                                    "The finalized recording could not be renamed");
                }
            }
        }
        staging_path_.clear();
        gpu_path_enabled_ = false;
        pump_failed_ = false;
        pump_error_code_.clear();
        return applied();
    }
#else
    [[nodiscard]] ProgramRecordingOperationResult cleanup_locked(bool, bool = true) noexcept {
        return applied();
    }
#endif

    std::shared_ptr<SceneRenderer> renderer_{};
    mutable std::mutex mutex_{};
    std::condition_variable first_frame_changed_{};
    ProgramRecordingConfiguration configuration_{};
    RecordingAudioSelection microphone_{};
    RecordingAudioSelection system_audio_{};
    ProgramRecordingState state_{};
    bool system_memory_enabled_{false};
    bool gpu_path_enabled_{false};
    bool first_frame_accepted_{false};
    bool pump_failed_{false};
    std::string pump_error_code_{};
    std::uint64_t recording_started_at_ns_{0U};
    std::jthread pump_{};
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    GstElement* pipeline_{nullptr};
    GstElement* video_source_{nullptr};
    GstElement* audio_mixer_{nullptr};
    AudioBranch microphone_branch_{};
    AudioBranch system_audio_branch_{};
    std::filesystem::path staging_path_{};
#endif
};

ProgramRecordingController::ProgramRecordingController(std::shared_ptr<SceneRenderer> renderer)
    : impl_(std::make_unique<Impl>(std::move(renderer))) {}

ProgramRecordingController::~ProgramRecordingController() = default;

bool ProgramRecordingController::supported() const noexcept { return impl_->supported(); }

ProgramRecordingOperationResult ProgramRecordingController::start(
    const ProgramRecordingConfiguration& configuration, const AudioDeviceSnapshot& devices) {
    return impl_->start(configuration, devices);
}

ProgramRecordingOperationResult ProgramRecordingController::set_audio(
    const RecordingAudioSelection& microphone,
    const RecordingAudioSelection& system_audio, const AudioDeviceSnapshot& devices) {
    return impl_->set_audio(microphone, system_audio, devices);
}

ProgramRecordingOperationResult ProgramRecordingController::stop() { return impl_->stop(); }

void ProgramRecordingController::refresh_audio_devices(const AudioDeviceSnapshot& devices) {
    impl_->refresh_audio_devices(devices);
}

ProgramRecordingState ProgramRecordingController::state() const { return impl_->state(); }

void ProgramRecordingController::shutdown() noexcept { impl_->shutdown(); }

} // namespace solin::media_engine
