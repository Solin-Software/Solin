#ifndef NOMINMAX
#define NOMINMAX
#endif

#include "solin/media_engine/virtual_camera.hpp"

#include "solin/media_engine/shared_video_frame_channel.hpp"

#ifdef _WIN32
#include "windows_virtual_camera_backend.hpp"
#endif

#include <filesystem>
#include <iostream>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_probe_never_advertises_a_partial_backend() {
    const auto probe = solin::media_engine::probe_platform_virtual_camera();
#ifdef _WIN32
    expect(probe.backend ==
                   solin::media_engine::VirtualCameraBackendKind::windows_directshow &&
               probe.platform_supported,
           "Windows selects the DirectShow backend contract");
#else
    expect(probe.backend ==
                   solin::media_engine::VirtualCameraBackendKind::unavailable &&
               !probe.platform_supported,
           "an unimplemented platform returns the unavailable backend contract");
#endif
    expect(!probe.operational ||
               (probe.platform_supported && probe.filter_registered_x86 &&
                probe.filter_registered_x64 &&
                probe.cross_process_transport_available),
           "the probe cannot advertise a partially installed camera backend");
    expect(probe.operational == probe.error_code.empty(),
           "operational state and the stable error code cannot disagree");
}

#ifdef _WIN32

void test_shared_frame_sink_publishes_validated_latest_frames() {
    solin::media_engine::VirtualCameraConfiguration configuration{
        .camera_id = "solin-main-camera",
        .friendly_name = "Solin Virtual Camera",
        .video_format = {
            .width = 4U,
            .height = 2U,
            .fps_numerator = 30U,
            .fps_denominator = 1U,
            .pixel_format = "nv12",
            .color_space = "bt709",
            .color_range = "limited",
        },
    };
    auto sink = solin::media_engine::make_shared_frame_virtual_camera_sink(
        configuration, 11U);
    expect(!sink->endpoint(), "a stopped sink does not expose a stale native handle");
    sink->start();
    const auto endpoint = sink->endpoint();
    expect(endpoint && endpoint.generation == 11U &&
               endpoint.layout.pixel_format ==
                   solin::media_engine::VideoFramePixelFormat::nv12,
           "a started sink exposes an immutable process-local broker endpoint");
    auto reader = solin::media_engine::make_shared_video_frame_reader(
        endpoint.process_local_mapping_handle, endpoint.mapping_size);
    solin::media_engine::PackedVideoFrame frame{
        .sequence = 42U,
        .presentation_timestamp_ns = 100U,
        .duration_ns = 200U,
        .produced_monotonic_ns = 300U,
        .discontinuity = true,
        .width = endpoint.layout.width,
        .height = endpoint.layout.height,
        .pixel_format = endpoint.layout.pixel_format,
        .plane_strides = endpoint.layout.plane_strides,
        .plane_offsets = endpoint.layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(endpoint.layout.payload_size, 0x80U),
    };
    expect(sink->publish(solin::media_engine::video_frame_view(frame)),
           "a canonical NV12 frame reaches the transport sink");
    solin::media_engine::PackedVideoFrame received;
    expect(reader->read_latest(received) && received.bytes == frame.bytes,
           "the source-side reader observes the exact newest frame bytes");
    expect(sink->health().published_frames == 1U &&
               sink->health().last_frame_sequence == 42U,
           "sink health accounts for published compositor frames");
    auto malformed = solin::media_engine::video_frame_view(frame);
    malformed.planes[1] = malformed.planes[1].first(malformed.planes[1].size() - 1U);
    expect(!sink->publish(malformed) &&
               sink->health().dropped_frames == 1U &&
               sink->health().state ==
                   solin::media_engine::VirtualCameraSinkState::degraded,
           "a malformed frame is dropped without crossing the process boundary");
    sink->stop();
    expect(!sink->endpoint() &&
               sink->health().state ==
                   solin::media_engine::VirtualCameraSinkState::stopped,
           "stopping the sink invalidates its broker endpoint");
}

void test_solin_output_device_cannot_be_selected_as_an_input() {
    constexpr std::string_view solin_device{
        "@device:sw:{860bb310-5d01-11d0-bd3b-00a0c911ce86}\\"
        "{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}"};
    expect(solin::media_engine::is_solin_virtual_camera_device(solin_device),
           "the exact DirectShow moniker CLSID identifies the Solin output");
    expect(!solin::media_engine::is_solin_virtual_camera_device(
               R"(\\?\usb#vid_0000&pid_0000#camera)"),
           "a hardware device is never hidden by display name alone");
    expect(!solin::media_engine::is_solin_virtual_camera_device(
               "@device:sw:{camera}"),
           "third-party virtual cameras remain available as explicit inputs");
    expect(solin::media_engine::is_solin_virtual_camera_device(solin_device),
           "renaming cannot bypass CLSID-based feedback prevention");
}

#endif

} // namespace

int main([[maybe_unused]] const int argument_count,
         [[maybe_unused]] char** arguments) {
    test_probe_never_advertises_a_partial_backend();
#ifdef _WIN32
    expect(argument_count == 2,
           "the Windows probe regression requires the DirectShow filter path");
    if (argument_count == 2) {
        const std::filesystem::path filter_path{arguments[1]};
        expect(solin::media_engine::windows_pe_dll_matches_architecture(
                   filter_path, true),
               "the x64 DirectShow DLL passes strict PE architecture validation");
        expect(!solin::media_engine::windows_pe_dll_matches_architecture(
                   filter_path, false),
               "the x64 DirectShow DLL is rejected from the x86 registry view");
        expect(!solin::media_engine::windows_pe_dll_matches_architecture(
                   filter_path.parent_path() / "missing-filter.dll", true),
               "a missing DLL fails PE validation closed");
    }
    test_shared_frame_sink_publishes_validated_latest_frames();
    test_solin_output_device_cannot_be_selected_as_an_input();
#endif
    return failures == 0 ? 0 : 1;
}
