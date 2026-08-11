#include "solin/media_engine/frame_channel.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace solin::media_engine {
namespace {

constexpr std::array<std::uint8_t, 8U> kMagic{'S', 'L', 'N', 'F', 'R', 'M', '0', '1'};
constexpr std::uint16_t kVersion = 3U;
constexpr std::size_t kHeaderSize = 128U;
constexpr std::size_t kSlotHeaderSize = 64U;
constexpr std::uint32_t kSlotCount = 3U;
constexpr std::uint32_t kBgraPixelFormat = 1U;
constexpr std::uint32_t kNv12PixelFormat = 2U;
constexpr std::uint32_t kDynamicPixelFormat = 0U;
constexpr std::size_t kPublishedSequenceOffset = 56U;
constexpr std::size_t kMaximumFrameBytes = 3'840U * 2'160U * 4U;
constexpr std::uint64_t kMaximumSequence = (std::numeric_limits<std::int64_t>::max)() / 2U;
#ifdef _WIN32
constexpr std::wstring_view kMutexPrefix = L"Local\\SolinFrameMutex.";
#endif

template <typename Value>
[[nodiscard]] Value read_value(const std::uint8_t* bytes, const std::size_t offset) {
    Value value{};
    std::memcpy(&value, bytes + offset, sizeof(Value));
    return value;
}

template <typename Value>
void write_value(std::uint8_t* bytes, const std::size_t offset, const Value value) {
    std::memcpy(bytes + offset, &value, sizeof(Value));
}

void validate_configuration(const FrameChannelConfiguration& configuration) {
    const auto frame_bytes =
        static_cast<std::uint64_t>(configuration.width) * configuration.height * 4U;
    const auto fixed_bgra =
        configuration.transport == "shared_memory_bgra" && configuration.pixel_format == "bgra";
    const auto dynamic_video =
        configuration.transport == "shared_memory_video" && configuration.pixel_format == "dynamic";
    if ((!fixed_bgra && !dynamic_video) || configuration.handle_token.empty() ||
        configuration.width == 0U || configuration.height == 0U ||
        frame_bytes > kMaximumFrameBytes) {
        throw std::runtime_error("frame_channel_configuration_invalid");
    }
}

void validate_writer_configuration(const FrameChannelConfiguration& configuration) {
    validate_configuration(configuration);
}

#ifdef _WIN32

class UniqueHandle final {
  public:
    explicit UniqueHandle(HANDLE value = nullptr) noexcept : value_(value) {}
    ~UniqueHandle() {
        if (value_ != nullptr) {
            static_cast<void>(CloseHandle(value_));
        }
    }

    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    UniqueHandle(UniqueHandle&&) = delete;
    UniqueHandle& operator=(UniqueHandle&&) = delete;

    [[nodiscard]] HANDLE get() const noexcept { return value_; }

  private:
    HANDLE value_{nullptr};
};

class UniqueView final {
  public:
    explicit UniqueView(void* value = nullptr) noexcept : value_(value) {}
    ~UniqueView() {
        if (value_ != nullptr) {
            static_cast<void>(UnmapViewOfFile(value_));
        }
    }

    UniqueView(const UniqueView&) = delete;
    UniqueView& operator=(const UniqueView&) = delete;
    UniqueView(UniqueView&& other) noexcept : value_(std::exchange(other.value_, nullptr)) {}
    UniqueView& operator=(UniqueView&& other) noexcept {
        if (this != &other) {
            if (value_ != nullptr) {
                static_cast<void>(UnmapViewOfFile(value_));
            }
            value_ = std::exchange(other.value_, nullptr);
        }
        return *this;
    }

    [[nodiscard]] void* get() const noexcept { return value_; }

  private:
    void* value_{nullptr};
};

[[nodiscard]] std::wstring utf8_to_utf16(const std::string& value) {
    if (value.empty() ||
        value.size() > static_cast<std::size_t>((std::numeric_limits<int>::max)())) {
        throw std::runtime_error("frame_channel_handle_invalid");
    }
    const auto length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                                            static_cast<int>(value.size()), nullptr, 0);
    if (length <= 0) {
        throw std::runtime_error("frame_channel_handle_invalid");
    }
    std::wstring result(static_cast<std::size_t>(length), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                            static_cast<int>(value.size()), result.data(), length) != length) {
        throw std::runtime_error("frame_channel_handle_invalid");
    }
    return result;
}

class MutexLease final {
  public:
    explicit MutexLease(HANDLE mutex) : mutex_(mutex) {
        const auto result = WaitForSingleObject(mutex_, 0U);
        if (result == WAIT_TIMEOUT) {
            return;
        }
        if (result != WAIT_OBJECT_0 && result != WAIT_ABANDONED) {
            throw std::runtime_error("frame_channel_mutex_failed");
        }
        acquired_ = true;
    }

