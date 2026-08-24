#include "windows_virtual_camera_backend.hpp"

#ifdef _WIN32

#include "solin/media_engine/shared_video_frame_channel.hpp"
#include "solin/media_engine/virtual_camera_broker.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <dshow.h>
#include <oleauto.h>
#include <windows.h>

#include <atomic>
#include <filesystem>
#include <fstream>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace solin::media_engine {
namespace {

constexpr std::uint32_t kMinimumWindowsBuild = 17'763U;
constexpr wchar_t kFilterClassIdText[] =
    L"{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}";
constexpr wchar_t kVideoInputDeviceCategoryText[] =
    L"{860BB310-5D01-11D0-BD3B-00A0C911CE86}";

class RegistryKey final {
  public:
    RegistryKey() = default;
    ~RegistryKey() { reset(); }
    RegistryKey(const RegistryKey&) = delete;
    RegistryKey& operator=(const RegistryKey&) = delete;

    [[nodiscard]] HKEY* put() noexcept {
        reset();
        return &value_;
    }
    [[nodiscard]] HKEY get() const noexcept { return value_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr;
    }

  private:
    void reset() noexcept {
        if (value_ != nullptr) {
            static_cast<void>(RegCloseKey(value_));
            value_ = nullptr;
        }
    }
    HKEY value_{nullptr};
};

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
    [[nodiscard]] bool usable() const noexcept {
        return SUCCEEDED(result_) || result_ == RPC_E_CHANGED_MODE;
    }

  private:
    HRESULT result_{E_FAIL};
    bool owns_initialization_{false};
};

struct RegistrationProbe final {
    bool present{false};
    bool valid{false};
};

[[nodiscard]] std::uint32_t windows_build_number() noexcept {
    using RtlGetVersionFunction = LONG(WINAPI*)(PRTL_OSVERSIONINFOW);
    const auto ntdll = GetModuleHandleW(L"ntdll.dll");
    if (ntdll == nullptr) {
        return 0U;
    }
    const auto rtl_get_version = reinterpret_cast<RtlGetVersionFunction>(
        GetProcAddress(ntdll, "RtlGetVersion"));
    if (rtl_get_version == nullptr) {
        return 0U;
    }
    RTL_OSVERSIONINFOW version{};
    version.dwOSVersionInfoSize = sizeof(version);
    return rtl_get_version(&version) == 0 ? version.dwBuildNumber : 0U;
}

[[nodiscard]] bool read_registry_string(HKEY key, const wchar_t* value_name,
                                        std::wstring& value) {
    DWORD type = 0U;
    DWORD size = 0U;
    if (RegQueryValueExW(key, value_name, nullptr, &type, nullptr, &size) !=
            ERROR_SUCCESS ||
        type != REG_SZ || size < sizeof(wchar_t) ||
        size % sizeof(wchar_t) != 0U) {
        return false;
    }
    std::vector<wchar_t> storage(size / sizeof(wchar_t));
    if (RegQueryValueExW(key, value_name, nullptr, &type,
                        reinterpret_cast<BYTE*>(storage.data()), &size) !=
        ERROR_SUCCESS) {
        return false;
    }
    storage.back() = L'\0';
    value.assign(storage.data());
    return !value.empty();
}

[[nodiscard]] bool has_filter_data(HKEY key) {
    DWORD type = 0U;
    DWORD size = 0U;
    return RegQueryValueExW(key, L"FilterData", nullptr, &type, nullptr,
                            &size) == ERROR_SUCCESS &&
           type == REG_BINARY && size != 0U;
}

