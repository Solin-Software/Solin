#include "broker_client.hpp"

#include "solin/media_engine/virtual_camera_broker_protocol.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>

#include <array>
#include <chrono>
#include <cstddef>
#include <filesystem>
#include <limits>
#include <span>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace solin::media_engine::windows_virtual_camera {
namespace {

using namespace std::chrono_literals;
constexpr auto kBrokerTimeout = 500ms;
constexpr auto kReconnectInterval = 250ms;
constexpr auto kProducerStaleTimeout = 1500ms;

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
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

  private:
    HANDLE value_{nullptr};
};

[[nodiscard]] bool cancel_and_drain(HANDLE pipe, OVERLAPPED& overlapped,
                                    DWORD& transferred) noexcept {
    static_cast<void>(CancelIoEx(pipe, &overlapped));
    return GetOverlappedResult(pipe, &overlapped, &transferred, TRUE) != FALSE;
}

[[nodiscard]] bool pipe_io(HANDLE pipe, const std::span<std::uint8_t> bytes,
                           const bool write) noexcept {
    const auto deadline = std::chrono::steady_clock::now() + kBrokerTimeout;
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        const UniqueHandle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!event) {
            return false;
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event.get();
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(bytes.size() - offset);
        const auto started =
            write ? WriteFile(pipe, bytes.data() + offset, remaining,
                              &transferred, &overlapped)
                  : ReadFile(pipe, bytes.data() + offset, remaining,
                             &transferred, &overlapped);
        if (started == FALSE) {
            if (GetLastError() != ERROR_IO_PENDING) {
                return false;
            }
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) {
                return cancel_and_drain(pipe, overlapped, transferred) &&
                       transferred == remaining;
            }
            const auto wait_ms = static_cast<DWORD>(
                std::chrono::duration_cast<std::chrono::milliseconds>(deadline -
                                                                      now)
                    .count());
            if (WaitForSingleObject(event.get(), wait_ms) != WAIT_OBJECT_0) {
                return cancel_and_drain(pipe, overlapped, transferred) &&
                       transferred == remaining;
            }
            if (GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) ==
                FALSE) {
                return false;
            }
        }
        if (transferred == 0U) {
            return false;
        }
        offset += transferred;
    }
    return true;
}

[[nodiscard]] std::wstring utf8_to_utf16(const std::string_view value) {
    if (value.empty() ||
        value.size() > static_cast<std::size_t>((std::numeric_limits<int>::max)())) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    const auto source_size = static_cast<int>(value.size());
    const auto required = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                               value.data(), source_size, nullptr,
                                               0);
    if (required <= 0) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    std::wstring result(static_cast<std::size_t>(required), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                            source_size, result.data(), required) != required) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    return result;
}

[[nodiscard]] std::unique_ptr<SharedVideoFrameReader> open_frame_reader(
    const VirtualCameraBrokerResponse& response) {
    const auto path = utf8_to_utf16(response.mapping_file_path_utf8);
    if (!std::filesystem::path{path}.is_absolute()) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    const UniqueHandle file{CreateFileW(
        path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_RANDOM_ACCESS, nullptr)};
    if (!file) {
        throw std::runtime_error("virtual_camera_broker_file_unavailable");
    }
    LARGE_INTEGER file_size{};
    if (GetFileSizeEx(file.get(), &file_size) == FALSE ||
        file_size.QuadPart < 0 ||
        static_cast<std::uint64_t>(file_size.QuadPart) != response.mapping_size) {
        throw std::runtime_error("virtual_camera_broker_file_invalid");
    }
    const UniqueHandle mapping{CreateFileMappingW(file.get(), nullptr, PAGE_READONLY,
                                                   0U, 0U, nullptr)};
    if (!mapping) {
        throw std::runtime_error("virtual_camera_broker_file_unavailable");
    }
    auto reader = make_shared_video_frame_reader(
        reinterpret_cast<std::uintptr_t>(mapping.get()), response.mapping_size);
    if (reader->configuration().generation != response.generation ||
        reader->configuration().layout != response.layout) {
        throw std::runtime_error("virtual_camera_broker_file_invalid");
    }
    return reader;
}

[[nodiscard]] bool connect_pipe(const std::wstring& pipe_name,
                                UniqueHandle& pipe) noexcept {
    if (WaitNamedPipeW(pipe_name.c_str(),
                       static_cast<DWORD>(kBrokerTimeout.count())) ==
        FALSE) {
        return false;
    }
    pipe = UniqueHandle{CreateFileW(
        pipe_name.c_str(), GENERIC_READ | GENERIC_WRITE, 0U, nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT |
            SECURITY_IDENTIFICATION,
        nullptr)};
    return static_cast<bool>(pipe);
}

