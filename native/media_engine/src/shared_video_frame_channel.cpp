#include "solin/media_engine/shared_video_frame_channel.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstring>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <objbase.h>
#include <sddl.h>
#endif

namespace solin::media_engine {
namespace {

constexpr std::array<std::uint8_t, 8U> kMagic{'S', 'L', 'N', 'V', 'D', 'O', '0', '1'};
constexpr std::uint16_t kVersion = 2U;
constexpr std::size_t kHeaderSize = 128U;
constexpr std::size_t kSlotHeaderSize = 64U;
constexpr std::uint32_t kSlotCount = 3U;
constexpr std::size_t kPublishedSequenceOffset = 80U;
constexpr std::size_t kHeartbeatOffset = 88U;
constexpr std::uint64_t kMaximumSequence =
    (std::numeric_limits<std::int64_t>::max)() / 2U;

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

[[nodiscard]] std::uint64_t aligned_slot_size(const std::uint64_t payload_size) {
    constexpr std::uint64_t alignment = 64U;
    if (payload_size > (std::numeric_limits<std::uint64_t>::max)() -
                           kSlotHeaderSize - (alignment - 1U)) {
        throw std::invalid_argument("shared_video_frame_channel_invalid");
    }
    return (kSlotHeaderSize + payload_size + alignment - 1U) & ~(alignment - 1U);
}

[[nodiscard]] std::uint64_t mapping_size_for(const PackedVideoFrameLayout& layout) {
    const auto slot_size = aligned_slot_size(layout.payload_size);
    if (slot_size > ((std::numeric_limits<std::uint64_t>::max)() - kHeaderSize) /
                        kSlotCount) {
        throw std::invalid_argument("shared_video_frame_channel_invalid");
    }
    return kHeaderSize + slot_size * kSlotCount;
}

void validate_configuration(const SharedVideoFrameChannelConfiguration& configuration) {
    const auto canonical = packed_video_frame_layout(
        configuration.layout.width, configuration.layout.height,
        configuration.layout.pixel_format);
    if (configuration.generation == 0U || canonical != configuration.layout) {
        throw std::invalid_argument("shared_video_frame_channel_invalid");
    }
    static_cast<void>(mapping_size_for(canonical));
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
    UniqueView(UniqueView&&) = delete;
    UniqueView& operator=(UniqueView&&) = delete;

    [[nodiscard]] void* get() const noexcept { return value_; }

  private:
    void* value_{nullptr};
};

class LocalMemory final {
  public:
    explicit LocalMemory(void* value = nullptr) noexcept : value_(value) {}
    ~LocalMemory() {
        if (value_ != nullptr) {
            static_cast<void>(LocalFree(value_));
        }
    }
    LocalMemory(const LocalMemory&) = delete;
    LocalMemory& operator=(const LocalMemory&) = delete;
    [[nodiscard]] void* get() const noexcept { return value_; }

  private:
    void* value_{nullptr};
};

[[nodiscard]] std::vector<std::uint8_t> token_user_information() {
    HANDLE raw_token = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw_token) == FALSE) {
        throw std::runtime_error("shared_video_frame_channel_security_unavailable");
    }
    const UniqueHandle token{raw_token};
    DWORD size = 0U;
    static_cast<void>(GetTokenInformation(token.get(), TokenUser, nullptr, 0U,
                                          &size));
    if (size == 0U || size > 64U * 1024U) {
        throw std::runtime_error("shared_video_frame_channel_security_unavailable");
    }
    std::vector<std::uint8_t> result(size);
    if (GetTokenInformation(token.get(), TokenUser, result.data(), size, &size) ==
        FALSE) {
        throw std::runtime_error("shared_video_frame_channel_security_unavailable");
    }
    return result;
}

[[nodiscard]] LocalMemory cross_process_file_security_descriptor(
    const bool current_user_read_only = false) {
    const auto information = token_user_information();
    const auto* user = reinterpret_cast<const TOKEN_USER*>(information.data());
    LPWSTR raw_sid = nullptr;
    if (ConvertSidToStringSidW(user->User.Sid, &raw_sid) == FALSE) {
        throw std::runtime_error("shared_video_frame_channel_security_unavailable");
    }
    const LocalMemory sid{raw_sid};
    const auto sddl = std::wstring{L"D:P(A;;GA;;;SY)(A;;"} +
                      (current_user_read_only ? L"GR" : L"GA") + L";;;" +
                      static_cast<const wchar_t*>(sid.get()) + L")";
    PSECURITY_DESCRIPTOR raw_descriptor = nullptr;
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.c_str(), SDDL_REVISION_1, &raw_descriptor, nullptr) == FALSE) {
        throw std::runtime_error("shared_video_frame_channel_security_unavailable");
    }
    return LocalMemory{raw_descriptor};
}