[[nodiscard]] RegistrationProbe probe_registration_view(const REGSAM view,
                                                        const bool x64) {
    const std::wstring class_path =
        std::wstring{L"Software\\Classes\\CLSID\\"} +
        kFilterClassIdText + L"\\InprocServer32";
    RegistryKey class_key;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, class_path.c_str(), 0U,
                      KEY_READ | view, class_key.put()) != ERROR_SUCCESS) {
        return {};
    }
    RegistrationProbe result{.present = true};
    std::wstring dll_path;
    std::wstring threading_model;
    if (!read_registry_string(class_key.get(), nullptr, dll_path) ||
        !read_registry_string(class_key.get(), L"ThreadingModel",
                              threading_model) ||
        _wcsicmp(threading_model.c_str(), L"Both") != 0 ||
        !std::filesystem::path{dll_path}.is_absolute() ||
        !std::filesystem::is_regular_file(dll_path) ||
        !windows_pe_dll_matches_architecture(dll_path, x64)) {
        return result;
    }

    const std::wstring category_path =
        std::wstring{L"Software\\Classes\\CLSID\\"} +
        kVideoInputDeviceCategoryText + L"\\Instance\\" +
        kFilterClassIdText;
    RegistryKey category_key;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, category_path.c_str(), 0U,
                      KEY_READ | view, category_key.put()) != ERROR_SUCCESS) {
        return result;
    }
    std::wstring registered_class_id;
    std::wstring friendly_name;
    result.valid =
        read_registry_string(category_key.get(), L"CLSID",
                             registered_class_id) &&
        _wcsicmp(registered_class_id.c_str(), kFilterClassIdText) == 0 &&
        read_registry_string(category_key.get(), L"FriendlyName",
                             friendly_name) &&
        friendly_name == windows_virtual_camera::kFriendlyName &&
        has_filter_data(category_key.get());
    return result;
}

[[nodiscard]] bool filter_activates_and_is_enumerated_x64() noexcept {
    ComApartment apartment;
    if (!apartment.usable()) {
        return false;
    }
    ComPtr<IBaseFilter> activated_filter;
    if (FAILED(CoCreateInstance(
            windows_virtual_camera::kDirectShowFilterClassId, nullptr,
            CLSCTX_INPROC_SERVER, IID_IBaseFilter,
            reinterpret_cast<void**>(activated_filter.put())))) {
        return false;
    }
    ComPtr<ICreateDevEnum> device_enumerator;
    if (FAILED(CoCreateInstance(
            CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER,
            IID_ICreateDevEnum,
            reinterpret_cast<void**>(device_enumerator.put())))) {
        return false;
    }
    ComPtr<IEnumMoniker> monikers;
    if (device_enumerator.get()->CreateClassEnumerator(
            CLSID_VideoInputDeviceCategory, monikers.put(), 0U) != S_OK) {
        return false;
    }
    for (;;) {
        IMoniker* raw_moniker = nullptr;
        ULONG fetched = 0U;
        if (monikers.get()->Next(1U, &raw_moniker, &fetched) != S_OK ||
            raw_moniker == nullptr) {
            return false;
        }
        ComPtr<IPropertyBag> properties;
        const auto bind_result = raw_moniker->BindToStorage(
            nullptr, nullptr, IID_IPropertyBag,
            reinterpret_cast<void**>(properties.put()));
        raw_moniker->Release();
        if (FAILED(bind_result)) {
            continue;
        }
        VARIANT class_id;
        VariantInit(&class_id);
        const auto read_result =
            properties.get()->Read(L"CLSID", &class_id, nullptr);
        CLSID parsed{};
        const auto matches =
            SUCCEEDED(read_result) && class_id.vt == VT_BSTR &&
            class_id.bstrVal != nullptr &&
            SUCCEEDED(CLSIDFromString(class_id.bstrVal, &parsed)) &&
            IsEqualGUID(parsed,
                        windows_virtual_camera::kDirectShowFilterClassId) != FALSE;
        VariantClear(&class_id);
        if (matches) {
            return true;
        }
    }
}

class WindowsDirectShowVirtualCameraSink final : public VirtualCameraSink {
  public:
    WindowsDirectShowVirtualCameraSink(VirtualCameraConfiguration configuration,
                                       const std::uint64_t generation)
        : configuration_(std::move(configuration)) {
        if (configuration_.video_format.pixel_format != "nv12" ||
            configuration_.video_format.color_space != "bt709" ||
            configuration_.video_format.color_range != "limited") {
            throw std::invalid_argument("virtual_camera_format_unsupported");
        }
        auto sink =
            make_shared_frame_virtual_camera_sink(configuration_, generation);
        frame_sink_ =
            std::shared_ptr<SharedFrameVirtualCameraSink>{std::move(sink)};
    }

