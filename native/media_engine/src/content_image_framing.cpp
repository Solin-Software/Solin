#include "solin/media_engine/content_image_framing.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace solin::media_engine {
namespace {

[[nodiscard]] double bezier_coordinate(const double parameter,
                                       const double control_1,
                                       const double control_2) noexcept {
    const auto inverse = 1.0 - parameter;
    return 3.0 * inverse * inverse * parameter * control_1 +
           3.0 * inverse * parameter * parameter * control_2 +
           parameter * parameter * parameter;
}

[[nodiscard]] double cubic_bezier_ease(const double progress, const double x1,
                                       const double y1, const double x2,
                                       const double y2) noexcept {
    if (progress <= 0.0) {
        return 0.0;
    }
    if (progress >= 1.0) {
        return 1.0;
    }
    auto low = 0.0;
    auto high = 1.0;
    for (auto iteration = 0U; iteration < 20U; ++iteration) {
        const auto parameter = (low + high) / 2.0;
        if (bezier_coordinate(parameter, x1, x2) < progress) {
            low = parameter;
        } else {
            high = parameter;
        }
    }
    return bezier_coordinate((low + high) / 2.0, y1, y2);
}

[[nodiscard]] ContentImageTransform interpolate(
    const ContentImageTransform start, const ContentImageTransform target,
    const double progress) noexcept {
    return {
        .zoom = start.zoom + (target.zoom - start.zoom) * progress,
        .norm_x = start.norm_x + (target.norm_x - start.norm_x) * progress,
        .norm_y = start.norm_y + (target.norm_y - start.norm_y) * progress,
    };
}

[[nodiscard]] std::uint32_t rounded_coordinate(
    const double value, const std::uint32_t extent) noexcept {
    const auto rounded = static_cast<std::int64_t>(std::llround(value));
    return static_cast<std::uint32_t>(std::clamp(
        rounded, std::int64_t{0}, static_cast<std::int64_t>(extent)));
}

} // namespace

ContentImageRect compute_content_image_rect(
    const std::uint32_t source_width, const std::uint32_t source_height,
    const std::uint32_t canvas_width, const std::uint32_t canvas_height,
    const ContentImageTransform transform) {
    if (source_width == 0U || source_height == 0U || canvas_width == 0U ||
        canvas_height == 0U || !std::isfinite(transform.zoom) ||
        transform.zoom <= 0.0 || !std::isfinite(transform.norm_x) ||
        !std::isfinite(transform.norm_y)) {
        throw std::invalid_argument("content image framing is invalid");
    }
    const auto scale = (std::min)(
        static_cast<double>(canvas_width) / source_width,
        static_cast<double>(canvas_height) / source_height);
    const auto fitted_width = static_cast<double>(source_width) * scale;
    const auto fitted_height = static_cast<double>(source_height) * scale;
    const auto width = fitted_width * transform.zoom;
    const auto height = fitted_height * transform.zoom;
    return {
        .x = (static_cast<double>(canvas_width) - width) / 2.0 +
             transform.norm_x * canvas_width,
        .y = (static_cast<double>(canvas_height) - height) / 2.0 +
             transform.norm_y * canvas_height,
        .width = width,
        .height = height,
    };
}

