#pragma once
#include "protocol.hpp"
#include <array>

namespace solin::media {
// Public display matrix's affine basis: x'=a*x+c*y, y'=b*x+d*y.
struct Orientation {
    double a = 1, b = 0, c = 0, d = 1;
};
struct Geometry {
    int width, height, scaled_width, scaled_height;
    double display_width, display_height, minimum_x, minimum_y, scale;
    Orientation orientation;
};
Orientation display_orientation(const std::array<int32_t, 9>& matrix);
Geometry contain_geometry(int width, int height, double sar, Orientation orientation,
                          int maximum_width, int maximum_height);
Image orient_bgra(std::vector<uint8_t> scaled_pixels, const Geometry& geometry, std::string kind);
} // namespace solin::media
