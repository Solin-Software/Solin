#include "local_camera_inventory.hpp"

#include "solin/media_engine/virtual_camera_identity.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <optional>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <objbase.h>
#endif

namespace solin::media_engine {
namespace {

#ifdef _WIN32
constexpr std::size_t kMaximumInventoryDevices = 64U;
constexpr std::size_t kMaximumDeviceIdBytes = 1024U;
constexpr std::size_t kMaximumDisplayNameBytes = 512U;

template <typename Interface> class ComPtr final {
  public:
    ComPtr() = default;
    ~ComPtr() { reset(); }
    ComPtr(const ComPtr&) = delete;
    ComPtr& operator=(const ComPtr&) = delete;

    [[nodiscard]] Interface* get() const noexcept { return value_; }
    [[nodiscard]] Interface** put() noexcept {
        reset();
        return &value_;
    }

  private:
    void reset() noexcept {
        if (value_ != nullptr) {
            value_->Release();
            value_ = nullptr;
        }
    }

    Interface* value_{nullptr};
};

class ComApartment final {
  public:
    ComApartment() noexcept
        : result_(CoInitializeEx(nullptr, COINIT_MULTITHREADED)),
          owns_initialization_(SUCCEEDED(result_)) {}
    ~ComApartment() {
        if (owns_initialization_) {
            CoUninitialize();
        }
    }

    [[nodiscard]] HRESULT result() const noexcept { return result_; }
    [[nodiscard]] bool usable() const noexcept {
        return SUCCEEDED(result_) || result_ == RPC_E_CHANGED_MODE;
    }

  private:
    HRESULT result_{E_FAIL};
    bool owns_initialization_{false};
};

class MediaFoundationSession final {
  public:
    MediaFoundationSession() noexcept : result_(MFStartup(MF_VERSION)) {}
    ~MediaFoundationSession() {
        if (SUCCEEDED(result_)) {
            static_cast<void>(MFShutdown());
        }
    }

    [[nodiscard]] HRESULT result() const noexcept { return result_; }
    [[nodiscard]] bool usable() const noexcept { return SUCCEEDED(result_); }

  private:
    HRESULT result_{E_FAIL};
};

class ActivateArray final {
  public:
    ActivateArray() = default;
    ~ActivateArray() {
        for (std::uint32_t index = 0U; value_ != nullptr && index < count_;
             ++index) {
            if (value_[index] != nullptr) {
                value_[index]->Release();
            }
        }
        CoTaskMemFree(value_);
    }
    ActivateArray(const ActivateArray&) = delete;
    ActivateArray& operator=(const ActivateArray&) = delete;

    [[nodiscard]] IMFActivate*** put() noexcept { return &value_; }
    [[nodiscard]] UINT32* count_put() noexcept { return &count_; }
    [[nodiscard]] IMFActivate* operator[](const std::uint32_t index) const noexcept {
        return value_[index];
    }
    [[nodiscard]] std::uint32_t size() const noexcept { return count_; }

  private:
    IMFActivate** value_{nullptr};
    UINT32 count_{0U};
};

class CoTaskMemString final {
  public:
    CoTaskMemString() = default;
    ~CoTaskMemString() { CoTaskMemFree(value_); }
    CoTaskMemString(const CoTaskMemString&) = delete;
    CoTaskMemString& operator=(const CoTaskMemString&) = delete;

    [[nodiscard]] WCHAR** put() noexcept { return &value_; }
    [[nodiscard]] const WCHAR* get() const noexcept { return value_; }

  private:
    WCHAR* value_{nullptr};
};

[[nodiscard]] std::string hresult_code(const HRESULT result) {
    std::ostringstream stream;
    stream << "0x" << std::uppercase << std::hex << std::setw(8)
           << std::setfill('0') << static_cast<std::uint32_t>(result);
    return stream.str();
}

[[nodiscard]] std::string utf8_from_wide(const WCHAR* value) {
    if (value == nullptr || *value == L'\0') {
        return {};
    }
    const auto size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1,
                                          nullptr, 0, nullptr, nullptr);
    if (size <= 1) {
        return {};
    }
    std::string result(static_cast<std::size_t>(size), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1,
                            result.data(), size, nullptr, nullptr) == 0) {
        return {};
    }
    result.pop_back();
    return result;
}

