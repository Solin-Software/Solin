#pragma once

#include "solin/media_engine/scene_snapshot.hpp"

#include <cstdint>
#include <stdexcept>
#include <string>

namespace solin::media_engine {

struct RenderRect {
    double x{0.0};
    double y{0.0};
    double width{0.0};
    double height{0.0};

    bool operator==(const RenderRect&) const = default;
};

struct RenderTextureCoordinates {
    double left{0.0};
    double top{0.0};
    double right{1.0};
    double bottom{1.0};

    bool operator==(const RenderTextureCoordinates&) const = default;
};

struct RenderLayerTransform {
    RenderRect layer_bounds{};
    RenderRect content_bounds{};
    RenderRect rotated_content_bounds{};
    RenderRect output_clip{};
    RenderTextureCoordinates source_uv{};
    double rotation_degrees{0.0};
    double opacity{1.0};
    double border_width_pixels{0.0};
    double corner_radius_pixels{0.0};
    bool mirror_x{false};
    bool mirror_y{false};
    bool intersects_output{false};
};

class RenderGeometryError final : public std::runtime_error {
  public:
    explicit RenderGeometryError(std::string message);
};

[[nodiscard]] RenderLayerTransform compute_render_layer_transform(
    const SceneLayerGeometry& geometry, std::uint32_t source_width,
    std::uint32_t source_height, std::uint32_t output_width,
    std::uint32_t output_height);

} // namespace solin::media_engine
