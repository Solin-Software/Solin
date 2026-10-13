#include "geometry.hpp"
#include <iostream>
#include <limits>
#include <stdexcept>
using namespace solin::media;
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
void test_orientation() {
    std::vector<uint8_t> pixels;
    for (uint8_t value = 1; value <= 6; ++value)
        pixels.insert(pixels.end(), {value, 0, 0, 255});
    // A 3x2 labeled grid exercises every quarter-turn/reflection permutation.
    const std::array<Orientation, 8> matrices{{{1, 0, 0, 1},
                                               {-1, 0, 0, 1},
                                               {1, 0, 0, -1},
                                               {-1, 0, 0, -1},
                                               {0, 1, -1, 0},
                                               {0, -1, 1, 0},
                                               {0, 1, 1, 0},
                                               {0, -1, -1, 0}}};
    const std::array<std::array<uint8_t, 6>, 8> expected{{{{1, 2, 3, 4, 5, 6}},
                                                          {{3, 2, 1, 6, 5, 4}},
                                                          {{4, 5, 6, 1, 2, 3}},
                                                          {{6, 5, 4, 3, 2, 1}},
                                                          {{4, 1, 5, 2, 6, 3}},
                                                          {{3, 6, 2, 5, 1, 4}},
                                                          {{1, 4, 2, 5, 3, 6}},
                                                          {{6, 3, 5, 2, 4, 1}}}};
    for (std::size_t i = 0; i < matrices.size(); ++i) {
        const auto geometry = contain_geometry(3, 2, 1, matrices[i], 640, 360);
        const auto output = orient_bgra(pixels, geometry, "frame");
        require(output.pixels.size() == 24, "Orientation changed pixel count");
        for (std::size_t j = 0; j < 6; ++j)
            require(output.pixels[j * 4] == expected[i][j], "Incorrect rotation/mirror");
    }
}
void test_contain() {
    auto value = contain_geometry(1920, 1080, 1, {}, 640, 360);
    require(value.width == 640 && value.height == 360, "HD contain");
    value = contain_geometry(720, 576, 16.0 / 15.0, {}, 640, 360);
    require(value.width == 480 && value.height == 360, "Anamorphic contain");
    value = contain_geometry(1920, 1080, 1, {0, 1, -1, 0}, 640, 360);
    require(value.width == 202 && value.height == 360, "Portrait rotation contain");
    value = contain_geometry(100, 100, 1, {0.707106781, 0.707106781, -0.707106781, 0.707106781},
                             640, 360);
    require(value.width == 141 && value.height == 141, "Arbitrary rotation bounds");
    for (const auto matrix : {std::array<int32_t, 9>{},
                              std::array<int32_t, 9>{65536, 0, 1, 0, 65536, 0, 0, 0, 1 << 30}}) {
        bool failed = false;
        try {
            (void)display_orientation(matrix);
        } catch (const Failure& failure) {
            failed = failure.kind == "format";
        }
        require(failed, "Invalid matrix accepted");
    }
    bool failed = false;
    try {
        (void)contain_geometry(16384, 16384, 1, {}, 640, 360);
    } catch (const Failure&) {
        failed = true;
    }
    require(failed, "Oversized decode accepted");
}
void test_decoded_size_boundaries() {
    // Exercise the exact 32 Mi-pixel boundary without allocating source pixels.
    for (const auto dimensions : {std::array<int, 2>{8192, 4096}, std::array<int, 2>{16384, 2048},
                                  std::array<int, 2>{7680, 4320}}) {
        const auto geometry = contain_geometry(dimensions[0], dimensions[1], 1, {}, 640, 360);
        require(geometry.width > 0 && geometry.width <= 640 && geometry.height > 0 &&
                    geometry.height <= 360,
                "Valid large decode did not produce bounded output");
    }
    for (const auto dimensions :
         {std::array<int, 2>{8192, 4097}, std::array<int, 2>{16385, 1},
          std::array<int, 2>{1, 16385}, std::array<int, 2>{0, 1}, std::array<int, 2>{1, -1},
          std::array<int, 2>{std::numeric_limits<int>::max(), std::numeric_limits<int>::max()}}) {
        bool failed = false;
        try {
            (void)contain_geometry(dimensions[0], dimensions[1], 1, {}, 640, 360);
        } catch (const Failure& failure) {
            failed = failure.kind == "format" && failure.code == "invalid_image_geometry";
        }
        require(failed, "Unsafe decoded dimensions accepted");
    }
}
} // namespace
int main() {
    try {
        test_orientation();
        test_contain();
        test_decoded_size_boundaries();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
