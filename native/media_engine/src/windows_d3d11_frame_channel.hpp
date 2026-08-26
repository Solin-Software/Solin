#pragma once

#include "solin/media_engine/frame_channel.hpp"
#include "solin/media_engine/scene_snapshot.hpp"

#ifdef _WIN32
#include <d3d11.h>
#endif

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>

namespace solin::media_engine {

#ifdef _WIN32

struct D3d11FrameChannelFrame final {
    // Changes whenever the producer replaces the underlying named texture
    // ring. This is deliberately distinct from the channel generation, which
    // stays stable across source-resolution changes.
    std::uint64_t resource_generation{0U};
    std::uint64_t sequence{0U};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t produced_monotonic_ns{0U};
    std::uint64_t media_epoch{0U};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    VideoFramePixelFormat pixel_format{VideoFramePixelFormat::nv12};
    ID3D11Texture2D* texture{nullptr};
};

class D3d11FrameChannelFrameLease final {
  public:
    D3d11FrameChannelFrameLease(const D3d11FrameChannelFrameLease&) = delete;
    D3d11FrameChannelFrameLease&
    operator=(const D3d11FrameChannelFrameLease&) = delete;
    D3d11FrameChannelFrameLease(D3d11FrameChannelFrameLease&&) noexcept = default;
    D3d11FrameChannelFrameLease&
    operator=(D3d11FrameChannelFrameLease&&) noexcept = default;
    ~D3d11FrameChannelFrameLease() = default;

    [[nodiscard]] const D3d11FrameChannelFrame& frame() const noexcept {
        return frame_;
    }

  private:
    D3d11FrameChannelFrameLease(D3d11FrameChannelFrame frame,
                                std::shared_ptr<void> lifetime)
        : frame_(frame), lifetime_(std::move(lifetime)) {}

    D3d11FrameChannelFrame frame_{};
    std::shared_ptr<void> lifetime_{};

    friend class D3d11FrameChannelReader;
};

class D3d11FrameChannelReader {
  public:
    virtual ~D3d11FrameChannelReader() = default;

    [[nodiscard]] virtual std::optional<D3d11FrameChannelFrameLease>
    read_latest(std::uint64_t after_sequence = 0U) = 0;
    [[nodiscard]] virtual std::optional<std::uint64_t> media_epoch() = 0;
    [[nodiscard]] virtual std::optional<FrameChannelImageTransform>
    image_transform() = 0;
    [[nodiscard]] virtual bool
    wait_for_frame(std::chrono::milliseconds timeout) = 0;
    virtual void wake() noexcept = 0;

  protected:
    D3d11FrameChannelReader() = default;

    [[nodiscard]] static D3d11FrameChannelFrameLease
    make_lease(D3d11FrameChannelFrame frame, std::shared_ptr<void> lifetime) {
        return D3d11FrameChannelFrameLease{frame, std::move(lifetime)};
    }
};

[[nodiscard]] std::unique_ptr<D3d11FrameChannelReader>
make_d3d11_frame_channel_reader(const FrameChannelConfiguration& configuration,
                                ID3D11Device* device);

#endif

} // namespace solin::media_engine