[[nodiscard]] std::wstring temporary_frame_file_path() {
    std::array<wchar_t, MAX_PATH + 1U> directory{};
    const auto directory_length =
        GetTempPathW(static_cast<DWORD>(directory.size()), directory.data());
    if (directory_length == 0U || directory_length >= directory.size()) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    GUID identifier{};
    if (FAILED(CoCreateGuid(&identifier))) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    std::array<wchar_t, 40U> identifier_text{};
    if (StringFromGUID2(identifier, identifier_text.data(),
                        static_cast<int>(identifier_text.size())) == 0) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    return std::wstring{directory.data(), directory_length} +
           L"Solin.VirtualCamera." + identifier_text.data() + L".frames";
}

[[nodiscard]] std::string utf16_to_utf8(const std::wstring& value) {
    if (value.empty() || value.size() > static_cast<std::size_t>(INT_MAX)) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    const auto required = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        nullptr, 0, nullptr, nullptr);
    if (required <= 0) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    std::string result(static_cast<std::size_t>(required), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                            static_cast<int>(value.size()), result.data(), required,
                            nullptr, nullptr) != required) {
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }
    return result;
}

[[nodiscard]] volatile LONG64* atomic_sequence(std::uint8_t* bytes,
                                               const std::size_t offset) noexcept {
    return reinterpret_cast<volatile LONG64*>(bytes + offset);
}

[[nodiscard]] const volatile LONG64*
atomic_sequence(const std::uint8_t* bytes, const std::size_t offset) noexcept {
    return reinterpret_cast<const volatile LONG64*>(bytes + offset);
}

[[nodiscard]] std::uint64_t load_sequence(const std::uint8_t* bytes,
                                          const std::size_t offset) noexcept {
    return static_cast<std::uint64_t>(ReadAcquire64(atomic_sequence(bytes, offset)));
}

void store_sequence(std::uint8_t* bytes, const std::size_t offset,
                    const std::uint64_t value) noexcept {
    static_cast<void>(InterlockedExchange64(
        atomic_sequence(bytes, offset), static_cast<LONG64>(value)));
}

void initialize_header(std::uint8_t* bytes,
                       const SharedVideoFrameChannelConfiguration& configuration,
                       const std::uint64_t slot_size,
                       const std::uint64_t mapping_size) {
    std::memset(bytes, 0, static_cast<std::size_t>(mapping_size));
    std::memcpy(bytes, kMagic.data(), kMagic.size());
    write_value<std::uint16_t>(bytes, 8U, kVersion);
    write_value<std::uint16_t>(bytes, 10U, static_cast<std::uint16_t>(kHeaderSize));
    write_value<std::uint32_t>(bytes, 12U, kSlotCount);
    write_value<std::uint32_t>(bytes, 16U, configuration.layout.width);
    write_value<std::uint32_t>(bytes, 20U, configuration.layout.height);
    write_value<std::uint32_t>(bytes, 24U,
                               static_cast<std::uint32_t>(
                                   configuration.layout.pixel_format));
    write_value<std::uint32_t>(bytes, 28U, configuration.layout.plane_count);
    write_value<std::uint32_t>(bytes, 32U,
                               configuration.layout.plane_strides[0]);
    write_value<std::uint32_t>(bytes, 36U,
                               configuration.layout.plane_strides[1]);
    write_value<std::uint64_t>(bytes, 40U,
                               configuration.layout.plane_offsets[1]);
    write_value<std::uint64_t>(bytes, 48U, configuration.generation);
    write_value<std::uint64_t>(bytes, 56U, configuration.layout.payload_size);
    write_value<std::uint64_t>(bytes, 64U, slot_size);
    write_value<std::uint64_t>(bytes, 72U, mapping_size);
    store_sequence(bytes, kPublishedSequenceOffset, 0U);
    store_sequence(bytes, kHeartbeatOffset, 1U);
}

