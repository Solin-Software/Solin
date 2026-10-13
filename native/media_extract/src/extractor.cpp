#include "extractor.hpp"
#include "geometry.hpp"
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <limits>
#include <memory>
#include <thread>

namespace solin::media {
namespace {
constexpr int64_t maximum_pixels = 33554432;
constexpr int maximum_packet_bytes = 64 * 1024 * 1024;

template <typename T> class Owner final {
  public:
    Owner(T* value, void (*release)(T**)) : value_(value), release_(release) {}
    ~Owner() { release_(&value_); }
    Owner(const Owner&) = delete;
    Owner& operator=(const Owner&) = delete;
    T* get() const noexcept { return value_; }
    T** address() noexcept { return &value_; }

  private:
    T* value_;
    void (*release_)(T**);
};
class Dictionary final {
  public:
    explicit Dictionary(FFmpeg& api) : api_(api) {}
    ~Dictionary() { api_.av_dict_free(&value); }
    void set(const char* key, const char* text) {
        if (api_.av_dict_set(&value, key, text, 0) < 0)
            throw Failure("transient", "resource_exhausted", "Cannot allocate media options");
    }
    AVDictionary* value = nullptr;

  private:
    FFmpeg& api_;
};
[[noreturn]] void media_failure(FFmpeg& api, Deadline& deadline, int error, const char* stage) {
    deadline.check();
    std::string kind = "format", code = "invalid_media";
    switch (error) {
    case AVERROR(ENOENT):
        kind = "permanent";
        code = "source_not_found";
        break;
    case AVERROR(EACCES):
    case AVERROR(EPERM):
    case AVERROR_HTTP_UNAUTHORIZED:
    case AVERROR_HTTP_FORBIDDEN:
        kind = "permanent";
        code = "permission_denied";
        break;
    case AVERROR_HTTP_NOT_FOUND:
        kind = "permanent";
        code = "source_unavailable";
        break;
    case AVERROR_PROTOCOL_NOT_FOUND:
    case AVERROR_DEMUXER_NOT_FOUND:
    case AVERROR_DECODER_NOT_FOUND:
    case AVERROR(ENOSYS):
        kind = "format";
        code = "unsupported_media";
        break;
    case AVERROR(ENOMEM):
        kind = "transient";
        code = "resource_exhausted";
        break;
    case AVERROR(ETIMEDOUT):
    case AVERROR(EAGAIN):
    case AVERROR(EIO):
    case AVERROR(ECONNREFUSED):
    case AVERROR(ECONNRESET):
    case AVERROR(ENETUNREACH):
    // OTHER_4XX includes retryable statuses (408/409/423/425 and 429 on
    // older FFmpeg). The exact status is unavailable in this public error.
    case AVERROR_HTTP_OTHER_4XX:
#ifdef AVERROR_HTTP_TOO_MANY_REQUESTS
    case AVERROR_HTTP_TOO_MANY_REQUESTS:
#endif
    case AVERROR_HTTP_SERVER_ERROR:
    case AVERROR_EXIT:
        kind = "transient";
        code = "media_io_failed";
        break;
    default:
        break;
    }
    char detail[AV_ERROR_MAX_STRING_SIZE]{};
    api.av_strerror(error, detail, sizeof(detail));
    throw Failure(std::move(kind), std::move(code), std::string(stage) + ": " + detail);
}
void require_allocated(const void* value) {
    if (!value)
        throw Failure("transient", "resource_exhausted", "Cannot allocate media context");
}
bool image_codec(AVCodecID codec) {
    switch (codec) {
    case AV_CODEC_ID_PNG:
    case AV_CODEC_ID_MJPEG:
    case AV_CODEC_ID_BMP:
    case AV_CODEC_ID_GIF:
    case AV_CODEC_ID_WEBP:
    case AV_CODEC_ID_TIFF:
    case AV_CODEC_ID_PPM:
        return true;
    default:
        return false;
    }
}
struct Streams {
    int cover = -1, frame = -1;
    bool has_video = false;
};
Streams classify(const AVFormatContext* format) {
    int attached = -1, image = -1, moving = -1, standalone = -1;
    bool audio = false;
    for (unsigned i = 0; i < format->nb_streams; ++i) {
        const auto* stream = format->streams[i];
        if (!stream || !stream->codecpar)
            throw Failure("format", "invalid_media", "Missing stream parameters");
        const auto* parameters = stream->codecpar;
        if (parameters->codec_type == AVMEDIA_TYPE_AUDIO) {
            audio = true;
            continue;
        }
        if (parameters->codec_type != AVMEDIA_TYPE_VIDEO)
            continue;
        const int index = static_cast<int>(i);
        if (stream->disposition & AV_DISPOSITION_ATTACHED_PIC) {
            if (attached < 0)
                attached = index;
        } else {
            if (standalone < 0)
                standalone = index;
            if (image_codec(parameters->codec_id)) {
                if (image < 0)
                    image = index;
            } else if (moving < 0)
                moving = index;
        }
    }
    const int cover = attached >= 0 ? attached : ((audio || moving >= 0) ? image : -1);
    // An image/GIF source is still a frame candidate, while has_video preserves metadata semantics.
    return {cover, moving >= 0 ? moving : standalone, moving >= 0};
}
int64_t duration_microseconds(FFmpeg& api, const AVFormatContext* format) {
    if (format->duration != AV_NOPTS_VALUE && format->duration > 0)
        return format->duration;
    int64_t result = 0;
    for (unsigned i = 0; i < format->nb_streams; ++i) {
        const auto* stream = format->streams[i];
        if (stream->duration != AV_NOPTS_VALUE && stream->duration > 0 &&
            stream->time_base.num > 0 && stream->time_base.den > 0) {
            const auto duration =
                api.av_rescale_q(stream->duration, stream->time_base, {1, AV_TIME_BASE});
            if (duration > 0)
                result = std::max(result, duration);
        }
    }
    return result;
}
std::string title_tag(FFmpeg& api, AVDictionary* metadata) {
    AVDictionaryEntry* entry = nullptr;
    while ((entry = api.av_dict_get(metadata, "", entry, AV_DICT_IGNORE_SUFFIX))) {
        const auto* key = entry->key;
        constexpr char title[] = "title";
        bool matches = key != nullptr;
        if (matches) {
            for (std::size_t i = 0; i < sizeof(title); ++i) {
                char character = key[i];
                if (character >= 'A' && character <= 'Z')
                    character = static_cast<char>(character - 'A' + 'a');
                if (character != title[i]) {
                    matches = false;
                    break;
                }
                if (!character)
                    break;
            }
        }
        if (!matches || !entry->value)
            continue;
        std::string value(entry->value);
        const auto start = value.find_first_not_of(" \t\r\n\v\f");
        if (start == std::string::npos)
            continue;
        return value.substr(start, value.find_last_not_of(" \t\r\n\v\f") - start + 1);
    }
    return {};
}
Orientation orientation(FFmpeg& api, AVFrame* frame, const AVStream* stream) {
    const uint8_t* data = nullptr;
    std::size_t size = 0;
    if (const auto* side = api.av_frame_get_side_data(frame, AV_FRAME_DATA_DISPLAYMATRIX)) {
        data = side->data;
        size = side->size;
    } else {
        const auto* parameters = stream->codecpar;
        for (int i = 0; i < parameters->nb_coded_side_data; ++i) {
            const auto& stream_side = parameters->coded_side_data[i];
            if (stream_side.type == AV_PKT_DATA_DISPLAYMATRIX) {
                data = stream_side.data;
                size = stream_side.size;
                break;
            }
        }
    }
    if (!data && size == 0)
        return {};
    if (!data || size < 9 * sizeof(int32_t))
        throw Failure("format", "invalid_display_matrix", "Truncated display matrix");
    std::array<int32_t, 9> matrix{};
    std::memcpy(matrix.data(), data, sizeof(matrix));
    return display_orientation(matrix);
}
Image convert_frame(FFmpeg& api, AVFormatContext* format, AVStream* stream, AVFrame* frame,
                    const Options& options, const char* kind, Deadline& deadline) {
    deadline.check();
    const auto pixel_format = static_cast<AVPixelFormat>(frame->format);
    if (frame->width <= 0 || frame->height <= 0 || frame->width > 16384 || frame->height > 16384 ||
        api.av_image_check_size2(static_cast<unsigned>(frame->width),
                                 static_cast<unsigned>(frame->height), maximum_pixels, pixel_format,
                                 0, nullptr) < 0)
        throw Failure("format", "invalid_image_dimensions",
                      "Decoded image exceeds safe size limits");
    const auto* descriptor = api.av_pix_fmt_desc_get(pixel_format);
    if (!descriptor || (descriptor->flags & AV_PIX_FMT_FLAG_HWACCEL) ||
        !api.sws_isSupportedInput(pixel_format))
        throw Failure("format", "unsupported_pixel_format",
                      "Decoded image has an unsupported software pixel format");
    if (frame->flags & AV_FRAME_FLAG_CORRUPT || frame->decode_error_flags)
        throw Failure("format", "invalid_media", "Corrupt decoded image");
    const auto sample_aspect = api.av_guess_sample_aspect_ratio(format, stream, frame);
    const double sar = sample_aspect.num > 0 && sample_aspect.den > 0
                           ? static_cast<double>(sample_aspect.num) / sample_aspect.den
                           : 1;
    const auto geometry =
        contain_geometry(frame->width, frame->height, sar, orientation(api, frame, stream),
                         options.width, options.height);
    std::unique_ptr<SwsContext, decltype(api.sws_freeContext)> scale(
        api.sws_getContext(frame->width, frame->height, pixel_format, geometry.scaled_width,
                           geometry.scaled_height, AV_PIX_FMT_BGRA, SWS_BILINEAR, nullptr, nullptr,
                           nullptr),
        api.sws_freeContext);
    require_allocated(scale.get());
    int colorspace = SWS_CS_DEFAULT;
    switch (frame->colorspace) {
    case AVCOL_SPC_BT709:
        colorspace = SWS_CS_ITU709;
        break;
    case AVCOL_SPC_BT2020_NCL:
    case AVCOL_SPC_BT2020_CL:
        colorspace = SWS_CS_BT2020;
        break;
    case AVCOL_SPC_FCC:
        colorspace = SWS_CS_FCC;
        break;
    case AVCOL_SPC_SMPTE240M:
        colorspace = SWS_CS_SMPTE240M;
        break;
    default:
        break;
    }
    const auto* coefficients = api.sws_getCoefficients(colorspace);
    if (api.sws_setColorspaceDetails(scale.get(), coefficients,
                                     frame->color_range == AVCOL_RANGE_JPEG, coefficients, 1, 0,
                                     1 << 16, 1 << 16) < 0)
        throw Failure("format", "unsupported_colorspace", "Cannot convert image colorspace");
    const int stride = geometry.scaled_width * 4;
    // libswscale may write SIMD padding; the separate trailing padding is never emitted.
    std::vector<uint8_t> pixels(static_cast<std::size_t>(stride) * geometry.scaled_height + 64);
    uint8_t* destinations[4]{pixels.data(), nullptr, nullptr, nullptr};
    int strides[4]{stride, 0, 0, 0};
    const int rows = api.sws_scale(scale.get(), frame->data, frame->linesize, 0, frame->height,
                                   destinations, strides);
    if (rows != geometry.scaled_height)
        throw Failure("format", "image_conversion_failed", "Incomplete image conversion");
    pixels.resize(static_cast<std::size_t>(stride) * geometry.scaled_height);
    deadline.check();
    auto image = orient_bgra(std::move(pixels), geometry, kind);
    deadline.check();
    return image;
}
AVPixelFormat software_format(AVCodecContext* context, const AVPixelFormat* formats) {
    const auto* api = static_cast<FFmpeg*>(context->opaque);
    for (auto format = formats; *format != AV_PIX_FMT_NONE; ++format) {
        const auto* descriptor = api->av_pix_fmt_desc_get(*format);
        if (descriptor && !(descriptor->flags & AV_PIX_FMT_FLAG_HWACCEL))
            return *format;
    }
    return AV_PIX_FMT_NONE;
}

class Decoder final {
  public:
    Decoder(FFmpeg& api, AVStream* stream, Deadline& deadline)
        : api_(api), stream_(stream), context_(nullptr, api.avcodec_free_context),
          packet_(api.av_packet_alloc(), api.av_packet_free),
          frame_(api.av_frame_alloc(), api.av_frame_free) {
        require_allocated(packet_.get());
        require_allocated(frame_.get());
        const auto* codec = api.avcodec_find_decoder(stream->codecpar->codec_id);
        if (!codec)
            throw Failure("format", "unsupported_media", "No software decoder for requested image");
        *context_.address() = api.avcodec_alloc_context3(codec);
        require_allocated(context_.get());
        int status = api.avcodec_parameters_to_context(context_.get(), stream->codecpar);
        if (status < 0)
            media_failure(api, deadline, status, "Cannot initialize image decoder");
        context_.get()->thread_count = 1;
        context_.get()->thread_type = 0;
        context_.get()->max_pixels = maximum_pixels;
        context_.get()->err_recognition = AV_EF_CAREFUL | AV_EF_EXPLODE;
        context_.get()->pkt_timebase = stream->time_base;
        context_.get()->opaque = &api;
        context_.get()->get_format = software_format;
        status = api.avcodec_open2(context_.get(), codec, nullptr);
        if (status < 0)
            media_failure(api, deadline, status, "Cannot open image decoder");
    }
    void flush() {
        api_.avcodec_flush_buffers(context_.get());
        api_.av_packet_unref(packet_.get());
        api_.av_frame_unref(frame_.get());
    }
    int64_t last_timestamp() const noexcept { return last_timestamp_; }
    std::optional<Image> read(AVFormatContext* format, const Options& options, Deadline& deadline,
                              int64_t target, const char* kind, bool attached) {
        bool pending = false, source_eof = false, drain_sent = false;
        bool attached_sent = false;
        while (true) {
            deadline.check();
            const int received = api_.avcodec_receive_frame(context_.get(), frame_.get());
            if (received >= 0) {
                const int64_t timestamp = frame_.get()->best_effort_timestamp != AV_NOPTS_VALUE
                                              ? frame_.get()->best_effort_timestamp
                                              : frame_.get()->pts;
                last_timestamp_ = timestamp;
                if (target == AV_NOPTS_VALUE || timestamp == AV_NOPTS_VALUE || timestamp >= target)
                    return convert_frame(api_, format, stream_, frame_.get(), options, kind,
                                         deadline);
                api_.av_frame_unref(frame_.get());
                continue;
            }
            if (received == AVERROR_EOF)
                return std::nullopt;
            if (received != AVERROR(EAGAIN))
                media_failure(api_, deadline, received, "Cannot decode image");
            if (drain_sent)
                throw Failure("format", "decoder_stalled", "Image decoder did not drain");
            if (!pending && !source_eof) {
                if (attached) {
                    source_eof = attached_sent;
                    pending = !attached_sent;
                } else {
                    int status = 0;
                    do {
                        deadline.check();
                        api_.av_packet_unref(packet_.get());
                        status = api_.av_read_frame(format, packet_.get());
                        if (status == AVERROR(EAGAIN)) {
                            std::this_thread::sleep_for(std::chrono::milliseconds(1));
                            continue;
                        }
                        if (status == AVERROR_EOF) {
                            source_eof = true;
                            break;
                        }
                        if (status < 0)
                            media_failure(api_, deadline, status, "Cannot read image packet");
                        if (packet_.get()->size < 0 || packet_.get()->size > maximum_packet_bytes)
                            throw Failure("format", "invalid_packet_size",
                                          "Image packet exceeds safe size limits");
                        if (packet_.get()->stream_index == stream_->index) {
                            pending = true;
                            break;
                        }
                    } while (!pending);
                }
            }
            const AVPacket* input =
                pending ? (attached ? &stream_->attached_pic : packet_.get()) : nullptr;
            if (input && (input->size <= 0 || input->size > maximum_packet_bytes || !input->data))
                throw Failure("format", "invalid_media", "Missing or oversized image packet");
            const int sent = api_.avcodec_send_packet(context_.get(), input);
            if (sent == AVERROR(EAGAIN))
                continue; // Retain and retry exactly this packet after receiving output.
            if (sent < 0)
                media_failure(api_, deadline, sent, "Cannot submit image packet");
            if (!input)
                drain_sent = true;
            else {
                pending = false;
                if (attached)
                    attached_sent = true;
                else
                    api_.av_packet_unref(packet_.get());
            }
        }
    }