    ~MutexLease() {
        if (acquired_) {
            static_cast<void>(ReleaseMutex(mutex_));
        }
    }

    MutexLease(const MutexLease&) = delete;
    MutexLease& operator=(const MutexLease&) = delete;

    [[nodiscard]] explicit operator bool() const noexcept { return acquired_; }

  private:
    HANDLE mutex_{nullptr};
    bool acquired_{false};
};

class WindowsSharedMemoryVideoReader final : public FrameChannelReader {
  public:
    explicit WindowsSharedMemoryVideoReader(FrameChannelConfiguration configuration)
        : configuration_(std::move(configuration)) {
        validate_configuration(configuration_);
        const auto expected = expected_mapping_size();
        const auto mapping_name = utf8_to_utf16(configuration_.handle_token);
        mapping_ = std::make_unique<UniqueHandle>(
            OpenFileMappingW(FILE_MAP_READ, FALSE, mapping_name.c_str()));
        if (mapping_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mapping_unavailable");
        }
        const UniqueView header_view{
            MapViewOfFile(mapping_->get(), FILE_MAP_READ, 0U, 0U, kHeaderSize)};
        if (header_view.get() == nullptr) {
            throw std::runtime_error("frame_channel_header_unavailable");
        }
        const auto* header = static_cast<const std::uint8_t*>(header_view.get());
        validate_header_prefix(header);
        mapping_size_ = read_value<std::uint64_t>(header, 48U);
        if (mapping_size_ != expected ||
            mapping_size_ > static_cast<std::uint64_t>((std::numeric_limits<SIZE_T>::max)())) {
            throw std::runtime_error("frame_channel_mapping_size_invalid");
        }
        view_ = UniqueView{MapViewOfFile(mapping_->get(), FILE_MAP_READ, 0U, 0U,
                                         static_cast<SIZE_T>(mapping_size_))};
        if (view_.get() == nullptr) {
            throw std::runtime_error("frame_channel_mapping_unavailable");
        }
        const auto mutex_name = std::wstring{kMutexPrefix} + mapping_name;
        mutex_ = std::make_unique<UniqueHandle>(
            OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, mutex_name.c_str()));
        if (mutex_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mutex_unavailable");
        }
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            throw std::runtime_error("frame_channel_writer_busy");
        }
        validate_header_prefix(bytes());
    }

    [[nodiscard]] std::optional<FrameChannelFrame> read_latest() override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        const auto* channel = bytes();
        validate_header_prefix(channel);
        const auto published_sequence =
            read_value<std::uint64_t>(channel, kPublishedSequenceOffset);
        if (published_sequence == 0U || published_sequence <= last_sequence_) {
            return std::nullopt;
        }
        if (published_sequence > kMaximumSequence) {
            throw std::runtime_error("frame_channel_sequence_invalid");
        }
        const auto slot_index = (published_sequence - 1U) % kSlotCount;
        const auto slot_offset = kHeaderSize + static_cast<std::size_t>(slot_index) * slot_size_;
        const auto marker = read_value<std::uint64_t>(channel, slot_offset);
        const auto frame_sequence = read_value<std::uint64_t>(channel, slot_offset + 8U);
        const auto payload_size = read_value<std::uint64_t>(channel, slot_offset + 40U);
        const auto width = read_value<std::uint32_t>(channel, slot_offset + 48U);
        const auto height = read_value<std::uint32_t>(channel, slot_offset + 52U);
        const auto stride = read_value<std::uint32_t>(channel, slot_offset + 56U);
        const auto pixel_format = read_value<std::uint32_t>(channel, slot_offset + 60U);
        const auto bgra = pixel_format == kBgraPixelFormat;
        const auto nv12 = pixel_format == kNv12PixelFormat;
        const auto actual_frame_bytes =
            static_cast<std::uint64_t>(width) * height * (bgra ? 4U : 3U) / (bgra ? 1U : 2U);
        const auto expected_stride = width * (bgra ? 4U : 1U);
        const auto format_allowed =
            configuration_.transport == "shared_memory_video" ? (bgra || nv12) : bgra;
        if (marker != published_sequence * 2U || frame_sequence != published_sequence ||
            width == 0U || width > configuration_.width || height == 0U ||
            height > configuration_.height ||
            (nv12 && ((width & 1U) != 0U || (height & 1U) != 0U)) || stride != expected_stride ||
            !format_allowed || payload_size != actual_frame_bytes || payload_size > frame_bytes_) {
            return std::nullopt;
        }
        FrameChannelFrame result{
            .sequence = frame_sequence,
            .presentation_timestamp_ns = read_value<std::uint64_t>(channel, slot_offset + 16U),
            .duration_ns = read_value<std::uint64_t>(channel, slot_offset + 24U),
            .produced_monotonic_ns = read_value<std::uint64_t>(channel, slot_offset + 32U),
            .width = width,
            .height = height,
            .pixel_format = bgra ? VideoFramePixelFormat::bgra : VideoFramePixelFormat::nv12,
            .bytes = std::vector<std::uint8_t>(static_cast<std::size_t>(payload_size)),
        };
        std::memcpy(result.bytes.data(), channel + slot_offset + kSlotHeaderSize,
                    static_cast<std::size_t>(payload_size));
        last_sequence_ = published_sequence;
        return result;
    }

  private:
    [[nodiscard]] const std::uint8_t* bytes() const noexcept {
        return static_cast<const std::uint8_t*>(view_.get());
    }

    [[nodiscard]] std::uint64_t expected_mapping_size() {
        frame_bytes_ = static_cast<std::size_t>(configuration_.width) * configuration_.height * 4U;
        slot_size_ = kSlotHeaderSize + frame_bytes_;
        return kHeaderSize + static_cast<std::uint64_t>(kSlotCount) * slot_size_;
    }

    void validate_header_prefix(const std::uint8_t* header) const {
        if (!std::equal(kMagic.begin(), kMagic.end(), header) ||
            read_value<std::uint16_t>(header, 8U) != kVersion ||
            read_value<std::uint16_t>(header, 10U) != kHeaderSize ||
            read_value<std::uint32_t>(header, 12U) != kSlotCount ||
            read_value<std::uint32_t>(header, 16U) != configuration_.width ||
            read_value<std::uint32_t>(header, 20U) != configuration_.height ||
            read_value<std::uint32_t>(header, 24U) !=
                (configuration_.transport == "shared_memory_video" ? 0U
                                                                   : configuration_.width * 4U) ||
            read_value<std::uint32_t>(header, 28U) !=
                (configuration_.transport == "shared_memory_video" ? kDynamicPixelFormat
                                                                   : kBgraPixelFormat) ||
            read_value<std::uint64_t>(header, 32U) != configuration_.generation ||
            read_value<std::uint64_t>(header, 40U) != slot_size_) {
            throw std::runtime_error("frame_channel_header_invalid");
        }
    }

    FrameChannelConfiguration configuration_{};
    std::unique_ptr<UniqueHandle> mapping_{};
    std::unique_ptr<UniqueHandle> mutex_{};
    UniqueView view_{};
    std::uint64_t mapping_size_{0U};
    std::size_t frame_bytes_{0U};
    std::size_t slot_size_{0U};
    std::uint64_t last_sequence_{0U};
};

