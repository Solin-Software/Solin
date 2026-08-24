#include "solin/media_engine/virtual_camera_output.hpp"

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
    // Every platform backend receives the engine's canonical NV12/BT.709-limited
    // wire format. Consumer-specific DirectShow adaptation stays in the filter.
    video_format.pixel_format = "nv12";
    video_format.color_space = "bt709";
    video_format.color_range = "limited";
    return {
        .camera_id = std::string{kSolinVirtualCameraId},
        .friendly_name = std::string{kSolinVirtualCameraFriendlyName},
        .video_format = std::move(video_format),
    };
}

constexpr auto kHeartbeatInterval = std::chrono::milliseconds{500};

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
        set_system_memory_output_enabled_locked(true);
        try {
            pump_ = std::jthread(
                [renderer = renderer_, sink = next_sink](
                    const std::stop_token stop_token) {
                    std::uint64_t last_sequence = 0U;
                    auto next_heartbeat =
                        std::chrono::steady_clock::now() + kHeartbeatInterval;
                    while (!stop_token.stop_requested()) {
                        bool published = false;
                        if (auto sequence = renderer->visit_latest_frame(
                                OutputBus::virtual_camera, last_sequence,
                                [&sink, &published](const VideoFrameView& frame) {
                                    published = sink->publish(frame);
                                });
                            sequence.has_value()) {
                            last_sequence = sequence.value();
                        }
                        const auto now = std::chrono::steady_clock::now();
                        if (published) {
                            next_heartbeat = now + kHeartbeatInterval;
                        } else if (now >= next_heartbeat) {
                            sink->heartbeat();
                            next_heartbeat = now + kHeartbeatInterval;
                        }
                        static_cast<void>(renderer->wait_for_frame(
                            OutputBus::virtual_camera, last_sequence, stop_token,
                            next_heartbeat));
                    }
                });
        } catch (...) {
            set_system_memory_output_enabled_locked(false);
            next_sink->stop();
            throw;
        }
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
        set_system_memory_output_enabled_locked(false);
        health_.state = VirtualCameraSinkState::stopped;
        health_.error_code.clear();
    }

    void set_system_memory_output_enabled_locked(const bool enabled) noexcept {
        if (system_memory_output_enabled_ == enabled) {
            return;
        }
        renderer_->set_system_memory_output_enabled(
            OutputBus::virtual_camera,
            SystemMemoryOutputConsumer::virtual_camera, enabled);
        system_memory_output_enabled_ = enabled;
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
    bool system_memory_output_enabled_{false};
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
