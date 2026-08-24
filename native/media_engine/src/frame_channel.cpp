#include "solin/media_engine/frame_channel.hpp"

#include <algorithm>
#include <array>
#include <cmath>
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
constexpr std::uint16_t kVersion = 6U;
constexpr std::size_t kHeaderSize = 128U;
constexpr std::size_t kSlotHeaderSize = 128U;
constexpr std::uint32_t kSlotCount = 3U;
constexpr std::uint32_t kBgraPixelFormat = 1U;
constexpr std::uint32_t kNv12PixelFormat = 2U;
constexpr std::uint32_t kDynamicPixelFormat = 0U;
constexpr std::size_t kPublishedSequenceOffset = 56U;
constexpr std::size_t kMediaEpochOffset = 64U;
constexpr std::size_t kImageTransformRevisionOffset = 72U;
constexpr std::size_t kImageTransformMediaEpochOffset = 80U;
constexpr std::size_t kImageTransformFlagsOffset = 88U;
constexpr std::size_t kImageTransformCanvasWidthOffset = 92U;
constexpr std::size_t kImageTransformCanvasHeightOffset = 96U;
constexpr std::size_t kImageTransformDurationMsOffset = 100U;
constexpr std::size_t kImageTransformZoomOffset = 104U;
constexpr std::size_t kImageTransformNormXOffset = 112U;
constexpr std::size_t kImageTransformNormYOffset = 120U;
constexpr std::uint32_t kImageTransformEnabled = 1U << 0U;
constexpr std::uint32_t kImageTransformAnimate = 1U << 1U;
constexpr std::uint32_t kImageTransformKnownFlags =
    kImageTransformEnabled | kImageTransformAnimate;
constexpr std::size_t kSlotLeaseCountOffset = 64U;
constexpr std::size_t kSlotLeaseOwnerPidOffset = 68U;
constexpr std::size_t kSlotLeaseOwnerCreationTimeOffset = 72U;
constexpr std::size_t kSlotSecondPlaneStrideOffset = 80U;
constexpr std::size_t kSlotSecondPlaneOffsetOffset = 88U;
constexpr std::size_t kSlotMediaEpochOffset = 96U;
constexpr std::size_t kMaximumFrameBytes = 3'840U * 2'160U * 4U;
constexpr std::uint64_t kMaximumSequence = (std::numeric_limits<std::int64_t>::max)() / 2U;
#ifdef _WIN32
constexpr std::wstring_view kMutexPrefix = L"Local\\SolinFrameMutex.";
constexpr std::wstring_view kEventPrefix = L"Local\\SolinFrameEvent.";
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

[[nodiscard]] volatile LONG* slot_lease_count(std::uint8_t* channel,
                                              const std::size_t slot_offset) noexcept {
    return reinterpret_cast<volatile LONG*>(channel + slot_offset +
                                             kSlotLeaseCountOffset);
}

[[nodiscard]] LONG current_slot_lease_count(std::uint8_t* channel,
                                            const std::size_t slot_offset) noexcept {
    return InterlockedCompareExchange(slot_lease_count(channel, slot_offset), 0L, 0L);
}

class SlotLease final {
  public:
    SlotLease(volatile LONG* lease_count, std::shared_ptr<UniqueView> view)
        : lease_count_(lease_count), view_(std::move(view)) {
        if (lease_count_ == nullptr || view_ == nullptr || view_->get() == nullptr) {
            throw std::invalid_argument("frame_channel_slot_lease_invalid");
        }
    }

    ~SlotLease() {
        const auto remaining = InterlockedDecrement(lease_count_);
        if (remaining < 0L) {
            static_cast<void>(InterlockedExchange(lease_count_, 0L));
        }
    }

    SlotLease(const SlotLease&) = delete;
    SlotLease& operator=(const SlotLease&) = delete;

  private:
    volatile LONG* lease_count_{nullptr};
    std::shared_ptr<UniqueView> view_{};
};

[[nodiscard]] std::uint64_t current_process_creation_time() noexcept {
    static const auto value = [] {
        FILETIME creation{};
        FILETIME exit{};
        FILETIME kernel{};
        FILETIME user{};
        if (GetProcessTimes(GetCurrentProcess(), &creation, &exit, &kernel, &user) ==
            FALSE) {
            return std::uint64_t{0U};
        }
        return (static_cast<std::uint64_t>(creation.dwHighDateTime) << 32U) |
               creation.dwLowDateTime;
    }();
    return value;
}

