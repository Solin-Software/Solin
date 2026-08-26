#include "solin/media_engine/virtual_camera_broker.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <sddl.h>
#endif

namespace solin::media_engine {
namespace {

#ifdef _WIN32

using namespace std::chrono_literals;

constexpr auto kHandshakeTimeout = 2s;
constexpr std::size_t kBrokerInstanceCount = 4U;

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

[[nodiscard]] LocalMemory pipe_security_descriptor() {
    const auto descriptor_text =
        L"D:P(A;;GA;;;SY)(A;;GRGW;;;" +
        windows_virtual_camera::current_user_sid_string() + L")";
    PSECURITY_DESCRIPTOR raw_descriptor = nullptr;
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor_text.c_str(), SDDL_REVISION_1,
            &raw_descriptor, nullptr) == FALSE) {
        throw std::runtime_error("virtual_camera_broker_security_failed");
    }
    return LocalMemory{raw_descriptor};
}

[[nodiscard]] bool client_matches_current_identity(HANDLE pipe) {
    ULONG client_process_id = 0U;
    DWORD client_session_id = 0U;
    if (GetNamedPipeClientProcessId(pipe, &client_process_id) == FALSE ||
        ProcessIdToSessionId(client_process_id, &client_session_id) == FALSE ||
        client_session_id !=
            windows_virtual_camera::current_process_session_id()) {
        return false;
    }
    PSID raw_current_sid = nullptr;
    const auto current_sid = windows_virtual_camera::current_user_sid_string();
    if (ConvertStringSidToSidW(current_sid.c_str(), &raw_current_sid) == FALSE) {
        return false;
    }
    LocalMemory expected_sid{raw_current_sid};
    if (ImpersonateNamedPipeClient(pipe) == FALSE) {
        return false;
    }
    struct RevertGuard final {
        ~RevertGuard() { static_cast<void>(RevertToSelf()); }
    } revert_guard;

    HANDLE raw_token = nullptr;
    if (OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &raw_token) ==
        FALSE) {
        return false;
    }
    UniqueHandle token{raw_token};
    DWORD required = 0U;
    static_cast<void>(GetTokenInformation(token.get(), TokenUser, nullptr, 0U,
                                          &required));
    if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || required == 0U) {
        return false;
    }
    std::vector<std::byte> storage(required);
    if (GetTokenInformation(token.get(), TokenUser, storage.data(), required,
                            &required) == FALSE) {
        return false;
    }
    DWORD token_session_id = 0U;
    DWORD session_size = 0U;
    if (GetTokenInformation(token.get(), TokenSessionId, &token_session_id,
                            sizeof(token_session_id), &session_size) == FALSE ||
        token_session_id != client_session_id) {
        return false;
    }
    const auto* client_user =
        reinterpret_cast<const TOKEN_USER*>(storage.data());
    return EqualSid(client_user->User.Sid, expected_sid.get()) != FALSE;
}

enum class PipeIoResult : std::uint8_t {
    completed,
    stopped,
    failed,
};

[[nodiscard]] bool cancel_and_drain(HANDLE pipe, OVERLAPPED& overlapped,
                                    DWORD& transferred) noexcept {
    static_cast<void>(CancelIoEx(pipe, &overlapped));
    return GetOverlappedResult(pipe, &overlapped, &transferred, TRUE) != FALSE;
}

[[nodiscard]] PipeIoResult pipe_io(HANDLE pipe, HANDLE stop_event,
                                   const std::span<std::uint8_t> bytes,
                                   const bool write) {
    const auto deadline = std::chrono::steady_clock::now() + kHandshakeTimeout;
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        const UniqueHandle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!event) {
            return PipeIoResult::failed;
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event.get();
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(bytes.size() - offset);
        const auto started = write
                                 ? WriteFile(pipe, bytes.data() + offset, remaining,
                                             &transferred, &overlapped)
                                 : ReadFile(pipe, bytes.data() + offset, remaining,
                                            &transferred, &overlapped);
        if (started == FALSE) {
            const auto error = GetLastError();
            if (error != ERROR_IO_PENDING) {
                return PipeIoResult::failed;
            }
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) {
                return cancel_and_drain(pipe, overlapped, transferred) &&
                               transferred == remaining
                           ? PipeIoResult::completed
                           : PipeIoResult::failed;
            }
            const auto wait_ms = static_cast<DWORD>(
                std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now)
                    .count());
            const std::array<HANDLE, 2U> events{stop_event, event.get()};
            const auto wait_result =
                WaitForMultipleObjects(static_cast<DWORD>(events.size()),
                                       events.data(), FALSE, wait_ms);
            if (wait_result == WAIT_OBJECT_0) {
                if (cancel_and_drain(pipe, overlapped, transferred) &&
                    transferred == remaining) {
                    offset += transferred;
                    return offset == bytes.size() ? PipeIoResult::completed
                                                  : PipeIoResult::stopped;
                }
                return PipeIoResult::stopped;
            }
            if (wait_result != WAIT_OBJECT_0 + 1U) {
                static_cast<void>(cancel_and_drain(pipe, overlapped, transferred));
                return PipeIoResult::failed;
            }
            if (GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) == FALSE) {
                return PipeIoResult::failed;
            }
        }
        if (transferred == 0U) {
            return PipeIoResult::failed;
        }
        offset += transferred;
    }
    return PipeIoResult::completed;
}

