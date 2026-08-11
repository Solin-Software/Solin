#include "solin/media_engine/render_geometry.hpp"

#include <algorithm>
#include <cmath>
#include <numbers>
#include <utility>

namespace solin::media_engine {
namespace {

[[nodiscard]] bool finite(const double value) noexcept { return std::isfinite(value); }

void validate_geometry(const SceneLayerGeometry& geometry, const std::uint32_t source_width,
                       const std::uint32_t source_height,
                       const std::uint32_t output_width,
                       const std::uint32_t output_height) {
    if (source_width == 0U || source_height == 0U || output_width == 0U ||
        output_height == 0U) {
        throw RenderGeometryError{"Video dimensions must be greater than zero"};
    }
    const auto values = {
        geometry.x,           geometry.y,           geometry.width,
        geometry.height,      geometry.crop_left,   geometry.crop_top,
        geometry.crop_right,  geometry.crop_bottom, geometry.rotation_degrees,
        geometry.opacity,     geometry.border_width,
        geometry.corner_radius,
    };
    if (!std::all_of(values.begin(), values.end(), finite)) {
        throw RenderGeometryError{"Layer geometry must contain finite numbers"};
    }
    if (geometry.width <= 0.0 || geometry.height <= 0.0) {
        throw RenderGeometryError{"Layer dimensions must be greater than zero"};
    }
    if (geometry.crop_left < 0.0 || geometry.crop_top < 0.0 ||
        geometry.crop_right < 0.0 || geometry.crop_bottom < 0.0 ||
        geometry.crop_left + geometry.crop_right >= 1.0 ||
        geometry.crop_top + geometry.crop_bottom >= 1.0) {
        throw RenderGeometryError{"Layer crop is invalid"};
    }
    if (geometry.opacity < 0.0 || geometry.opacity > 1.0 ||
        geometry.border_width < 0.0 || geometry.corner_radius < 0.0) {
        throw RenderGeometryError{"Layer appearance is invalid"};
    }
    if (geometry.fit_mode != "contain" && geometry.fit_mode != "cover" &&
        geometry.fit_mode != "stretch") {
        throw RenderGeometryError{"Layer fit mode is invalid"};
    }
}

[[nodiscard]] RenderRect rotated_bounds(const RenderRect& value,
                                        const double rotation_degrees) noexcept {
    const auto radians = rotation_degrees * std::numbers::pi / 180.0;
    const auto cosine = std::abs(std::cos(radians));
    const auto sine = std::abs(std::sin(radians));
    const auto width = value.width * cosine + value.height * sine;
    const auto height = value.width * sine + value.height * cosine;
    return {
        .x = value.x + (value.width - width) / 2.0,
        .y = value.y + (value.height - height) / 2.0,
        .width = width,
        .height = height,
    };
}

[[nodiscard]] RenderRect intersect(const RenderRect& left,
                                   const RenderRect& right) noexcept {
    const auto x = std::max(left.x, right.x);
    const auto y = std::max(left.y, right.y);
    const auto right_edge = std::min(left.x + left.width, right.x + right.width);
    const auto bottom_edge = std::min(left.y + left.height, right.y + right.height);
    return {
        .x = x,
        .y = y,
        .width = std::max(0.0, right_edge - x),
        .height = std::max(0.0, bottom_edge - y),
    };
}

} // namespace

RenderGeometryError::RenderGeometryError(std::string message)
    : std::runtime_error(std::move(message)) {}

RenderLayerTransform compute_render_layer_transform(
    const SceneLayerGeometry& geometry, const std::uint32_t source_width,
    const std::uint32_t source_height, const std::uint32_t output_width,
    const std::uint32_t output_height) {
    validate_geometry(geometry, source_width, source_height, output_width, output_height);

    RenderLayerTransform result{
        .layer_bounds = {
            .x = geometry.x * static_cast<double>(output_width),
            .y = geometry.y * static_cast<double>(output_height),
            .width = geometry.width * static_cast<double>(output_width),
            .height = geometry.height * static_cast<double>(output_height),
        },
        .source_uv = {
            .left = geometry.crop_left,
            .top = geometry.crop_top,
            .right = 1.0 - geometry.crop_right,
            .bottom = 1.0 - geometry.crop_bottom,
        },
        .rotation_degrees = geometry.rotation_degrees,
        .opacity = geometry.opacity,
        .mirror_x = geometry.mirror_x,
        .mirror_y = geometry.mirror_y,
    };
    result.content_bounds = result.layer_bounds;

    const auto visible_source_width =
        static_cast<double>(source_width) * (result.source_uv.right - result.source_uv.left);
    const auto visible_source_height =
        static_cast<double>(source_height) * (result.source_uv.bottom - result.source_uv.top);
    const auto source_aspect = visible_source_width / visible_source_height;
    const auto layer_aspect = result.layer_bounds.width / result.layer_bounds.height;

    if (geometry.fit_mode == "contain") {
        if (source_aspect > layer_aspect) {
            result.content_bounds.height = result.layer_bounds.width / source_aspect;
            result.content_bounds.y +=
                (result.layer_bounds.height - result.content_bounds.height) / 2.0;
        } else {
            result.content_bounds.width = result.layer_bounds.height * source_aspect;
            result.content_bounds.x +=
                (result.layer_bounds.width - result.content_bounds.width) / 2.0;
        }
    } else if (geometry.fit_mode == "cover") {
        if (source_aspect > layer_aspect) {
            const auto next_width =
                (result.source_uv.bottom - result.source_uv.top) *
                static_cast<double>(source_height) * layer_aspect /
                static_cast<double>(source_width);
            const auto center = (result.source_uv.left + result.source_uv.right) / 2.0;
            result.source_uv.left = center - next_width / 2.0;
            result.source_uv.right = center + next_width / 2.0;
        } else {
            const auto next_height =
                (result.source_uv.right - result.source_uv.left) *
                static_cast<double>(source_width) /
                (layer_aspect * static_cast<double>(source_height));
            const auto center = (result.source_uv.top + result.source_uv.bottom) / 2.0;
            result.source_uv.top = center - next_height / 2.0;
            result.source_uv.bottom = center + next_height / 2.0;
        }
    }

    const auto shortest_layer_edge =
        std::min(result.layer_bounds.width, result.layer_bounds.height);
    result.border_width_pixels = geometry.border_width * shortest_layer_edge;
    result.corner_radius_pixels = geometry.corner_radius * shortest_layer_edge;
    result.rotated_content_bounds =
        rotated_bounds(result.content_bounds, result.rotation_degrees);
    result.output_clip = intersect(
        result.rotated_content_bounds,
        {.x = 0.0,
         .y = 0.0,
         .width = static_cast<double>(output_width),
         .height = static_cast<double>(output_height)});
    result.intersects_output =
        geometry.visible && geometry.opacity > 0.0 && result.output_clip.width > 0.0 &&
        result.output_clip.height > 0.0;
    return result;
}

} // namespace solin::media_engine
