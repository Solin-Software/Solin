#include "windows_virtual_camera_backend.hpp"

#ifdef _WIN32

#include "solin/media_engine/virtual_camera_broker.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winternl.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfvirtualcamera.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iterator>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace solin::media_engine {
namespace {

constexpr std::uint32_t kMinimumVirtualCameraBuild = 22'000U;
constexpr wchar_t kExpectedThreadingModel[] = L"Both";

class UniqueModule final {
  public:
    explicit UniqueModule(HMODULE value = nullptr) noexcept : value_(value) {}
    ~UniqueModule() {
        if (value_ != nullptr) {
            static_cast<void>(FreeLibrary(value_));
        }
    }

    UniqueModule(const UniqueModule&) = delete;
    UniqueModule& operator=(const UniqueModule&) = delete;
    UniqueModule(UniqueModule&& other) noexcept
        : value_(std::exchange(other.value_, nullptr)) {}

    [[nodiscard]] HMODULE get() const noexcept { return value_; }

  private:
    HMODULE value_{nullptr};
};

class UniqueRegistryKey final {
  public:
    explicit UniqueRegistryKey(HKEY value = nullptr) noexcept : value_(value) {}
    ~UniqueRegistryKey() {
        if (value_ != nullptr) {
            static_cast<void>(RegCloseKey(value_));
        }
    }

    UniqueRegistryKey(const UniqueRegistryKey&) = delete;
    UniqueRegistryKey& operator=(const UniqueRegistryKey&) = delete;

    [[nodiscard]] HKEY get() const noexcept { return value_; }

  private:
    HKEY value_{nullptr};
};

class UniqueHandle final {
  public:
    explicit UniqueHandle(HANDLE value = nullptr) noexcept : value_(value) {}
    ~UniqueHandle() { reset(); }
    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    UniqueHandle(UniqueHandle&& other) noexcept
        : value_(std::exchange(other.value_, nullptr)) {}
    UniqueHandle& operator=(UniqueHandle&& other) noexcept {
        if (this != &other) {
            reset(std::exchange(other.value_, nullptr));
        }
        return *this;
    }
    void reset(HANDLE value = nullptr) noexcept {
        if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
            static_cast<void>(CloseHandle(value_));
        }
        value_ = value;
    }
    [[nodiscard]] HANDLE get() const noexcept { return value_; }
    [[nodiscard]] HANDLE release() noexcept {
        return std::exchange(value_, nullptr);
    }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

  private:
    HANDLE value_{nullptr};
};

struct RegistrationResult final {
    HRESULT status{E_FAIL};
    std::string error_code{"virtual_camera_registration_failed"};
};

constexpr std::uint64_t kRegistrationHostConfigurationMagic =
    0x324746434D435653ULL; // SVCMCFG2
constexpr std::uint64_t kRegistrationHostResultMagic =
    0x32544C5256435653ULL; // SVCVRLT2
constexpr std::uint32_t kRegistrationHostProtocolVersion = 2U;
constexpr std::size_t kRegistrationHostTextCapacity = 256U;
constexpr auto kRegistrationHostStartTimeout = std::chrono::seconds{5};
constexpr auto kRegistrationHostStopTimeout = std::chrono::seconds{2};

struct RegistrationHostConfigurationWire final {
    std::uint64_t magic{kRegistrationHostConfigurationMagic};
    std::uint32_t version{kRegistrationHostProtocolVersion};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{0U};
    std::uint16_t pipe_name_size{0U};
    std::uint16_t friendly_name_size{0U};
    std::uint32_t reserved{0U};
    VirtualCameraBrokerToken token{};
    std::array<char, kRegistrationHostTextCapacity> pipe_name{};
    std::array<char, kRegistrationHostTextCapacity> friendly_name{};
};

struct RegistrationHostResultWire final {
    std::uint64_t magic{kRegistrationHostResultMagic};
    std::int32_t status{static_cast<std::int32_t>(E_FAIL)};
    std::uint16_t error_code_size{0U};
    std::uint16_t reserved{0U};
    std::array<char, 128U> error_code{};
};

