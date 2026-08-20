#include "frame_adapter.hpp"
#include "standby_resources.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <libyuv/convert.h>
#include <libyuv/convert_from.h>
#include <libyuv/scale.h>

#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace solin::media_engine::windows_virtual_camera {
namespace {

struct RgbPixel final {
    std::uint8_t red{0U};
    std::uint8_t green{0U};
    std::uint8_t blue{0U};
};

constexpr RgbPixel kStandbyBackground{15U, 21U, 32U};
const std::uint8_t kResourceModuleAnchor = 0U;

[[nodiscard]] std::size_t nv12_size(const DirectShowMediaProfile& profile) {
    return static_cast<std::size_t>(profile.width) * profile.height * 3U / 2U;
}

[[nodiscard]] bool plane_covers_rows(
    const std::span<const std::uint8_t> plane, const std::int32_t stride,
    const std::uint32_t rows, const std::uint32_t row_bytes) noexcept {
    if (rows == 0U || stride <= 0 ||
        static_cast<std::uint32_t>(stride) < row_bytes) {
        return false;
    }
    const auto unsigned_stride = static_cast<std::size_t>(stride);
    const auto preceding_rows = static_cast<std::size_t>(rows - 1U);
    if (preceding_rows >
        ((std::numeric_limits<std::size_t>::max)() - row_bytes) /
            unsigned_stride) {
        return false;
    }
    return preceding_rows * unsigned_stride + row_bytes <= plane.size();
}

void render_standby_icon(std::vector<RgbPixel>& pixels,
                         const std::uint32_t width,
                         const std::uint32_t height) noexcept {
    HMODULE module = nullptr;
    if (GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&kResourceModuleAnchor), &module) == FALSE) {
        return;
    }
    const auto extent = static_cast<int>(std::clamp(
        (std::min)(width, height) * 3U / 10U, 96U, 512U));
    const auto icon = reinterpret_cast<HICON>(LoadImageW(
        module, MAKEINTRESOURCEW(SOLIN_STANDBY_ICON), IMAGE_ICON, extent, extent,
        LR_DEFAULTCOLOR));
    if (icon == nullptr) {
        return;
    }
    BITMAPINFO bitmap_info{};
    bitmap_info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bitmap_info.bmiHeader.biWidth = static_cast<LONG>(width);
    bitmap_info.bmiHeader.biHeight = -static_cast<LONG>(height);
    bitmap_info.bmiHeader.biPlanes = 1U;
    bitmap_info.bmiHeader.biBitCount = 32U;
    bitmap_info.bmiHeader.biCompression = BI_RGB;
    void* bitmap_bytes = nullptr;
    const auto bitmap = CreateDIBSection(nullptr, &bitmap_info, DIB_RGB_COLORS,
                                         &bitmap_bytes, nullptr, 0U);
    const auto dc = CreateCompatibleDC(nullptr);
    if (bitmap == nullptr || bitmap_bytes == nullptr || dc == nullptr) {
        if (dc != nullptr) {
            static_cast<void>(DeleteDC(dc));
        }
        if (bitmap != nullptr) {
            static_cast<void>(DeleteObject(bitmap));
        }
        static_cast<void>(DestroyIcon(icon));
        return;
    }
    auto* bgra = static_cast<std::uint8_t*>(bitmap_bytes);
    for (std::size_t index = 0U; index < pixels.size(); ++index) {
        bgra[index * 4U] = kStandbyBackground.blue;
        bgra[index * 4U + 1U] = kStandbyBackground.green;
        bgra[index * 4U + 2U] = kStandbyBackground.red;
        bgra[index * 4U + 3U] = 255U;
    }
    const auto previous = SelectObject(dc, bitmap);
    if (previous != nullptr && previous != HGDI_ERROR) {
        const auto x = (static_cast<int>(width) - extent) / 2;
        const auto y = (static_cast<int>(height) - extent) / 2;
        if (DrawIconEx(dc, x, y, icon, extent, extent, 0U, nullptr,
                       DI_NORMAL) != FALSE) {
            for (std::size_t index = 0U; index < pixels.size(); ++index) {
                pixels[index] = {bgra[index * 4U + 2U],
                                 bgra[index * 4U + 1U],
                                 bgra[index * 4U]};
            }
        }
        static_cast<void>(SelectObject(dc, previous));
    }
    static_cast<void>(DeleteDC(dc));
    static_cast<void>(DeleteObject(bitmap));
    static_cast<void>(DestroyIcon(icon));
}

[[nodiscard]] std::uint8_t limited_luma(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        16.0 + 0.182586 * pixel.red + 0.614231 * pixel.green +
            0.062007 * pixel.blue,
        16.0, 235.0));
}

[[nodiscard]] std::uint8_t limited_blue(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        128.0 - 0.100644 * pixel.red - 0.338572 * pixel.green +
            0.439216 * pixel.blue,
        16.0, 240.0));
}

