#pragma once

#include <chrono>
#include <cstdint>

namespace solin::media_engine {

struct ContentImageTransform final {
    double zoom{1.0};
    double norm_x{0.0};
    double norm_y{0.0};

    bool operator==(const ContentImageTransform&) const = default;
};

struct ContentImageRect final {
    double x{0.0};
    double y{0.0};
    double width{0.0};
    double height{0.0};

    bool operator==(const ContentImageRect&) const = default;
};

struct ContentImagePlacement final {
    std::uint32_t source_x{0U};
    std::uint32_t source_y{0U};
    std::uint32_t source_width{0U};
    std::uint32_t source_height{0U};
    std::int32_t destination_x{0};
    std::int32_t destination_y{0};
    std::uint32_t destination_width{0U};
    std::uint32_t destination_height{0U};

    [[nodiscard]] bool visible() const noexcept {
        return source_width != 0U && source_height != 0U &&
               destination_width != 0U && destination_height != 0U;
    }

    bool operator==(const ContentImagePlacement&) const = default;
};

[[nodiscard]] ContentImageRect compute_content_image_rect(
    std::uint32_t source_width, std::uint32_t source_height,
    std::uint32_t canvas_width, std::uint32_t canvas_height,
    ContentImageTransform transform);

// Convert the fallback's potentially off-canvas painter rectangle into an
// explicit source crop and a non-negative destination. GStreamer compositor
// positions are output pixels, but its negative-position crop is interpreted
// as source pixels; passing the painter rectangle through directly distorts
// scaled images whenever zoom moves an edge outside the canvas.
[[nodiscard]] ContentImagePlacement compute_content_image_placement(
    std::uint32_t source_width, std::uint32_t source_height,
    std::uint32_t canvas_width, std::uint32_t canvas_height,
    ContentImageRect painter_rect);

// Clock-driven counterpart of the Qt fallback animation. The engine owns the
// interpolation so Python sends one target instead of issuing per-frame IPC.
class ContentImageTransformAnimation final {
  public:
    void request(ContentImageTransform target, std::chrono::milliseconds duration,
                 bool animate, std::chrono::steady_clock::time_point now) noexcept;
    [[nodiscard]] ContentImageTransform
    sample(std::chrono::steady_clock::time_point now) noexcept;
    void delay(std::chrono::steady_clock::duration duration) noexcept;
    void reset() noexcept;

    [[nodiscard]] ContentImageTransform current() const noexcept;
    [[nodiscard]] ContentImageTransform target() const noexcept;
    [[nodiscard]] bool active() const noexcept;

  private:
    void sample_at(std::chrono::steady_clock::time_point now) noexcept;

    ContentImageTransform current_{};
    ContentImageTransform start_{};
    ContentImageTransform target_{};
    std::chrono::steady_clock::time_point started_at_{};
    std::chrono::milliseconds duration_{1U};
    bool active_{false};
    bool interrupted_{false};
};

} // namespace solin::media_engine