[[nodiscard]] SharedVideoFrameChannelConfiguration parse_header(
    const std::uint8_t* bytes, const std::uint64_t supplied_mapping_size) {
    if (!std::equal(kMagic.begin(), kMagic.end(), bytes) ||
        read_value<std::uint16_t>(bytes, 8U) != kVersion ||
        read_value<std::uint16_t>(bytes, 10U) != kHeaderSize ||
        read_value<std::uint32_t>(bytes, 12U) != kSlotCount ||
        read_value<std::uint64_t>(bytes, 72U) != supplied_mapping_size) {
        throw std::runtime_error("shared_video_frame_channel_header_invalid");
    }
    const auto raw_pixel_format = read_value<std::uint32_t>(bytes, 24U);
    if (raw_pixel_format < static_cast<std::uint32_t>(VideoFramePixelFormat::bgra) ||
        raw_pixel_format > static_cast<std::uint32_t>(VideoFramePixelFormat::yuy2)) {
        throw std::runtime_error("shared_video_frame_channel_header_invalid");
    }
    SharedVideoFrameChannelConfiguration result{
        .generation = read_value<std::uint64_t>(bytes, 48U),
        .layout = packed_video_frame_layout(
            read_value<std::uint32_t>(bytes, 16U),
            read_value<std::uint32_t>(bytes, 20U),
            static_cast<VideoFramePixelFormat>(raw_pixel_format)),
    };
    validate_configuration(result);
    if (read_value<std::uint32_t>(bytes, 28U) != result.layout.plane_count ||
        read_value<std::uint32_t>(bytes, 32U) != result.layout.plane_strides[0] ||
        read_value<std::uint32_t>(bytes, 36U) != result.layout.plane_strides[1] ||
        read_value<std::uint64_t>(bytes, 40U) != result.layout.plane_offsets[1] ||
        read_value<std::uint64_t>(bytes, 56U) != result.layout.payload_size ||
        read_value<std::uint64_t>(bytes, 64U) !=
            aligned_slot_size(result.layout.payload_size) ||
        mapping_size_for(result.layout) != supplied_mapping_size) {
        throw std::runtime_error("shared_video_frame_channel_header_invalid");
    }
    return result;
}

class WindowsSharedVideoFramePublisher final : public SharedVideoFramePublisher {
  public:
    explicit WindowsSharedVideoFramePublisher(
        SharedVideoFrameChannelConfiguration configuration,
        const bool cross_process)
        : configuration_(std::move(configuration)),
          slot_size_(aligned_slot_size(configuration_.layout.payload_size)),
          mapping_size_(mapping_size_for(configuration_.layout)) {
        validate_configuration(configuration_);
        if (mapping_size_ > static_cast<std::uint64_t>(
                                (std::numeric_limits<SIZE_T>::max)())) {
            throw std::invalid_argument("shared_video_frame_channel_invalid");
        }
        const auto high = static_cast<DWORD>(mapping_size_ >> 32U);
        const auto low = static_cast<DWORD>(mapping_size_ & 0xFFFFFFFFULL);
        if (cross_process) {
            create_cross_process_backing_file();
        }
        mapping_ = std::make_unique<UniqueHandle>(CreateFileMappingW(
            backing_file_ == nullptr ? INVALID_HANDLE_VALUE : backing_file_->get(),
            nullptr, PAGE_READWRITE, high, low, nullptr));
        if (mapping_->get() == nullptr) {
            throw std::runtime_error("shared_video_frame_channel_unavailable");
        }
        view_ = std::make_unique<UniqueView>(MapViewOfFile(
            mapping_->get(), FILE_MAP_ALL_ACCESS, 0U, 0U,
            static_cast<SIZE_T>(mapping_size_)));
        if (view_->get() == nullptr) {
            throw std::runtime_error("shared_video_frame_channel_unavailable");
        }
        initialize_header(bytes(), configuration_, slot_size_, mapping_size_);
    }

