#include "solin/media_engine/frame_channel_output.hpp"

#include "solin/media_engine/frame_channel.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <exception>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <thread>
#include <utility>

namespace solin::media_engine {
namespace {

[[nodiscard]] std::chrono::microseconds pump_interval(
    const OutputVideoFormat& format) noexcept {
    if (format.fps_numerator == 0U || format.fps_denominator == 0U) {
        return std::chrono::milliseconds{4};
    }
    const auto frame_duration_us =
        1'000'000ULL * format.fps_denominator / format.fps_numerator;
    return std::chrono::microseconds{
        std::clamp<std::uint64_t>(frame_duration_us / 4U, 1'000U, 4'000U)};
}

[[nodiscard]] bool valid_configuration(
    const FrameChannelConfiguration& channel,
    const SceneOutputDefinition& output, const OutputBus bus) noexcept {
    const auto fixed_bgra = output.video_format.pixel_format == "bgra" &&
                            channel.transport == "shared_memory_bgra" &&
                            channel.pixel_format == "bgra";
    const auto dynamic_video = output.video_format.pixel_format == "nv12" &&
                               channel.transport == "shared_memory_video" &&
                               channel.pixel_format == "dynamic";
    return output.bus == bus && (fixed_bgra || dynamic_video) &&
           channel.producer_kind == "native_compositor" &&
           channel.width == output.video_format.width &&
           channel.height == output.video_format.height;
}

} // namespace

class FrameChannelOutputController::Impl final {
  public:
    Impl(std::shared_ptr<SceneRenderer> renderer, const OutputBus bus,
         const std::optional<std::uint32_t> maximum_frames_per_second)
        : renderer_(std::move(renderer)), bus_(bus),
          maximum_frames_per_second_(maximum_frames_per_second) {
        if (renderer_ == nullptr) {
            throw std::invalid_argument("frame_channel_output_renderer_required");
        }
        if (maximum_frames_per_second_.has_value() &&
            (maximum_frames_per_second_.value() == 0U ||
             maximum_frames_per_second_.value() > 1'000'000'000U)) {
            throw std::invalid_argument("frame_channel_output_cadence_invalid");
        }
    }

    ~Impl() { shutdown(); }

    [[nodiscard]] bool configure(
        const std::optional<FrameChannelConfiguration>& channel,
        const SceneOutputDefinition& output) noexcept {
        try {
            std::scoped_lock lock{mutex_};
            if (!channel.has_value()) {
                stop_locked();
                channel_.reset();
                format_ = output.video_format;
                return true;
            }
            if (!valid_configuration(channel.value(), output, bus_)) {
                return false;
            }
            if (channel_ == channel && format_ == output.video_format) {
                return !desired_enabled_ || writer_ != nullptr;
            }
            stop_locked();
            channel_ = channel;
            format_ = output.video_format;
            return !desired_enabled_ || start_locked();
        } catch (...) {
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
            if (!channel_.has_value()) {
                return true;
            }
            return writer_ != nullptr || start_locked();
        } catch (...) {
            return false;
        }
    }

    void shutdown() noexcept {
        try {
            std::scoped_lock lock{mutex_};
            desired_enabled_ = false;
            stop_locked();
            channel_.reset();
        } catch (...) {
        }
    }

  private:
    [[nodiscard]] bool start_locked() {
        if (!channel_.has_value()) {
            return true;
        }
        auto unique_writer = make_frame_channel_writer(channel_.value());
        std::shared_ptr<FrameChannelWriter> next_writer{std::move(unique_writer)};
        const auto interval = pump_interval(format_);
        const auto minimum_publication_interval =
            maximum_frames_per_second_.has_value()
                ? std::chrono::nanoseconds{
                      (1'000'000'000ULL +
                       maximum_frames_per_second_.value() - 1U) /
                      maximum_frames_per_second_.value()}
                : std::chrono::nanoseconds::zero();
        pump_ = std::jthread(
            [renderer = renderer_, writer = next_writer, bus = bus_,
             interval, minimum_publication_interval](
                const std::stop_token stop_token) {
                std::uint64_t last_sequence = 0U;
                auto next_publication_at = std::chrono::steady_clock::time_point::min();
                while (!stop_token.stop_requested()) {
                    const auto now = std::chrono::steady_clock::now();
                    if (now < next_publication_at) {
                        std::this_thread::sleep_for(interval);
                        continue;
                    }
                    std::exception_ptr failure;
                    bool published = false;
                    if (auto sequence = renderer->visit_latest_frame(
                            bus, last_sequence,
                            [&writer, &failure, &published](
                                const VideoFrameView& frame) {
                                try {
                                    published = writer->publish(frame);
                                } catch (...) {
                                    failure = std::current_exception();
                                }
                            });
                        sequence.has_value()) {
                        last_sequence = sequence.value();
                    }
                    if (failure != nullptr) {
                        return;
                    }
                    if (published && minimum_publication_interval.count() > 0) {
                        const auto published_at = std::chrono::steady_clock::now();
                        if (next_publication_at ==
                            std::chrono::steady_clock::time_point::min()) {
                            next_publication_at =
                                published_at + minimum_publication_interval;
                        } else {
                            const auto following_deadline =
                                next_publication_at + minimum_publication_interval;
                            // Preserve the rational average through ordinary
                            // scheduler jitter, but rebase after a full missed
                            // interval so a stalled reader never emits a catch-up
                            // burst.
                            next_publication_at =
                                published_at >= following_deadline
                                    ? published_at + minimum_publication_interval
                                    : following_deadline;
                        }
                    }
                    std::this_thread::sleep_for(interval);
                }
            });
        writer_ = std::move(next_writer);
        return true;
    }

    void stop_locked() noexcept {
        if (pump_.joinable()) {
            pump_.request_stop();
            pump_.join();
        }
        writer_.reset();
    }

    std::shared_ptr<SceneRenderer> renderer_{};
    OutputBus bus_{OutputBus::media_windows};
    std::optional<std::uint32_t> maximum_frames_per_second_{};
    std::mutex mutex_{};
    std::optional<FrameChannelConfiguration> channel_{};
    OutputVideoFormat format_{};
    bool desired_enabled_{false};
    std::shared_ptr<FrameChannelWriter> writer_{};
    std::jthread pump_{};
};

FrameChannelOutputController::FrameChannelOutputController(
    std::shared_ptr<SceneRenderer> renderer, const OutputBus bus,
    const std::optional<std::uint32_t> maximum_frames_per_second)
    : impl_(std::make_unique<Impl>(std::move(renderer), bus,
                                  maximum_frames_per_second)) {}

FrameChannelOutputController::~FrameChannelOutputController() = default;

bool FrameChannelOutputController::configure(
    const std::optional<FrameChannelConfiguration>& channel,
    const SceneOutputDefinition& output) noexcept {
    return impl_->configure(channel, output);
}

bool FrameChannelOutputController::set_enabled(const bool enabled) noexcept {
    return impl_->set_enabled(enabled);
}

void FrameChannelOutputController::shutdown() noexcept { impl_->shutdown(); }

} // namespace solin::media_engine
