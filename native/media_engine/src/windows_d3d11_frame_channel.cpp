#include "windows_d3d11_frame_channel.hpp"

#ifdef _WIN32

#include <solin/media_transport/d3d11_frame_channel_protocol.hpp>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11_1.h>
#include <dxgi1_2.h>
#include <windows.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

namespace solin::media_engine {
namespace {

using Microsoft::WRL::ComPtr;
namespace protocol = solin::media_transport::d3d11_frame_channel;

class UniqueHandle final {
  public:
    explicit UniqueHandle(HANDLE value = nullptr) noexcept : value_(value) {}
    ~UniqueHandle() {
        if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
            static_cast<void>(CloseHandle(value_));
        }
    }

    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    UniqueHandle(UniqueHandle&&) = delete;
    UniqueHandle& operator=(UniqueHandle&&) = delete;

    [[nodiscard]] HANDLE get() const noexcept { return value_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

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

    [[nodiscard]] std::uint8_t* bytes() const noexcept {
        return static_cast<std::uint8_t*>(value_);
    }

  private:
    void* value_{nullptr};
};

class MutexLease final {
  public:
    explicit MutexLease(HANDLE mutex, const DWORD timeout_ms = 0U) {
        const auto result = WaitForSingleObject(mutex, timeout_ms);
        if (result == WAIT_OBJECT_0 || result == WAIT_ABANDONED) {
            mutex_ = mutex;
            return;
        }
        if (result != WAIT_TIMEOUT) {
            throw std::runtime_error("d3d11_channel_mutex_failed");
        }
    }
    ~MutexLease() {
        if (mutex_ != nullptr) {
            static_cast<void>(ReleaseMutex(mutex_));
        }
    }

    MutexLease(const MutexLease&) = delete;
    MutexLease& operator=(const MutexLease&) = delete;
    [[nodiscard]] explicit operator bool() const noexcept {
        return mutex_ != nullptr;
    }

  private:
    HANDLE mutex_{nullptr};
};

class SlotLease final {
  public:
    SlotLease(volatile LONG* count, std::shared_ptr<UniqueView> view,
              ID3D11Texture2D* texture)
        : count_(count), view_(std::move(view)), texture_(texture) {
        if (count_ == nullptr || view_ == nullptr || view_->bytes() == nullptr ||
            texture_ == nullptr) {
            throw std::invalid_argument("d3d11_channel_slot_lease_invalid");
        }
    }
    ~SlotLease() {
        const auto remaining = InterlockedDecrement(count_);
        if (remaining < 0L) {
            static_cast<void>(InterlockedExchange(count_, 0L));
        }
    }

    SlotLease(const SlotLease&) = delete;
    SlotLease& operator=(const SlotLease&) = delete;

