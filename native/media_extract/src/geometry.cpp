#include "geometry.hpp"
#include <algorithm>
#include <cmath>

namespace solin::media {
namespace {
[[noreturn]] void invalid_geometry() {
    throw Failure("format", "invalid_image_geometry",
                  "Invalid image dimensions, sample aspect ratio or display matrix");
}
void validate_orientation(Orientation value) {
    const double determinant = value.a * value.d - value.b * value.c;
    if (!std::isfinite(determinant) || std::abs(determinant) < 1e-8 || std::abs(value.a) > 1e4 ||
        std::abs(value.b) > 1e4 || std::abs(value.c) > 1e4 || std::abs(value.d) > 1e4)
        invalid_geometry();
}
} // namespace
Orientation display_orientation(const std::array<int32_t, 9>& matrix) {
    // Perspective matrices cannot be represented by this bounded affine thumbnail pipeline.
    if (matrix[2] != 0 || matrix[5] != 0 || matrix[8] == 0)
        invalid_geometry();
    const double factor = static_cast<double>(matrix[8]) / (1LL << 30);
    Orientation value{matrix[0] / 65536.0 / factor, matrix[1] / 65536.0 / factor,
                      matrix[3] / 65536.0 / factor, matrix[4] / 65536.0 / factor};
    validate_orientation(value);
    return value;
}
Geometry contain_geometry(int width, int height, double sar, Orientation orientation,
                          int maximum_width, int maximum_height) {
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384 ||
        static_cast<int64_t>(width) * height > 33554432 || !std::isfinite(sar) || sar <= 0 ||
        sar > 10000 || sar < 0.0001 || maximum_width <= 0 || maximum_width > 640 ||
        maximum_height <= 0 || maximum_height > 360)
        invalid_geometry();
    validate_orientation(orientation);
    const double display_width = width * sar;
    const double display_height = height;
    const std::array<double, 4> xs{0, orientation.a * display_width, orientation.c * display_height,
                                   orientation.a * display_width + orientation.c * display_height};
    const std::array<double, 4> ys{0, orientation.b * display_width, orientation.d * display_height,
                                   orientation.b * display_width + orientation.d * display_height};
    const auto [min_x, max_x] = std::minmax_element(xs.begin(), xs.end());
    const auto [min_y, max_y] = std::minmax_element(ys.begin(), ys.end());
    const double extent_x = *max_x - *min_x, extent_y = *max_y - *min_y;
    if (!std::isfinite(extent_x) || !std::isfinite(extent_y) || extent_x <= 0 || extent_y <= 0)
        invalid_geometry();
    const double scale = std::min({1.0, maximum_width / extent_x, maximum_height / extent_y});
    const int output_width =
        std::clamp(static_cast<int>(std::floor(extent_x * scale + 1e-7)), 1, maximum_width);
    const int output_height =
        std::clamp(static_cast<int>(std::floor(extent_y * scale + 1e-7)), 1, maximum_height);
    // Normalize orientation scale only for the intermediate allocation. The inverse mapping retains
    // it.
    const double basis_x = std::hypot(orientation.a, orientation.b);
    const double basis_y = std::hypot(orientation.c, orientation.d);
    const int scaled_width =
        std::clamp(static_cast<int>(std::ceil(display_width * scale * basis_x)), 1, 1024);
    const int scaled_height =
        std::clamp(static_cast<int>(std::ceil(display_height * scale * basis_y)), 1, 1024);
    return {output_width,   output_height, scaled_width, scaled_height, display_width,
            display_height, *min_x,        *min_y,       scale,         orientation};
}
Image orient_bgra(std::vector<uint8_t> pixels, const Geometry& geometry, std::string kind) {
    if (pixels.size() !=
        static_cast<std::size_t>(geometry.scaled_width) * geometry.scaled_height * 4)
        throw Failure("permanent", "invalid_result",
                      "Scaled image buffer does not match its dimensions");
    Image output;
    output.width = geometry.width;
    output.height = geometry.height;
    output.stride = output.width * 4;
    output.kind = std::move(kind);
    const auto matrix = geometry.orientation;
    if (matrix.a == 1 && matrix.b == 0 && matrix.c == 0 && matrix.d == 1 &&
        output.width == geometry.scaled_width && output.height == geometry.scaled_height) {
        output.pixels = std::move(pixels);
        return output;
    }
    output.pixels.resize(static_cast<std::size_t>(output.stride) * output.height);
    const double determinant = matrix.a * matrix.d - matrix.b * matrix.c;
    for (int y = 0; y < output.height; ++y) {
        for (int x = 0; x < output.width; ++x) {
            const double display_x = (x + 0.5) / geometry.scale + geometry.minimum_x;
            const double display_y = (y + 0.5) / geometry.scale + geometry.minimum_y;
            const double source_x = (matrix.d * display_x - matrix.c * display_y) / determinant;
            const double source_y = (-matrix.b * display_x + matrix.a * display_y) / determinant;
            auto* destination =
                output.pixels.data() + static_cast<std::size_t>(y * output.width + x) * 4;
            if (source_x < 0 || source_x >= geometry.display_width || source_y < 0 ||
                source_y >= geometry.display_height)
                continue; // Transparent rotated corners.
            const double sample_x = source_x / geometry.display_width * geometry.scaled_width - 0.5;
            const double sample_y =
                source_y / geometry.display_height * geometry.scaled_height - 0.5;
            const int x0 = static_cast<int>(std::floor(sample_x)),
                      y0 = static_cast<int>(std::floor(sample_y));
            const double weight_x = sample_x - x0, weight_y = sample_y - y0;
            const auto sample = [&](int sx, int sy, int channel) {
                sx = std::clamp(sx, 0, geometry.scaled_width - 1);
                sy = std::clamp(sy, 0, geometry.scaled_height - 1);
                return pixels[static_cast<std::size_t>(sy * geometry.scaled_width + sx) * 4 +
                              channel];
            };
            for (int channel = 0; channel < 4; ++channel) {
                const double top = sample(x0, y0, channel) * (1 - weight_x) +
                                   sample(x0 + 1, y0, channel) * weight_x;
                const double bottom = sample(x0, y0 + 1, channel) * (1 - weight_x) +
                                      sample(x0 + 1, y0 + 1, channel) * weight_x;
                destination[channel] = static_cast<uint8_t>(
                    std::clamp(std::lround(top * (1 - weight_y) + bottom * weight_y), 0L, 255L));
            }
        }
    }
    return output;
}
} // namespace solin::media