enum class ClientLifetime : std::uint8_t {
    legacy,
    persistent,
    stopped,
};

[[nodiscard]] ClientLifetime wait_for_client_disconnect(
    HANDLE pipe, HANDLE stop_event) noexcept {
    bool presence_marker_received = false;
    for (;;) {
        const UniqueHandle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!event) {
            return presence_marker_received ? ClientLifetime::persistent
                                            : ClientLifetime::legacy;
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event.get();
        std::uint8_t sentinel = 0U;
        DWORD transferred = 0U;
        if (ReadFile(pipe, &sentinel, sizeof(sentinel), &transferred,
                     &overlapped) != FALSE) {
            if (transferred == 0U) {
                return presence_marker_received ? ClientLifetime::persistent
                                                : ClientLifetime::legacy;
            }
            if (sentinel != kVirtualCameraBrokerPresenceMarker) {
                return ClientLifetime::legacy;
            }
            presence_marker_received = true;
            continue;
        }
        const auto error = GetLastError();
        if (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED ||
            error == ERROR_NO_DATA) {
            return presence_marker_received ? ClientLifetime::persistent
                                            : ClientLifetime::legacy;
        }
        if (error != ERROR_IO_PENDING) {
            return presence_marker_received ? ClientLifetime::persistent
                                            : ClientLifetime::legacy;
        }
        const std::array<HANDLE, 2U> events{stop_event, event.get()};
        const auto wait_result = WaitForMultipleObjects(
            static_cast<DWORD>(events.size()), events.data(), FALSE, INFINITE);
        if (wait_result == WAIT_OBJECT_0) {
            static_cast<void>(cancel_and_drain(pipe, overlapped, transferred));
            return ClientLifetime::stopped;
        }
        if (wait_result != WAIT_OBJECT_0 + 1U ||
            GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) ==
                FALSE ||
            transferred == 0U) {
            return presence_marker_received ? ClientLifetime::persistent
                                            : ClientLifetime::legacy;
        }
        if (sentinel != kVirtualCameraBrokerPresenceMarker) {
            return ClientLifetime::legacy;
        }
        presence_marker_received = true;
    }
}

class WindowsVirtualCameraFrameBroker final : public VirtualCameraFrameBroker {
  public:
    explicit WindowsVirtualCameraFrameBroker(
        std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink,
        VirtualCameraBrokerEndpoint endpoint)
        : frame_sink_(std::move(frame_sink)), endpoint_(std::move(endpoint)) {
        if (frame_sink_ == nullptr) {
            throw std::invalid_argument("virtual_camera_broker_sink_required");
        }
        constexpr std::string_view pipe_prefix{R"(\\.\pipe\)"};
        if (!endpoint_.pipe_name.starts_with(pipe_prefix) ||
            endpoint_.pipe_name.size() <= pipe_prefix.size() ||
            endpoint_.pipe_name.size() > 256U ||
            endpoint_.protocol_version !=
                kVirtualCameraBrokerProtocolVersion) {
            throw std::invalid_argument("virtual_camera_broker_endpoint_invalid");
        }
    }

    ~WindowsVirtualCameraFrameBroker() override { stop(); }

