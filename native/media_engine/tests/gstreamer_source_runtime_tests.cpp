#include "solin/media_engine/media_runtime.hpp"
#include "solin/media_engine/scene_graph.hpp"
#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/shared_video_frame_channel.hpp"
#include "solin/media_engine/virtual_camera.hpp"
#include "gstreamer_source_runtime.hpp"

#include <gst/d3d11/gstd3d11memory.h>
#include <gst/gst.h>
#include <gst/rtsp-server/rtsp-server.h>
#include <gst/video/video-frame.h>

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <memory>
#include <optional>
#include <ranges>
#include <string>
#include <string_view>
#include <thread>

namespace {

using namespace std::chrono_literals;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (condition) {
        return;
    }
    ++failures;
    std::cerr << "FAILED: " << description << '\n';
}

class TestRtspServer final {
  public:
    TestRtspServer() {
        context_ = g_main_context_new();
        loop_ = context_ == nullptr ? nullptr : g_main_loop_new(context_, FALSE);
        server_ = gst_rtsp_server_new();
        if (context_ == nullptr || loop_ == nullptr || server_ == nullptr) {
            return;
        }
        g_object_set(server_, "address", "127.0.0.1", "service", "0", nullptr);
        auto* mounts = gst_rtsp_server_get_mount_points(server_);
        auto* factory = gst_rtsp_media_factory_new();
        if (mounts == nullptr || factory == nullptr) {
            if (mounts != nullptr) {
                g_object_unref(mounts);
            }
            if (factory != nullptr) {
                g_object_unref(factory);
            }
            return;
        }
        gst_rtsp_media_factory_set_launch(
            factory,
            "( videotestsrc is-live=true pattern=ball ! "
            "videoconvert ! video/x-raw,format=I420,width=320,height=240,framerate=30/1 ! "
            "jpegenc ! rtpjpegpay name=pay0 pt=96 )");
        gst_rtsp_media_factory_set_shared(factory, TRUE);
        gst_rtsp_mount_points_add_factory(mounts, "/camera", factory);
        g_object_unref(mounts);

        g_main_context_push_thread_default(context_);
        source_id_ = gst_rtsp_server_attach(server_, context_);
        g_main_context_pop_thread_default(context_);
        if (source_id_ == 0U) {
            return;
        }
        worker_ = std::thread([this] { g_main_loop_run(loop_); });
        const auto deadline = std::chrono::steady_clock::now() + 2s;
        while (std::chrono::steady_clock::now() < deadline) {
            port_ = gst_rtsp_server_get_bound_port(server_);
            if (port_ != 0U) {
                break;
            }
            std::this_thread::sleep_for(10ms);
        }
    }

    ~TestRtspServer() {
        if (loop_ != nullptr) {
            g_main_loop_quit(loop_);
        }
        if (worker_.joinable()) {
            worker_.join();
        }
        if (context_ != nullptr && source_id_ != 0U) {
            auto* source = g_main_context_find_source_by_id(context_, source_id_);
            if (source != nullptr) {
                g_source_destroy(source);
            }
        }
        if (server_ != nullptr) {
            g_object_unref(server_);
        }
        if (loop_ != nullptr) {
            g_main_loop_unref(loop_);
        }
        if (context_ != nullptr) {
            g_main_context_unref(context_);
        }
    }

    TestRtspServer(const TestRtspServer&) = delete;
    TestRtspServer& operator=(const TestRtspServer&) = delete;

    [[nodiscard]] bool ready() const noexcept { return port_ != 0U; }
    [[nodiscard]] std::string uri() const {
        return "rtsp://127.0.0.1:" + std::to_string(port_) + "/camera";
    }
    void disconnect_clients() const {
        auto* clients = gst_rtsp_server_client_filter(
            server_,
            [](GstRTSPServer*, GstRTSPClient*, gpointer) {
                return GST_RTSP_FILTER_REF;
            },
            nullptr);
        for (auto* entry = clients; entry != nullptr; entry = entry->next) {
            auto* client = GST_RTSP_CLIENT(entry->data);
            gst_rtsp_client_close(client);
            g_object_unref(client);
        }
        g_list_free(clients);
    }

  private:
    GMainContext* context_{nullptr};
    GMainLoop* loop_{nullptr};
    GstRTSPServer* server_{nullptr};
    guint source_id_{0U};
    guint port_{0U};
    std::thread worker_{};
};

