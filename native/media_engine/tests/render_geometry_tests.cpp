#include "solin/media_engine/render_geometry.hpp"

#include <cmath>
#include <iostream>
#include <limits>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

[[nodiscard]] bool near(const double left, const double right) {
    return std::abs(left - right) < 0.000001;
}

[[nodiscard]] solin::media_engine::SceneLayerGeometry geometry(
    const std::string& fit_mode) {
    return {
        .x = 0.0,
        .y = 0.0,
        .width = 1.0,
        .height = 1.0,
        .opacity = 1.0,
        .fit_mode = fit_mode,
        .visible = true,
    };
}

void test_cover_crops_source_without_changing_destination() {
    const auto result = solin::media_engine::compute_render_layer_transform(
        geometry("cover"), 1920U, 1080U, 1000U, 1000U);
    expect(result.content_bounds == result.layer_bounds &&
               near(result.source_uv.left, 0.21875) &&
               near(result.source_uv.right, 0.78125) &&
               near(result.source_uv.top, 0.0) && near(result.source_uv.bottom, 1.0),
           "cover center-crops source UVs to the layer aspect ratio");
}

void test_contain_centers_content_inside_layer() {
    const auto result = solin::media_engine::compute_render_layer_transform(
        geometry("contain"), 1920U, 1080U, 1000U, 1000U);
    expect(near(result.content_bounds.x, 0.0) &&
               near(result.content_bounds.y, 218.75) &&
               near(result.content_bounds.width, 1000.0) &&
               near(result.content_bounds.height, 562.5) &&
               result.source_uv == solin::media_engine::RenderTextureCoordinates{},
           "contain preserves all source UVs and centers the fitted content");
}

void test_user_crop_precedes_fit_and_stretch_preserves_crop() {
    auto cover = geometry("cover");
    cover.crop_left = 0.25;
    const auto covered = solin::media_engine::compute_render_layer_transform(
        cover, 1000U, 1000U, 1000U, 1000U);
    expect(near(covered.source_uv.left, 0.25) && near(covered.source_uv.right, 1.0) &&
               near(covered.source_uv.top, 0.125) &&
               near(covered.source_uv.bottom, 0.875),
           "cover applies its center crop after the explicit user crop");

    auto stretch = geometry("stretch");
    stretch.crop_left = 0.1;
    stretch.crop_bottom = 0.2;
    const auto stretched = solin::media_engine::compute_render_layer_transform(
        stretch, 1920U, 1080U, 1280U, 720U);
    expect(stretched.content_bounds == stretched.layer_bounds &&
               near(stretched.source_uv.left, 0.1) &&
               near(stretched.source_uv.bottom, 0.8),
           "stretch fills the layer while retaining explicit source crop");
}

void test_transform_metadata_and_output_intersection() {
    auto value = geometry("contain");
    value.rotation_degrees = 90.0;
    value.mirror_x = true;
    value.mirror_y = true;
    value.border_width = 0.01;
    value.corner_radius = 0.1;
    const auto result = solin::media_engine::compute_render_layer_transform(
        value, 1920U, 1080U, 1000U, 1000U);
    expect(result.mirror_x && result.mirror_y &&
               near(result.rotated_content_bounds.width, 562.5) &&
               near(result.rotated_content_bounds.height, 1000.0) &&
               near(result.border_width_pixels, 10.0) &&
               near(result.corner_radius_pixels, 100.0) && result.intersects_output,
           "rotation, mirroring, border and radius survive geometry compilation");

    value.x = 2.0;
    value.rotation_degrees = 0.0;
    const auto outside = solin::media_engine::compute_render_layer_transform(
        value, 1920U, 1080U, 1000U, 1000U);
    expect(!outside.intersects_output && near(outside.output_clip.width, 0.0),
           "layers outside the output produce an empty output clip");
}

void test_invalid_geometry_is_rejected() {
    auto invalid_crop = geometry("cover");
    invalid_crop.crop_left = 0.5;
    invalid_crop.crop_right = 0.5;
    try {
        static_cast<void>(solin::media_engine::compute_render_layer_transform(
            invalid_crop, 1920U, 1080U, 1920U, 1080U));
        expect(false, "invalid crop must fail");
    } catch (const solin::media_engine::RenderGeometryError&) {
    }

    auto non_finite = geometry("cover");
    non_finite.x = std::numeric_limits<double>::quiet_NaN();
    try {
        static_cast<void>(solin::media_engine::compute_render_layer_transform(
            non_finite, 1920U, 1080U, 1920U, 1080U));
        expect(false, "non-finite geometry must fail");
    } catch (const solin::media_engine::RenderGeometryError&) {
    }
}

} // namespace

int main() {
    test_cover_crops_source_without_changing_destination();
    test_contain_centers_content_inside_layer();
    test_user_crop_precedes_fit_and_stretch_preserves_crop();
    test_transform_metadata_and_output_intersection();
    test_invalid_geometry_is_rejected();
    if (failures != 0) {
        std::cerr << failures << " render geometry test(s) failed\n";
        return 1;
    }
    return 0;
}