    ~WindowsDirectShowVirtualCameraSink() override { stop(); }

    void start() override {
        std::scoped_lock lock{mutex_};
        const auto state = frame_sink_->health().state;
        if (state == VirtualCameraSinkState::ready ||
            state == VirtualCameraSinkState::starting) {
            return;
        }
        try {
            frame_sink_->start();
            broker_ = make_virtual_camera_frame_broker(frame_sink_);
            broker_->start();
        } catch (...) {
            if (broker_ != nullptr) {
                broker_->stop();
                broker_.reset();
            }
            frame_sink_->stop();
            throw;
        }
    }

    [[nodiscard]] bool publish(const VideoFrameView& frame) noexcept override {
        return frame_sink_->publish(frame);
    }

    void heartbeat() noexcept override { frame_sink_->heartbeat(); }

    void stop() noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (broker_ != nullptr) {
                broker_->stop();
                broker_.reset();
            }
            frame_sink_->stop();
        } catch (...) {
        }
    }

    [[nodiscard]] VirtualCameraSinkHealth health() const override {
        std::scoped_lock lock{mutex_};
        auto result = frame_sink_->health();
        if (broker_ != nullptr) {
            const auto broker_health = broker_->health();
            if (!broker_health.running &&
                result.state == VirtualCameraSinkState::ready) {
                result.state = VirtualCameraSinkState::degraded;
                result.error_code = broker_health.error_code.empty()
                                        ? "virtual_camera_broker_unavailable"
                                        : broker_health.error_code;
            }
        }
        return result;
    }

  private:
    VirtualCameraConfiguration configuration_{};
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink_{};
    mutable std::mutex mutex_{};
    std::unique_ptr<VirtualCameraFrameBroker> broker_{};
};

class WindowsDirectShowVirtualCameraBackend final
    : public VirtualCameraBackend {
  public:
    [[nodiscard]] VirtualCameraBackendProbe probe() const override {
        return probe_windows_directshow_virtual_camera();
    }

    [[nodiscard]] std::unique_ptr<VirtualCameraSink> create_sink(
        const VirtualCameraConfiguration& configuration) override {
        const auto availability = probe();
        if (!availability.operational) {
            throw std::runtime_error(availability.error_code);
        }
        const auto generation = next_generation_.fetch_add(1U);
        if (generation == 0U) {
            throw std::overflow_error("virtual_camera_generation_exhausted");
        }
        return std::make_unique<WindowsDirectShowVirtualCameraSink>(
            configuration, generation);
    }

  private:
    std::atomic_uint64_t next_generation_{1U};
};

} // namespace