void test_automatic_camera_format_is_bounded_and_deterministic() {
    const solin::media_engine::LocalCameraDevice device{
        .device_id = "camera-test",
        .display_name = "Camera test",
        .formats = {
            {.media_type = "video/x-raw",
             .pixel_format = "NV12",
             .width = 3'840U,
             .height = 2'160U,
             .fps_numerator = 60U},
            {.media_type = "image/jpeg",
             .pixel_format = "JPEG",
             .width = 1'920U,
             .height = 1'080U,
             .fps_numerator = 30U},
            {.media_type = "video/x-raw",
             .pixel_format = "NV12",
             .width = 1'920U,
             .height = 1'080U,
             .fps_numerator = 30U},
            {.media_type = "video/x-raw",
             .pixel_format = "NV12",
             .width = 1'280U,
             .height = 720U,
             .fps_numerator = 60U},
        },
    };
    const auto selected =
        solin::media_engine::preferred_automatic_camera_format(device);
    expect(selected.has_value() && selected->media_type == "video/x-raw" &&
               selected->pixel_format == "NV12" && selected->width == 1'920U &&
               selected->height == 1'080U && selected->fps_numerator == 30U,
           "automatic camera selection prefers exact raw 1080p30 over 4K and encoded peers");
    expect(!solin::media_engine::preferred_automatic_camera_format(
                solin::media_engine::LocalCameraDevice{})
                .has_value(),
           "automatic camera selection rejects an empty device capability set");
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot color_snapshot() {
    return {
        .document_id = "gstreamer-source-test",
        .document_revision = 1U,
        .sources = {{
            .id = "color-1",
            .kind = solin::media_engine::SceneSourceKind::color,
            .enabled = true,
            .configuration =
                solin::media_engine::ColorSourceConfiguration{"#336699FF"},
        }},
    };
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot compositor_snapshot() {
    auto value = color_snapshot();
    value.document_id = "gstreamer-compositor-test";
    value.sources.push_back({
        .id = "scene-reference-1",
        .kind = solin::media_engine::SceneSourceKind::scene_reference,
        .enabled = true,
        .configuration =
            solin::media_engine::SceneReferenceSourceConfiguration{"scene-leaf"},
    });
    const solin::media_engine::SceneLayerGeometry geometry{
        .x = 0.0,
        .y = 0.0,
        .width = 1.0,
        .height = 1.0,
        .opacity = 1.0,
        .fit_mode = "stretch",
        .border_color = "#00000000",
        .visible = true,
    };
    value.scenes = {
        {.id = "scene-leaf",
         .layers = {{.id = "color-layer",
                     .source_id = "color-1",
                     .geometry = geometry}}},
        {.id = "scene-root",
         .layers = {{.id = "reference-layer",
                     .source_id = "scene-reference-1",
                     .geometry = geometry}}},
    };
    const solin::media_engine::OutputVideoFormat format{
        .width = 640U,
        .height = 360U,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
        .pixel_format = "bgra",
        .color_space = "bt709",
        .color_range = "full",
    };
    auto virtual_camera_format = format;
    virtual_camera_format.pixel_format = "nv12";
    virtual_camera_format.color_range = "limited";
    value.outputs = {
        solin::media_engine::SceneOutputDefinition{
            .bus = solin::media_engine::OutputBus::media_windows,
            .default_scene_id = "scene-root",
            .transition = "cut",
            .video_format = format,
        },
        solin::media_engine::SceneOutputDefinition{
            .bus = solin::media_engine::OutputBus::virtual_camera,
            .default_scene_id = "scene-root",
            .transition = "cut",
            .video_format = virtual_camera_format,
        },
    };
    value.active_scene_ids = {"scene-root", "scene-root"};
    value.render_enabled = {true, false};
    value.output_enabled = {true, false};
    return value;
}

[[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
wait_for_frame(solin::media_engine::SourceRuntime& runtime,
               const std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
        auto frame = runtime.latest_frame();
        if (frame != nullptr) {
            return frame;
        }
        std::this_thread::sleep_for(10ms);
    }
    return {};
}

[[nodiscard]] solin::media_engine::SourceFrameMemory canonical_memory(
    const std::shared_ptr<solin::media_engine::SourceRuntimeFactory>& factory) {
    return solin::media_engine::gstreamer_d3d11_device(factory)
               ? solin::media_engine::SourceFrameMemory::d3d11
               : solin::media_engine::SourceFrameMemory::system_memory;
}

[[nodiscard]] bool sample_uses_device(GstSample* sample, GstD3D11Device* device) {
    auto* buffer = sample == nullptr ? nullptr : gst_sample_get_buffer(sample);
    if (buffer == nullptr || device == nullptr || gst_buffer_n_memory(buffer) == 0U) {
        return false;
    }
    for (guint index = 0U; index < gst_buffer_n_memory(buffer); ++index) {
        auto* memory = gst_buffer_peek_memory(buffer, index);
        if (memory == nullptr || gst_is_d3d11_memory(memory) == FALSE ||
            GST_D3D11_MEMORY_CAST(memory)->device != device) {
            return false;
        }
    }
    return true;
}

[[nodiscard]] bool sample_center_is_test_color(GstSample* sample) {
    auto* caps = sample == nullptr ? nullptr : gst_sample_get_caps(sample);
    auto* buffer = sample == nullptr ? nullptr : gst_sample_get_buffer(sample);
    GstVideoInfo info{};
    GstVideoFrame frame{};
    if (caps == nullptr || buffer == nullptr ||
        gst_video_info_from_caps(&info, caps) == FALSE ||
        gst_video_frame_map(&frame, &info, buffer, GST_MAP_READ) == FALSE) {
        return false;
    }
    const auto x = GST_VIDEO_INFO_WIDTH(&info) / 2U;
    const auto y = GST_VIDEO_INFO_HEIGHT(&info) / 2U;
    const auto* pixels =
        static_cast<const guint8*>(GST_VIDEO_FRAME_PLANE_DATA(&frame, 0U));
    const auto* pixel = pixels +
                        static_cast<std::size_t>(y) * static_cast<std::size_t>(
                            GST_VIDEO_FRAME_PLANE_STRIDE(&frame, 0U)) +
                        static_cast<std::size_t>(x) * 4U;
    const bool matches = pixel[0] == 0x99U && pixel[1] == 0x66U &&
                         pixel[2] == 0x33U && pixel[3] == 0xFFU;
    gst_video_frame_unmap(&frame);
    return matches;
}

void test_color_source_publishes_bounded_latest_d3d11_frames(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto factory = media_runtime.source_runtime_factory();
    const auto expected_memory = canonical_memory(factory);
    expect(factory == media_runtime.source_runtime_factory(),
           "one media runtime exposes one canonical source factory");
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(color_snapshot());
    std::chrono::steady_clock::time_point stop_started;
    {
        auto lease = registry.acquire("color-1", "source-runtime-test");
        auto first_frame = wait_for_frame(lease.runtime(), 5s);
        expect(first_frame != nullptr, "the generated source publishes a frame");
        std::uint64_t first_sequence = 0U;
        std::uint64_t first_epoch = 0U;
        if (first_frame != nullptr) {
            first_sequence = first_frame->sequence;
            first_epoch = first_frame->stream_epoch;
            expect(first_frame->sequence > 0U, "published frames carry a sequence");
            expect(first_frame->stream_epoch > 0U,
                   "published frames identify their stream epoch");
            expect(first_frame->sequence != 1U || first_frame->discontinuity,
                   "the first frame of an epoch is marked as discontinuous");
            expect(first_frame->width > 0U && first_frame->height > 0U,
                   "published frames carry negotiated dimensions");
            expect(first_frame->pixel_format == "BGRA",
                   "the source registry normalizes frames to BGRA");
            expect(first_frame->memory == expected_memory,
                   "the source uses the available canonical memory path");
            expect(first_frame->payload != nullptr,
                   "the latest frame retains its native GStreamer payload");
            auto sample = solin::media_engine::gstreamer_sample(first_frame);
            expect(static_cast<bool>(sample) && gst_sample_get_buffer(sample.sample) != nullptr,
                   "a GStreamer sample lease retains the native sample and its frame owner");
            const auto d3d11_device =
                solin::media_engine::gstreamer_d3d11_device(factory);
            if (d3d11_device) {
                expect(sample_uses_device(sample.sample, d3d11_device.device),
                       "D3D11 frame memory uses the factory's shared device");
            }
        }

        std::this_thread::sleep_for(100ms);
        const auto later_frame = lease.runtime().latest_frame();
        expect(later_frame != nullptr && later_frame->sequence > first_sequence,
               "latest-frame publication advances without a consumer queue");
        expect(later_frame != nullptr && later_frame->stream_epoch == first_epoch &&
                   !later_frame->discontinuity,
               "continuous frames retain the epoch without repeating discontinuity");
        const auto health = lease.runtime().health();
        expect(health.status == solin::media_engine::SourceRuntimeStatus::ready &&
                   health.error_code.empty(),
               "a producing source reports ready without sensitive errors");
        stop_started = std::chrono::steady_clock::now();
    }
    expect(std::chrono::steady_clock::now() - stop_started < 2s,
           "generated source shutdown remains bounded");
    expect(registry.entries()[0].consumer_count == 0U,
           "the registry releases the runtime after its last consumer");
}

void test_scene_renderer_composes_nested_scene_to_shared_d3d11_frame(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto renderer = media_runtime.scene_renderer();
    expect(renderer != nullptr, "the D3D11 probe exposes a scene renderer");
    if (renderer == nullptr) {
        return;
    }
    solin::media_engine::SceneGraphRuntime graph{
        media_runtime.source_runtime_factory(), renderer};
    graph.hydrate(compositor_snapshot(), 1U);

    std::shared_ptr<const solin::media_engine::SourceFrame> first_frame;
    bool rendered_source = false;
    const auto deadline = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < deadline) {
        first_frame = renderer->latest_frame(
            solin::media_engine::OutputBus::media_windows);
        const auto sample = solin::media_engine::gstreamer_sample(first_frame);
        if (sample && sample_center_is_test_color(sample.sample)) {
            rendered_source = true;
            break;
        }
        std::this_thread::sleep_for(10ms);
    }
    expect(first_frame != nullptr && first_frame->width == 640U &&
               first_frame->height == 360U && first_frame->pixel_format == "BGRA" &&
               first_frame->memory == solin::media_engine::SourceFrameMemory::d3d11,
           "a nested scene renders into the configured canonical D3D11 output");
    expect(rendered_source,
           "the nested compositor output contains the source layer, not only its background");
    std::uint64_t enable_virtual_camera_sequence = 4U;
    if (first_frame != nullptr) {
        const auto sample = solin::media_engine::gstreamer_sample(first_frame);
        const auto device = solin::media_engine::gstreamer_d3d11_device(
            media_runtime.source_runtime_factory());
        expect(sample && device && sample_uses_device(sample.sample, device.device),
               "the rendered frame stays on the source factory's shared D3D11 device");
        std::this_thread::sleep_for(100ms);
        const auto later_frame = renderer->latest_frame(
            solin::media_engine::OutputBus::media_windows);
        expect(later_frame != nullptr && later_frame->sequence > first_frame->sequence,
               "the compositor publishes a bounded advancing latest-frame stream");

        const auto prepare_started = std::chrono::steady_clock::now();
        const auto preparation = graph.prepare(
            solin::media_engine::OutputBus::media_windows, "scene-leaf", 1U,
            "switch-scene", 2U);
        const auto prepare_elapsed = std::chrono::steady_clock::now() - prepare_started;
        expect(prepare_elapsed < 1s,
               "a warm-source scene prepares without waiting for the preroll timeout");
        graph.take(preparation, "cut", 0U, 1U, 3U);
        std::shared_ptr<const solin::media_engine::SourceFrame> switched_frame;
        const auto switched_deadline = std::chrono::steady_clock::now() + 1s;
        while (std::chrono::steady_clock::now() < switched_deadline) {
            switched_frame = renderer->latest_frame(
                solin::media_engine::OutputBus::media_windows);
            if (switched_frame != nullptr) {
                break;
            }
            std::this_thread::sleep_for(5ms);
        }
        expect(switched_frame != nullptr &&
                   switched_frame->sequence > later_frame->sequence,
               "the first frame after a cut advances the bus sequence immediately");

        auto previous_sequence =
            switched_frame == nullptr ? 0U : switched_frame->sequence;
        std::string current_scene{"scene-leaf"};
        std::uint64_t mutation_sequence = 4U;
        bool every_return_cut_produced = true;
        for (std::uint32_t index = 0U; index < 20U; ++index) {
            const std::string target_scene =
                current_scene == "scene-leaf" ? "scene-root" : "scene-leaf";
            const auto request_id = "warm-return-" + std::to_string(index);
            const auto prepared = graph.prepare(
                solin::media_engine::OutputBus::media_windows, target_scene, 1U,
                request_id, mutation_sequence++);
            graph.take(prepared, "cut", 0U, 1U, mutation_sequence++);
            const auto return_deadline = std::chrono::steady_clock::now() + 1s;
            std::shared_ptr<const solin::media_engine::SourceFrame> returned;
            while (std::chrono::steady_clock::now() < return_deadline) {
                returned = renderer->latest_frame(
                    solin::media_engine::OutputBus::media_windows);
                if (returned != nullptr && returned->sequence > previous_sequence) {
                    break;
                }
                std::this_thread::sleep_for(2ms);
            }
            if (returned == nullptr || returned->sequence <= previous_sequence) {
                every_return_cut_produced = false;
                break;
            }
            previous_sequence = returned->sequence;
            current_scene = target_scene;
        }
        expect(every_return_cut_produced,
               "repeated return cuts keep every retained compositor graph producing");
        enable_virtual_camera_sequence = mutation_sequence;
    }
    expect(renderer->latest_frame(
               solin::media_engine::OutputBus::virtual_camera) == nullptr,
           "a disabled output bus does not publish its prepared renderer frame");
    graph.set_output_enabled(
        solin::media_engine::OutputBus::virtual_camera, true, 1U,
        enable_virtual_camera_sequence);
    std::optional<solin::media_engine::PackedVideoFrame> virtual_camera_frame;
    std::uint64_t virtual_camera_sequence = 0U;
    const auto camera_deadline = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < camera_deadline) {
        if (const auto sequence = renderer->visit_latest_frame(
                solin::media_engine::OutputBus::virtual_camera,
                virtual_camera_sequence,
                [&virtual_camera_frame](
                    const solin::media_engine::VideoFrameView& frame) {
                    virtual_camera_frame = solin::media_engine::copy_video_frame(frame);
                });
            sequence.has_value()) {
            virtual_camera_sequence = sequence.value();
        }
        if (virtual_camera_frame.has_value()) {
            break;
        }
        std::this_thread::sleep_for(10ms);
    }
    expect(virtual_camera_frame.has_value(),
           "enabling a prepared bus exposes its independently rendered frame");
    expect(virtual_camera_frame.has_value() &&
               virtual_camera_frame->pixel_format ==
                   solin::media_engine::VideoFramePixelFormat::nv12 &&
               virtual_camera_frame->plane_strides[0] == 640U &&
               virtual_camera_frame->plane_offsets[1] == 640U * 360U &&
               virtual_camera_frame->bytes.size() == 640U * 360U * 3U / 2U,
           "the virtual-camera branch downloads one tightly packed NV12 frame");
    if (virtual_camera_frame.has_value()) {
        auto sink = solin::media_engine::make_shared_frame_virtual_camera_sink(
            {
                .camera_id = "test-camera",
                .friendly_name = "Solin Test Camera",
                .video_format = compositor_snapshot().outputs[1].video_format,
            },
            1U);
        sink->start();
        const auto endpoint = sink->endpoint();
        auto reader = solin::media_engine::make_shared_video_frame_reader(
            endpoint.process_local_mapping_handle, endpoint.mapping_size);
        expect(sink->publish(solin::media_engine::video_frame_view(
                                 virtual_camera_frame.value())),
               "the packed compositor frame reaches the virtual-camera sink");
        solin::media_engine::PackedVideoFrame transported;
        expect(reader->read_latest(transported) &&
                   transported.bytes == virtual_camera_frame->bytes &&
                   transported.presentation_timestamp_ns ==
                       virtual_camera_frame->presentation_timestamp_ns,
               "virtual-camera transport preserves the rendered NV12 frame and timing");
    }
}

void test_frame_payload_outlives_media_runtime_and_registry_owners() {
    solin::media_engine::GStreamerSampleLease sample;
    {
        std::unique_ptr<solin::media_engine::SourceRegistry> registry;
        std::optional<solin::media_engine::SourceLease> lease;
        {
            solin::media_engine::MediaRuntime media_runtime;
            const auto probe = media_runtime.initialize();
            expect(probe.initialized,
                   "the lifetime test initializes an independent media runtime");
            if (!probe.initialized) {
                return;
            }
            registry = std::make_unique<solin::media_engine::SourceRegistry>(
                media_runtime.source_runtime_factory());
            registry->replace_snapshot(color_snapshot());
            lease.emplace(registry->acquire("color-1", "lifetime-test"));
            sample = solin::media_engine::gstreamer_sample(
                wait_for_frame(lease->runtime(), 5s));
            expect(static_cast<bool>(sample),
                   "the lifetime test captures a native sample lease");
        }
        expect(sample && gst_sample_get_buffer(sample.sample) != nullptr,
               "the sample remains valid after MediaRuntime destruction");
        lease.reset();
        registry.reset();
    }
    expect(sample && gst_sample_get_buffer(sample.sample) != nullptr,
           "the frame payload owns GStreamer after registry destruction");
}

void test_first_hardware_camera_publishes_its_exact_selected_format(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto expected_memory =
        canonical_memory(media_runtime.source_runtime_factory());
    solin::media_engine::LocalCameraSnapshot cameras;
    const auto discovery_deadline = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < discovery_deadline) {
        cameras = media_runtime.local_cameras();
        if (cameras.ready) {
            break;
        }
        std::this_thread::sleep_for(20ms);
    }
    const auto device = std::ranges::find_if(cameras.devices, [](const auto& candidate) {
        return !candidate.software_device && !candidate.formats.empty();
    });
    expect(device != cameras.devices.end(), "a hardware camera with exact caps is available");
    if (device == cameras.devices.end()) {
        return;
    }
    const auto& format = device->formats.front();
    solin::media_engine::SceneHydrationSnapshot snapshot{
        .document_id = "gstreamer-camera-test",
        .document_revision = 1U,
        .sources = {{
            .id = "camera-1",
            .kind = solin::media_engine::SceneSourceKind::local_camera,
            .enabled = true,
            .configuration = solin::media_engine::LocalCameraSourceConfiguration{
                .device_id = device->device_id,
                .width = format.width,
                .height = format.height,
                .fps_numerator = format.fps_numerator,
                .fps_denominator = format.fps_denominator,
                .media_type = format.media_type,
                .pixel_format = format.pixel_format,
            },
        }},
    };
    {
        solin::media_engine::SourceRegistry registry{media_runtime.source_runtime_factory()};
        registry.replace_snapshot(snapshot);
        auto lease = registry.acquire("camera-1", "hardware-camera-test");
        auto second_lease =
            registry.acquire("camera-1", "hardware-camera-test-second-bus");
        expect(&lease.runtime() == &second_lease.runtime(),
               "both output buses share one physical camera runtime");
        const auto frame = wait_for_frame(lease.runtime(), 10s);
        expect(frame != nullptr, "the Media Foundation camera publishes a decoded frame");
        if (frame != nullptr) {
            expect(frame->width == format.width && frame->height == format.height,
                   "the camera pipeline uses the exact selected dimensions");
            expect(frame->pixel_format == "BGRA" &&
                       frame->memory == expected_memory,
                   "the camera frame is normalized once into canonical BGRA");
        }
        const auto health = lease.runtime().health();
        expect(health.status == solin::media_engine::SourceRuntimeStatus::ready &&
                   health.error_code.empty(),
               "the physical camera reaches ready state");
    }

    snapshot.document_revision = 2U;
    snapshot.sources[0].id = "camera-auto";
    snapshot.sources[0].configuration = solin::media_engine::LocalCameraSourceConfiguration{
        .device_id = device->device_id,
    };
    solin::media_engine::SourceRegistry automatic_registry{
        media_runtime.source_runtime_factory()};
    automatic_registry.replace_snapshot(snapshot);
    auto automatic_lease =
        automatic_registry.acquire("camera-auto", "hardware-camera-auto-test");
    const auto automatic_frame = wait_for_frame(automatic_lease.runtime(), 10s);
    expect(automatic_frame != nullptr,
           "the Media Foundation camera publishes with automatic format selection");
    if (automatic_frame != nullptr) {
        expect(automatic_frame->pixel_format == "BGRA" &&
                   automatic_frame->memory == expected_memory,
               "automatic camera selection preserves the canonical memory contract");
        std::this_thread::sleep_for(1s);
        const auto later_automatic_frame = automatic_lease.runtime().latest_frame();
        const auto automatic_health = automatic_lease.runtime().health();
        expect(later_automatic_frame != nullptr &&
                   later_automatic_frame->sequence > automatic_frame->sequence + 5U,
               "automatic camera selection sustains an advancing frame stream");
        expect(automatic_health.status ==
                       solin::media_engine::SourceRuntimeStatus::ready &&
                   automatic_health.error_code.empty(),
               "automatic camera selection remains healthy after its first frame");
    }
}