[[nodiscard]] bool valid_utf8(const std::string& value) noexcept {
    return value.empty() ||
           MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                               static_cast<int>(value.size()), nullptr, 0) > 0;
}

[[nodiscard]] std::string bounded_display_name(std::string value) {
    std::ranges::replace_if(
        value,
        [](const unsigned char character) {
            return character < 32U || character == 127U;
        },
        ' ');
    if (value.size() <= kMaximumDisplayNameBytes) {
        return value;
    }
    value.resize(kMaximumDisplayNameBytes);
    while (!value.empty() && !valid_utf8(value)) {
        value.pop_back();
    }
    return value;
}

[[nodiscard]] std::string activate_string(IMFActivate* activate,
                                          const GUID& attribute) {
    CoTaskMemString raw_value;
    UINT32 character_count = 0U;
    if (activate == nullptr ||
        FAILED(activate->GetAllocatedString(attribute, raw_value.put(),
                                            &character_count)) ||
        character_count == 0U) {
        return {};
    }
    return utf8_from_wide(raw_value.get());
}

[[nodiscard]] LocalCameraInventorySnapshot windows_camera_inventory() {
    static thread_local std::optional<ComApartment> apartment;
    if (!apartment.has_value() || !apartment->usable()) {
        apartment.emplace();
    }
    if (!apartment->usable()) {
        return {
            .supported = true,
            .error_code = "camera_inventory_com_initialization_failed",
            .native_error_code = hresult_code(apartment->result()),
        };
    }
    static thread_local std::optional<MediaFoundationSession> media_foundation;
    if (!media_foundation.has_value() || !media_foundation->usable()) {
        media_foundation.emplace();
    }
    if (!media_foundation->usable()) {
        return {
            .supported = true,
            .error_code = "camera_inventory_media_foundation_startup_failed",
            .native_error_code = hresult_code(media_foundation->result()),
        };
    }
    ComPtr<IMFAttributes> attributes;
    auto result = MFCreateAttributes(attributes.put(), 1U);
    if (SUCCEEDED(result)) {
        result = attributes.get()->SetGUID(
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    }
    ActivateArray activations;
    if (SUCCEEDED(result)) {
        result = MFEnumDeviceSources(attributes.get(), activations.put(),
                                     activations.count_put());
    }
    if (FAILED(result)) {
        return {
            .supported = true,
            .error_code = "camera_inventory_enumeration_failed",
            .native_error_code = hresult_code(result),
        };
    }

    LocalCameraInventorySnapshot snapshot{.supported = true};
    snapshot.devices.reserve(
        (std::min)(static_cast<std::size_t>(activations.size()),
                   kMaximumInventoryDevices));
    for (std::uint32_t index = 0U;
         index < activations.size() &&
         snapshot.devices.size() < kMaximumInventoryDevices;
         ++index) {
        auto device_id = activate_string(
            activations[index],
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK);
        auto display_name = bounded_display_name(activate_string(
            activations[index], MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME));
        if (device_id.empty() || device_id.size() > kMaximumDeviceIdBytes ||
            display_name.empty() ||
            is_solin_virtual_camera_device(device_id)) {
            continue;
        }
        const auto duplicate = std::ranges::any_of(
            snapshot.devices, [&device_id](const LocalCameraInventoryDevice& device) {
                return same_local_camera_device_id(device.device_id, device_id);
            });
        if (duplicate) {
            continue;
        }
        UINT32 hardware_source = TRUE;
        const auto hardware_attribute = activations[index]->GetUINT32(
            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_HW_SOURCE,
            &hardware_source);
        const auto software_device =
            SUCCEEDED(hardware_attribute)
                ? hardware_source == FALSE
                : is_windows_software_camera_device(device_id);
        snapshot.devices.push_back({
            .device_id = std::move(device_id),
            .display_name = std::move(display_name),
            .software_device = software_device,
        });
    }
    return snapshot;
}
#endif

} // namespace

LocalCameraInventorySnapshot platform_local_camera_inventory() noexcept {
#ifdef _WIN32
    try {
        return windows_camera_inventory();
    } catch (...) {
        return {
            .supported = true,
            .error_code = "camera_inventory_failed",
        };
    }
#else
    return {};
#endif
}

} // namespace solin::media_engine
