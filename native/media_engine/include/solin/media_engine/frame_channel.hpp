#pragma once

#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

namespace solin::media_engine {

struct FrameChannelFrame final {
    std::uint64_t sequence{0U};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t produced_monotonic_ns{0U};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
    std::vector<std::uint8_t> bytes{};
};

class FrameChannelReader {
  public:
    virtual ~FrameChannelReader() = default;

    [[nodiscard]] virtual std::optional<FrameChannelFrame> read_latest() = 0;

  protected:
    FrameChannelReader() = default;
};

class FrameChannelWriter {
  public:
    virtual ~FrameChannelWriter() = default;

    FrameChannelWriter(const FrameChannelWriter&) = delete;
    FrameChannelWriter& operator=(const FrameChannelWriter&) = delete;
    FrameChannelWriter(FrameChannelWriter&&) = delete;
    FrameChannelWriter& operator=(FrameChannelWriter&&) = delete;

    // Returns false when the consumer owns the bounded channel lock. Output is
    // latest-frame, so producers drop instead of blocking the compositor.
    [[nodiscard]] virtual bool publish(const VideoFrameView& frame) = 0;

  protected:
    FrameChannelWriter() = default;
};

[[nodiscard]] std::unique_ptr<FrameChannelReader>
make_frame_channel_reader(const FrameChannelConfiguration& configuration);

[[nodiscard]] std::unique_ptr<FrameChannelWriter>
make_frame_channel_writer(const FrameChannelConfiguration& configuration);

} // namespace solin::media_engine