[[nodiscard]] std::uint8_t limited_red(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        128.0 + 0.439216 * pixel.red - 0.398942 * pixel.green -
            0.040274 * pixel.blue,
        16.0, 240.0));
}

void render_standby_nv12(const DirectShowMediaProfile& profile,
                         std::span<std::uint8_t> output) {
    std::vector<RgbPixel> pixels(
        static_cast<std::size_t>(profile.width) * profile.height,
        kStandbyBackground);
    render_standby_icon(pixels, profile.width, profile.height);
    for (std::uint32_t row = 0U; row < profile.height; ++row) {
        for (std::uint32_t column = 0U; column < profile.width; ++column) {
            const auto index =
                static_cast<std::size_t>(row) * profile.width + column;
            output[index] = limited_luma(pixels[index]);
        }
    }
    const auto chroma_offset =
        static_cast<std::size_t>(profile.width) * profile.height;
    for (std::uint32_t row = 0U; row < profile.height; row += 2U) {
        for (std::uint32_t column = 0U; column < profile.width; column += 2U) {
            std::uint32_t blue = 0U;
            std::uint32_t red = 0U;
            for (std::uint32_t dy = 0U; dy < 2U; ++dy) {
                for (std::uint32_t dx = 0U; dx < 2U; ++dx) {
                    const auto pixel = pixels[static_cast<std::size_t>(row + dy) *
                                                  profile.width +
                                              column + dx];
                    blue += limited_blue(pixel);
                    red += limited_red(pixel);
                }
            }
            const auto index = chroma_offset +
                               static_cast<std::size_t>(row / 2U) *
                                   profile.width +
                               column;
            output[index] = static_cast<std::uint8_t>(blue / 4U);
            output[index + 1U] = static_cast<std::uint8_t>(red / 4U);
        }
    }
}

void copy_nv12(const VideoFrameView& source,
               const DirectShowMediaProfile& profile,
               std::span<std::uint8_t> destination) {
    auto* y = destination.data();
    auto* uv = y + static_cast<std::size_t>(profile.width) * profile.height;
    for (std::uint32_t row = 0U; row < profile.height; ++row) {
        std::memcpy(y + static_cast<std::size_t>(row) * profile.width,
                    source.planes[0].data() +
                        static_cast<std::size_t>(row) * source.plane_strides[0],
                    profile.width);
    }
    for (std::uint32_t row = 0U; row < profile.height / 2U; ++row) {
        std::memcpy(uv + static_cast<std::size_t>(row) * profile.width,
                    source.planes[1].data() +
                        static_cast<std::size_t>(row) * source.plane_strides[1],
                    profile.width);
    }
}

} // namespace

DirectShowFrameAdapter::DirectShowFrameAdapter(DirectShowMediaProfile profile)
    : profile_(profile), scaled_nv12_(nv12_size(profile)),
      standby_output_(profile.sample_size()) {
    if (find_media_profile(profile.width, profile.height, profile.pixel_format,
                           profile.fps_numerator,
                           profile.fps_denominator) == nullptr ||
        profile.sample_size() == 0U) {
        throw std::invalid_argument("virtual_camera_media_profile_invalid");
    }
    if (profile.pixel_format == DirectShowPixelFormat::yuy2) {
        i420_scratch_.resize(nv12_size(profile));
    }
    render_standby_nv12(profile_, scaled_nv12_);
    if (!convert_nv12_to_output(scaled_nv12_, standby_output_)) {
        throw std::runtime_error("virtual_camera_standby_generation_failed");
    }
}

const DirectShowMediaProfile& DirectShowFrameAdapter::profile() const noexcept {
    return profile_;
}

bool DirectShowFrameAdapter::write_direct_nv12(
    const VideoFrameView& source,
    const std::span<std::uint8_t> destination) const noexcept {
    try {
        if (profile_.pixel_format != DirectShowPixelFormat::nv12 ||
            source.pixel_format != VideoFramePixelFormat::nv12 ||
            source.width != profile_.width || source.height != profile_.height ||
            !plane_covers_rows(source.planes[0], source.plane_strides[0],
                               source.height, source.width) ||
            !plane_covers_rows(source.planes[1], source.plane_strides[1],
                               source.height / 2U, source.width) ||
            destination.size() != profile_.sample_size()) {
            return false;
        }
        copy_nv12(source, profile_, destination);
        return true;
    } catch (...) {
        return false;
    }
}

