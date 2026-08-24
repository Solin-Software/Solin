#include "solin/media_engine/virtual_camera.hpp"

#include "solin/media_engine/shared_video_frame_channel.hpp"

#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>

#ifdef _WIN32
#include "windows_virtual_camera_backend.hpp"
#endif

namespace solin::media_engine {
namespace {

[[nodiscard]] VideoFramePixelFormat pixel_format_for(
    const OutputVideoFormat& format) {
    if (format.pixel_format == "nv12") {
        return VideoFramePixelFormat::nv12;
    }
    if (format.pixel_format == "bgra") {
        return VideoFramePixelFormat::bgra;
    }
    throw std::invalid_argument("virtual_camera_format_invalid");
}

void validate_sink_configuration(const VirtualCameraConfiguration& configuration,
                                 const std::uint64_t generation) {
    if (configuration.camera_id.empty() || configuration.camera_id.size() > 128U ||
        configuration.friendly_name.empty() ||
        configuration.friendly_name.size() > 256U || generation == 0U ||
        configuration.video_format.fps_numerator == 0U ||
        configuration.video_format.fps_denominator == 0U ||
        configuration.video_format.fps_denominator > 1'001U ||
        configuration.video_format.fps_numerator >
            240U * configuration.video_format.fps_denominator) {
        throw std::invalid_argument("virtual_camera_configuration_invalid");
    }
    static_cast<void>(packed_video_frame_layout(
        configuration.video_format.width, configuration.video_format.height,
        pixel_format_for(configuration.video_format)));
}

class SharedFrameVirtualCameraSinkImpl final : public SharedFrameVirtualCameraSink {
  public:
    SharedFrameVirtualCameraSinkImpl(VirtualCameraConfiguration configuration,
                                     const std::uint64_t generation)
        : configuration_(std::move(configuration)), generation_(generation),
          layout_(packed_video_frame_layout(
              configuration_.video_format.width,
              configuration_.video_format.height,
              pixel_format_for(configuration_.video_format))) {
        validate_sink_configuration(configuration_, generation_);
    }

    ~SharedFrameVirtualCameraSinkImpl() override { stop(); }

    void start() override {
        std::scoped_lock lock{mutex_};
        if (publisher_ != nullptr) {
            return;
        }
        health_.state = VirtualCameraSinkState::starting;
        health_.error_code.clear();
        try {
            publisher_ = make_cross_process_shared_video_frame_publisher({
                .generation = generation_,
                .layout = layout_,
            });
            health_.state = VirtualCameraSinkState::ready;
        } catch (...) {
            health_.state = VirtualCameraSinkState::failed;
            health_.error_code = "virtual_camera_frame_transport_unavailable";
            throw;
        }
    }

    [[nodiscard]] bool publish(const VideoFrameView& frame) noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (publisher_ == nullptr || health_.state == VirtualCameraSinkState::stopped ||
                health_.state == VirtualCameraSinkState::failed) {
                ++health_.dropped_frames;
                return false;
            }
            try {
                static_cast<void>(publisher_->publish(frame));
            } catch (const std::invalid_argument&) {
                ++health_.dropped_frames;
                health_.state = VirtualCameraSinkState::degraded;
                health_.error_code = "virtual_camera_frame_mismatch";
                return false;
            } catch (...) {
                ++health_.dropped_frames;
                health_.state = VirtualCameraSinkState::failed;
                health_.error_code = "virtual_camera_frame_transport_failed";
                return false;
            }
            ++health_.published_frames;
            health_.last_frame_sequence = frame.sequence;
            health_.state = VirtualCameraSinkState::ready;
            health_.error_code.clear();
            return true;
        } catch (...) {
            return false;
        }
    }

    void heartbeat() noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (publisher_ != nullptr &&
                health_.state != VirtualCameraSinkState::stopped &&
                health_.state != VirtualCameraSinkState::failed) {
                publisher_->heartbeat();
            }
        } catch (...) {
        }
    }

    void stop() noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            publisher_.reset();
            health_.state = VirtualCameraSinkState::stopped;
            health_.error_code.clear();
        } catch (...) {
        }
    }

    [[nodiscard]] VirtualCameraSinkHealth health() const override {
        std::scoped_lock lock{mutex_};
        return health_;
    }

    [[nodiscard]] VirtualCameraFrameEndpoint endpoint() const noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (publisher_ == nullptr || health_.state == VirtualCameraSinkState::stopped ||
                health_.state == VirtualCameraSinkState::failed) {
                return {};
            }
            return {
                .generation = generation_,
                .layout = layout_,
                .fps_numerator = configuration_.video_format.fps_numerator,
                .fps_denominator = configuration_.video_format.fps_denominator,
                .process_local_mapping_handle = publisher_->native_mapping_handle(),
                .mapping_size = publisher_->mapping_size(),
                .cross_process_file_path_utf8 =
                    std::string{publisher_->backing_file_path_utf8()},
            };
        } catch (...) {
            return {};
        }
    }

  private:
    VirtualCameraConfiguration configuration_{};
    std::uint64_t generation_{0U};
    PackedVideoFrameLayout layout_{};
    mutable std::mutex mutex_{};
    std::unique_ptr<SharedVideoFramePublisher> publisher_{};
    VirtualCameraSinkHealth health_{};
};

#ifndef _WIN32

class UnavailableVirtualCameraBackend final : public VirtualCameraBackend {
  public:
    [[nodiscard]] VirtualCameraBackendProbe probe() const override { return {}; }

    [[nodiscard]] std::unique_ptr<VirtualCameraSink> create_sink(
        const VirtualCameraConfiguration& configuration) override {
        static_cast<void>(configuration);
        throw std::runtime_error("virtual_camera_platform_unsupported");
    }
};

#endif

} // namespace

VirtualCameraBackendProbe probe_platform_virtual_camera() noexcept {
#ifdef _WIN32
    return probe_windows_directshow_virtual_camera();
#else
    return {};
#endif
}

std::unique_ptr<VirtualCameraBackend> make_platform_virtual_camera_backend() {
#ifdef _WIN32
    return make_windows_directshow_virtual_camera_backend();
#else
    return std::make_unique<UnavailableVirtualCameraBackend>();
#endif
}

std::unique_ptr<SharedFrameVirtualCameraSink>
make_shared_frame_virtual_camera_sink(
    const VirtualCameraConfiguration& configuration, const std::uint64_t generation) {
    validate_sink_configuration(configuration, generation);
    return std::make_unique<SharedFrameVirtualCameraSinkImpl>(configuration, generation);
}

} // namespace solin::media_engine