  private:
    volatile LONG* count_{nullptr};
    std::shared_ptr<UniqueView> view_{};
    // A resource-generation change replaces the reader's texture array while
    // Raw and Program transitions can still retain an outgoing frame. Keep the
    // exact COM resource alive for the full slot lease, not merely its named
    // handle and shared-memory lease counter.
    ComPtr<ID3D11Texture2D> texture_{};
};

template <typename Value>
[[nodiscard]] Value read_value(const std::uint8_t* bytes,
                               const std::size_t offset) noexcept {
    Value value{};
    std::memcpy(&value, bytes + offset, sizeof(Value));
    return value;
}

template <typename Value>
void write_value(std::uint8_t* bytes, const std::size_t offset,
                 const Value value) noexcept {
    std::memcpy(bytes + offset, &value, sizeof(Value));
}

[[nodiscard]] std::uint64_t process_creation_time(HANDLE process) noexcept {
    FILETIME creation{};
    FILETIME exit{};
    FILETIME kernel{};
    FILETIME user{};
    if (GetProcessTimes(process, &creation, &exit, &kernel, &user) == FALSE) {
        return 0U;
    }
    return (static_cast<std::uint64_t>(creation.dwHighDateTime) << 32U) |
           creation.dwLowDateTime;
}

[[nodiscard]] std::uint64_t current_process_creation_time() noexcept {
    static const auto value = process_creation_time(GetCurrentProcess());
    return value;
}

[[nodiscard]] bool process_identity_is_alive(
    const std::uint32_t process_id,
    const std::uint64_t expected_creation_time) noexcept {
    if (process_id == 0U || expected_creation_time == 0U) {
        return false;
    }
    const UniqueHandle process{OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, process_id)};
    return process && WaitForSingleObject(process.get(), 0U) == WAIT_TIMEOUT &&
           process_creation_time(process.get()) == expected_creation_time;
}

[[nodiscard]] std::wstring widen_ascii(const std::string& value) {
    if (value.empty() || value.size() > 128U ||
        !std::all_of(value.begin(), value.end(), [](const unsigned char item) {
            return (item >= '0' && item <= '9') ||
                   (item >= 'a' && item <= 'z') ||
                   (item >= 'A' && item <= 'Z') || item == '-' || item == '_';
        })) {
        throw std::runtime_error("d3d11_channel_handle_invalid");
    }
    return std::wstring{value.begin(), value.end()};
}

[[nodiscard]] std::wstring object_name(const wchar_t* prefix,
                                       const std::wstring& token) {
    return std::wstring{prefix} + token;
}

[[nodiscard]] std::wstring texture_name(const std::wstring& token,
                                        const std::uint64_t resource_generation,
                                        const std::uint32_t slot) {
    return std::wstring{protocol::kTexturePrefix} + token + L"." +
           std::to_wstring(resource_generation) + L"." +
           std::to_wstring(slot);
}

[[nodiscard]] std::uint64_t adapter_luid(ID3D11Device* device) {
    ComPtr<IDXGIDevice> dxgi_device;
    ComPtr<IDXGIAdapter> adapter;
    DXGI_ADAPTER_DESC description{};
    if (device == nullptr || FAILED(device->QueryInterface(IID_PPV_ARGS(&dxgi_device))) ||
        FAILED(dxgi_device->GetAdapter(&adapter)) ||
        FAILED(adapter->GetDesc(&description))) {
        throw std::runtime_error("d3d11_channel_adapter_unavailable");
    }
    std::uint64_t result = 0U;
    std::memcpy(&result, &description.AdapterLuid,
                sizeof(description.AdapterLuid));
    return result;
}

[[nodiscard]] DXGI_FORMAT expected_dxgi_format(
    const FrameChannelConfiguration& configuration) {
    if (configuration.pixel_format == "dynamic") {
        return DXGI_FORMAT_UNKNOWN;
    }
    if (configuration.pixel_format == "nv12") {
        return DXGI_FORMAT_NV12;
    }
    if (configuration.pixel_format == "bgra") {
        return DXGI_FORMAT_B8G8R8A8_UNORM;
    }
    throw std::runtime_error("d3d11_channel_pixel_format_invalid");
}

[[nodiscard]] VideoFramePixelFormat video_pixel_format(
    const DXGI_FORMAT value) {
    if (value == DXGI_FORMAT_NV12) {
        return VideoFramePixelFormat::nv12;
    }
    if (value == DXGI_FORMAT_B8G8R8A8_UNORM) {
        return VideoFramePixelFormat::bgra;
    }
    throw std::runtime_error("d3d11_channel_pixel_format_invalid");
}

class WindowsD3d11FrameChannelReader final
    : public D3d11FrameChannelReader {
  public:
    WindowsD3d11FrameChannelReader(FrameChannelConfiguration configuration,
                                   ID3D11Device* device)
        : configuration_(std::move(configuration)), device_(device),
          token_(widen_ascii(configuration_.handle_token)),
          device_adapter_luid_(adapter_luid(device_.Get())),
          expected_format_(expected_dxgi_format(configuration_)) {
        if (configuration_.transport != "d3d11_shared_texture" ||
            configuration_.generation == 0U || configuration_.width == 0U ||
            configuration_.height == 0U || device_ == nullptr ||
            FAILED(device_.As(&device1_))) {
            throw std::runtime_error("d3d11_channel_configuration_invalid");
        }

        mapping_ = std::make_unique<UniqueHandle>(OpenFileMappingW(
            FILE_MAP_READ | FILE_MAP_WRITE, FALSE,
            object_name(protocol::kMappingPrefix, token_).c_str()));
        if (!*mapping_) {
            throw std::runtime_error("d3d11_channel_mapping_unavailable");
        }
        view_ = std::make_shared<UniqueView>(MapViewOfFile(
            mapping_->get(), FILE_MAP_READ | FILE_MAP_WRITE, 0U, 0U,
            protocol::mapping_size()));
        if (view_->bytes() == nullptr) {
            throw std::runtime_error("d3d11_channel_mapping_unavailable");
        }
        mutex_ = std::make_unique<UniqueHandle>(OpenMutexW(
            SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE,
            object_name(protocol::kMutexPrefix, token_).c_str()));
        event_ = std::make_unique<UniqueHandle>(OpenEventW(
            SYNCHRONIZE | EVENT_MODIFY_STATE, FALSE,
            object_name(protocol::kEventPrefix, token_).c_str()));
        if (!*mutex_ || !*event_) {
            throw std::runtime_error("d3d11_channel_synchronization_unavailable");
        }
        const MutexLease lease{mutex_->get(), 250U};
        if (!lease) {
            throw std::runtime_error("d3d11_channel_producer_busy");
        }
        validate_header();
        const auto producer_id = read_value<std::uint32_t>(
            bytes(), protocol::kProducerProcessIdOffset);
        const auto producer_creation = read_value<std::uint64_t>(
            bytes(), protocol::kProducerCreationTimeOffset);
        if (!process_identity_is_alive(producer_id, producer_creation)) {
            throw std::runtime_error("d3d11_channel_producer_unavailable");
        }
    }

    [[nodiscard]] std::optional<D3d11FrameChannelFrameLease>
    read_latest(const std::uint64_t after_sequence) override {
        const MutexLease channel_lease{mutex_->get()};
        if (!channel_lease) {
            return std::nullopt;
        }
        validate_header();
        const auto sequence = read_value<std::uint64_t>(
            bytes(), protocol::kPublishedSequenceOffset);
        if (sequence == 0U || sequence <= after_sequence) {
            return std::nullopt;
        }
        if (sequence > protocol::kMaximumSequence) {
            throw std::runtime_error("d3d11_channel_sequence_invalid");
        }
        const auto published_slot = read_value<std::uint32_t>(
            bytes(), protocol::kPublishedSlotOffset);
        if (published_slot >= protocol::kSlotCount) {
            throw std::runtime_error("d3d11_channel_slot_invalid");
        }
        ensure_resources();

        const auto offset = protocol::slot_offset(published_slot);
        if (read_value<std::uint64_t>(bytes(),
                                      offset + protocol::kSlotMarkerOffset) !=
                sequence * 2U ||
            read_value<std::uint64_t>(
                bytes(), offset + protocol::kSlotSequenceOffset) != sequence) {
            return std::nullopt;
        }
        auto* lease_count = reinterpret_cast<volatile LONG*>(
            bytes() + offset + protocol::kSlotLeaseCountOffset);
        if (InterlockedCompareExchange(lease_count, 1L, 0L) != 0L) {
            return std::nullopt;
        }
        write_value(bytes(),
                    offset + protocol::kSlotLeaseOwnerProcessIdOffset,
                    static_cast<std::uint32_t>(GetCurrentProcessId()));
        write_value(bytes(),
                    offset + protocol::kSlotLeaseOwnerCreationTimeOffset,
                    current_process_creation_time());
        std::shared_ptr<SlotLease> lifetime;
        try {
            lifetime = std::make_shared<SlotLease>(
                lease_count, view_, textures_[published_slot].Get());
        } catch (...) {
            static_cast<void>(InterlockedExchange(lease_count, 0L));
            throw;
        }
        const auto format = video_pixel_format(resource_format_);
        return make_lease(
            {.resource_generation = resource_generation_,
             .sequence = sequence,
             .presentation_timestamp_ns = read_value<std::uint64_t>(
                 bytes(),
                 offset + protocol::kSlotPresentationTimestampOffset),
             .duration_ns = read_value<std::uint64_t>(
                 bytes(), offset + protocol::kSlotDurationOffset),
             .produced_monotonic_ns = read_value<std::uint64_t>(
                 bytes(), offset + protocol::kSlotProducedMonotonicOffset),
             .media_epoch = read_value<std::uint64_t>(
                 bytes(), offset + protocol::kSlotMediaEpochOffset),
             .width = resource_width_,
              .height = resource_height_,
              .pixel_format = format,
              .texture = textures_[published_slot].Get()},
            std::move(lifetime));
    }

    [[nodiscard]] std::optional<std::uint64_t> media_epoch() override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        validate_header();
        return read_value<std::uint64_t>(bytes(), protocol::kMediaEpochOffset);
    }

