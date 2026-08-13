#include "solin/media_engine/virtual_camera_broker.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <iostream>
#include <memory>
#include <span>
#include <string>
#include <thread>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

#ifdef _WIN32

class WindowsHandle final {
  public:
    explicit WindowsHandle(HANDLE value = nullptr) noexcept : value_(value) {}
    ~WindowsHandle() {
        if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
            static_cast<void>(CloseHandle(value_));
        }
    }
    WindowsHandle(const WindowsHandle&) = delete;
    WindowsHandle& operator=(const WindowsHandle&) = delete;
    WindowsHandle(WindowsHandle&& other) noexcept
        : value_(std::exchange(other.value_, nullptr)) {}
    WindowsHandle& operator=(WindowsHandle&& other) noexcept {
        if (this != &other) {
            if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
                static_cast<void>(CloseHandle(value_));
            }
            value_ = std::exchange(other.value_, nullptr);
        }
        return *this;
    }
    [[nodiscard]] HANDLE get() const noexcept { return value_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

  private:
    HANDLE value_{nullptr};
};

[[nodiscard]] bool exact_pipe_io(HANDLE pipe, std::span<std::uint8_t> bytes,
                                 const bool write) {
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(bytes.size() - offset);
        const auto succeeded =
            write ? WriteFile(pipe, bytes.data() + offset, remaining,
                              &transferred, nullptr)
                  : ReadFile(pipe, bytes.data() + offset, remaining,
                             &transferred, nullptr);
        if (succeeded == FALSE || transferred == 0U) {
            return false;
        }
        offset += transferred;
    }
    return true;
}

[[nodiscard]] bool complete_handshake(const std::string& pipe_name,
                                      const std::uint8_t nonce_value) {
    WindowsHandle pipe;
    for (int attempt = 0; attempt < 20 && !pipe; ++attempt) {
        pipe = WindowsHandle{CreateFileA(
            pipe_name.c_str(), GENERIC_READ | GENERIC_WRITE, 0U, nullptr,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | SECURITY_SQOS_PRESENT |
                SECURITY_IDENTIFICATION,
            nullptr)};
        if (!pipe && GetLastError() == ERROR_PIPE_BUSY) {
            static_cast<void>(WaitNamedPipeA(pipe_name.c_str(), 100U));
        }
    }
    if (!pipe) {
        return false;
    }
    solin::media_engine::VirtualCameraBrokerRequest request{};
    request.nonce.fill(nonce_value);
    auto request_bytes =
        solin::media_engine::encode_virtual_camera_broker_request(request);
    std::array<std::uint8_t,
               solin::media_engine::kVirtualCameraBrokerResponseSize>
        response_bytes{};
    if (!exact_pipe_io(pipe.get(), request_bytes, true) ||
        !exact_pipe_io(pipe.get(), response_bytes, false)) {
        return false;
    }
    const auto response =
        solin::media_engine::decode_virtual_camera_broker_response(response_bytes);
    return response.status == solin::media_engine::VirtualCameraBrokerStatus::ok &&
           response.nonce == request.nonce && response.generation == 17U &&
           !response.mapping_file_path_utf8.empty();
}

void test_broker_authenticates_the_same_user_and_session() {
    auto unique_sink = solin::media_engine::make_shared_frame_virtual_camera_sink(
        {
            .camera_id = "broker-test-camera",
            .friendly_name = "Solin Broker Test Camera",
            .video_format = {
                .width = 4U,
                .height = 2U,
                .fps_numerator = 30U,
                .fps_denominator = 1U,
                .pixel_format = "nv12",
                .color_space = "bt709",
                .color_range = "limited",
            },
        },
        17U);
    std::shared_ptr<solin::media_engine::SharedFrameVirtualCameraSink> sink{
        std::move(unique_sink)};
    sink->start();
    const auto pipe_name =
        std::string{R"(\\.\pipe\Solin.VirtualCamera.FrameBroker.test.)"} +
        std::to_string(GetCurrentProcessId());
    auto broker = solin::media_engine::make_virtual_camera_frame_broker(
        sink, {.pipe_name = pipe_name});
    broker->start();
    const auto endpoint = broker->endpoint();
    expect(endpoint.pipe_name == pipe_name &&
               endpoint.protocol_version ==
                   solin::media_engine::kVirtualCameraBrokerProtocolVersion,
           "the running broker exposes its private versioned endpoint");

    WindowsHandle pipe;
    for (int attempt = 0; attempt < 20 && !pipe; ++attempt) {
        pipe = WindowsHandle{CreateFileA(
            endpoint.pipe_name.c_str(), GENERIC_READ | GENERIC_WRITE, 0U,
            nullptr, OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | SECURITY_SQOS_PRESENT |
                SECURITY_IDENTIFICATION,
            nullptr)};
        if (!pipe && GetLastError() == ERROR_PIPE_BUSY) {
            static_cast<void>(WaitNamedPipeA(endpoint.pipe_name.c_str(), 100U));
        }
    }
    expect(static_cast<bool>(pipe),
           "the current user can connect to the private broker pipe");
    if (pipe) {
        solin::media_engine::VirtualCameraBrokerRequest request{};
        request.nonce.fill(0x51U);
        const auto request_bytes =
            solin::media_engine::encode_virtual_camera_broker_request(request);
        DWORD transferred = 0U;
        expect(WriteFile(pipe.get(), request_bytes.data(),
                         static_cast<DWORD>(request_bytes.size()), &transferred,
                         nullptr) != FALSE &&
                   transferred == request_bytes.size(),
               "the complete broker request is written");
        std::array<std::uint8_t,
                   solin::media_engine::kVirtualCameraBrokerResponseSize>
            response_bytes{};
        transferred = 0U;
        expect(ReadFile(pipe.get(), response_bytes.data(),
                        static_cast<DWORD>(response_bytes.size()), &transferred,
                        nullptr) != FALSE &&
                   transferred == response_bytes.size(),
               "the complete broker response is read");
        const auto response =
            solin::media_engine::decode_virtual_camera_broker_response(
                response_bytes);
        expect(response.status ==
                       solin::media_engine::VirtualCameraBrokerStatus::ok &&
                   response.nonce == request.nonce && response.generation == 17U &&
                   !response.mapping_file_path_utf8.empty(),
               "the broker discloses transport metadata only after identity validation");
    }
    std::atomic_int concurrent_successes{0};
    std::array<std::thread, 4U> consumers{};
    for (std::size_t index = 0U; index < consumers.size(); ++index) {
        consumers[index] = std::thread([&, index]() {
            if (complete_handshake(pipe_name,
                                   static_cast<std::uint8_t>(0x60U + index))) {
                concurrent_successes.fetch_add(1);
            }
        });
    }
    for (auto& consumer : consumers) {
        consumer.join();
    }
    expect(concurrent_successes.load() ==
               static_cast<int>(consumers.size()),
           "bounded broker instances serve concurrent consumers independently");
    broker->stop();
    const auto health = broker->health();
    expect(!health.running && health.accepted_connections == 5U &&
               health.denied_connections == 0U,
           "broker health accounts for the authenticated handshake");
    sink->stop();
}

#endif

} // namespace

int main() {
#ifdef _WIN32
    try {
        test_broker_authenticates_the_same_user_and_session();
    } catch (const std::exception& error) {
        ++failures;
        std::cerr << "FAILED: unexpected broker exception: " << error.what()
                  << '\n';
    }
#else
    expect(true, "the Windows broker test is skipped on other platforms");
#endif
    return failures == 0 ? 0 : 1;
}