    [[nodiscard]] std::uint64_t publish(const VideoFrameView& frame) override {
        validate_video_frame_view(frame, configuration_.layout);
        std::scoped_lock lock{mutex_};
        if (sequence_ >= kMaximumSequence) {
            throw std::overflow_error("shared_video_frame_channel_exhausted");
        }
        const auto sequence = sequence_ + 1U;
        const auto slot_index = (sequence - 1U) % kSlotCount;
        const auto slot_offset = kHeaderSize + slot_index * slot_size_;
        store_sequence(bytes(), static_cast<std::size_t>(slot_offset),
                       sequence * 2U - 1U);
        write_value<std::uint64_t>(bytes(), static_cast<std::size_t>(slot_offset + 8U),
                                   sequence);
        write_value<std::uint64_t>(bytes(), static_cast<std::size_t>(slot_offset + 16U),
                                   frame.presentation_timestamp_ns);
        write_value<std::uint64_t>(bytes(), static_cast<std::size_t>(slot_offset + 24U),
                                   frame.duration_ns);
        write_value<std::uint64_t>(bytes(), static_cast<std::size_t>(slot_offset + 32U),
                                   frame.produced_monotonic_ns);
        write_value<std::uint64_t>(bytes(), static_cast<std::size_t>(slot_offset + 40U),
                                   configuration_.layout.payload_size);
        write_value<std::uint32_t>(bytes(), static_cast<std::size_t>(slot_offset + 48U),
                                   frame.discontinuity ? 1U : 0U);
        copy_video_frame_pixels(
            frame,
            std::span<std::uint8_t>{bytes() + slot_offset + kSlotHeaderSize,
                                    static_cast<std::size_t>(
                                        configuration_.layout.payload_size)});
        store_sequence(bytes(), static_cast<std::size_t>(slot_offset), sequence * 2U);
        store_sequence(bytes(), kPublishedSequenceOffset, sequence);
        sequence_ = sequence;
        heartbeat_locked();
        return sequence;
    }

    void heartbeat() noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            heartbeat_locked();
        } catch (...) {
        }
    }

    [[nodiscard]] std::uintptr_t native_mapping_handle() const noexcept override {
        return reinterpret_cast<std::uintptr_t>(mapping_->get());
    }

    [[nodiscard]] std::string_view backing_file_path_utf8() const noexcept override {
        return backing_file_path_utf8_;
    }

    [[nodiscard]] std::uint64_t mapping_size() const noexcept override {
        return mapping_size_;
    }

    [[nodiscard]] const SharedVideoFrameChannelConfiguration&
    configuration() const noexcept override {
        return configuration_;
    }

  private:
    void heartbeat_locked() noexcept {
        if (heartbeat_ >= kMaximumSequence) {
            heartbeat_ = 1U;
        } else {
            ++heartbeat_;
        }
        store_sequence(bytes(), kHeartbeatOffset, heartbeat_);
    }

    void create_cross_process_backing_file() {
        if (mapping_size_ >
            static_cast<std::uint64_t>((std::numeric_limits<LONGLONG>::max)())) {
            throw std::invalid_argument("shared_video_frame_channel_invalid");
        }
        auto descriptor = cross_process_file_security_descriptor();
        SECURITY_ATTRIBUTES attributes{
            .nLength = sizeof(SECURITY_ATTRIBUTES),
            .lpSecurityDescriptor = descriptor.get(),
            .bInheritHandle = FALSE,
        };
        for (std::uint32_t attempt = 0U; attempt < 8U; ++attempt) {
            const auto path = temporary_frame_file_path();
            auto file = std::make_unique<UniqueHandle>(CreateFileW(
                path.c_str(), GENERIC_READ | GENERIC_WRITE | DELETE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                &attributes, CREATE_NEW,
                FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr));
            if (file->get() == INVALID_HANDLE_VALUE) {
                const auto error = GetLastError();
                if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS) {
                    continue;
                }
                throw std::runtime_error(
                    "shared_video_frame_channel_file_unavailable");
            }
            LARGE_INTEGER size{};
            size.QuadPart = static_cast<LONGLONG>(mapping_size_);
            if (SetFilePointerEx(file->get(), size, nullptr, FILE_BEGIN) == FALSE ||
                SetEndOfFile(file->get()) == FALSE) {
                throw std::runtime_error(
                    "shared_video_frame_channel_file_unavailable");
            }
            auto read_only_descriptor =
                cross_process_file_security_descriptor(true);
            if (SetFileSecurityW(
                    path.c_str(), DACL_SECURITY_INFORMATION,
                    static_cast<PSECURITY_DESCRIPTOR>(
                        read_only_descriptor.get())) == FALSE) {
                throw std::runtime_error(
                    "shared_video_frame_channel_security_unavailable");
            }
            backing_file_path_utf8_ = utf16_to_utf8(path);
            backing_file_ = std::move(file);
            return;
        }
        throw std::runtime_error("shared_video_frame_channel_file_unavailable");
    }

    [[nodiscard]] std::uint8_t* bytes() const noexcept {
        return static_cast<std::uint8_t*>(view_->get());
    }

    SharedVideoFrameChannelConfiguration configuration_{};
    std::uint64_t slot_size_{0U};
    std::uint64_t mapping_size_{0U};
    std::unique_ptr<UniqueHandle> backing_file_{};
    std::unique_ptr<UniqueHandle> mapping_{};
    std::unique_ptr<UniqueView> view_{};
    std::mutex mutex_{};
    std::uint64_t sequence_{0U};
    std::uint64_t heartbeat_{1U};
    std::string backing_file_path_utf8_{};
};