ContentImagePlacement compute_content_image_placement(
    const std::uint32_t source_width, const std::uint32_t source_height,
    const std::uint32_t canvas_width, const std::uint32_t canvas_height,
    const ContentImageRect painter_rect) {
    if (source_width == 0U || source_height == 0U || canvas_width == 0U ||
        canvas_height == 0U ||
        canvas_width > static_cast<std::uint32_t>(
                           (std::numeric_limits<std::int32_t>::max)()) ||
        canvas_height > static_cast<std::uint32_t>(
                            (std::numeric_limits<std::int32_t>::max)()) ||
        !std::isfinite(painter_rect.x) || !std::isfinite(painter_rect.y) ||
        !std::isfinite(painter_rect.width) ||
        !std::isfinite(painter_rect.height) || painter_rect.width <= 0.0 ||
        painter_rect.height <= 0.0) {
        throw std::invalid_argument("content image placement is invalid");
    }

    const auto visible_left = std::clamp(
        painter_rect.x, 0.0, static_cast<double>(canvas_width));
    const auto visible_top = std::clamp(
        painter_rect.y, 0.0, static_cast<double>(canvas_height));
    const auto visible_right = std::clamp(
        painter_rect.x + painter_rect.width, 0.0,
        static_cast<double>(canvas_width));
    const auto visible_bottom = std::clamp(
        painter_rect.y + painter_rect.height, 0.0,
        static_cast<double>(canvas_height));
    if (visible_right <= visible_left || visible_bottom <= visible_top) {
        return {};
    }

    const auto destination_left =
        rounded_coordinate(visible_left, canvas_width);
    const auto destination_top =
        rounded_coordinate(visible_top, canvas_height);
    const auto destination_right =
        rounded_coordinate(visible_right, canvas_width);
    const auto destination_bottom =
        rounded_coordinate(visible_bottom, canvas_height);
    if (destination_right <= destination_left ||
        destination_bottom <= destination_top) {
        return {};
    }

    const auto source_coordinate = [](const std::uint32_t destination,
                                      const double destination_origin,
                                      const double destination_extent,
                                      const std::uint32_t source_extent) {
        const auto normalized =
            (static_cast<double>(destination) - destination_origin) /
            destination_extent;
        return rounded_coordinate(normalized * source_extent, source_extent);
    };
    const auto source_left = source_coordinate(
        destination_left, painter_rect.x, painter_rect.width, source_width);
    const auto source_top = source_coordinate(
        destination_top, painter_rect.y, painter_rect.height, source_height);
    const auto source_right = source_coordinate(
        destination_right, painter_rect.x, painter_rect.width, source_width);
    const auto source_bottom = source_coordinate(
        destination_bottom, painter_rect.y, painter_rect.height, source_height);
    if (source_right <= source_left || source_bottom <= source_top) {
        return {};
    }

    return {
        .source_x = source_left,
        .source_y = source_top,
        .source_width = source_right - source_left,
        .source_height = source_bottom - source_top,
        .destination_x = static_cast<std::int32_t>(destination_left),
        .destination_y = static_cast<std::int32_t>(destination_top),
        .destination_width = destination_right - destination_left,
        .destination_height = destination_bottom - destination_top,
    };
}

void ContentImageTransformAnimation::request(
    const ContentImageTransform target, const std::chrono::milliseconds duration,
    const bool animate, const std::chrono::steady_clock::time_point now) noexcept {
    if (active_) {
        sample_at(now);
    }
    if (animate && active_ && target == target_) {
        return;
    }
    const auto interrupted = active_;
    target_ = target;
    duration_ = (std::max)(duration, std::chrono::milliseconds{1U});
    if (!animate || target == current_) {
        current_ = target;
        start_ = target;
        active_ = false;
        interrupted_ = false;
        return;
    }
    start_ = current_;
    started_at_ = now;
    active_ = true;
    interrupted_ = interrupted;
}

ContentImageTransform ContentImageTransformAnimation::sample(
    const std::chrono::steady_clock::time_point now) noexcept {
    if (active_) {
        sample_at(now);
    }
    return current_;
}

void ContentImageTransformAnimation::delay(
    const std::chrono::steady_clock::duration duration) noexcept {
    if (active_ && duration > std::chrono::steady_clock::duration::zero()) {
        started_at_ += duration;
    }
}

void ContentImageTransformAnimation::reset() noexcept {
    current_ = {};
    start_ = {};
    target_ = {};
    started_at_ = {};
    duration_ = std::chrono::milliseconds{1U};
    active_ = false;
    interrupted_ = false;
}

ContentImageTransform ContentImageTransformAnimation::current() const noexcept {
    return current_;
}

ContentImageTransform ContentImageTransformAnimation::target() const noexcept {
    return target_;
}

bool ContentImageTransformAnimation::active() const noexcept { return active_; }

void ContentImageTransformAnimation::sample_at(
    const std::chrono::steady_clock::time_point now) noexcept {
    if (!active_) {
        return;
    }
    const auto elapsed = (std::max)(
        std::chrono::steady_clock::duration::zero(), now - started_at_);
    const auto progress = std::clamp(
        std::chrono::duration<double>(elapsed).count() /
            std::chrono::duration<double>(duration_).count(),
        0.0, 1.0);
    const auto eased = interrupted_
                           ? cubic_bezier_ease(progress, 0.0, 0.0, 0.58, 1.0)
                           : cubic_bezier_ease(progress, 0.25, 0.1, 0.25, 1.0);
    current_ = interpolate(start_, target_, eased);
    if (progress >= 1.0) {
        current_ = target_;
        active_ = false;
        interrupted_ = false;
    }
}

} // namespace solin::media_engine
