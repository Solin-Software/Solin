#include "solin/media_engine/virtual_camera_broker.hpp"
#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <array>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

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
    [[nodiscard]] HANDLE get() const noexcept { return value_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

  private:
    HANDLE value_{nullptr};
};

void test_broker_denies_an_interactive_process_even_with_the_token() {
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
        1U);
    std::shared_ptr<solin::media_engine::SharedFrameVirtualCameraSink> sink{
        std::move(unique_sink)};
    sink->start();
    const auto pipe_name =
        std::string{R"(\\.\pipe\Solin.VirtualCamera.FrameBroker.test.)"} +
        std::to_string(GetCurrentProcessId());
    bool rejected_zero_token = false;
    try {
        static_cast<void>(
            solin::media_engine::make_virtual_camera_frame_broker(
                sink, {.pipe_name = pipe_name}));
    } catch (const std::invalid_argument&) {
        rejected_zero_token = true;
    }
    expect(rejected_zero_token,
           "the broker rejects an all-zero activation token");
    auto broker = solin::media_engine::make_virtual_camera_frame_broker(
        sink,
        {
            .pipe_name = pipe_name,
            .token = solin::media_engine::windows_virtual_camera::
                kBrokerContractToken,
        });
    broker->start();
    const auto activation = broker->activation();
    expect(!activation.pipe_name.empty() &&
               activation.protocol_version ==
                   solin::media_engine::kVirtualCameraBrokerProtocolVersion,
           "the running broker exposes a versioned activation endpoint");

    const WindowsHandle pipe{CreateFileA(
        activation.pipe_name.c_str(), GENERIC_READ | GENERIC_WRITE, 0U, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr)};
    expect(!pipe && GetLastError() == ERROR_ACCESS_DENIED,
           "the pipe ACL rejects interactive processes before the handshake");
    broker->stop();
    const auto health = broker->health();
    expect(!health.running && health.denied_connections == 0U &&
               health.accepted_connections == 0U,
           "an ACL rejection never enters the broker handshake");
    sink->stop();
}

#endif

} // namespace

int main() {
#ifdef _WIN32
    test_broker_denies_an_interactive_process_even_with_the_token();
#else
    expect(true, "the Windows broker test is skipped on other platforms");
#endif
    return failures == 0 ? 0 : 1;
}