class WindowsSharedVideoFrameReader final : public SharedVideoFrameReader {
  public:
    WindowsSharedVideoFrameReader(const std::uintptr_t handle,
                                  const std::uint64_t mapping_size)
        : mapping_size_(mapping_size) {
        if (handle == 0U || mapping_size < kHeaderSize ||
            mapping_size > static_cast<std::uint64_t>(
                               (std::numeric_limits<SIZE_T>::max)())) {
            throw std::invalid_argument("shared_video_frame_channel_handle_invalid");
        }
        view_ = std::make_unique<UniqueView>(MapViewOfFile(
            reinterpret_cast<HANDLE>(handle), FILE_MAP_READ, 0U, 0U,
            static_cast<SIZE_T>(mapping_size)));
        if (view_->get() == nullptr) {
            throw std::runtime_error("shared_video_frame_channel_unavailable");
        }
        configuration_ = parse_header(bytes(), mapping_size_);
        slot_size_ = aligned_slot_size(configuration_.layout.payload_size);
    }

    [[nodiscard]] bool read_latest(PackedVideoFrame& destination) override {
        SlotSnapshot snapshot{};
        if (!read_current_slot(snapshot) ||
            snapshot.published <= last_sequence_) {
            return false;
        }
        destination.bytes.resize(
            static_cast<std::size_t>(configuration_.layout.payload_size));
        std::memcpy(destination.bytes.data(),
                    bytes() + snapshot.slot_offset + kSlotHeaderSize,
                    destination.bytes.size());
        if (!slot_is_current(snapshot)) {
            return false;
        }
        destination.sequence = snapshot.published;
        destination.presentation_timestamp_ns = snapshot.presentation_timestamp_ns;
        destination.duration_ns = snapshot.duration_ns;
        destination.produced_monotonic_ns = snapshot.produced_monotonic_ns;
        destination.discontinuity = snapshot.discontinuity;
        destination.width = configuration_.layout.width;
        destination.height = configuration_.layout.height;
        destination.pixel_format = configuration_.layout.pixel_format;
        destination.plane_strides = configuration_.layout.plane_strides;
        destination.plane_offsets = configuration_.layout.plane_offsets;
        last_sequence_ = snapshot.published;
        return true;
    }

