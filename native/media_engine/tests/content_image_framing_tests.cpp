#include "solin/media_engine/content_image_framing.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>

namespace {

using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

[[nodiscard]] bool near(const double left, const double right,
                        const double tolerance = 0.000001) {
    return std::abs(left - right) < tolerance;
}

void test_geometry_matches_the_qt_fallback_contract() {
    const auto identity = solin::media_engine::compute_content_image_rect(
        1'000U, 1'000U, 1'920U, 1'080U, {});
    expect(near(identity.x, 420.0) && near(identity.y, 0.0) &&
               near(identity.width, 1'080.0) && near(identity.height, 1'080.0),
           "identity framing uses a centered aspect fit");

    const auto transformed = solin::media_engine::compute_content_image_rect(
        1'000U, 1'000U, 1'920U, 1'080U,
        {.zoom = 2.0, .norm_x = 0.25, .norm_y = -0.1});
    expect(near(transformed.x, 360.0) && near(transformed.y, -648.0) &&
               near(transformed.width, 2'160.0) &&
               near(transformed.height, 2'160.0),
           "zoom and normalized pan use the full canvas like the Qt painter");
}

void test_off_canvas_geometry_becomes_source_crop_and_safe_destination() {
    const auto centered_rect = solin::media_engine::compute_content_image_rect(
        800U, 600U, 1'920U, 1'080U,
        {.zoom = 2.0, .norm_x = 0.0, .norm_y = 0.0});
    const auto centered = solin::media_engine::compute_content_image_placement(
        800U, 600U, 1'920U, 1'080U, centered_rect);
    expect(centered == solin::media_engine::ContentImagePlacement{
                           .source_x = 133U,
                           .source_y = 150U,
                           .source_width = 534U,
                           .source_height = 300U,
                           .destination_x = 0,
                           .destination_y = 0,
                           .destination_width = 1'920U,
                           .destination_height = 1'080U,
                       },
           "off-canvas zoom is represented as a proportional source crop");

    const auto panned_rect = solin::media_engine::compute_content_image_rect(
        800U, 600U, 1'920U, 1'080U,
        {.zoom = 2.0, .norm_x = 0.25, .norm_y = 0.0});
    const auto panned = solin::media_engine::compute_content_image_placement(
        800U, 600U, 1'920U, 1'080U, panned_rect);
    expect(panned == solin::media_engine::ContentImagePlacement{
                         .source_x = 0U,
                         .source_y = 150U,
                         .source_width = 533U,
                         .source_height = 300U,
                         .destination_x = 0,
                         .destination_y = 0,
                         .destination_width = 1'920U,
                         .destination_height = 1'080U,
                     },
           "normalized pan maps to the same source region as the Qt painter");

    const auto hidden = solin::media_engine::compute_content_image_placement(
        800U, 600U, 1'920U, 1'080U,
        {.x = 2'000.0, .y = 0.0, .width = 800.0, .height = 600.0});
    expect(!hidden.visible(), "a fully off-canvas image produces a black frame");
}

void test_placement_grid_stays_bounded_and_preserves_scale() {
    constexpr auto source_width = 800U;
    constexpr auto source_height = 600U;
    constexpr auto canvas_width = 1'920U;
    constexpr auto canvas_height = 1'080U;
    for (const auto zoom : {1.0, 1.5, 2.0, 4.0}) {
        for (const auto norm_x : {-0.75, -0.25, 0.0, 0.25, 0.75}) {
            for (const auto norm_y : {-0.75, -0.25, 0.0, 0.25, 0.75}) {
                const auto painter =
                    solin::media_engine::compute_content_image_rect(
                        source_width, source_height, canvas_width, canvas_height,
                        {.zoom = zoom, .norm_x = norm_x, .norm_y = norm_y});
                const auto placement =
                    solin::media_engine::compute_content_image_placement(
                        source_width, source_height, canvas_width, canvas_height,
                        painter);
                if (!placement.visible()) {
                    continue;
                }
                expect(placement.destination_x >= 0 && placement.destination_y >= 0,
                       "visible destinations never use negative compositor positions");
                expect(static_cast<std::uint64_t>(placement.destination_x) +
                               placement.destination_width <=
                           canvas_width &&
                           static_cast<std::uint64_t>(placement.destination_y) +
                                   placement.destination_height <=
                               canvas_height,
                       "visible destinations stay inside the output canvas");
                expect(static_cast<std::uint64_t>(placement.source_x) +
                               placement.source_width <=
                           source_width &&
                           static_cast<std::uint64_t>(placement.source_y) +
                                   placement.source_height <=
                               source_height,
                       "proportional crops stay inside the source image");

                const auto scale_x =
                    static_cast<double>(placement.destination_width) /
                    placement.source_width;
                const auto scale_y =
                    static_cast<double>(placement.destination_height) /
                    placement.source_height;
                const auto relative_scale_error =
                    std::abs(scale_x - scale_y) / (std::max)(scale_x, scale_y);
                expect(relative_scale_error < 0.015,
                       "source crop never introduces visible aspect distortion");
            }
        }
    }
}

void test_animation_matches_duration_and_supports_interruption() {
    solin::media_engine::ContentImageTransformAnimation animation;
    const auto start = std::chrono::steady_clock::time_point{};
    animation.request({.zoom = 2.0, .norm_x = 0.2, .norm_y = -0.1}, 2'100ms,
                      true, start);
    const auto halfway = animation.sample(start + 1'050ms);
    expect(animation.active() && halfway.zoom > 1.0 && halfway.zoom < 2.0,
           "the CSS ease animation advances from the exact current transform");

    animation.request({.zoom = 1.5, .norm_x = 0.0, .norm_y = 0.0}, 2'100ms,
                      true, start + 1'050ms);
    const auto interrupted_origin = animation.current();
    const auto after_interrupt = animation.sample(start + 1'100ms);
    expect(after_interrupt.zoom < interrupted_origin.zoom && animation.active(),
           "retargeting continues without snapping and switches to ease-out");

    const auto target = animation.sample(start + 3'200ms);
    expect(!animation.active() && near(target.zoom, 1.5) && near(target.norm_x, 0.0) &&
               near(target.norm_y, 0.0),
           "the final sample commits the requested transform exactly");
}

void test_instant_replay_and_reset_do_not_animate() {
    solin::media_engine::ContentImageTransformAnimation animation;
    animation.request({.zoom = 3.0, .norm_x = 0.1, .norm_y = 0.2}, 2'100ms,
                      false, {});
    expect(!animation.active() && animation.current().zoom == 3.0,
           "retained image framing replays instantly after attach");
    animation.reset();
    expect(animation.current() == solin::media_engine::ContentImageTransform{} &&
               !animation.active(),
           "reset returns to identity without leaving an active clock");
}

} // namespace

int main() {
    test_geometry_matches_the_qt_fallback_contract();
    test_off_canvas_geometry_becomes_source_crop_and_safe_destination();
    test_placement_grid_stays_bounded_and_preserves_scale();
    test_animation_matches_duration_and_supports_interruption();
    test_instant_replay_and_reset_do_not_animate();
    if (failures != 0) {
        std::cerr << failures << " content image framing test(s) failed\n";
        return 1;
    }
    return 0;
}