void test_rtsp_source_decodes_to_the_same_bounded_frame_contract(
    solin::media_engine::MediaRuntime& media_runtime) {
    TestRtspServer server;
    expect(server.ready(), "the isolated RTSP test server starts");
    if (!server.ready()) {
        return;
    }
    solin::media_engine::SceneHydrationSnapshot snapshot{
        .document_id = "gstreamer-rtsp-test",
        .document_revision = 1U,
        .sources = {{
            .id = "rtsp-1",
            .kind = solin::media_engine::SceneSourceKind::rtsp_camera,
            .enabled = true,
            .configuration = solin::media_engine::RtspCameraSourceConfiguration{
                .uri = server.uri(),
                .use_tcp = true,
                .latency_ms = 50U,
            },
        }},
    };
    solin::media_engine::SourceRegistry registry{media_runtime.source_runtime_factory()};
    const auto expected_memory =
        canonical_memory(media_runtime.source_runtime_factory());
    registry.replace_snapshot(snapshot);
    auto lease = registry.acquire("rtsp-1", "rtsp-test");
    const auto frame = wait_for_frame(lease.runtime(), 10s);
    expect(frame != nullptr, "the RTSP pipeline publishes a decoded frame");
    if (frame != nullptr) {
        expect(frame->width == 320U && frame->height == 240U,
               "the RTSP pipeline preserves decoded dimensions");
        expect(frame->pixel_format == "BGRA" &&
                   frame->memory == expected_memory,
               "RTSP frames share the canonical BGRA memory contract");
    }
    const auto health_before_disconnect = lease.runtime().health();
    expect(health_before_disconnect.status ==
               solin::media_engine::SourceRuntimeStatus::ready,
           "the RTSP source reaches ready state");
    if (frame == nullptr) {
        return;
    }

    server.disconnect_clients();
    std::shared_ptr<const solin::media_engine::SourceFrame> recovered_frame;
    solin::media_engine::SourceRuntimeHealth recovered_health;
    const auto reconnect_deadline = std::chrono::steady_clock::now() + 10s;
    while (std::chrono::steady_clock::now() < reconnect_deadline) {
        recovered_frame = lease.runtime().latest_frame();
        recovered_health = lease.runtime().health();
        if (recovered_frame != nullptr &&
            recovered_frame->stream_epoch > frame->stream_epoch &&
            recovered_health.status == solin::media_engine::SourceRuntimeStatus::ready) {
            break;
        }
        std::this_thread::sleep_for(10ms);
    }
    expect(recovered_frame != nullptr &&
               recovered_frame->stream_epoch > frame->stream_epoch,
           "the RTSP source publishes a new epoch after disconnect and reconnect");
    expect(recovered_health.reconnect_count > health_before_disconnect.reconnect_count,
           "RTSP recovery increments the reconnect counter");
    expect(recovered_health.dropped_frames >= health_before_disconnect.dropped_frames,
           "drop diagnostics remain monotonic across RTSP reconnects");
}