bool DirectShowFrameAdapter::adapt(
    const VideoFrameView& source,
    const std::span<std::uint8_t> destination) noexcept {
    try {
        if (source.pixel_format != VideoFramePixelFormat::nv12 ||
            source.width == 0U || source.height == 0U ||
            source.width % 2U != 0U || source.height % 2U != 0U ||
            !plane_covers_rows(source.planes[0], source.plane_strides[0],
                               source.height, source.width) ||
            !plane_covers_rows(source.planes[1], source.plane_strides[1],
                               source.height / 2U, source.width) ||
            destination.size() != profile_.sample_size()) {
            return false;
        }
        if (write_direct_nv12(source, destination)) {
            return true;
        }
        if (!write_nv12(source, scaled_nv12_)) {
            return false;
        }
        return convert_nv12_to_output(scaled_nv12_, destination);
    } catch (...) {
        return false;
    }
}

bool DirectShowFrameAdapter::write_standby(
    const std::span<std::uint8_t> destination) const noexcept {
    if (destination.size() != standby_output_.size()) {
        return false;
    }
    std::memcpy(destination.data(), standby_output_.data(),
                standby_output_.size());
    return true;
}

bool DirectShowFrameAdapter::write_nv12(
    const VideoFrameView& source,
    const std::span<std::uint8_t> destination) noexcept {
    if (destination.size() != nv12_size(profile_)) {
        return false;
    }
    if (source.width == profile_.width && source.height == profile_.height) {
        copy_nv12(source, profile_, destination);
        return true;
    }
    std::fill_n(destination.data(),
                static_cast<std::size_t>(profile_.width) * profile_.height,
                static_cast<std::uint8_t>(16U));
    std::fill(destination.begin() +
                  static_cast<std::ptrdiff_t>(profile_.width) * profile_.height,
              destination.end(), static_cast<std::uint8_t>(128U));

    std::uint32_t active_width = profile_.width;
    std::uint32_t active_height = profile_.height;
    if (static_cast<std::uint64_t>(source.width) * profile_.height >
        static_cast<std::uint64_t>(profile_.width) * source.height) {
        active_height = static_cast<std::uint32_t>(
            static_cast<std::uint64_t>(source.height) * profile_.width /
            source.width);
    } else {
        active_width = static_cast<std::uint32_t>(
            static_cast<std::uint64_t>(source.width) * profile_.height /
            source.height);
    }
    active_width = (std::max)(2U, active_width & ~1U);
    active_height = (std::max)(2U, active_height & ~1U);
    const auto offset_x = ((profile_.width - active_width) / 2U) & ~1U;
    const auto offset_y = ((profile_.height - active_height) / 2U) & ~1U;
    auto* destination_y = destination.data() +
                          static_cast<std::size_t>(offset_y) * profile_.width +
                          offset_x;
    auto* destination_uv =
        destination.data() +
        static_cast<std::size_t>(profile_.width) * profile_.height +
        static_cast<std::size_t>(offset_y / 2U) * profile_.width + offset_x;
    const auto filter =
        source.width > active_width || source.height > active_height
            ? libyuv::kFilterBox
            : libyuv::kFilterBilinear;
    return libyuv::NV12Scale(
               source.planes[0].data(), source.plane_strides[0],
               source.planes[1].data(),
               source.plane_strides[1], static_cast<int>(source.width),
               static_cast<int>(source.height), destination_y,
               static_cast<int>(profile_.width), destination_uv,
               static_cast<int>(profile_.width), static_cast<int>(active_width),
               static_cast<int>(active_height), filter) == 0;
}

bool DirectShowFrameAdapter::convert_nv12_to_output(
    const std::span<const std::uint8_t> nv12,
    const std::span<std::uint8_t> destination) noexcept {
    if (nv12.size() != nv12_size(profile_) ||
        destination.size() != profile_.sample_size()) {
        return false;
    }
    if (profile_.pixel_format == DirectShowPixelFormat::nv12) {
        std::memcpy(destination.data(), nv12.data(), nv12.size());
        return true;
    }
    const auto y_size =
        static_cast<std::size_t>(profile_.width) * profile_.height;
    auto* i420_y = i420_scratch_.data();
    auto* i420_u = i420_y + y_size;
    auto* i420_v = i420_u + y_size / 4U;
    if (libyuv::NV12ToI420(
            nv12.data(), static_cast<int>(profile_.width), nv12.data() + y_size,
            static_cast<int>(profile_.width), i420_y,
            static_cast<int>(profile_.width), i420_u,
            static_cast<int>(profile_.width / 2U), i420_v,
            static_cast<int>(profile_.width / 2U),
            static_cast<int>(profile_.width),
            static_cast<int>(profile_.height)) != 0) {
        return false;
    }
    return libyuv::I420ToYUY2(
               i420_y, static_cast<int>(profile_.width), i420_u,
               static_cast<int>(profile_.width / 2U), i420_v,
               static_cast<int>(profile_.width / 2U), destination.data(),
               static_cast<int>(profile_.width * 2U),
               static_cast<int>(profile_.width),
               static_cast<int>(profile_.height)) == 0;
}

} // namespace solin::media_engine::windows_virtual_camera
