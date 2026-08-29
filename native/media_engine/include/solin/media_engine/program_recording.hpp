#pragma once

#include "solin/media_engine/scene_renderer.hpp"

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

inline constexpr std::uint32_t kMaximumProgramRecordingDimension = 3'840U;
inline constexpr std::uint64_t kMaximumProgramRecordingPixels = 3'840ULL * 2'160ULL;
inline constexpr std::uint32_t kMaximumProgramRecordingFramesPerSecond = 60U;

enum class AudioDeviceDirection : std::uint8_t {
    input,
    output,
};

struct AudioDevice final {
    std::string device_id{};
    std::string display_name{};
    AudioDeviceDirection direction{AudioDeviceDirection::input};
    bool is_default{false};

    bool operator==(const AudioDevice&) const = default;
};

struct AudioDeviceSnapshot final {
    bool supported{false};
    bool ready{true};
    std::uint64_t generation{0U};
    std::vector<AudioDevice> devices{};
    std::string error_code{"media_runtime_unavailable"};
};

enum class RecordingAudioSelectionMode : std::uint8_t {
    system_default,
    none,
    device,
};

struct RecordingAudioSelection final {
    RecordingAudioSelectionMode mode{RecordingAudioSelectionMode::system_default};
    std::string device_id{};

    bool operator==(const RecordingAudioSelection&) const = default;
};

struct ProgramRecordingConfiguration final {
    std::filesystem::path path{};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{1U};
    RecordingAudioSelection microphone{};
    RecordingAudioSelection system_audio{};
};

enum class ProgramRecordingStatus : std::uint8_t {
    idle,
    starting,
    recording,
    stopping,
    failed,
};

struct ProgramRecordingState final {
    std::uint64_t generation{0U};
    ProgramRecordingStatus status{ProgramRecordingStatus::idle};
    std::uint64_t started_at_monotonic_ns{0U};
    std::filesystem::path path{};
    std::string error_code{};
    std::string error_message{};
    std::string microphone_warning{};
    std::string system_audio_warning{};
    std::uint64_t frames_written{0U};
    std::uint64_t frames_duplicated{0U};
    std::uint64_t frames_dropped{0U};
    std::uint64_t frame_feed_p95_ns{0U};

    bool operator==(const ProgramRecordingState&) const = default;
};

struct ProgramRecordingOperationResult final {
    bool applied{false};
    std::string error_code{};
    std::string error_message{};
};

[[nodiscard]] std::string_view
program_recording_status_text(ProgramRecordingStatus status) noexcept;
[[nodiscard]] std::filesystem::path
program_recording_staging_path(const std::filesystem::path& final_path);
[[nodiscard]] bool program_recording_runtime_supported() noexcept;
[[nodiscard]] std::string_view program_recording_aac_encoder_factory(
    bool media_foundation_available, bool libav_available) noexcept;
void validate_program_recording_configuration(
    const ProgramRecordingConfiguration& configuration);

class ProgramRecordingController final {
  public:
    explicit ProgramRecordingController(std::shared_ptr<SceneRenderer> renderer);
    ~ProgramRecordingController();

    ProgramRecordingController(const ProgramRecordingController&) = delete;
    ProgramRecordingController& operator=(const ProgramRecordingController&) = delete;

    [[nodiscard]] bool supported() const noexcept;
    [[nodiscard]] ProgramRecordingOperationResult
    start(const ProgramRecordingConfiguration& configuration,
          const AudioDeviceSnapshot& devices);
    [[nodiscard]] ProgramRecordingOperationResult
    set_audio(const RecordingAudioSelection& microphone,
              const RecordingAudioSelection& system_audio,
              const AudioDeviceSnapshot& devices);
    [[nodiscard]] ProgramRecordingOperationResult stop();
    void refresh_audio_devices(const AudioDeviceSnapshot& devices);
    [[nodiscard]] ProgramRecordingState state() const;
    void shutdown() noexcept;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace solin::media_engine