    [[nodiscard]] std::optional<FrameChannelImageTransform>
    image_transform() override {
        const MutexLease lease{mutex_->get()};
        if (!lease) {
            return std::nullopt;
        }
        validate_header();
        const auto revision = read_value<std::uint64_t>(
            bytes(), protocol::kImageTransformRevisionOffset);
        if (revision == 0U) {
            return std::nullopt;
        }
        const auto flags = read_value<std::uint32_t>(
            bytes(), protocol::kImageTransformFlagsOffset);
        FrameChannelImageTransform result{
            .revision = revision,
            .media_epoch = read_value<std::uint64_t>(
                bytes(), protocol::kImageTransformMediaEpochOffset),
            .enabled = (flags & protocol::kImageTransformEnabled) != 0U,
            .animate = (flags & protocol::kImageTransformAnimate) != 0U,
            .canvas_width = read_value<std::uint32_t>(
                bytes(), protocol::kImageTransformCanvasWidthOffset),
            .canvas_height = read_value<std::uint32_t>(
                bytes(), protocol::kImageTransformCanvasHeightOffset),
            .duration_ms = read_value<std::uint32_t>(
                bytes(), protocol::kImageTransformDurationMsOffset),
            .zoom = read_value<double>(bytes(),
                                       protocol::kImageTransformZoomOffset),
            .norm_x = read_value<double>(bytes(),
                                         protocol::kImageTransformNormXOffset),
            .norm_y = read_value<double>(bytes(),
                                         protocol::kImageTransformNormYOffset),
        };
        const auto canvas_pixels =
            static_cast<std::uint64_t>(result.canvas_width) *
            result.canvas_height;
        if ((flags & ~protocol::kImageTransformKnownFlags) != 0U ||
            result.canvas_width == 0U ||
            result.canvas_width > configuration_.width ||
            result.canvas_height == 0U ||
            result.canvas_height > configuration_.height ||
            canvas_pixels > 3'840ULL * 2'160ULL ||
            result.duration_ms == 0U || result.duration_ms > 60'000U ||
            !std::isfinite(result.zoom) || result.zoom < 0.1 ||
            result.zoom > 10.0 || !std::isfinite(result.norm_x) ||
            std::abs(result.norm_x) > 16.0 || !std::isfinite(result.norm_y) ||
            std::abs(result.norm_y) > 16.0) {
            throw std::runtime_error("d3d11_channel_image_transform_invalid");
        }
        return result;
    }