bool windows_pe_dll_matches_architecture(
    const std::filesystem::path& path, const bool x64) noexcept {
    try {
        std::ifstream binary{path, std::ios::binary};
        if (!binary) {
            return false;
        }
        binary.seekg(0, std::ios::end);
        const auto file_size = binary.tellg();
        if (file_size < static_cast<std::streamoff>(sizeof(IMAGE_DOS_HEADER))) {
            return false;
        }
        binary.seekg(0, std::ios::beg);
        IMAGE_DOS_HEADER dos_header{};
        if (!binary.read(reinterpret_cast<char*>(&dos_header),
                         sizeof(dos_header)) ||
            dos_header.e_magic != IMAGE_DOS_SIGNATURE ||
            dos_header.e_lfanew <
                static_cast<LONG>(sizeof(IMAGE_DOS_HEADER))) {
            return false;
        }
        constexpr auto nt_prefix_size =
            static_cast<std::streamoff>(sizeof(DWORD) +
                                        sizeof(IMAGE_FILE_HEADER));
        const auto nt_offset = static_cast<std::streamoff>(dos_header.e_lfanew);
        if (nt_offset > file_size - nt_prefix_size) {
            return false;
        }
        binary.seekg(nt_offset, std::ios::beg);
        DWORD signature = 0U;
        IMAGE_FILE_HEADER file_header{};
        if (!binary.read(reinterpret_cast<char*>(&signature),
                         sizeof(signature)) ||
            !binary.read(reinterpret_cast<char*>(&file_header),
                         sizeof(file_header)) ||
            signature != IMAGE_NT_SIGNATURE ||
            (file_header.Characteristics & IMAGE_FILE_DLL) == 0U ||
            (file_header.Characteristics & IMAGE_FILE_EXECUTABLE_IMAGE) == 0U ||
            file_header.SizeOfOptionalHeader < sizeof(WORD) ||
            nt_offset + nt_prefix_size + file_header.SizeOfOptionalHeader >
                file_size) {
            return false;
        }
        WORD optional_magic = 0U;
        if (!binary.read(reinterpret_cast<char*>(&optional_magic),
                         sizeof(optional_magic))) {
            return false;
        }
        const auto expected_machine =
            x64 ? IMAGE_FILE_MACHINE_AMD64 : IMAGE_FILE_MACHINE_I386;
        const auto expected_magic =
            x64 ? IMAGE_NT_OPTIONAL_HDR64_MAGIC : IMAGE_NT_OPTIONAL_HDR32_MAGIC;
        return file_header.Machine == expected_machine &&
               optional_magic == expected_magic;
    } catch (...) {
        return false;
    }
}

VirtualCameraBackendProbe
probe_windows_directshow_virtual_camera() noexcept {
    try {
        VirtualCameraBackendProbe result{
            .backend = VirtualCameraBackendKind::windows_directshow,
            .operating_system_build = windows_build_number(),
        };
        result.platform_supported =
            result.operating_system_build >= kMinimumWindowsBuild;
        if (!result.platform_supported) {
            result.error_code = "virtual_camera_windows_version_unsupported";
            return result;
        }
        const auto x86 = probe_registration_view(KEY_WOW64_32KEY, false);
        const auto x64 = probe_registration_view(KEY_WOW64_64KEY, true);
        result.filter_registered_x86 = x86.valid;
        result.filter_registered_x64 = x64.valid;
        if (!x86.valid) {
            result.error_code = x86.present
                                    ? "virtual_camera_filter_x86_invalid"
                                    : "virtual_camera_filter_x86_missing";
            return result;
        }
        if (!x64.valid) {
            result.error_code = x64.present
                                    ? "virtual_camera_filter_x64_invalid"
                                    : "virtual_camera_filter_x64_missing";
            return result;
        }
        if (!filter_activates_and_is_enumerated_x64()) {
            result.error_code = "virtual_camera_filter_x64_unavailable";
            return result;
        }
        try {
            static_cast<void>(
                windows_virtual_camera::current_user_broker_pipe_name());
            auto transport_probe =
                make_cross_process_shared_video_frame_publisher(
                    {
                        .generation = 1U,
                        .layout = packed_video_frame_layout(
                            2U, 2U, VideoFramePixelFormat::nv12),
                    });
            result.cross_process_transport_available =
                transport_probe->native_mapping_handle() != 0U &&
                !transport_probe->backing_file_path_utf8().empty() &&
                transport_probe->mapping_size() != 0U;
        } catch (...) {
            result.cross_process_transport_available = false;
        }
        if (!result.cross_process_transport_available) {
            result.error_code = "virtual_camera_cross_process_transport_unavailable";
            return result;
        }
        result.operational = true;
        result.error_code.clear();
        return result;
    } catch (...) {
        return {
            .backend = VirtualCameraBackendKind::windows_directshow,
            .operating_system_build = windows_build_number(),
            .error_code = "virtual_camera_probe_failed",
        };
    }
}

std::unique_ptr<VirtualCameraBackend>
make_windows_directshow_virtual_camera_backend() {
    return std::make_unique<WindowsDirectShowVirtualCameraBackend>();
}

} // namespace solin::media_engine

#endif