[[nodiscard]] bool read_exact(HANDLE handle, void* destination,
                              const std::size_t size) noexcept {
    auto* bytes = static_cast<std::uint8_t*>(destination);
    std::size_t offset = 0U;
    while (offset < size) {
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(size - offset);
        if (ReadFile(handle, bytes + offset, remaining, &transferred, nullptr) ==
                FALSE ||
            transferred == 0U) {
            return false;
        }
        offset += transferred;
    }
    return true;
}

[[nodiscard]] bool write_exact(HANDLE handle, const void* source,
                               const std::size_t size) noexcept {
    const auto* bytes = static_cast<const std::uint8_t*>(source);
    std::size_t offset = 0U;
    while (offset < size) {
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(size - offset);
        if (WriteFile(handle, bytes + offset, remaining, &transferred, nullptr) ==
                FALSE ||
            transferred == 0U) {
            return false;
        }
        offset += transferred;
    }
    return true;
}

[[nodiscard]] std::uint32_t operating_system_build() noexcept {
    const auto module = GetModuleHandleW(L"ntdll.dll");
    if (module == nullptr) {
        return 0U;
    }
    using RtlGetVersionFunction = LONG(WINAPI*)(PRTL_OSVERSIONINFOW);
    const auto function = reinterpret_cast<RtlGetVersionFunction>(
        GetProcAddress(module, "RtlGetVersion"));
    if (function == nullptr) {
        return 0U;
    }
    RTL_OSVERSIONINFOW version{};
    version.dwOSVersionInfoSize = sizeof(version);
    return function(&version) >= 0 ? version.dwBuildNumber : 0U;
}

[[nodiscard]] UniqueModule load_registration_module() noexcept {
    return UniqueModule{LoadLibraryExW(L"mfsensorgroup.dll", nullptr,
                                       LOAD_LIBRARY_SEARCH_SYSTEM32)};
}

[[nodiscard]] std::wstring registry_string(HKEY key,
                                           const wchar_t* value_name) {
    DWORD type = 0U;
    DWORD byte_count = 0U;
    if (RegQueryValueExW(key, value_name, nullptr, &type, nullptr, &byte_count) !=
            ERROR_SUCCESS ||
        (type != REG_SZ && type != REG_EXPAND_SZ) ||
        byte_count < sizeof(wchar_t) || byte_count > 32U * 1024U) {
        return {};
    }
    std::vector<wchar_t> raw(byte_count / sizeof(wchar_t) + 1U, L'\0');
    if (RegQueryValueExW(key, value_name, nullptr, &type,
                        reinterpret_cast<BYTE*>(raw.data()), &byte_count) !=
        ERROR_SUCCESS) {
        return {};
    }
    std::wstring result{raw.data()};
    if (type != REG_EXPAND_SZ) {
        return result;
    }
    const auto required = ExpandEnvironmentStringsW(result.c_str(), nullptr, 0U);
    if (required == 0U || required > 32U * 1024U) {
        return {};
    }
    std::wstring expanded(required, L'\0');
    if (ExpandEnvironmentStringsW(result.c_str(), expanded.data(), required) !=
        required) {
        return {};
    }
    expanded.resize(required - 1U);
    return expanded;
}

[[nodiscard]] std::wstring source_class_id() {
    wchar_t value[40]{};
    if (StringFromGUID2(windows_virtual_camera::kSourceClassId, value,
                        static_cast<int>(std::size(value))) == 0) {
        throw std::runtime_error("virtual_camera_source_id_unavailable");
    }
    return value;
}

[[nodiscard]] RegistrationHostConfigurationWire registration_host_configuration(
    const VirtualCameraBrokerActivation& activation,
    const VirtualCameraConfiguration& configuration) {
    if (activation.pipe_name.empty() ||
        activation.pipe_name.size() > kRegistrationHostTextCapacity ||
        configuration.friendly_name.empty() ||
        configuration.friendly_name.size() > kRegistrationHostTextCapacity) {
        throw std::invalid_argument("virtual_camera_host_configuration_invalid");
    }
    RegistrationHostConfigurationWire result{
        .width = configuration.video_format.width,
        .height = configuration.video_format.height,
        .fps_numerator = configuration.video_format.fps_numerator,
        .fps_denominator = configuration.video_format.fps_denominator,
        .pipe_name_size = static_cast<std::uint16_t>(activation.pipe_name.size()),
        .friendly_name_size =
            static_cast<std::uint16_t>(configuration.friendly_name.size()),
        .token = activation.token,
    };
    std::copy(activation.pipe_name.begin(), activation.pipe_name.end(),
              result.pipe_name.begin());
    std::copy(configuration.friendly_name.begin(),
              configuration.friendly_name.end(), result.friendly_name.begin());
    return result;
}