    [[nodiscard]] bool
    wait_for_frame(const std::chrono::milliseconds timeout) override {
        const auto bounded = (std::clamp)(
            timeout.count(), std::int64_t{0},
            static_cast<std::int64_t>((std::numeric_limits<DWORD>::max)() -
                                      1U));
        const auto result = WaitForSingleObject(
            event_->get(), static_cast<DWORD>(bounded));
        if (result == WAIT_OBJECT_0) {
            return true;
        }
        if (result == WAIT_TIMEOUT) {
            return false;
        }
        throw std::runtime_error("d3d11_channel_event_failed");
    }

    void wake() noexcept override {
        if (event_ != nullptr && *event_) {
            static_cast<void>(SetEvent(event_->get()));
        }
    }

  private:
    [[nodiscard]] std::uint8_t* bytes() const noexcept {
        return view_->bytes();
    }

    void validate_header() const {
        if (std::memcmp(bytes() + protocol::kMagicOffset,
                        protocol::kMagic.data(), protocol::kMagic.size()) != 0 ||
            read_value<std::uint16_t>(bytes(), protocol::kVersionOffset) !=
                protocol::kVersion ||
            read_value<std::uint16_t>(bytes(), protocol::kHeaderSizeOffset) !=
                protocol::kHeaderSize ||
            read_value<std::uint32_t>(bytes(), protocol::kSlotCountOffset) !=
                protocol::kSlotCount ||
            read_value<std::uint32_t>(bytes(),
                                      protocol::kCapacityWidthOffset) !=
                configuration_.width ||
            read_value<std::uint32_t>(bytes(),
                                      protocol::kCapacityHeightOffset) !=
                configuration_.height ||
            read_value<std::uint64_t>(bytes(), protocol::kGenerationOffset) !=
                configuration_.generation) {
            throw std::runtime_error("d3d11_channel_header_invalid");
        }
    }

