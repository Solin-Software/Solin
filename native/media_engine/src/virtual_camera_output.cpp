#include "solin/media_engine/virtual_camera_output.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace solin::media_engine {
namespace {

[[nodiscard]] VirtualCameraConfiguration camera_configuration(
    const SceneOutputDefinition& output) {
    if (output.bus != OutputBus::virtual_camera) {
        throw std::invalid_argument("virtual_camera_output_bus_invalid");
    }
    auto video_format = output.video_format;
    // The Windows Frame Server source has one canonical wire format. Renderer
    // conversion happens before this boundary and never inside the COM source.
    video_format.pixel_format = "nv12";
    video_format.color_space = "bt709";
    video_format.color_range = "limited";
    return {
        .camera_id = std::string{kSolinVirtualCameraId},
        .friendly_name = std::string{kSolinVirtualCameraFriendlyName},
        .video_format = std::move(video_format),
    };
}

[[nodiscard]] std::chrono::microseconds pump_interval(
    const OutputVideoFormat& format) noexcept {
    if (format.fps_numerator == 0U || format.fps_denominator == 0U) {
        return std::chrono::milliseconds{4};
    }
    const auto frame_duration_us =
        1'000'000ULL * format.fps_denominator / format.fps_numerator;
    const auto interval_us =
        std::clamp<std::uint64_t>(frame_duration_us / 4U, 1'000U, 4'000U);
    return std::chrono::microseconds{interval_us};
}

} // namespace

class VirtualCameraOutputController::Impl final {
  public:
    Impl(std::shared_ptr<SceneRenderer> renderer,
         std::unique_ptr<VirtualCameraBackend> backend)
        : renderer_(std::move(renderer)), backend_(std::move(backend)) {
        if (renderer_ == nullptr || backend_ == nullptr) {
            throw std::invalid_argument("virtual_camera_output_dependency_required");
        }
    }

    ~Impl() { shutdown(); }

    [[nodiscard]] bool configure(
        const SceneOutputDefinition& output) noexcept {
        try {
            auto next = camera_configuration(output);
            std::scoped_lock lock{mutex_};
            if (configuration_ == next) {
                return sink_ == nullptr ||
                       sink_->health().state != VirtualCameraSinkState::failed;
            }
            const auto restart = desired_enabled_;
            stop_locked();
            configuration_ = std::move(next);
            return !restart || start_locked();
        } catch (const std::exception& error) {
            record_failure(error.what());
            return false;
        } catch (...) {
            record_failure("virtual_camera_output_configuration_failed");
            return false;
        }
    }

    [[nodiscard]] bool set_enabled(const bool enabled) noexcept {
        try {
            std::scoped_lock lock{mutex_};
            desired_enabled_ = enabled;
            if (!enabled) {
                stop_locked();
                return true;
            }
            if (sink_ != nullptr) {
                const auto state = sink_->health().state;
                if (state == VirtualCameraSinkState::ready ||
                    state == VirtualCameraSinkState::starting) {
                    return true;
                }
            }
            return start_locked();
        } catch (const std::exception& error) {
            record_failure(error.what());
            return false;
        } catch (...) {
            record_failure("virtual_camera_output_start_failed");
            return false;
        }
    }

    [[nodiscard]] VirtualCameraBackendProbe probe() const noexcept {
        try {
            return backend_->probe();
        } catch (...) {
            return {
                .error_code = "virtual_camera_probe_failed",
            };
        }
    }

    [[nodiscard]] VirtualCameraSinkHealth health() const noexcept {
        try {
            std::scoped_lock lock{mutex_};
            return sink_ == nullptr ? health_ : sink_->health();
        } catch (...) {
            return {
                .state = VirtualCameraSinkState::failed,
                .error_code = "virtual_camera_health_unavailable",
            };
        }
    }

    void shutdown() noexcept {
        try {
            std::scoped_lock lock{mutex_};
            desired_enabled_ = false;
            stop_locked();
        } catch (...) {
        }
    }

  private:
    [[nodiscard]] bool start_locked() {
        if (!configuration_.has_value()) {
            health_.state = VirtualCameraSinkState::failed;
            health_.error_code = "virtual_camera_output_not_configured";
            return false;
        }
        const auto capability = backend_->probe();
        if (!capability.operational) {
            health_.state = VirtualCameraSinkState::failed;
            health_.error_code = capability.error_code;
            return false;
        }
        if (sink_ != nullptr) {
            stop_locked();
        }
        auto unique_sink = backend_->create_sink(configuration_.value());
        if (unique_sink == nullptr) {
            throw std::runtime_error("virtual_camera_sink_unavailable");
        }
        std::shared_ptr<VirtualCameraSink> next_sink{std::move(unique_sink)};
        next_sink->start();
        const auto interval = pump_interval(configuration_->video_format);
        pump_ = std::jthread(
            [renderer = renderer_, sink = next_sink,
             interval](const std::stop_token stop_token) {
                std::uint64_t last_sequence = 0U;
                while (!stop_token.stop_requested()) {
                    if (auto sequence = renderer->visit_latest_frame(
                            OutputBus::virtual_camera, last_sequence,
                            [&sink](const VideoFrameView& frame) {
                                static_cast<void>(sink->publish(frame));
                            });
                        sequence.has_value()) {
                        last_sequence = sequence.value();
                    }
                    sink->heartbeat();
                    std::this_thread::sleep_for(interval);
                }
            });
        sink_ = std::move(next_sink);
        health_ = sink_->health();
        return health_.state == VirtualCameraSinkState::ready ||
               health_.state == VirtualCameraSinkState::starting;
    }

    void stop_locked() noexcept {
        if (pump_.joinable()) {
            pump_.request_stop();
            pump_.join();
        }
        if (sink_ != nullptr) {
            sink_->stop();
            sink_.reset();
        }
        health_.state = VirtualCameraSinkState::stopped;
        health_.error_code.clear();
    }

    void record_failure(const std::string_view error_code) noexcept {
        try {
            std::scoped_lock lock{mutex_};
            stop_locked();
            health_.state = VirtualCameraSinkState::failed;
            health_.error_code =
                error_code.empty() ? "virtual_camera_output_failed"
                                   : std::string{error_code.substr(0U, 128U)};
        } catch (...) {
        }
    }

    std::shared_ptr<SceneRenderer> renderer_{};
    std::unique_ptr<VirtualCameraBackend> backend_{};
    mutable std::mutex mutex_{};
    std::optional<VirtualCameraConfiguration> configuration_{};
    bool desired_enabled_{false};
    std::shared_ptr<VirtualCameraSink> sink_{};
    std::jthread pump_{};
    VirtualCameraSinkHealth health_{};
};

VirtualCameraOutputController::VirtualCameraOutputController(
    std::shared_ptr<SceneRenderer> renderer,
    std::unique_ptr<VirtualCameraBackend> backend)
    : impl_(std::make_unique<Impl>(std::move(renderer), std::move(backend))) {}

VirtualCameraOutputController::~VirtualCameraOutputController() = default;

bool VirtualCameraOutputController::configure(
    const SceneOutputDefinition& output) noexcept {
    return impl_->configure(output);
}

bool VirtualCameraOutputController::set_enabled(const bool enabled) noexcept {
    return impl_->set_enabled(enabled);
}

VirtualCameraBackendProbe VirtualCameraOutputController::probe() const noexcept {
    return impl_->probe();
}

VirtualCameraSinkHealth VirtualCameraOutputController::health() const noexcept {
    return impl_->health();
}

void VirtualCameraOutputController::shutdown() noexcept { impl_->shutdown(); }

} // namespace solin::media_engine