class WindowsSharedMemoryVideoReader final : public FrameChannelReader {
  public:
    explicit WindowsSharedMemoryVideoReader(FrameChannelConfiguration configuration)
        : configuration_(std::move(configuration)) {
        validate_configuration(configuration_);
        const auto expected = expected_mapping_size();
        const auto mapping_name = utf8_to_utf16(configuration_.handle_token);
        mapping_ = std::make_unique<UniqueHandle>(OpenFileMappingW(
            FILE_MAP_READ | FILE_MAP_WRITE, FALSE, mapping_name.c_str()));
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
        view_ = std::make_shared<UniqueView>(
            MapViewOfFile(mapping_->get(), FILE_MAP_READ | FILE_MAP_WRITE, 0U, 0U,
                          static_cast<SIZE_T>(mapping_size_)));
        if (view_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mapping_unavailable");
        }
        const auto mutex_name = std::wstring{kMutexPrefix} + mapping_name;
        mutex_ = std::make_unique<UniqueHandle>(
            OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, mutex_name.c_str()));
        if (mutex_->get() == nullptr) {
            throw std::runtime_error("frame_channel_mutex_unavailable");
        }
        const auto event_name = std::wstring{kEventPrefix} + mapping_name;
        event_ = std::make_unique<UniqueHandle>(
            OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, event_name.c_str()));
        if (event_->get() == nullptr) {
            throw std::runtime_error("frame_channel_event_unavailable");
        }
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            throw std::runtime_error("frame_channel_writer_busy");
        }
        validate_header_prefix(bytes());
    }

    [[nodiscard]] std::optional<FrameChannelFrameLease>
    read_latest(const std::uint64_t after_sequence) override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        const auto* channel = bytes();
        validate_header_prefix(channel);
        const auto published_sequence =
            read_value<std::uint64_t>(channel, kPublishedSequenceOffset);
        if (published_sequence == 0U || published_sequence <= after_sequence) {
            return std::nullopt;
        }
        if (published_sequence > kMaximumSequence) {
            throw std::runtime_error("frame_channel_sequence_invalid");
        }
        std::optional<std::size_t> selected_slot_offset;
        for (std::size_t index = 0U; index < kSlotCount; ++index) {
            const auto candidate = kHeaderSize + index * slot_size_;
            if (read_value<std::uint64_t>(channel, candidate) ==
                    published_sequence * 2U &&
                read_value<std::uint64_t>(channel, candidate + 8U) ==
                    published_sequence) {
                selected_slot_offset = candidate;
                break;
            }
        }
        if (!selected_slot_offset.has_value()) {
            return std::nullopt;
        }
        const auto slot_offset = selected_slot_offset.value();
        const auto marker = read_value<std::uint64_t>(channel, slot_offset);
        const auto frame_sequence = read_value<std::uint64_t>(channel, slot_offset + 8U);
        const auto payload_size = read_value<std::uint64_t>(channel, slot_offset + 40U);
        const auto width = read_value<std::uint32_t>(channel, slot_offset + 48U);
        const auto height = read_value<std::uint32_t>(channel, slot_offset + 52U);
        const auto stride = read_value<std::uint32_t>(channel, slot_offset + 56U);
        const auto pixel_format = read_value<std::uint32_t>(channel, slot_offset + 60U);
        const auto second_stride =
            read_value<std::uint32_t>(channel, slot_offset + kSlotSecondPlaneStrideOffset);
        const auto second_offset =
            read_value<std::uint64_t>(channel, slot_offset + kSlotSecondPlaneOffsetOffset);
        const auto bgra = pixel_format == kBgraPixelFormat;
        const auto nv12 = pixel_format == kNv12PixelFormat;
        const auto expected_stride = width * (bgra ? 4U : 1U);
        const auto first_plane_bytes =
            height == 0U ? 0U
                         : static_cast<std::uint64_t>(stride) * (height - 1U) + expected_stride;
        const auto second_plane_rows = height / 2U;
        const auto second_plane_bytes =
            second_plane_rows == 0U
                ? 0U
                : static_cast<std::uint64_t>(second_stride) * (second_plane_rows - 1U) + width;
        const auto actual_frame_bytes =
            bgra ? first_plane_bytes : second_offset + second_plane_bytes;
        const auto format_allowed =
            configuration_.transport == "shared_memory_video" ? (bgra || nv12) : bgra;
        if (marker != published_sequence * 2U || frame_sequence != published_sequence ||
            width == 0U || width > configuration_.width || height == 0U ||
            height > configuration_.height ||
            (nv12 && ((width & 1U) != 0U || (height & 1U) != 0U)) ||
            stride < expected_stride ||
            (bgra && (second_stride != 0U || second_offset != 0U)) ||
            (nv12 && (second_stride < width || second_offset != first_plane_bytes)) ||
            !format_allowed || payload_size != actual_frame_bytes || payload_size > frame_bytes_) {
            return std::nullopt;
        }
        auto* mutable_channel = const_cast<std::uint8_t*>(channel);
        auto* lease_count = slot_lease_count(mutable_channel, slot_offset);
        const auto active_leases = InterlockedIncrement(lease_count);
        if (active_leases <= 0L) {
            static_cast<void>(InterlockedExchange(lease_count, 0L));
            throw std::runtime_error("frame_channel_slot_lease_invalid");
        }
        write_value<std::uint32_t>(mutable_channel,
                                   slot_offset + kSlotLeaseOwnerPidOffset,
                                   GetCurrentProcessId());
        write_value<std::uint64_t>(mutable_channel,
                                   slot_offset + kSlotLeaseOwnerCreationTimeOffset,
                                   current_process_creation_time());
        std::shared_ptr<SlotLease> lifetime;
        try {
            lifetime = std::make_shared<SlotLease>(lease_count, view_);
        } catch (...) {
            static_cast<void>(InterlockedDecrement(lease_count));
            throw;
        }
        FrameChannelFrame result{
            .generation = configuration_.generation,
            .sequence = frame_sequence,
            .presentation_timestamp_ns = read_value<std::uint64_t>(channel, slot_offset + 16U),
            .duration_ns = read_value<std::uint64_t>(channel, slot_offset + 24U),
            .produced_monotonic_ns = read_value<std::uint64_t>(channel, slot_offset + 32U),
            .media_epoch = read_value<std::uint64_t>(channel, slot_offset + kSlotMediaEpochOffset),
            .width = width,
            .height = height,
            .pixel_format = bgra ? VideoFramePixelFormat::bgra : VideoFramePixelFormat::nv12,
            .plane_strides = {stride, second_stride},
            .plane_offsets = {0U, second_offset},
            .bytes = std::span<const std::uint8_t>{
                channel + slot_offset + kSlotHeaderSize,
                static_cast<std::size_t>(payload_size),
            },
        };
        return make_lease(result, std::move(lifetime));
    }

    [[nodiscard]] std::optional<std::uint64_t> media_epoch() override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        const auto* channel = bytes();
        validate_header_prefix(channel);
        return read_value<std::uint64_t>(channel, kMediaEpochOffset);
    }

    [[nodiscard]] std::optional<FrameChannelImageTransform>
    image_transform() override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        const auto* channel = bytes();
        validate_header_prefix(channel);
        const auto revision =
            read_value<std::uint64_t>(channel, kImageTransformRevisionOffset);
        if (revision == 0U) {
            return std::nullopt;
        }
        const auto flags =
            read_value<std::uint32_t>(channel, kImageTransformFlagsOffset);
        FrameChannelImageTransform result{
            .revision = revision,
            .media_epoch = read_value<std::uint64_t>(
                channel, kImageTransformMediaEpochOffset),
            .enabled = (flags & kImageTransformEnabled) != 0U,
            .animate = (flags & kImageTransformAnimate) != 0U,
            .canvas_width = read_value<std::uint32_t>(
                channel, kImageTransformCanvasWidthOffset),
            .canvas_height = read_value<std::uint32_t>(
                channel, kImageTransformCanvasHeightOffset),
            .duration_ms = read_value<std::uint32_t>(
                channel, kImageTransformDurationMsOffset),
            .zoom = read_value<double>(channel, kImageTransformZoomOffset),
            .norm_x = read_value<double>(channel, kImageTransformNormXOffset),
            .norm_y = read_value<double>(channel, kImageTransformNormYOffset),
        };
        const auto canvas_pixels =
            static_cast<std::uint64_t>(result.canvas_width) * result.canvas_height;
        if ((flags & ~kImageTransformKnownFlags) != 0U ||
            result.canvas_width == 0U ||
            result.canvas_width > configuration_.width ||
            result.canvas_height == 0U ||
            result.canvas_height > configuration_.height ||
            canvas_pixels > kMaximumFrameBytes / 4U ||
            result.duration_ms == 0U || result.duration_ms > 60'000U ||
            !std::isfinite(result.zoom) || result.zoom < 0.1 ||
            result.zoom > 10.0 || !std::isfinite(result.norm_x) ||
            std::abs(result.norm_x) > 16.0 || !std::isfinite(result.norm_y) ||
            std::abs(result.norm_y) > 16.0) {
            throw std::runtime_error("frame_channel_image_transform_invalid");
        }
        return result;
    }

    [[nodiscard]] bool wait_for_frame(const std::chrono::milliseconds timeout) override {
        const auto bounded_timeout = (std::clamp)(
            timeout.count(), std::int64_t{0},
            static_cast<std::int64_t>((std::numeric_limits<DWORD>::max)() - 1U));
        const auto result =
            WaitForSingleObject(event_->get(), static_cast<DWORD>(bounded_timeout));
        if (result == WAIT_OBJECT_0) {
            return true;
        }
        if (result == WAIT_TIMEOUT) {
            return false;
        }
        throw std::runtime_error("frame_channel_event_failed");
    }

    void wake() noexcept override {
        if (event_ != nullptr) {
            static_cast<void>(SetEvent(event_->get()));
        }
    }

  private:
    [[nodiscard]] const std::uint8_t* bytes() const noexcept {
        return static_cast<const std::uint8_t*>(view_->get());
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
    std::unique_ptr<UniqueHandle> event_{};
    std::shared_ptr<UniqueView> view_{};
    std::uint64_t mapping_size_{0U};
    std::size_t frame_bytes_{0U};
    std::size_t slot_size_{0U};
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
        const auto event_name = std::wstring{kEventPrefix} + mapping_name;
        event_ = std::make_unique<UniqueHandle>(
            OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE, event_name.c_str()));
        if (event_->get() == nullptr) {
            throw std::runtime_error("frame_channel_event_unavailable");
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
        auto* channel = bytes();
        std::optional<std::size_t> selected_slot_offset;
        const auto preferred_slot = static_cast<std::size_t>((sequence - 1U) % kSlotCount);
        for (std::size_t attempt = 0U; attempt < kSlotCount; ++attempt) {
            const auto slot_index = (preferred_slot + attempt) % kSlotCount;
            const auto candidate = kHeaderSize + slot_index * slot_size_;
            if (current_slot_lease_count(channel, candidate) == 0L) {
                selected_slot_offset = candidate;
                break;
            }
        }
        if (!selected_slot_offset.has_value()) {
            return false;
        }
        const auto slot_offset = selected_slot_offset.value();
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
        write_value<std::uint32_t>(
            channel, slot_offset + kSlotSecondPlaneStrideOffset,
            frame.pixel_format == VideoFramePixelFormat::nv12 ? frame.width : 0U);
        write_value<std::uint64_t>(
            channel, slot_offset + kSlotSecondPlaneOffsetOffset,
            frame.pixel_format == VideoFramePixelFormat::nv12
                ? static_cast<std::uint64_t>(frame.width) * frame.height
                : 0U);
        write_value<std::uint64_t>(channel, slot_offset + kSlotMediaEpochOffset, 0U);
        copy_video_frame_pixels(
            frame,
            std::span<std::uint8_t>{channel + slot_offset + kSlotHeaderSize, payload_size});
        write_value<std::uint64_t>(channel, slot_offset, sequence * 2U);
        write_value<std::uint64_t>(channel, kPublishedSequenceOffset, sequence);
        sequence_ = sequence;
        if (SetEvent(event_->get()) == FALSE) {
            throw std::runtime_error("frame_channel_event_failed");
        }
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
    std::unique_ptr<UniqueHandle> event_{};
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