  private:
    FFmpeg& api_;
    AVStream* stream_;
    Owner<AVCodecContext> context_;
    Owner<AVPacket> packet_;
    Owner<AVFrame> frame_;
    int64_t last_timestamp_ = AV_NOPTS_VALUE;
};

std::optional<Image> extract_image(FFmpeg& api, AVFormatContext* format, const Options& options,
                                   const Streams& streams, int64_t duration, Deadline& deadline) {
    const bool cover = streams.cover >= 0;
    int index =
        cover ? streams.cover : (options.image_policy == ImagePolicy::frame ? streams.frame : -1);
    if (index < 0)
        return std::nullopt;
    deadline.begin_extraction();
    if (cover) {
        try {
            auto* stream = format->streams[index];
            Decoder decoder(api, stream, deadline);
            const bool attached = (stream->disposition & AV_DISPOSITION_ATTACHED_PIC) != 0;
            auto image = decoder.read(format, options, deadline, AV_NOPTS_VALUE, "cover", attached);
            if (!image)
                throw Failure("format", "image_not_decoded",
                              "Cover stream contains no decodable image");
            return image;
        } catch (const Failure& failure) {
            if (options.image_policy != ImagePolicy::frame || failure.kind != "format" ||
                streams.frame < 0 || streams.frame == index)
                throw;
            deadline.check();
            index = streams.frame;
        }
    }
    auto* stream = format->streams[index];
    if (stream->time_base.num <= 0 || stream->time_base.den <= 0)
        throw Failure("format", "invalid_time_base", "Invalid image stream time base");
    Decoder decoder(api, stream, deadline);
    const int64_t origin =
        format->start_time != AV_NOPTS_VALUE
            ? api.av_rescale_q(format->start_time, {1, AV_TIME_BASE}, stream->time_base)
            : (stream->start_time != AV_NOPTS_VALUE ? stream->start_time : 0);
    const int64_t offset = api.av_rescale_q(duration > 0 ? duration / 20 : AV_TIME_BASE,
                                            {1, AV_TIME_BASE}, stream->time_base);
    if (offset < 0 || origin > std::numeric_limits<int64_t>::max() - offset)
        throw Failure("format", "invalid_timestamp", "Image timestamp exceeds supported range");
    const int64_t target = origin + offset;
    // Cache a decode-safe start before seeking: failed seeks may discard probe
    // packets, while resetting to presentation start can skip an earlier I-frame DTS.
    auto first = decoder.read(format, options, deadline, AV_NOPTS_VALUE, "frame", false);
    if (!first)
        throw Failure("format", "image_not_decoded", "Video stream contains no decodable image");
    if (decoder.last_timestamp() == AV_NOPTS_VALUE || decoder.last_timestamp() >= target)
        return first;
    // HTTP Range is left to libavformat. Nonseekable inputs retain probe packets and read forward.
    const bool seekable = !format->pb || (format->pb->seekable & AVIO_SEEKABLE_NORMAL);
    const int64_t forward_window =
        api.av_rescale_q(AV_TIME_BASE, {1, AV_TIME_BASE}, stream->time_base);
    if (seekable && offset > forward_window) {
        const int status = api.avformat_seek_file(
            format, index, std::numeric_limits<int64_t>::min(), target, target, 0);
        deadline.check();
        if (status < 0)
            return first;
        decoder.flush();
    }
    auto image = decoder.read(format, options, deadline, target, "frame", false);
    return image ? std::move(image) : std::move(first);
}

} // namespace

Result extract(FFmpeg& api, const Options& options, Deadline& deadline) {
    deadline.check();
    Owner<AVFormatContext> format(api.avformat_alloc_context(), api.avformat_close_input);
    require_allocated(format.get());
    format.get()->interrupt_callback = {Deadline::callback, &deadline};
    Dictionary input_options(api);
    input_options.set("rw_timeout", "15000000");
    input_options.set("probesize", "5000000");
    input_options.set("analyzeduration", "5000000");
    input_options.set("max_streams", "128");
    input_options.set("skip_estimate_duration_from_pts", "1");
    int status = api.avformat_open_input(format.address(), options.source.c_str(), nullptr,
                                         &input_options.value);
    if (status < 0)
        media_failure(api, deadline, status, "Cannot open media source");
    deadline.check();
    // FFmpeg 6.1/7.1 also forces threads=1 for streams discovered during probing.
    // Known streams additionally receive pixel and thread-type limits here.
    struct ProbeOptions {
        FFmpeg& api;
        std::vector<AVDictionary*> values;
        ~ProbeOptions() {
            for (auto*& value : values)
                api.av_dict_free(&value);
        }
    } probe{api, std::vector<AVDictionary*>(format.get()->nb_streams, nullptr)};
    for (auto*& value : probe.values) {
        if (api.av_dict_set(&value, "threads", "1", 0) < 0 ||
            api.av_dict_set(&value, "thread_type", "0", 0) < 0 ||
            api.av_dict_set(&value, "max_pixels", "33554432", 0) < 0)
            throw Failure("transient", "resource_exhausted",
                          "Cannot allocate stream probe options");
    }
    status = api.avformat_find_stream_info(format.get(),
                                           probe.values.empty() ? nullptr : probe.values.data());
    if (status < 0)
        media_failure(api, deadline, status, "Cannot probe media source");
    deadline.check();
    if (!format.get()->nb_streams)
        throw Failure("format", "invalid_media", "Media source contains no streams");
    const auto streams = classify(format.get());
    const int64_t duration = duration_microseconds(api, format.get());
    Result result;
    result.has_video = streams.has_video;
    if (options.title)
        result.title = title_tag(api, format.get()->metadata);
    if (options.duration)
        result.duration_ms = duration / 1000;
    if (options.image_policy != ImagePolicy::none)
        result.image = extract_image(api, format.get(), options, streams, duration, deadline);
    deadline.check();
    return result;
}
} // namespace solin::media
