#pragma once

#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <chrono>
#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <utility>

namespace solin::media_engine {

struct FrameChannelFrame final {
    std::uint64_t generation{0U};
    std::uint64_t sequence{0U};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t produced_monotonic_ns{0U};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
    std::array<std::uint32_t, 2U> plane_strides{};
    std::array<std::uint64_t, 2U> plane_offsets{};
    std::span<const std::uint8_t> bytes{};
};

class FrameChannelFrameLease final {
  public:
    FrameChannelFrameLease(const FrameChannelFrameLease&) = delete;
    FrameChannelFrameLease& operator=(const FrameChannelFrameLease&) = delete;
    FrameChannelFrameLease(FrameChannelFrameLease&&) noexcept = default;
    FrameChannelFrameLease& operator=(FrameChannelFrameLease&&) noexcept = default;
    ~FrameChannelFrameLease() = default;

    [[nodiscard]] const FrameChannelFrame& frame() const noexcept { return frame_; }

  private:
    FrameChannelFrameLease(FrameChannelFrame frame, std::shared_ptr<void> lifetime)
        : frame_(frame), lifetime_(std::move(lifetime)) {}

    FrameChannelFrame frame_{};
    std::shared_ptr<void> lifetime_{};

    friend class FrameChannelReader;
};

class FrameChannelReader {
  public:
    virtual ~FrameChannelReader() = default;

    [[nodiscard]] virtual std::optional<FrameChannelFrameLease>
    read_latest(std::uint64_t after_sequence = 0U) = 0;
    [[nodiscard]] virtual bool wait_for_frame(std::chrono::milliseconds timeout) = 0;
    virtual void wake() noexcept = 0;

  protected:
    FrameChannelReader() = default;

    [[nodiscard]] static FrameChannelFrameLease
    make_lease(FrameChannelFrame frame, std::shared_ptr<void> lifetime) {
        return FrameChannelFrameLease{frame, std::move(lifetime)};
    }
};

class FrameChannelWriter {
  public:
    virtual ~FrameChannelWriter() = default;

    FrameChannelWriter(const FrameChannelWriter&) = delete;
    FrameChannelWriter& operator=(const FrameChannelWriter&) = delete;
    FrameChannelWriter(FrameChannelWriter&&) = delete;
    FrameChannelWriter& operator=(FrameChannelWriter&&) = delete;

    // Returns false when the bounded channel lock is busy or every slot is
    // leased. Output is latest-frame, so producers drop instead of blocking.
    [[nodiscard]] virtual bool publish(const VideoFrameView& frame) = 0;

  protected:
    FrameChannelWriter() = default;
};

[[nodiscard]] std::unique_ptr<FrameChannelReader>
make_frame_channel_reader(const FrameChannelConfiguration& configuration);

[[nodiscard]] std::unique_ptr<FrameChannelWriter>
make_frame_channel_writer(const FrameChannelConfiguration& configuration);

} // namespace solin::media_engine