[[nodiscard]] RegistrationResult registration_result(
    const RegistrationHostResultWire& result) {
    if (result.magic != kRegistrationHostResultMagic || result.reserved != 0U ||
        result.error_code_size > result.error_code.size()) {
        return {E_FAIL, "virtual_camera_registration_host_protocol_failed"};
    }
    const auto status = static_cast<HRESULT>(result.status);
    const std::string error_code{result.error_code.data(), result.error_code_size};
    if ((SUCCEEDED(status) && !error_code.empty()) ||
        (FAILED(status) && error_code.empty()) ||
        error_code.find('\0') != std::string::npos) {
        return {E_FAIL, "virtual_camera_registration_host_protocol_failed"};
    }
    return {status, error_code};
}

struct RegistrationHostProcess final {
    UniqueHandle process{};
    UniqueHandle job{};
    UniqueHandle command_write{};
    UniqueHandle status_read{};
};

[[nodiscard]] std::wstring current_executable_path() {
    std::wstring result(32'768U, L'\0');
    const auto length = GetModuleFileNameW(nullptr, result.data(),
                                           static_cast<DWORD>(result.size()));
    if (length == 0U || length >= result.size()) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    result.resize(length);
    return result;
}

[[nodiscard]] RegistrationHostProcess launch_registration_host() {
    SECURITY_ATTRIBUTES inheritable{
        .nLength = sizeof(SECURITY_ATTRIBUTES),
        .lpSecurityDescriptor = nullptr,
        .bInheritHandle = TRUE,
    };
    HANDLE raw_command_read = nullptr;
    HANDLE raw_command_write = nullptr;
    if (CreatePipe(&raw_command_read, &raw_command_write, &inheritable, 0U) ==
        FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    UniqueHandle command_read{raw_command_read};
    UniqueHandle command_write{raw_command_write};
    HANDLE raw_status_read = nullptr;
    HANDLE raw_status_write = nullptr;
    if (CreatePipe(&raw_status_read, &raw_status_write, &inheritable, 0U) == FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    UniqueHandle status_read{raw_status_read};
    UniqueHandle status_write{raw_status_write};
    if (SetHandleInformation(command_write.get(), HANDLE_FLAG_INHERIT, 0U) == FALSE ||
        SetHandleInformation(status_read.get(), HANDLE_FLAG_INHERIT, 0U) == FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }

    SIZE_T attribute_size = 0U;
    static_cast<void>(InitializeProcThreadAttributeList(nullptr, 1U, 0U,
                                                        &attribute_size));
    if (attribute_size == 0U) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    std::vector<std::byte> attribute_storage(attribute_size);
    auto* attribute_list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(
        attribute_storage.data());
    if (InitializeProcThreadAttributeList(attribute_list, 1U, 0U,
                                          &attribute_size) == FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    struct AttributeListGuard final {
        LPPROC_THREAD_ATTRIBUTE_LIST value;
        ~AttributeListGuard() { DeleteProcThreadAttributeList(value); }
    } attribute_guard{attribute_list};
    std::array<HANDLE, 2U> inherited_handles{command_read.get(),
                                             status_write.get()};
    if (UpdateProcThreadAttribute(
            attribute_list, 0U, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            inherited_handles.data(), sizeof(inherited_handles), nullptr,
            nullptr) == FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }

    const auto executable = current_executable_path();
    auto command_line = L"\"" + executable +
                        L"\" --virtual-camera-registration-host " +
                        std::to_wstring(reinterpret_cast<std::uintptr_t>(
                            command_read.get())) +
                        L" " +
                        std::to_wstring(reinterpret_cast<std::uintptr_t>(
                            status_write.get()));
    std::vector<wchar_t> mutable_command_line(command_line.begin(),
                                               command_line.end());
    mutable_command_line.push_back(L'\0');
    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.lpAttributeList = attribute_list;
    PROCESS_INFORMATION process_information{};
    if (CreateProcessW(
            executable.c_str(), mutable_command_line.data(), nullptr, nullptr,
            TRUE,
            CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
            nullptr, nullptr, &startup.StartupInfo, &process_information) == FALSE) {
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    UniqueHandle process{process_information.hProcess};
    UniqueHandle thread{process_information.hThread};
    UniqueHandle job{CreateJobObjectW(nullptr, nullptr)};
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job ||
        SetInformationJobObject(job.get(), JobObjectExtendedLimitInformation,
                                &limits, sizeof(limits)) == FALSE ||
        AssignProcessToJobObject(job.get(), process.get()) == FALSE ||
        ResumeThread(thread.get()) == static_cast<DWORD>(-1)) {
        static_cast<void>(TerminateProcess(process.get(), 70U));
        throw std::runtime_error("virtual_camera_registration_host_unavailable");
    }
    command_read.reset();
    status_write.reset();
    return {
        .process = std::move(process),
        .job = UniqueHandle{job.release()},
        .command_write = std::move(command_write),
        .status_read = std::move(status_read),
    };
}

[[nodiscard]] bool source_component_installed() {
    const auto registration_key =
        std::wstring{L"SOFTWARE\\Classes\\CLSID\\"} + source_class_id() +
        L"\\InprocServer32";
    HKEY raw_key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, registration_key.c_str(), 0U,
                      KEY_QUERY_VALUE | KEY_WOW64_64KEY, &raw_key) !=
        ERROR_SUCCESS) {
        return false;
    }
    const UniqueRegistryKey key{raw_key};
    auto path = registry_string(key.get(), nullptr);
    const auto threading_model = registry_string(key.get(), L"ThreadingModel");
    if (path.size() >= 2U && path.front() == L'"' && path.back() == L'"') {
        path = path.substr(1U, path.size() - 2U);
    }
    if (threading_model != kExpectedThreadingModel || path.empty() ||
        !std::filesystem::path{path}.is_absolute()) {
        return false;
    }
    const auto attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES &&
           (attributes & FILE_ATTRIBUTE_DIRECTORY) == 0U;
}

[[nodiscard]] std::wstring utf8_to_wide(const std::string_view value) {
    if (value.empty() || value.size() > static_cast<std::size_t>(INT_MAX)) {
        throw std::invalid_argument("virtual_camera_text_invalid");
    }
    const auto required = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        nullptr, 0);
    if (required <= 0) {
        throw std::invalid_argument("virtual_camera_text_invalid");
    }
    std::wstring result(static_cast<std::size_t>(required), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                            static_cast<int>(value.size()), result.data(),
                            required) != required) {
        throw std::invalid_argument("virtual_camera_text_invalid");
    }
    return result;
}