    void ensure_resources() {
        const auto generation = read_value<std::uint64_t>(
            bytes(), protocol::kResourceGenerationOffset);
        const auto width = read_value<std::uint32_t>(
            bytes(), protocol::kResourceWidthOffset);
        const auto height = read_value<std::uint32_t>(
            bytes(), protocol::kResourceHeightOffset);
        const auto format = static_cast<DXGI_FORMAT>(read_value<std::uint32_t>(
            bytes(), protocol::kResourceFormatOffset));
        const auto channel_adapter = read_value<std::uint64_t>(
            bytes(), protocol::kAdapterLuidOffset);
        if (generation == 0U || width == 0U || height == 0U ||
            width > configuration_.width || height > configuration_.height ||
            (expected_format_ != DXGI_FORMAT_UNKNOWN &&
             format != expected_format_) ||
            (format != DXGI_FORMAT_NV12 &&
             format != DXGI_FORMAT_B8G8R8A8_UNORM) ||
            channel_adapter != device_adapter_luid_) {
            throw std::runtime_error("d3d11_channel_resource_invalid");
        }
        if (generation == resource_generation_) {
            if (width != resource_width_ || height != resource_height_ ||
                format != resource_format_) {
                throw std::runtime_error("d3d11_channel_resource_invalid");
            }
            return;
        }

        std::array<ComPtr<ID3D11Texture2D>, protocol::kSlotCount> replacement{};
        for (std::uint32_t index = 0U; index < protocol::kSlotCount; ++index) {
            const auto name = texture_name(token_, generation, index);
            if (FAILED(device1_->OpenSharedResourceByName(
                    name.c_str(), DXGI_SHARED_RESOURCE_READ,
                    IID_PPV_ARGS(&replacement[index]))) ||
                replacement[index] == nullptr) {
                throw std::runtime_error("d3d11_channel_texture_unavailable");
            }
            D3D11_TEXTURE2D_DESC description{};
            replacement[index]->GetDesc(&description);
            if (description.Width != width || description.Height != height ||
                description.Format != format || description.ArraySize != 1U ||
                description.MipLevels != 1U ||
                (description.MiscFlags &
                 (D3D11_RESOURCE_MISC_SHARED_NTHANDLE |
                  D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX)) !=
                    (D3D11_RESOURCE_MISC_SHARED_NTHANDLE |
                     D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX)) {
                throw std::runtime_error("d3d11_channel_texture_invalid");
            }
        }
        textures_ = std::move(replacement);
        resource_generation_ = generation;
        resource_width_ = width;
        resource_height_ = height;
        resource_format_ = format;
    }

    FrameChannelConfiguration configuration_{};
    ComPtr<ID3D11Device> device_{};
    ComPtr<ID3D11Device1> device1_{};
    std::wstring token_{};
    std::uint64_t device_adapter_luid_{0U};
    DXGI_FORMAT expected_format_{DXGI_FORMAT_UNKNOWN};
    std::unique_ptr<UniqueHandle> mapping_{};
    std::shared_ptr<UniqueView> view_{};
    std::unique_ptr<UniqueHandle> mutex_{};
    std::unique_ptr<UniqueHandle> event_{};
    std::array<ComPtr<ID3D11Texture2D>, protocol::kSlotCount> textures_{};
    std::uint64_t resource_generation_{0U};
    std::uint32_t resource_width_{0U};
    std::uint32_t resource_height_{0U};
    DXGI_FORMAT resource_format_{DXGI_FORMAT_UNKNOWN};
};

} // namespace

std::unique_ptr<D3d11FrameChannelReader> make_d3d11_frame_channel_reader(
    const FrameChannelConfiguration& configuration, ID3D11Device* device) {
    return std::make_unique<WindowsD3d11FrameChannelReader>(configuration,
                                                            device);
}

} // namespace solin::media_engine

#endif