    [[nodiscard]] bool visit_current_frame(
        const FrameVisitor& visitor) override {
        if (!visitor) {
            return false;
        }
        for (std::uint32_t attempt = 0U; attempt < 2U; ++attempt) {
            SlotSnapshot snapshot{};
            if (!read_current_slot(snapshot)) {
                return false;
            }
            visitor(frame_view(snapshot));
            if (slot_is_unchanged(snapshot)) {
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] std::uint64_t heartbeat() const noexcept override {
        return load_sequence(bytes(), kHeartbeatOffset);
    }

    [[nodiscard]] const SharedVideoFrameChannelConfiguration&
    configuration() const noexcept override {
        return configuration_;
    }

  private:
    struct SlotSnapshot final {
        std::uint64_t published{0U};
        std::uint64_t slot_offset{0U};
        std::uint64_t expected_marker{0U};
        std::uint64_t presentation_timestamp_ns{0U};
        std::uint64_t duration_ns{0U};
        std::uint64_t produced_monotonic_ns{0U};
        bool discontinuity{false};
    };

    [[nodiscard]] bool read_current_slot(SlotSnapshot& snapshot) const noexcept {
        const auto published = load_sequence(bytes(), kPublishedSequenceOffset);
        if (published == 0U || published > kMaximumSequence) {
            return false;
        }
        const auto slot_index = (published - 1U) % kSlotCount;
        const auto slot_offset = kHeaderSize + slot_index * slot_size_;
        const auto expected_marker = published * 2U;
        if (load_sequence(bytes(), static_cast<std::size_t>(slot_offset)) !=
            expected_marker) {
            return false;
        }
        const auto sequence = read_value<std::uint64_t>(
            bytes(), static_cast<std::size_t>(slot_offset + 8U));
        const auto payload_size = read_value<std::uint64_t>(
            bytes(), static_cast<std::size_t>(slot_offset + 40U));
        if (sequence != published ||
            payload_size != configuration_.layout.payload_size) {
            return false;
        }
        snapshot = {
            .published = published,
            .slot_offset = slot_offset,
            .expected_marker = expected_marker,
            .presentation_timestamp_ns = read_value<std::uint64_t>(
                bytes(), static_cast<std::size_t>(slot_offset + 16U)),
            .duration_ns = read_value<std::uint64_t>(
                bytes(), static_cast<std::size_t>(slot_offset + 24U)),
            .produced_monotonic_ns = read_value<std::uint64_t>(
                bytes(), static_cast<std::size_t>(slot_offset + 32U)),
            .discontinuity =
                read_value<std::uint32_t>(
                    bytes(), static_cast<std::size_t>(slot_offset + 48U)) != 0U,
        };
        return true;
    }

    [[nodiscard]] bool slot_is_current(
        const SlotSnapshot& snapshot) const noexcept {
        return slot_is_unchanged(snapshot) &&
               load_sequence(bytes(), kPublishedSequenceOffset) ==
                   snapshot.published;
    }

    [[nodiscard]] bool slot_is_unchanged(
        const SlotSnapshot& snapshot) const noexcept {
        return load_sequence(bytes(),
                             static_cast<std::size_t>(snapshot.slot_offset)) ==
               snapshot.expected_marker;
    }

    [[nodiscard]] VideoFrameView frame_view(
        const SlotSnapshot& snapshot) const noexcept {
        const auto& layout = configuration_.layout;
        const auto* payload =
            bytes() + snapshot.slot_offset + kSlotHeaderSize;
        const auto first_plane_size =
            layout.plane_count == 2U
                ? layout.plane_offsets[1] - layout.plane_offsets[0]
                : layout.payload_size - layout.plane_offsets[0];
        const auto second_plane_size =
            layout.plane_count == 2U
                ? layout.payload_size - layout.plane_offsets[1]
                : 0U;
        return {
            .sequence = snapshot.published,
            .presentation_timestamp_ns = snapshot.presentation_timestamp_ns,
            .duration_ns = snapshot.duration_ns,
            .produced_monotonic_ns = snapshot.produced_monotonic_ns,
            .discontinuity = snapshot.discontinuity,
            .width = layout.width,
            .height = layout.height,
            .pixel_format = layout.pixel_format,
            .planes = {
                std::span<const std::uint8_t>{
                    payload + layout.plane_offsets[0],
                    static_cast<std::size_t>(first_plane_size)},
                std::span<const std::uint8_t>{
                    payload + layout.plane_offsets[1],
                    static_cast<std::size_t>(second_plane_size)},
            },
            .plane_strides = {
                static_cast<std::int32_t>(layout.plane_strides[0]),
                static_cast<std::int32_t>(layout.plane_strides[1]),
            },
        };
    }

    [[nodiscard]] const std::uint8_t* bytes() const noexcept {
        return static_cast<const std::uint8_t*>(view_->get());
    }

    std::uint64_t mapping_size_{0U};
    std::uint64_t slot_size_{0U};
    std::unique_ptr<UniqueView> view_{};
    SharedVideoFrameChannelConfiguration configuration_{};
    std::uint64_t last_sequence_{0U};
};

#endif

} // namespace

std::unique_ptr<SharedVideoFramePublisher> make_shared_video_frame_publisher(
    const SharedVideoFrameChannelConfiguration& configuration) {
    validate_configuration(configuration);
#ifdef _WIN32
    return std::make_unique<WindowsSharedVideoFramePublisher>(configuration, false);
#else
    static_cast<void>(configuration);
    throw std::runtime_error("shared_video_frame_channel_platform_unsupported");
#endif
}

std::unique_ptr<SharedVideoFramePublisher>
make_cross_process_shared_video_frame_publisher(
    const SharedVideoFrameChannelConfiguration& configuration) {
    validate_configuration(configuration);
#ifdef _WIN32
    return std::make_unique<WindowsSharedVideoFramePublisher>(configuration, true);
#else
    static_cast<void>(configuration);
    throw std::runtime_error("shared_video_frame_channel_platform_unsupported");
#endif
}

std::unique_ptr<SharedVideoFrameReader>
make_shared_video_frame_reader(const std::uintptr_t native_mapping_handle,
                               const std::uint64_t mapping_size) {
#ifdef _WIN32
    return std::make_unique<WindowsSharedVideoFrameReader>(native_mapping_handle,
                                                           mapping_size);
#else
    static_cast<void>(native_mapping_handle);
    static_cast<void>(mapping_size);
    throw std::runtime_error("shared_video_frame_channel_platform_unsupported");
#endif
}

} // namespace solin::media_engine