void test_forced_system_memory_and_global_d3d11_invalidation(
    solin::media_engine::MediaRuntime& media_runtime) {
    auto forced_system_factory =
        solin::media_engine::make_gstreamer_source_runtime_factory(
            std::make_shared<int>(1), false);
    expect(!solin::media_engine::gstreamer_d3d11_device(forced_system_factory),
           "the deterministic system-memory factory exposes no D3D11 lease");
    solin::media_engine::SourceRegistry system_registry{forced_system_factory};
    system_registry.replace_snapshot(color_snapshot());
    auto system_lease = system_registry.acquire("color-1", "forced-system-test");
    const auto system_frame = wait_for_frame(system_lease.runtime(), 5s);
    expect(system_frame != nullptr &&
               system_frame->memory ==
                   solin::media_engine::SourceFrameMemory::system_memory,
           "the forced fallback publishes canonical system-memory frames");

    const auto shared_factory = media_runtime.source_runtime_factory();
    if (!solin::media_engine::gstreamer_d3d11_device(shared_factory)) {
        return;
    }
    solin::media_engine::SourceRegistry active_registry{shared_factory};
    active_registry.replace_snapshot(color_snapshot());
    auto active_lease = active_registry.acquire("color-1", "active-device-loss-test");
    const auto d3d11_frame = wait_for_frame(active_lease.runtime(), 5s);
    auto retained_sample = solin::media_engine::gstreamer_sample(d3d11_frame);
    expect(d3d11_frame != nullptr, "the device-loss test publishes an initial frame");
    if (d3d11_frame == nullptr) {
        return;
    }
    if (d3d11_frame->memory != solin::media_engine::SourceFrameMemory::d3d11) {
        expect(!solin::media_engine::gstreamer_d3d11_device(shared_factory) &&
                   !media_runtime.initialize().d3d11_compositor,
               "an organic D3D11 pipeline failure globally reconciles capabilities");
        return;
    }
    expect(solin::media_engine::invalidate_gstreamer_d3d11_device(shared_factory),
           "device loss invalidates the shared D3D11 manager once");
    expect(!solin::media_engine::gstreamer_d3d11_device(shared_factory) &&
               !media_runtime.initialize().d3d11_compositor,
           "factory leases and capability probes agree after D3D11 invalidation");
    std::shared_ptr<const solin::media_engine::SourceFrame> recovered_frame;
    const auto fallback_deadline = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < fallback_deadline) {
        recovered_frame = active_lease.runtime().latest_frame();
        if (recovered_frame != nullptr && d3d11_frame != nullptr &&
            recovered_frame->stream_epoch > d3d11_frame->stream_epoch &&
            recovered_frame->memory ==
                solin::media_engine::SourceFrameMemory::system_memory) {
            break;
        }
        std::this_thread::sleep_for(10ms);
    }
    expect(recovered_frame != nullptr && d3d11_frame != nullptr &&
               recovered_frame->stream_epoch > d3d11_frame->stream_epoch &&
               recovered_frame->memory ==
                   solin::media_engine::SourceFrameMemory::system_memory,
           "an active runtime reconnects through system memory after device loss");
    expect(retained_sample && gst_sample_get_buffer(retained_sample.sample) != nullptr,
           "an in-flight frame safely retains its old D3D11 device after invalidation");
    solin::media_engine::SourceRegistry invalidated_registry{shared_factory};
    invalidated_registry.replace_snapshot(color_snapshot());
    auto invalidated_lease =
        invalidated_registry.acquire("color-1", "invalidated-device-test");
    const auto invalidated_frame = wait_for_frame(invalidated_lease.runtime(), 5s);
    expect(invalidated_frame != nullptr &&
               invalidated_frame->memory ==
                   solin::media_engine::SourceFrameMemory::system_memory,
           "new runtimes stop retrying a globally invalidated D3D11 device");
}

} // namespace