    void start() override {
        std::scoped_lock lock{lifecycle_mutex_};
        if (!workers_.empty()) {
            bool running = false;
            {
                std::scoped_lock health_lock{health_mutex_};
                running = health_.running;
            }
            if (running) {
                return;
            }
            for (auto& worker : workers_) {
                worker.join();
            }
            workers_.clear();
            stop_event_.reset();
        }
        if (!frame_sink_->endpoint()) {
            throw std::runtime_error("virtual_camera_broker_transport_unavailable");
        }
        UniqueHandle stop_event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!stop_event) {
            throw std::runtime_error("virtual_camera_broker_start_failed");
        }
        auto pipe = create_pipe(true);
        stop_event_ = std::move(stop_event);
        workers_.reserve(kBrokerInstanceCount);
        active_workers_.store(kBrokerInstanceCount, std::memory_order_release);
        try {
            workers_.emplace_back(
                [this, first_pipe = std::move(pipe)]() mutable {
                    serve(first_pipe.release());
                });
            for (std::size_t index = 1U; index < kBrokerInstanceCount; ++index) {
                workers_.emplace_back([this]() { serve(nullptr); });
            }
        } catch (...) {
            static_cast<void>(SetEvent(stop_event_.get()));
            for (auto& worker : workers_) {
                worker.join();
            }
            workers_.clear();
            active_workers_.store(0U, std::memory_order_release);
            stop_event_.reset();
            throw std::runtime_error("virtual_camera_broker_start_failed");
        }
        std::scoped_lock health_lock{health_mutex_};
        health_.running = true;
        health_.error_code.clear();
    }

    void stop() noexcept override {
        try {
            std::scoped_lock lock{lifecycle_mutex_};
            if (workers_.empty()) {
                return;
            }
            static_cast<void>(SetEvent(stop_event_.get()));
            for (auto& worker : workers_) {
                worker.join();
            }
            workers_.clear();
            stop_event_.reset();
            std::scoped_lock health_lock{health_mutex_};
            health_.running = false;
            health_.active_connections = 0U;
            health_.legacy_consumer_present = false;
        } catch (...) {
        }
    }

    [[nodiscard]] VirtualCameraBrokerEndpoint endpoint() const override {
        return endpoint_;
    }

    [[nodiscard]] VirtualCameraBrokerHealth health() const override {
        std::scoped_lock lock{health_mutex_};
        return health_;
    }

  private:
    [[nodiscard]] UniqueHandle create_pipe(const bool first_instance) const {
        auto descriptor = pipe_security_descriptor();
        SECURITY_ATTRIBUTES attributes{
            .nLength = sizeof(SECURITY_ATTRIBUTES),
            .lpSecurityDescriptor = descriptor.get(),
            .bInheritHandle = FALSE,
        };
        UniqueHandle result{CreateNamedPipeA(
            endpoint_.pipe_name.c_str(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
                (first_instance ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0U),
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            static_cast<DWORD>(kBrokerInstanceCount),
            static_cast<DWORD>(kVirtualCameraBrokerResponseSize),
            static_cast<DWORD>(kVirtualCameraBrokerRequestSize), 0U, &attributes)};
        if (!result) {
            throw std::runtime_error("virtual_camera_broker_pipe_unavailable");
        }
        return result;
    }

    [[nodiscard]] bool connect(HANDLE pipe) const noexcept {
        const UniqueHandle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!event) {
            return false;
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event.get();
        if (ConnectNamedPipe(pipe, &overlapped) != FALSE) {
            return true;
        }
        const auto error = GetLastError();
        if (error == ERROR_PIPE_CONNECTED) {
            return true;
        }
        if (error != ERROR_IO_PENDING) {
            return false;
        }
        const std::array<HANDLE, 2U> events{stop_event_.get(), event.get()};
        const auto result = WaitForMultipleObjects(
            static_cast<DWORD>(events.size()), events.data(), FALSE, INFINITE);
        if (result == WAIT_OBJECT_0) {
            DWORD transferred = 0U;
            static_cast<void>(cancel_and_drain(pipe, overlapped, transferred));
            return false;
        }
        DWORD transferred = 0U;
        if (result != WAIT_OBJECT_0 + 1U) {
            static_cast<void>(cancel_and_drain(pipe, overlapped, transferred));
            return false;
        }
        return GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) != FALSE;
    }

    void serve(HANDLE first_pipe) noexcept {
        UniqueHandle pipe{first_pipe};
        while (WaitForSingleObject(stop_event_.get(), 0U) == WAIT_TIMEOUT) {
            if (!pipe) {
                try {
                    pipe = create_pipe(false);
                } catch (...) {
                    record_failure("virtual_camera_broker_pipe_unavailable");
                    break;
                }
            }
            if (!connect(pipe.get())) {
                if (WaitForSingleObject(stop_event_.get(), 0U) != WAIT_OBJECT_0) {
                    record_failure("virtual_camera_broker_connect_failed");
                }
                break;
            }
            handle_client(pipe.get());
            static_cast<void>(DisconnectNamedPipe(pipe.get()));
            pipe.reset();
        }
        if (active_workers_.fetch_sub(1U, std::memory_order_acq_rel) == 1U) {
            std::scoped_lock lock{health_mutex_};
            health_.running = false;
        }
    }

    void handle_client(HANDLE pipe) noexcept {
        std::array<std::uint8_t, kVirtualCameraBrokerRequestSize> request_bytes{};
        const auto read_result =
            pipe_io(pipe, stop_event_.get(), request_bytes, false);
        if (read_result != PipeIoResult::completed) {
            if (read_result == PipeIoResult::failed) {
                record_connection_failure();
            }
            return;
        }

        VirtualCameraBrokerResponse response{};
        try {
            const auto request = decode_virtual_camera_broker_request(request_bytes);
            response.nonce = request.nonce;
            if (!client_matches_current_identity(pipe)) {
                response.status = VirtualCameraBrokerStatus::unauthorized;
                record_denial();
            } else {
                const auto endpoint = frame_sink_->endpoint();
                if (!endpoint) {
                    response.status = VirtualCameraBrokerStatus::transport_unavailable;
                    record_connection_failure();
                } else {
                    response.status = VirtualCameraBrokerStatus::ok;
                    response.mapping_file_path_utf8 =
                        endpoint.cross_process_file_path_utf8;
                    response.mapping_size = endpoint.mapping_size;
                    response.generation = endpoint.generation;
                    response.layout = endpoint.layout;
                    response.fps_numerator = endpoint.fps_numerator;
                    response.fps_denominator = endpoint.fps_denominator;
                }
            }
        } catch (const std::invalid_argument&) {
            response.status = VirtualCameraBrokerStatus::invalid_request;
            record_connection_failure();
        } catch (...) {
            response.status = VirtualCameraBrokerStatus::transport_unavailable;
            record_connection_failure();
        }

        bool delivered = false;
        try {
            auto response_bytes = encode_virtual_camera_broker_response(response);
            delivered = pipe_io(pipe, stop_event_.get(), response_bytes, true) ==
                        PipeIoResult::completed;
        } catch (...) {
        }
        if (response.status == VirtualCameraBrokerStatus::ok && delivered) {
            {
                std::scoped_lock lock{health_mutex_};
                ++health_.accepted_connections;
                ++health_.active_connections;
            }
            const auto lifetime =
                wait_for_client_disconnect(pipe, stop_event_.get());
            std::scoped_lock lock{health_mutex_};
            if (health_.active_connections != 0U) {
                --health_.active_connections;
            }
            if (lifetime == ClientLifetime::legacy) {
                // Protocol v3 filters shipped before persistent presence
                // signalling close the broker pipe after receiving the shared
                // channel. Keep correctness during an in-place upgrade; the
                // optimization resumes after the engine restarts with the new
                // filter. New filters retain their pipe and remain exact.
                health_.legacy_consumer_present = true;
            }
        }
    }

    void record_denial() noexcept {
        try {
            std::scoped_lock lock{health_mutex_};
            ++health_.denied_connections;
        } catch (...) {
        }
    }

    void record_connection_failure() noexcept {
        try {
            std::scoped_lock lock{health_mutex_};
            ++health_.failed_connections;
        } catch (...) {
        }
    }

    void record_failure(const std::string& error_code) noexcept {
        try {
            std::scoped_lock lock{health_mutex_};
            health_.error_code = error_code;
            ++health_.failed_connections;
        } catch (...) {
        }
    }

    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink_{};
    VirtualCameraBrokerEndpoint endpoint_{};
    mutable std::mutex lifecycle_mutex_{};
    UniqueHandle stop_event_{};
    std::vector<std::thread> workers_{};
    std::atomic<std::size_t> active_workers_{0U};
    mutable std::mutex health_mutex_{};
    VirtualCameraBrokerHealth health_{};
};

#endif

} // namespace

std::unique_ptr<VirtualCameraFrameBroker> make_virtual_camera_frame_broker(
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink) {
    return make_virtual_camera_frame_broker(
        std::move(frame_sink),
        {
#ifdef _WIN32
            .pipe_name =
                windows_virtual_camera::current_user_broker_pipe_name_utf8(),
#endif
        });
}

std::unique_ptr<VirtualCameraFrameBroker> make_virtual_camera_frame_broker(
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink,
    VirtualCameraBrokerEndpoint endpoint) {
    if (frame_sink == nullptr) {
        throw std::invalid_argument("virtual_camera_broker_sink_required");
    }
#ifdef _WIN32
    return std::make_unique<WindowsVirtualCameraFrameBroker>(
        std::move(frame_sink), std::move(endpoint));
#else
    static_cast<void>(frame_sink);
    static_cast<void>(endpoint);
    throw std::runtime_error("virtual_camera_broker_platform_unsupported");
#endif
}

} // namespace solin::media_engine