[[nodiscard]] bool open_broker_generation(
    const std::wstring& pipe_name, VirtualCameraBrokerResponse& response,
    std::unique_ptr<SharedVideoFrameReader>& reader) noexcept {
    try {
        VirtualCameraBrokerRequest request{};
        if (BCryptGenRandom(nullptr, request.nonce.data(),
                            static_cast<ULONG>(request.nonce.size()),
                            BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
            return false;
        }
        UniqueHandle pipe;
        if (!connect_pipe(pipe_name, pipe)) {
            return false;
        }
        auto request_bytes = encode_virtual_camera_broker_request(request);
        std::array<std::uint8_t, kVirtualCameraBrokerResponseSize> response_bytes{};
        if (!pipe_io(pipe.get(), request_bytes, true) ||
            !pipe_io(pipe.get(), response_bytes, false)) {
            return false;
        }
        auto decoded = decode_virtual_camera_broker_response(response_bytes);
        if (decoded.nonce != request.nonce ||
            decoded.status != VirtualCameraBrokerStatus::ok ||
            decoded.layout.pixel_format != VideoFramePixelFormat::nv12) {
            return false;
        }
        auto opened_reader = open_frame_reader(decoded);
        response = std::move(decoded);
        reader = std::move(opened_reader);
        return true;
    } catch (...) {
        return false;
    }
}

} // namespace

BrokerFrameProvider::BrokerFrameProvider() noexcept
    : last_progress_(std::chrono::steady_clock::now()) {
    try {
        pipe_name_ = current_user_broker_pipe_name();
        reconnect_worker_ = std::thread([this]() noexcept { reconnect_loop(); });
    } catch (...) {
        // Identity or thread creation failure must not prevent an offline camera
        // graph from opening and receiving its pre-rendered standby frame.
        pipe_name_.clear();
    }
}

BrokerFrameProvider::~BrokerFrameProvider() {
    stop_requested_.store(true);
    reconnect_wakeup_.notify_all();
    if (reconnect_worker_.joinable()) {
        reconnect_worker_.join();
    }
}

bool BrokerFrameProvider::visit_live_frame(
    const SharedVideoFrameReader::FrameVisitor& visitor) noexcept {
    if (!visitor) {
        return false;
    }
    try {
        std::unique_lock lock{mutex_};
        const auto now = std::chrono::steady_clock::now();
        if (reader_ == nullptr) {
            return false;
        }
        const auto heartbeat = reader_->heartbeat();
        if (heartbeat != 0U && heartbeat != last_heartbeat_) {
            last_heartbeat_ = heartbeat;
            last_progress_ = now;
        }
        if (now - last_progress_ >= kProducerStaleTimeout) {
            reader_.reset();
            generation_.store(0U);
            last_heartbeat_ = 0U;
            reconnect_requested_ = true;
            reconnect_wakeup_.notify_one();
            return false;
        }
        return reader_->visit_current_frame(visitor);
    } catch (...) {
        try {
            std::scoped_lock lock{mutex_};
            reader_.reset();
            generation_.store(0U);
            last_heartbeat_ = 0U;
            reconnect_requested_ = true;
            reconnect_wakeup_.notify_one();
        } catch (...) {
        }
        return false;
    }
}

std::uint64_t BrokerFrameProvider::generation() const noexcept {
    return generation_.load();
}

void BrokerFrameProvider::reconnect_loop() noexcept {
    while (!stop_requested_.load()) {
        {
            std::unique_lock lock{mutex_};
            reconnect_wakeup_.wait_for(
                lock, kReconnectInterval, [this]() {
                    return stop_requested_.load() || reconnect_requested_;
                });
            if (stop_requested_.load()) {
                return;
            }
            if (!reconnect_requested_ && reader_ != nullptr) {
                continue;
            }
            reconnect_requested_ = false;
        }

        VirtualCameraBrokerResponse response{};
        std::unique_ptr<SharedVideoFrameReader> reader;
        if (open_broker_generation(pipe_name_, response, reader)) {
            std::scoped_lock lock{mutex_};
            if (!stop_requested_.load()) {
                reader_ = std::move(reader);
                generation_.store(response.generation);
                last_heartbeat_ = reader_->heartbeat();
                last_progress_ = std::chrono::steady_clock::now();
            }
        } else {
            std::unique_lock lock{mutex_};
            reconnect_wakeup_.wait_for(lock, kReconnectInterval, [this]() {
                return stop_requested_.load();
            });
            reconnect_requested_ = true;
        }
    }
}

} // namespace solin::media_engine::windows_virtual_camera
