#include "solin/media_engine/virtual_camera_broker.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <array>
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
    PSECURITY_DESCRIPTOR raw_descriptor = nullptr;
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:P(A;;GA;;;SY)(A;;GRGW;;;LS)", SDDL_REVISION_1,
            &raw_descriptor, nullptr) == FALSE) {
        throw std::runtime_error("virtual_camera_broker_security_failed");
    }
    return LocalMemory{raw_descriptor};
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

class WindowsVirtualCameraFrameBroker final : public VirtualCameraFrameBroker {
  public:
    explicit WindowsVirtualCameraFrameBroker(
        std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink,
        VirtualCameraBrokerActivation activation)
        : frame_sink_(std::move(frame_sink)), activation_(std::move(activation)) {
        if (frame_sink_ == nullptr) {
            throw std::invalid_argument("virtual_camera_broker_sink_required");
        }
        constexpr std::string_view pipe_prefix{R"(\\.\pipe\)"};
        const auto token_is_nonzero = std::any_of(
            activation_.token.cbegin(), activation_.token.cend(),
            [](const std::uint8_t byte) { return byte != 0U; });
        if (!activation_.pipe_name.starts_with(pipe_prefix) ||
            activation_.pipe_name.size() <= pipe_prefix.size() ||
            activation_.pipe_name.size() > 256U ||
            !token_is_nonzero ||
            activation_.protocol_version !=
                kVirtualCameraBrokerProtocolVersion) {
            throw std::invalid_argument("virtual_camera_broker_activation_invalid");
        }
    }

    ~WindowsVirtualCameraFrameBroker() override { stop(); }

    void start() override {
        std::scoped_lock lock{lifecycle_mutex_};
        if (worker_.joinable()) {
            bool running = false;
            {
                std::scoped_lock health_lock{health_mutex_};
                running = health_.running;
            }
            if (running) {
                return;
            }
            worker_.join();
            stop_event_.reset();
        }
        if (!frame_sink_->endpoint()) {
            throw std::runtime_error("virtual_camera_broker_transport_unavailable");
        }
        UniqueHandle stop_event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!stop_event) {
            throw std::runtime_error("virtual_camera_broker_start_failed");
        }
        auto pipe = create_pipe();
        stop_event_ = std::move(stop_event);
        try {
            worker_ = std::thread([this, first_pipe = std::move(pipe)]() mutable {
                serve(first_pipe.release());
            });
        } catch (...) {
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
            if (!worker_.joinable()) {
                return;
            }
            static_cast<void>(SetEvent(stop_event_.get()));
            worker_.join();
            stop_event_.reset();
            std::scoped_lock health_lock{health_mutex_};
            health_.running = false;
        } catch (...) {
        }
    }

    [[nodiscard]] VirtualCameraBrokerActivation activation() const override {
        return activation_;
    }

    [[nodiscard]] VirtualCameraBrokerHealth health() const override {
        std::scoped_lock lock{health_mutex_};
        return health_;
    }

  private:
    [[nodiscard]] UniqueHandle create_pipe() const {
        auto descriptor = pipe_security_descriptor();
        SECURITY_ATTRIBUTES attributes{
            .nLength = sizeof(SECURITY_ATTRIBUTES),
            .lpSecurityDescriptor = descriptor.get(),
            .bInheritHandle = FALSE,
        };
        UniqueHandle result{CreateNamedPipeA(
            activation_.pipe_name.c_str(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
                FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,
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
                    pipe = create_pipe();
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
        std::scoped_lock lock{health_mutex_};
        health_.running = false;
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
            const auto token_matches =
                constant_time_token_equal(request.token, activation_.token);
            // The pipe DACL is the source of truth for the service identity:
            // only LocalService and LocalSystem can connect. Re-deriving the
            // token user here is incorrect for restricted service tokens, whose
            // effective access can come from service groups instead of TokenUser.
            if (!token_matches) {
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
                        endpoint.cross_session_file_path_utf8;
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
            std::scoped_lock lock{health_mutex_};
            ++health_.accepted_connections;
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
            health_.running = false;
            health_.error_code = error_code;
            ++health_.failed_connections;
        } catch (...) {
        }
    }

    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink_{};
    VirtualCameraBrokerActivation activation_{};
    mutable std::mutex lifecycle_mutex_{};
    UniqueHandle stop_event_{};
    std::thread worker_{};
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
            .pipe_name = windows_virtual_camera::kBrokerPipeNameUtf8,
            .token = windows_virtual_camera::kBrokerContractToken,
#endif
        });
}

std::unique_ptr<VirtualCameraFrameBroker> make_virtual_camera_frame_broker(
    std::shared_ptr<SharedFrameVirtualCameraSink> frame_sink,
    VirtualCameraBrokerActivation activation) {
    if (frame_sink == nullptr) {
        throw std::invalid_argument("virtual_camera_broker_sink_required");
    }
#ifdef _WIN32
    return std::make_unique<WindowsVirtualCameraFrameBroker>(
        std::move(frame_sink), std::move(activation));
#else
    static_cast<void>(frame_sink);
    static_cast<void>(activation);
    throw std::runtime_error("virtual_camera_broker_platform_unsupported");
#endif
}

} // namespace solin::media_engine