class WindowsSharedMemoryFrameWriter final : public FrameChannelWriter {
  public:
    explicit WindowsSharedMemoryFrameWriter(FrameChannelConfiguration configuration)
        : configuration_(std::move(configuration)) {
        validate_writer_configuration(configuration_);
        frame_bytes_ = static_cast<std::size_t>(configuration_.width) * configuration_.height * 4U;
        slot_size_ = kSlotHeaderSize + frame_bytes_;
        mapping_size_ = kHeaderSize + static_cast<std::uint64_t>(kSlotCount) * slot_size_;
        const auto mapping_name = utf8_to_utf16(configuration_.handle_token);
        mapping_ = std::make_unique<UniqueHandle>(
            OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, mapping_name.c_str()));
        if (mapping_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mapping_unavailable");
        }
        view_ = UniqueView{MapViewOfFile(mapping_->get(), FILE_MAP_READ | FILE_MAP_WRITE, 0U, 0U,
                                         static_cast<SIZE_T>(mapping_size_))};
        if (view_.get() == nullptr) {
            throw std::runtime_error("frame_channel_mapping_unavailable");
        }
        const auto mutex_name = std::wstring{kMutexPrefix} + mapping_name;
        mutex_ = std::make_unique<UniqueHandle>(
            OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, mutex_name.c_str()));
        if (mutex_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mutex_unavailable");
        }
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            throw std::runtime_error("frame_channel_reader_busy");
        }
        validate_header();
        sequence_ = read_value<std::uint64_t>(bytes(), kPublishedSequenceOffset);
        if (sequence_ > kMaximumSequence) {
            throw std::runtime_error("frame_channel_sequence_invalid");
        }
    }

    [[nodiscard]] bool publish(const VideoFrameView& frame) override {
        const auto dynamic = configuration_.transport == "shared_memory_video";
        if (!dynamic && frame.pixel_format != VideoFramePixelFormat::bgra) {
            throw std::runtime_error("frame_channel_pixel_format_invalid");
        }
        const auto expected_layout = packed_video_frame_layout(
            frame.width, frame.height, frame.pixel_format);
        validate_video_frame_view(frame, expected_layout);
        if (frame.width > configuration_.width || frame.height > configuration_.height) {
            throw std::runtime_error("frame_channel_dimensions_invalid");
        }
        const auto payload_size = expected_layout.payload_size;
        if (payload_size > frame_bytes_) {
            throw std::runtime_error("frame_channel_frame_size_invalid");
        }
        const auto pixel_format = frame.pixel_format == VideoFramePixelFormat::bgra
                                      ? kBgraPixelFormat
                                      : kNv12PixelFormat;
        const auto stride = frame.width *
                            (frame.pixel_format == VideoFramePixelFormat::bgra ? 4U : 1U);
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return false;
        }
        validate_header();
        if (sequence_ >= kMaximumSequence) {
            throw std::overflow_error("frame_channel_sequence_exhausted");
        }
        const auto sequence = sequence_ + 1U;
        const auto slot_index = (sequence - 1U) % kSlotCount;
        const auto slot_offset = kHeaderSize + static_cast<std::size_t>(slot_index) * slot_size_;
        auto* channel = bytes();
        write_value<std::uint64_t>(channel, slot_offset, sequence * 2U - 1U);
        write_value<std::uint64_t>(channel, slot_offset + 8U, sequence);
        write_value<std::uint64_t>(channel, slot_offset + 16U, frame.presentation_timestamp_ns);
        write_value<std::uint64_t>(channel, slot_offset + 24U, frame.duration_ns);
        write_value<std::uint64_t>(channel, slot_offset + 32U, frame.produced_monotonic_ns);
        write_value<std::uint64_t>(channel, slot_offset + 40U, payload_size);
        write_value<std::uint32_t>(channel, slot_offset + 48U, frame.width);
        write_value<std::uint32_t>(channel, slot_offset + 52U, frame.height);
        write_value<std::uint32_t>(channel, slot_offset + 56U, stride);
        write_value<std::uint32_t>(channel, slot_offset + 60U, pixel_format);
        copy_video_frame_pixels(
            frame,
            std::span<std::uint8_t>{channel + slot_offset + kSlotHeaderSize, payload_size});
        write_value<std::uint64_t>(channel, slot_offset, sequence * 2U);
        write_value<std::uint64_t>(channel, kPublishedSequenceOffset, sequence);
        sequence_ = sequence;
        return true;
    }

  private:
    [[nodiscard]] std::uint8_t* bytes() const noexcept {
        return static_cast<std::uint8_t*>(view_.get());
    }

    void validate_header() const {
        const auto* header = bytes();
        if (!std::equal(kMagic.begin(), kMagic.end(), header) ||
            read_value<std::uint16_t>(header, 8U) != kVersion ||
            read_value<std::uint16_t>(header, 10U) != kHeaderSize ||
            read_value<std::uint32_t>(header, 12U) != kSlotCount ||
            read_value<std::uint32_t>(header, 16U) != configuration_.width ||
            read_value<std::uint32_t>(header, 20U) != configuration_.height ||
            read_value<std::uint32_t>(header, 24U) !=
                (configuration_.transport == "shared_memory_video" ? 0U
                                                                    : configuration_.width * 4U) ||
            read_value<std::uint32_t>(header, 28U) !=
                (configuration_.transport == "shared_memory_video" ? kDynamicPixelFormat
                                                                    : kBgraPixelFormat) ||
            read_value<std::uint64_t>(header, 32U) != configuration_.generation ||
            read_value<std::uint64_t>(header, 40U) != slot_size_ ||
            read_value<std::uint64_t>(header, 48U) != mapping_size_) {
            throw std::runtime_error("frame_channel_header_invalid");
        }
    }

    FrameChannelConfiguration configuration_{};
    std::unique_ptr<UniqueHandle> mapping_{};
    std::unique_ptr<UniqueHandle> mutex_{};
    UniqueView view_{};
    std::uint64_t mapping_size_{0U};
    std::size_t frame_bytes_{0U};
    std::size_t slot_size_{0U};
    std::uint64_t sequence_{0U};
};

#endif

} // namespace

std::unique_ptr<FrameChannelReader>
make_frame_channel_reader(const FrameChannelConfiguration& configuration) {
    validate_configuration(configuration);
#ifdef _WIN32
    return std::make_unique<WindowsSharedMemoryVideoReader>(configuration);
#else
    static_cast<void>(configuration);
    throw std::runtime_error("frame_channel_platform_unsupported");
#endif
}

std::unique_ptr<FrameChannelWriter>
make_frame_channel_writer(const FrameChannelConfiguration& configuration) {
    validate_writer_configuration(configuration);
#ifdef _WIN32
    return std::make_unique<WindowsSharedMemoryFrameWriter>(configuration);
#else
    static_cast<void>(configuration);
    throw std::runtime_error("frame_channel_platform_unsupported");
#endif
}

} // namespace solin::media_engine