using CreateVirtualCameraFunction = HRESULT(STDAPICALLTYPE*)(
    MFVirtualCameraType, MFVirtualCameraLifetime, MFVirtualCameraAccess, LPCWSTR,
    LPCWSTR, const GUID*, ULONG, IMFVirtualCamera**);

class WindowsMediaFoundationVirtualCameraSink final : public VirtualCameraSink {
  public:
    WindowsMediaFoundationVirtualCameraSink(VirtualCameraConfiguration configuration,
                                             const std::uint64_t generation)
        : configuration_(std::move(configuration)),
          frame_sink_(make_shared_frame_virtual_camera_sink(configuration_,
                                                             generation)) {
        if (configuration_.video_format.pixel_format != "nv12" ||
            configuration_.video_format.color_space != "bt709" ||
            configuration_.video_format.color_range != "limited") {
            throw std::invalid_argument("virtual_camera_format_unsupported");
        }
    }

    ~WindowsMediaFoundationVirtualCameraSink() override { stop(); }

    void start() override {
        std::scoped_lock lock{mutex_};
        if (health_.state == VirtualCameraSinkState::ready ||
            health_.state == VirtualCameraSinkState::starting) {
            return;
        }
        if (worker_.joinable()) {
            throw std::logic_error("virtual_camera_lifecycle_invalid");
        }
        health_.state = VirtualCameraSinkState::starting;
        health_.error_code.clear();
        try {
            frame_sink_->start();
            broker_ = make_virtual_camera_frame_broker(frame_sink_);
            broker_->start();
            const auto activation = broker_->activation();
            const auto host_configuration =
                registration_host_configuration(activation, configuration_);
            stop_requested_.store(false);
            worker_ = std::thread(
                [this, host_configuration]() noexcept {
                    registration_worker(host_configuration);
                });
        } catch (...) {
            health_.state = VirtualCameraSinkState::failed;
            if (health_.error_code.empty()) {
                health_.error_code = "virtual_camera_start_failed";
            }
            stop_components_locked();
            throw;
        }
    }

