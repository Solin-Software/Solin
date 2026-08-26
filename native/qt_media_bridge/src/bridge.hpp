#pragma once

#include <QtGui/qimage.h>
#include <QtMultimedia/qvideoframe.h>
#include <QtMultimedia/qvideosink.h>

#include <cstdint>
#include <memory>
#include <string>

namespace solin::qt_media_bridge {

inline constexpr std::uint32_t kTransportSlotCount = 3U;

enum class SubmitResult : std::uint8_t {
    accepted,
    dropped,
    unavailable,
    no_demand,
    session_rejected,
};

struct BridgeDescriptor final {
    std::string channel_id{};
    std::string handle_token{};
    std::uint64_t generation{1U};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
};

struct BridgeStatus final {
    bool available{false};
    bool direct_submission_active{false};
    std::uint64_t published_sequence{0U};
    std::uint64_t dropped_frames{0U};
    std::uint64_t resource_generation{0U};
    std::uint32_t resource_width{0U};
    std::uint32_t resource_height{0U};
    std::string error_code{};
};

struct ImageTransform final {
    bool enabled{false};
    bool animate{false};
    std::uint64_t media_epoch{0U};
    std::uint32_t canvas_width{0U};
    std::uint32_t canvas_height{0U};
    std::uint32_t duration_ms{0U};
    double zoom{1.0};
    double norm_x{0.0};
    double norm_y{0.0};
};

class FrameBridge {
  public:
    virtual ~FrameBridge() = default;

    FrameBridge(const FrameBridge&) = delete;
    FrameBridge& operator=(const FrameBridge&) = delete;

    [[nodiscard]] virtual const BridgeDescriptor& descriptor() const noexcept = 0;
    [[nodiscard]] virtual BridgeStatus status() const = 0;
    virtual void set_route_state(std::uint64_t session_id, bool accepting_frames,
                                 bool demanded) = 0;
    virtual void set_decoder_frame_gate(std::uint64_t playback_session_id,
                                        bool accepting_frames) = 0;
    virtual void set_direct_submission(bool enabled,
                                       std::uint32_t maximum_fps) = 0;
    virtual void stage_media_epoch(std::uint64_t media_epoch) = 0;
    virtual void set_image_transform(const ImageTransform& transform) = 0;
    virtual void bind_video_sink(QVideoSink* sink) = 0;
    virtual void unbind_video_sink() noexcept = 0;
    [[nodiscard]] virtual SubmitResult submit(QVideoFrame frame,
                                              std::uint64_t session_id) = 0;
    [[nodiscard]] virtual SubmitResult submit_image(QImage image,
                                                    std::uint64_t session_id) = 0;
    virtual void close() noexcept = 0;

  protected:
    FrameBridge() = default;
};

[[nodiscard]] std::unique_ptr<FrameBridge>
make_frame_bridge(std::uint32_t width, std::uint32_t height,
                  std::uint64_t generation);

} // namespace solin::qt_media_bridge
