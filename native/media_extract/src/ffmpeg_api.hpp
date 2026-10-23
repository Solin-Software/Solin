#pragma once

#include "protocol.hpp"
#include <array>
#include <filesystem>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/dict.h>
#include <libavutil/frame.h>
#include <libavutil/imgutils.h>
#include <libavutil/pixdesc.h>
#include <libswscale/swscale.h>
}

namespace solin::media {

#if defined(__linux__)
constexpr std::array<unsigned, 4> minimum_versions = {
    AV_VERSION_INT(60, 16, 100), AV_VERSION_INT(60, 31, 102), AV_VERSION_INT(58, 29, 100),
    AV_VERSION_INT(7, 5, 100)};
#else
constexpr std::array<unsigned, 4> minimum_versions = {
    AV_VERSION_INT(62, 12, 102), AV_VERSION_INT(62, 28, 102), AV_VERSION_INT(60, 26, 102),
    AV_VERSION_INT(9, 5, 102)};
#endif
constexpr std::array<unsigned, 4> compiled_versions = {
    LIBAVFORMAT_VERSION_INT, LIBAVCODEC_VERSION_INT, LIBAVUTIL_VERSION_INT, LIBSWSCALE_VERSION_INT};
constexpr bool valid_sdk() {
    for (std::size_t i = 0; i < compiled_versions.size(); ++i) {
        if (AV_VERSION_MAJOR(compiled_versions[i]) != AV_VERSION_MAJOR(minimum_versions[i]) ||
            compiled_versions[i] < minimum_versions[i])
            return false;
    }
    return true;
}
static_assert(
    valid_sdk(),
    "FFmpeg SDK must match platform ABI and minimum FFmpeg 6.1 (Linux) / 8.x (Windows/macOS)");

// Function pointer types come exclusively from the public SDK declarations.
#define SOLIN_FORMAT_FUNCTIONS(X)                                                                  \
    X(avformat_version)                                                                            \
    X(avformat_alloc_context)                                                                      \
    X(avformat_open_input)                                                                         \
    X(avformat_find_stream_info)                                                                   \
    X(avformat_close_input) X(av_read_frame) X(avformat_seek_file) X(avformat_network_init)        \
        X(avformat_network_deinit) X(av_guess_sample_aspect_ratio)
#define SOLIN_CODEC_FUNCTIONS(X)                                                                   \
    X(avcodec_version)                                                                             \
    X(avcodec_find_decoder)                                                                        \
    X(avcodec_alloc_context3)                                                                      \
    X(avcodec_parameters_to_context)                                                               \
    X(avcodec_open2) X(avcodec_free_context) X(avcodec_send_packet) X(avcodec_receive_frame)       \
        X(avcodec_flush_buffers) X(av_packet_alloc) X(av_packet_free) X(av_packet_unref)
#define SOLIN_UTIL_FUNCTIONS(X)                                                                    \
    X(avutil_version)                                                                              \
    X(av_dict_set)                                                                                 \
    X(av_dict_get)                                                                                 \
    X(av_dict_free)                                                                                \
    X(av_frame_alloc) X(av_frame_free) X(av_frame_unref) X(av_frame_get_side_data) X(av_rescale_q) \
        X(av_log_set_callback) X(av_strerror) X(av_image_check_size2) X(av_pix_fmt_desc_get)
#define SOLIN_SCALE_FUNCTIONS(X)                                                                   \
    X(swscale_version)                                                                             \
    X(sws_getContext)                                                                              \
    X(sws_scale)                                                                                   \
    X(sws_freeContext) X(sws_isSupportedInput) X(sws_setColorspaceDetails) X(sws_getCoefficients)

class FFmpeg final {
  public:
    explicit FFmpeg(const std::filesystem::path& runtime_directory);
    ~FFmpeg();
    FFmpeg(const FFmpeg&) = delete;
    FFmpeg& operator=(const FFmpeg&) = delete;
    std::array<unsigned, 4> runtime_versions{};
    std::string version_header() const;
#define SOLIN_DECLARE(name) decltype(&::name) name = nullptr;
    SOLIN_FORMAT_FUNCTIONS(SOLIN_DECLARE)
    SOLIN_CODEC_FUNCTIONS(SOLIN_DECLARE)
    SOLIN_UTIL_FUNCTIONS(SOLIN_DECLARE)
    SOLIN_SCALE_FUNCTIONS(SOLIN_DECLARE)
#undef SOLIN_DECLARE
  private:
    std::array<void*, 4> handles_{};
    std::array<std::string, 4> library_paths_{};
    bool network_initialized_ = false;
    void close() noexcept;
};

std::filesystem::path executable_directory();
std::filesystem::path runtime_directory();

} // namespace solin::media