    [[nodiscard]] bool publish(const VideoFrameView& frame) noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (health_.state != VirtualCameraSinkState::ready) {
                ++health_.dropped_frames;
                return false;
            }
            const auto published = frame_sink_->publish(frame);
            const auto transport_health = frame_sink_->health();
            health_.published_frames = transport_health.published_frames;
            health_.dropped_frames = transport_health.dropped_frames;
            health_.last_frame_sequence = transport_health.last_frame_sequence;
            if (!published) {
                health_.state = transport_health.state;
                health_.error_code = transport_health.error_code;
            }
            return published;
        } catch (...) {
            return false;
        }
    }

    void heartbeat() noexcept override {
        try {
            std::scoped_lock lock{mutex_};
            if (health_.state == VirtualCameraSinkState::ready) {
                frame_sink_->heartbeat();
            }
        } catch (...) {
        }
    }

    void stop() noexcept override {
        try {
            stop_requested_.store(true);
            lifecycle_wakeup_.notify_all();
            if (worker_.joinable()) {
                worker_.join();
            }
            std::scoped_lock lock{mutex_};
            stop_components_locked();
            health_.state = VirtualCameraSinkState::stopped;
            health_.error_code.clear();
        } catch (...) {
        }
    }

    [[nodiscard]] VirtualCameraSinkHealth health() const override {
        std::scoped_lock lock{mutex_};
        auto result = health_;
        if (result.state == VirtualCameraSinkState::ready && broker_ != nullptr) {
            const auto broker_health = broker_->health();
            if (!broker_health.running) {
                result.state = VirtualCameraSinkState::degraded;
                result.error_code = broker_health.error_code.empty()
                                        ? "virtual_camera_broker_unavailable"
                                        : broker_health.error_code;
            }
        }
        return result;
    }

  private:
    void registration_worker(
        const RegistrationHostConfigurationWire& configuration) noexcept {
        RegistrationResult result{E_FAIL,
                                  "virtual_camera_registration_host_unavailable"};
        RegistrationHostProcess host;
        try {
            host = launch_registration_host();
            if (!write_exact(host.command_write.get(), &configuration,
                             sizeof(configuration))) {
                throw std::runtime_error(
                    "virtual_camera_registration_host_protocol_failed");
            }
            const auto deadline =
                std::chrono::steady_clock::now() + kRegistrationHostStartTimeout;
            while (!stop_requested_.load() &&
                   std::chrono::steady_clock::now() < deadline) {
                DWORD available = 0U;
                if (PeekNamedPipe(host.status_read.get(), nullptr, 0U, nullptr,
                                  &available, nullptr) == FALSE) {
                    break;
                }
                if (available >= sizeof(RegistrationHostResultWire)) {
                    RegistrationHostResultWire wire{};
                    if (read_exact(host.status_read.get(), &wire, sizeof(wire))) {
                        result = registration_result(wire);
                    } else {
                        result = {
                            E_FAIL,
                            "virtual_camera_registration_host_protocol_failed"};
                    }
                    break;
                }
                if (WaitForSingleObject(host.process.get(), 0U) == WAIT_OBJECT_0) {
                    result = {E_FAIL,
                              "virtual_camera_registration_host_terminated"};
                    break;
                }
                std::unique_lock lifecycle_lock{lifecycle_mutex_};
                lifecycle_wakeup_.wait_for(lifecycle_lock,
                                           std::chrono::milliseconds{10});
            }
            if (stop_requested_.load()) {
                result = {HRESULT_FROM_WIN32(ERROR_CANCELLED),
                          "virtual_camera_start_cancelled"};
            } else if (std::chrono::steady_clock::now() >= deadline &&
                       FAILED(result.status)) {
                result = {HRESULT_FROM_WIN32(ERROR_TIMEOUT),
                          "virtual_camera_registration_timeout"};
            }
        } catch (...) {
            result = {E_FAIL, "virtual_camera_registration_host_unavailable"};
        }

        {
            std::scoped_lock lock{mutex_};
            if (!stop_requested_.load()) {
                health_.state = SUCCEEDED(result.status)
                                    ? VirtualCameraSinkState::ready
                                    : VirtualCameraSinkState::failed;
                health_.error_code = result.error_code;
            }
        }

        if (SUCCEEDED(result.status)) {
            while (!stop_requested_.load()) {
                if (WaitForSingleObject(host.process.get(), 0U) == WAIT_OBJECT_0) {
                    std::scoped_lock lock{mutex_};
                    health_.state = VirtualCameraSinkState::failed;
                    health_.error_code =
                        "virtual_camera_registration_host_terminated";
                    break;
                }
                std::unique_lock lifecycle_lock{lifecycle_mutex_};
                lifecycle_wakeup_.wait_for(lifecycle_lock,
                                           std::chrono::milliseconds{50});
            }
        }

        if (host.process) {
            if (SUCCEEDED(result.status) && stop_requested_.load()) {
                constexpr std::uint8_t stop_command = 1U;
                static_cast<void>(write_exact(host.command_write.get(),
                                              &stop_command,
                                              sizeof(stop_command)));
                host.command_write.reset();
            }
            if (WaitForSingleObject(
                    host.process.get(),
                    static_cast<DWORD>(kRegistrationHostStopTimeout.count() *
                                       1'000)) != WAIT_OBJECT_0) {
                static_cast<void>(TerminateProcess(host.process.get(), 70U));
                static_cast<void>(WaitForSingleObject(host.process.get(), 2'000U));
            }
        }
    }

    void stop_components_locked() noexcept {
        if (broker_ != nullptr) {
            broker_->stop();
            broker_.reset();
        }
        frame_sink_->stop();
    }

    VirtualCameraConfiguration configuration_{};
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink_{};
    std::unique_ptr<VirtualCameraFrameBroker> broker_{};
    mutable std::mutex mutex_{};
    VirtualCameraSinkHealth health_{};
    std::mutex lifecycle_mutex_{};
    std::condition_variable lifecycle_wakeup_{};
    std::atomic_bool stop_requested_{false};
    std::thread worker_{};
};

class WindowsMediaFoundationVirtualCameraBackend final
    : public VirtualCameraBackend {
  public:
    [[nodiscard]] VirtualCameraBackendProbe probe() const override {
        return probe_windows_media_foundation_virtual_camera();
    }

    [[nodiscard]] std::unique_ptr<VirtualCameraSink> create_sink(
        const VirtualCameraConfiguration& configuration) override {
        const auto current_probe = probe();
        if (!current_probe.operational) {
            throw std::runtime_error(current_probe.error_code);
        }
        static std::atomic_uint64_t next_generation{1U};
        const auto generation = next_generation.fetch_add(1U);
        if (generation == 0U) {
            throw std::overflow_error("virtual_camera_generation_exhausted");
        }
        return std::make_unique<WindowsMediaFoundationVirtualCameraSink>(
            configuration, generation);
    }
};

} // namespace

bool windows_virtual_camera_is_registered(
    IMFAttributes* attributes) noexcept {
    if (attributes == nullptr) {
        return false;
    }
    UINT32 symbolic_link_length = 0U;
    return SUCCEEDED(attributes->GetStringLength(
               MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK,
               &symbolic_link_length)) &&
           symbolic_link_length != 0U;
}

int run_windows_virtual_camera_registration_host(
    const std::uintptr_t command_read_handle,
    const std::uintptr_t status_write_handle) noexcept {
    const UniqueHandle command_read{
        reinterpret_cast<HANDLE>(command_read_handle)};
    const UniqueHandle status_write{
        reinterpret_cast<HANDLE>(status_write_handle)};
    if (!command_read || !status_write ||
        GetFileType(command_read.get()) != FILE_TYPE_PIPE ||
        GetFileType(status_write.get()) != FILE_TYPE_PIPE) {
        return 64;
    }

    auto send_result = [&](const HRESULT status,
                           const std::string_view error_code) noexcept {
        RegistrationHostResultWire wire{
            .status = static_cast<std::int32_t>(status),
            .error_code_size = static_cast<std::uint16_t>(
                (std::min)(error_code.size(),
                           RegistrationHostResultWire{}.error_code.size())),
        };
        std::copy_n(error_code.begin(), wire.error_code_size,
                    wire.error_code.begin());
        return write_exact(status_write.get(), &wire, sizeof(wire));
    };

    RegistrationHostConfigurationWire configuration{};
    if (!read_exact(command_read.get(), &configuration, sizeof(configuration)) ||
        configuration.magic != kRegistrationHostConfigurationMagic ||
        configuration.version != kRegistrationHostProtocolVersion ||
        configuration.reserved != 0U || configuration.width == 0U ||
        configuration.height == 0U || configuration.fps_numerator == 0U ||
        configuration.fps_denominator == 0U ||
        configuration.pipe_name_size == 0U ||
        configuration.pipe_name_size > configuration.pipe_name.size() ||
        configuration.friendly_name_size == 0U ||
        configuration.friendly_name_size > configuration.friendly_name.size() ||
        !std::all_of(
            configuration.pipe_name.begin() + configuration.pipe_name_size,
            configuration.pipe_name.end(),
            [](const char value) { return value == '\0'; }) ||
        !std::all_of(
            configuration.friendly_name.begin() +
                configuration.friendly_name_size,
            configuration.friendly_name.end(),
            [](const char value) { return value == '\0'; })) {
        static_cast<void>(send_result(
            E_INVALIDARG, "virtual_camera_registration_host_protocol_failed"));
        return 65;
    }

    const std::string pipe_name{configuration.pipe_name.data(),
                                configuration.pipe_name_size};
    const std::string friendly_name{configuration.friendly_name.data(),
                                    configuration.friendly_name_size};
    if (pipe_name.find('\0') != std::string::npos ||
        friendly_name.find('\0') != std::string::npos ||
        !std::string_view{pipe_name}.starts_with(
            "\\\\.\\pipe\\Solin.VirtualCamera.")) {
        static_cast<void>(send_result(
            E_INVALIDARG, "virtual_camera_registration_host_protocol_failed"));
        return 65;
    }

    bool com_initialized = false;
    bool media_foundation_initialized = false;
    bool registration_changed = false;
    bool camera_started_by_host = false;
    Microsoft::WRL::ComPtr<IMFVirtualCamera> camera;
    HRESULT status = E_FAIL;
    std::string error_code{"virtual_camera_registration_failed"};
    try {
        status = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (SUCCEEDED(status)) {
            com_initialized = true;
            status = MFStartup(MF_VERSION, MFSTARTUP_FULL);
        }
        if (SUCCEEDED(status)) {
            media_foundation_initialized = true;
            auto module = load_registration_module();
            const auto create =
                module.get() == nullptr
                    ? nullptr
                    : reinterpret_cast<CreateVirtualCameraFunction>(
                          GetProcAddress(module.get(), "MFCreateVirtualCamera"));
            if (create == nullptr) {
                status = HRESULT_FROM_WIN32(ERROR_PROC_NOT_FOUND);
                error_code = "virtual_camera_registration_api_unavailable";
            } else {
                const auto class_id = source_class_id();
                const auto wide_friendly_name = utf8_to_wide(friendly_name);
                status = create(MFVirtualCameraType_SoftwareCameraSource,
                                MFVirtualCameraLifetime_System,
                                MFVirtualCameraAccess_CurrentUser,
                                wide_friendly_name.c_str(), class_id.c_str(),
                                nullptr, 0U, &camera);
                error_code = "virtual_camera_registration_failed";
            }
        } else if (!com_initialized) {
            error_code = "virtual_camera_com_initialization_failed";
        } else {
            error_code = "virtual_camera_media_foundation_failed";
        }
        const auto camera_registered =
            SUCCEEDED(status) &&
            windows_virtual_camera_is_registered(camera.Get());
        const auto wide_pipe_name = utf8_to_wide(pipe_name);
        if (SUCCEEDED(status) && !camera_registered) {
            // Runtime startup only creates a missing camera. Updating an
            // existing system-lifetime registration would disrupt consumers;
            // contract migrations belong to the explicit installer flow.
            registration_changed = true;
            status = camera->SetString(
                windows_virtual_camera::kBrokerPipeAttribute,
                wide_pipe_name.c_str());
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->SetBlob(
                windows_virtual_camera::kBrokerTokenAttribute,
                configuration.token.data(),
                static_cast<UINT32>(configuration.token.size()));
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->SetUINT32(
                windows_virtual_camera::kFrameWidthAttribute,
                configuration.width);
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->SetUINT32(
                windows_virtual_camera::kFrameHeightAttribute,
                configuration.height);
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->SetUINT32(
                windows_virtual_camera::kFrameRateNumeratorAttribute,
                configuration.fps_numerator);
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->SetUINT32(
                windows_virtual_camera::kFrameRateDenominatorAttribute,
                configuration.fps_denominator);
        }
        if (SUCCEEDED(status) && !camera_registered) {
            status = camera->Start(nullptr);
            camera_started_by_host = SUCCEEDED(status);
            error_code = camera_started_by_host
                             ? std::string{}
                             : "virtual_camera_activation_failed";
        } else if (SUCCEEDED(status)) {
            // MFVirtualCameraLifetime_System keeps the device registered after
            // Solin exits. Reconfiguring that same instance while Frame Server
            // is streaming tears down active consumers such as Zoom.
            error_code.clear();
        }
    } catch (...) {
        status = E_FAIL;
        error_code = "virtual_camera_registration_failed";
    }

    const auto delivered = send_result(status, error_code);
    if (SUCCEEDED(status) && delivered) {
        std::uint8_t command = 0U;
        static_cast<void>(read_exact(command_read.get(), &command,
                                     sizeof(command)));
    }
    if (camera != nullptr) {
        // A system-lifetime camera is intentionally left registered when the
        // frame producer stops. This keeps the device stable across output
        // toggles, Solin restarts, and Windows reboots. A failed registration
        // is rolled back so a partially configured device is never retained.
        if ((FAILED(status) || !delivered) && registration_changed) {
            if (camera_started_by_host) {
                static_cast<void>(camera->Stop());
            }
            static_cast<void>(camera->Remove());
        }
        static_cast<void>(camera->Shutdown());
        camera.Reset();
    }
    if (media_foundation_initialized) {
        static_cast<void>(MFShutdown());
    }
    if (com_initialized) {
        CoUninitialize();
    }
    return SUCCEEDED(status) && delivered ? 0 : 1;
}

VirtualCameraBackendProbe
probe_windows_media_foundation_virtual_camera() noexcept {
    try {
        VirtualCameraBackendProbe result{
            .backend = VirtualCameraBackendKind::windows_media_foundation,
            .platform_supported = true,
            .operating_system_build = operating_system_build(),
        };
        if (result.operating_system_build < kMinimumVirtualCameraBuild) {
            result.error_code = "virtual_camera_windows_build_unsupported";
            return result;
        }
        const auto module = load_registration_module();
        result.registration_api_available =
            module.get() != nullptr &&
            GetProcAddress(module.get(), "MFCreateVirtualCamera") != nullptr;
        if (!result.registration_api_available) {
            result.error_code = "virtual_camera_registration_api_unavailable";
            return result;
        }
        result.source_component_installed = source_component_installed();
        if (!result.source_component_installed) {
            result.error_code = "virtual_camera_source_not_installed";
            return result;
        }
        result.cross_session_transport_available = true;
        result.operational = true;
        result.error_code.clear();
        return result;
    } catch (...) {
        return {
            .backend = VirtualCameraBackendKind::windows_media_foundation,
            .platform_supported = true,
            .error_code = "virtual_camera_probe_failed",
        };
    }
}

std::unique_ptr<VirtualCameraBackend>
make_windows_media_foundation_virtual_camera_backend() {
    return std::make_unique<WindowsMediaFoundationVirtualCameraBackend>();
}

} // namespace solin::media_engine

#endif