int main(const int argc, const char* const argv[]) {
    try {
    test_automatic_camera_format_is_bounded_and_deterministic();
    {
        solin::media_engine::MediaRuntime media_runtime;
        const auto probe = media_runtime.initialize();
        expect(probe.initialized, "the pinned GStreamer runtime initializes");
        if (probe.initialized) {
            test_color_source_publishes_bounded_latest_d3d11_frames(media_runtime);
            test_rtsp_source_decodes_to_the_same_bounded_frame_contract(media_runtime);
            if (probe.d3d11_compositor) {
                test_scene_renderer_composes_nested_scene_to_shared_d3d11_frame(
                    media_runtime);
            }
            if (argc == 2 && std::string_view{argv[1]} == "--local-camera") {
                test_first_hardware_camera_publishes_its_exact_selected_format(media_runtime);
            }
            test_forced_system_memory_and_global_d3d11_invalidation(media_runtime);
        }
    }
    test_frame_payload_outlives_media_runtime_and_registry_owners();
    if (failures != 0) {
        std::cerr << failures << " GStreamer source runtime test(s) failed\n";
        return 1;
    }
    return 0;
    } catch (const std::exception& error) {
        std::cerr << "UNCAUGHT: " << error.what() << '\n';
        return 2;
    } catch (...) {
        std::cerr << "UNCAUGHT: unknown exception\n";
        return 2;
    }
}
