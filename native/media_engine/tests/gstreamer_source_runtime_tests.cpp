#include "solin/media_engine/media_runtime.hpp"
#include "solin/media_engine/scene_graph.hpp"
#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/shared_video_frame_channel.hpp"
#include "solin/media_engine/virtual_camera.hpp"
#include "gstreamer_error_diagnostics.hpp"
#include "gstreamer_frame_transition.hpp"
#include "gstreamer_source_runtime.hpp"

#include <gst/d3d11/gstd3d11memory.h>
#include <gst/gst.h>
#include <gst/rtsp-server/rtsp-server.h>
#include <gst/video/video-frame.h>

#include <algorithm>
#include <chrono>
#include <array>
#include <cstddef>
#include <cstdint>
#include <condition_variable>
#include <exception>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <ranges>
#include <stop_token>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

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

void test_gstreamer_diagnostics_extract_only_the_native_hresult() {
    expect(solin::media_engine::extract_hresult_code(
               "Activation failed for a private device path: 0x80070005") ==
               "0x80070005",
           "camera diagnostics retain a bounded HRESULT");
    expect(solin::media_engine::extract_hresult_code(
               "Activation failed with 0XaBcDeF12") == "0xABCDEF12",
           "camera diagnostics normalize HRESULT casing");
    expect(solin::media_engine::extract_hresult_code(
               "No native code; private path remains redacted")
               .empty(),
           "camera diagnostics do not retain raw GStreamer messages");
    expect(solin::media_engine::extract_hresult_code("Not an HRESULT: 0x80070005ABC")
               .empty(),
           "camera diagnostics reject oversized hexadecimal values");
}

void test_local_camera_caps_preserve_exact_native_frame_rates() {
    for (const auto& [numerator, denominator] :
         {std::pair{10'000'000U, 333'333U}, std::pair{10'000'000U, 166'667U},
          std::pair{2'147'483'647U, 143'165'577U},
          std::pair{2'147'483'647U, 35'791'395U}, std::pair{1U, 2'147'483'647U}}) {
        try {
            const std::unique_ptr<GstCaps, decltype(&gst_caps_unref)> caps{
                solin::media_engine::gstreamer_local_camera_caps({
                    .width = 1'920U,
                    .height = 1'080U,
                    .fps_numerator = numerator,
                    .fps_denominator = denominator,
                    .media_type = "image/jpeg",
                    .pixel_format = "JPEG",
                }),
                &gst_caps_unref,
            };
            gint actual_numerator = 0;
            gint actual_denominator = 0;
            const auto* structure = gst_caps_get_structure(caps.get(), 0U);
            expect(gst_structure_get_fraction(structure, "framerate", &actual_numerator,
                                              &actual_denominator) != FALSE &&
                       actual_numerator == static_cast<gint>(numerator) &&
                       actual_denominator == static_cast<gint>(denominator),
                   "camera capture caps retain the exact selected fraction without approximation");
        } catch (const std::exception&) {
            expect(false, "capture accepts bounded exact native frame rate components");
        }
    }
}

void test_local_camera_caps_reject_invalid_native_frame_rates() {
    for (const auto& [numerator, denominator] :
         {std::pair{2'147'483'648U, 2'147'483'647U}, std::pair{1U, 2'147'483'648U},
          std::pair{20'000'000U, 666'666U}, std::pair{10'000'000U, 166'666U},
          std::pair{10'000'000U, 0U}, std::pair{0U, 1U}}) {
        try {
            const std::unique_ptr<GstCaps, decltype(&gst_caps_unref)> caps{
                solin::media_engine::gstreamer_local_camera_caps({
                    .width = 1'920U,
                    .height = 1'080U,
                    .fps_numerator = numerator,
                    .fps_denominator = denominator,
                    .media_type = "image/jpeg",
                    .pixel_format = "JPEG",
                }),
                &gst_caps_unref,
            };
            expect(false, "invalid camera rates are rejected before creating GStreamer caps");
        } catch (const std::exception& error) {
            expect(std::string_view{error.what()} == "local_camera_format_unsupported",
                   "invalid capture fractions produce a stable diagnostic");
        }
    }
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

class FailedSourceRuntime final : public solin::media_engine::SourceRuntime {
  public:
    void start() override {}
    void stop() noexcept override {}

    [[nodiscard]] solin::media_engine::SourceRuntimeHealth health() const override {
        return {
            .status = solin::media_engine::SourceRuntimeStatus::failed,
            .error_code = "local_camera_stream_failed",
        };
    }
};

class SelectiveFailureFactory final : public solin::media_engine::SourceRuntimeFactory {
  public:
    explicit SelectiveFailureFactory(
        std::shared_ptr<solin::media_engine::SourceRuntimeFactory> delegate)
        : delegate_(std::move(delegate)) {}

    [[nodiscard]] std::shared_ptr<solin::media_engine::SourceRuntime>
    create(const solin::media_engine::SceneSource& source,
           const std::uint64_t generation) override {
        if (source.id == "failed-camera") {
            return std::make_shared<FailedSourceRuntime>();
        }
        return delegate_->create(source, generation);
    }

  private:
    std::shared_ptr<solin::media_engine::SourceRuntimeFactory> delegate_{};
};

class GatedSourceRuntime final : public solin::media_engine::SourceRuntime {
  public:
    GatedSourceRuntime(
        std::shared_ptr<solin::media_engine::SourceRuntime> delegate,
        std::shared_ptr<solin::media_engine::GStreamerFrameSignal> frame_signal)
        : delegate_(std::move(delegate)), frame_signal_(std::move(frame_signal)) {}

    void start() override { delegate_->start(); }
    void stop() noexcept override { delegate_->stop(); }

    [[nodiscard]] solin::media_engine::SourceRuntimeHealth health() const override {
        if (!released_.load()) {
            return {.status = solin::media_engine::SourceRuntimeStatus::starting};
        }
        return delegate_->health();
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame() const override {
        return released_.load() ? delegate_->latest_frame() : nullptr;
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        return released_.load() &&
               delegate_->wait_for_frame(after_sequence, stop_token, deadline);
    }

    void wake_frame_waiters() noexcept override { delegate_->wake_frame_waiters(); }

    void release() noexcept {
        released_.store(true);
        frame_signal_->notify();
    }

  private:
    std::shared_ptr<solin::media_engine::SourceRuntime> delegate_{};
    std::shared_ptr<solin::media_engine::GStreamerFrameSignal> frame_signal_{};
    std::atomic_bool released_{false};
};

class StartupGateFactory final : public solin::media_engine::SourceRuntimeFactory {
  public:
    explicit StartupGateFactory(
        std::shared_ptr<solin::media_engine::SourceRuntimeFactory> delegate)
        : delegate_(std::move(delegate)),
          frame_signal_(solin::media_engine::gstreamer_frame_signal(delegate_)) {}

    [[nodiscard]] std::shared_ptr<solin::media_engine::SourceRuntime>
    create(const solin::media_engine::SceneSource& source,
           const std::uint64_t generation) override {
        auto runtime = delegate_->create(source, generation);
        if (source.id != "startup-camera") {
            return runtime;
        }
        auto gated = std::make_shared<GatedSourceRuntime>(std::move(runtime), frame_signal_);
        camera_ = gated;
        return gated;
    }

    [[nodiscard]] std::shared_ptr<GatedSourceRuntime> camera() const noexcept {
        return camera_.lock();
    }

  private:
    std::shared_ptr<solin::media_engine::SourceRuntimeFactory> delegate_{};
    std::shared_ptr<solin::media_engine::GStreamerFrameSignal> frame_signal_{};
    std::weak_ptr<GatedSourceRuntime> camera_{};
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
    auto precise = device.formats[2];
    precise.fps_numerator = 2'147'483'647U;
    precise.fps_denominator = 143'165'577U;
    auto faster = precise;
    faster.fps_numerator = 60U;
    faster.fps_denominator = 1U;
    const auto precise_choice = solin::media_engine::preferred_automatic_camera_format(
        {.formats = {faster, precise}});
    expect(precise_choice == precise,
           "automatic FPS budget comparison does not overflow large exact denominators");
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
            .video_format = format,
        },
        solin::media_engine::SceneOutputDefinition{
            .bus = solin::media_engine::OutputBus::virtual_camera,
            .default_scene_id = "scene-root",
            .video_format = virtual_camera_format,
        },
    };
    value.active_scene_ids = {"scene-root", "scene-root"};
    value.render_enabled = {true, false};
    value.output_enabled = {true, false};
    return value;
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot
compositor_with_failed_camera_snapshot() {
    auto value = compositor_snapshot();
    value.document_id = "gstreamer-degraded-compositor-test";
    value.sources.push_back({
        .id = "failed-camera",
        .kind = solin::media_engine::SceneSourceKind::local_camera,
        .enabled = true,
        .configuration = solin::media_engine::LocalCameraSourceConfiguration{
            .device_id = "blocked-camera",
        },
    });
    auto root = std::ranges::find(value.scenes, "scene-root",
                                  &solin::media_engine::SceneGraphDefinition::id);
    if (root != value.scenes.end()) {
        root->layers.push_back({
            .id = "failed-camera-layer",
            .source_id = "failed-camera",
            .geometry = {
                .x = 0.7,
                .y = 0.7,
                .width = 0.25,
                .height = 0.25,
                .opacity = 1.0,
                .fit_mode = "cover",
                .border_color = "#00000000",
                .visible = true,
            },
        });
    }
    return value;
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot
startup_deferred_camera_snapshot() {
    auto value = compositor_snapshot();
    value.document_id = "gstreamer-startup-program-cadence-test";
    value.sources = {
        {.id = "startup-content",
         .kind = solin::media_engine::SceneSourceKind::color,
         .enabled = true,
         .configuration =
             solin::media_engine::ColorSourceConfiguration{"#FF0000FF"}},
        {.id = "startup-camera",
         .kind = solin::media_engine::SceneSourceKind::color,
         .enabled = true,
         .configuration =
             solin::media_engine::ColorSourceConfiguration{"#0000FFFF"}},
    };
    const solin::media_engine::SceneLayerGeometry full{
        .x = 0.0,
        .y = 0.0,
        .width = 1.0,
        .height = 1.0,
        .opacity = 1.0,
        .fit_mode = "stretch",
        .border_color = "#00000000",
        .visible = true,
    };
    auto inset = full;
    inset.x = 0.7;
    inset.y = 0.7;
    inset.width = 0.25;
    inset.height = 0.25;
    value.scenes = {
        {.id = "startup-camera-scene",
         .layers = {{.id = "startup-camera-full",
                     .source_id = "startup-camera",
                     .geometry = full}}},
        {.id = "startup-media-scene",
         .layers = {
             {.id = "startup-content-full",
              .source_id = "startup-content",
              .geometry = full},
             {.id = "startup-camera-inset",
              .source_id = "startup-camera",
              .geometry = inset},
         }},
    };
    value.outputs[0].default_scene_id = "startup-camera-scene";
    value.outputs[1].default_scene_id = "startup-camera-scene";
    value.active_scene_ids = {"startup-camera-scene", "startup-camera-scene"};
    value.render_enabled = {false, true};
    value.output_enabled = {false, true};
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

[[nodiscard]] bool sample_center_is_color(
    GstSample* sample, const std::array<guint8, 4U> expected) {
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
    const bool matches =
        std::equal(expected.begin(), expected.end(), pixel);
    gst_video_frame_unmap(&frame);
    return matches;
}

[[nodiscard]] bool sample_center_is_test_color(GstSample* sample) {
    return sample_center_is_color(sample, {0x99U, 0x66U, 0x33U, 0xFFU});
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

struct CenterNv12Frame final {
    std::uint64_t sequence{0U};
    std::array<std::uint8_t, 3U> yuv{};
    std::vector<std::uint8_t> bytes{};
};

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot transition_snapshot();
[[nodiscard]] std::optional<CenterNv12Frame> wait_for_program_center(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer,
    std::uint64_t after_sequence, std::chrono::milliseconds timeout);
[[nodiscard]] bool near_channel(std::uint8_t value, std::uint8_t expected,
                                std::uint8_t tolerance);
void test_route_change_cannot_be_lost_before_graph_wait_registration(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer);
void test_output_cursor_rebases_when_the_active_graph_changes(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer);
void test_terminal_graph_wait_is_rate_limited(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer);
void test_renderer_hydration_and_output_updates_drive_graph_demand(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer);

void test_program_transitions_render_real_synthetic_frames(
    solin::media_engine::SceneGraphRuntime& graph,
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer) {
    using solin::media_engine::OutputBus;
    using solin::media_engine::SceneTransitionKind;
    constexpr auto transition_duration = 1'200U;
    graph.hydrate(transition_snapshot(), 1'000U);
    const auto red = wait_for_program_center(renderer, 0U, 5s);
    expect(red.has_value() && near_channel(red->yuv[0], 63U, 12U) &&
               near_channel(red->yuv[1], 102U, 15U) &&
               near_channel(red->yuv[2], 240U, 15U),
           "the synthetic Program transition starts on its red scene");
    if (!red.has_value()) {
        return;
    }
    auto last_sequence = red->sequence;

    const auto red_with_pip = graph.prepare(
        OutputBus::virtual_camera, "scene-red-pip", 1U, "dissolve-red-pip",
        1'001U,
        {.kind = SceneTransitionKind::dissolve,
         .duration_ms = transition_duration});
    graph.take(red_with_pip, 1U, 1'002U);
    std::uint8_t minimum_shared_background_luma = 255U;
    std::size_t shared_background_samples = 0U;
    const auto shared_background_deadline =
        std::chrono::steady_clock::now() + 900ms;
    while (std::chrono::steady_clock::now() < shared_background_deadline) {
        const auto frame = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!frame.has_value()) {
            continue;
        }
        last_sequence = frame->sequence;
        minimum_shared_background_luma =
            (std::min)(minimum_shared_background_luma, frame->yuv[0]);
        ++shared_background_samples;
    }
    expect(shared_background_samples >= 5U,
           "Dissolve publishes enough shared-background frames for continuity checks");
    expect(minimum_shared_background_luma >= 58U,
           "Dissolve preserves an unchanged scene background while a PiP layer appears");
    std::this_thread::sleep_for(400ms);
    const auto stable_red_with_pip =
        wait_for_program_center(renderer, last_sequence, 1s);
    expect(stable_red_with_pip.has_value() &&
               near_channel(stable_red_with_pip->yuv[0], 63U, 12U),
           "the PiP destination retains the shared red background");
    if (!stable_red_with_pip.has_value()) {
        return;
    }
    last_sequence = stable_red_with_pip->sequence;

    const auto dissolve = graph.prepare(
        OutputBus::virtual_camera, "scene-blue", 1U, "dissolve-blue", 1'003U,
        {.kind = SceneTransitionKind::dissolve,
         .duration_ms = transition_duration});
    expect(dissolve.effective_transition.kind == SceneTransitionKind::dissolve &&
               !dissolve.fallback_applied,
           "the D3D11 renderer prepares Dissolve without a CUT fallback");
    graph.take(dissolve, 1U, 1'004U);
    std::optional<CenterNv12Frame> purple;
    std::uint32_t purple_distance = 1'000U;
    const auto dissolve_midpoint_deadline =
        std::chrono::steady_clock::now() + 900ms;
    while (std::chrono::steady_clock::now() < dissolve_midpoint_deadline) {
        const auto frame = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!frame.has_value()) {
            continue;
        }
        last_sequence = frame->sequence;
        const auto distance =
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[0]) - 48)) +
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[1]) - 171)) +
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[2]) - 179));
        if (distance < purple_distance) {
            purple_distance = distance;
            purple = frame;
        }
    }
    expect(purple.has_value() && near_channel(purple->yuv[0], 48U, 18U) &&
               near_channel(purple->yuv[1], 171U, 24U) &&
               near_channel(purple->yuv[2], 179U, 24U),
           "Dissolve produces the expected real red/blue midpoint on Program");
    std::this_thread::sleep_for(400ms);
    const auto blue = wait_for_program_center(renderer, last_sequence, 1s);
    expect(blue.has_value() && near_channel(blue->yuv[0], 32U, 12U) &&
               near_channel(blue->yuv[1], 240U, 15U) &&
               near_channel(blue->yuv[2], 118U, 15U),
           "Dissolve converges to the exact blue destination stream");
    if (!blue.has_value()) {
        return;
    }

    const auto fade = graph.prepare(
        OutputBus::virtual_camera, "scene-red", 1U, "fade-red", 1'005U,
        {.kind = SceneTransitionKind::fade_to_black,
         .duration_ms = transition_duration});
    expect(fade.effective_transition.kind == SceneTransitionKind::fade_to_black &&
               !fade.fallback_applied,
           "the D3D11 renderer prepares Fade through black without fallback");
    graph.take(fade, 1U, 1'006U);
    last_sequence = blue->sequence;
    std::optional<CenterNv12Frame> black;
    std::uint32_t black_distance = 1'000U;
    const auto fade_midpoint_deadline = std::chrono::steady_clock::now() + 900ms;
    while (std::chrono::steady_clock::now() < fade_midpoint_deadline) {
        const auto frame = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!frame.has_value()) {
            continue;
        }
        last_sequence = frame->sequence;
        const auto distance =
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[0]) - 16)) +
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[1]) - 128)) +
            static_cast<std::uint32_t>(std::abs(static_cast<int>(frame->yuv[2]) - 128));
        if (distance < black_distance) {
            black_distance = distance;
            black = frame;
        }
    }
    expect(black.has_value() && near_channel(black->yuv[0], 16U, 10U) &&
               near_channel(black->yuv[1], 128U, 12U) &&
               near_channel(black->yuv[2], 128U, 12U),
           "Fade through black produces a real black Program midpoint");
    std::this_thread::sleep_for(400ms);
    std::optional<CenterNv12Frame> final_red;
    const auto final_red_deadline = std::chrono::steady_clock::now() + 1s;
    while (std::chrono::steady_clock::now() < final_red_deadline) {
        const auto candidate = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!candidate.has_value()) {
            continue;
        }
        last_sequence = candidate->sequence;
        final_red = candidate;
        if (candidate->bytes == red->bytes) {
            break;
        }
    }
    expect(final_red.has_value() && final_red->bytes == red->bytes,
           "the final Program frame is byte-identical to the red destination");
    if (!final_red.has_value()) {
        return;
    }

    last_sequence = final_red->sequence;
    const auto prewarmed = graph.prepare(
        OutputBus::virtual_camera, "scene-blue", 1U, "prewarmed-blue", 1'007U,
        {.kind = SceneTransitionKind::dissolve, .duration_ms = 50U});
    // Preparation is intentionally allowed to run before Take. This is the real
    // control contract and exposes output gates that discard their sticky caps
    // while the prepared transition is already producing frames.
    std::this_thread::sleep_for(250ms);
    graph.take(prewarmed, 1U, 1'008U);
    std::optional<CenterNv12Frame> prewarmed_blue;
    const auto prewarmed_deadline = std::chrono::steady_clock::now() + 1s;
    while (std::chrono::steady_clock::now() < prewarmed_deadline) {
        const auto candidate = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!candidate.has_value()) {
            continue;
        }
        last_sequence = candidate->sequence;
        if (near_channel(candidate->yuv[0], 32U, 12U) &&
            near_channel(candidate->yuv[1], 240U, 15U) &&
            near_channel(candidate->yuv[2], 118U, 15U)) {
            prewarmed_blue = candidate;
            break;
        }
    }
    expect(prewarmed_blue.has_value(),
           "a prepared Program transition still reaches the virtual camera after a delayed Take");
    if (!prewarmed_blue.has_value()) {
        return;
    }

    const auto returned = graph.prepare(
        OutputBus::virtual_camera, "scene-red", 1U, "return-red", 1'009U,
        {.kind = SceneTransitionKind::dissolve, .duration_ms = 50U});
    graph.take(returned, 1U, 1'010U);
    std::optional<CenterNv12Frame> returned_red;
    const auto returned_deadline = std::chrono::steady_clock::now() + 1s;
    while (std::chrono::steady_clock::now() < returned_deadline) {
        const auto candidate = wait_for_program_center(renderer, last_sequence, 50ms);
        if (!candidate.has_value()) {
            continue;
        }
        last_sequence = candidate->sequence;
        if (near_channel(candidate->yuv[0], 63U, 12U) &&
            near_channel(candidate->yuv[1], 102U, 15U) &&
            near_channel(candidate->yuv[2], 240U, 15U)) {
            returned_red = candidate;
            break;
        }
    }
    expect(returned_red.has_value(),
           "the Program remains routable after the delayed prepared transition");
    if (!returned_red.has_value()) {
        return;
    }
    last_sequence = returned_red->sequence;

    std::uint8_t minimum_dissolve_luma = 255U;
    std::size_t sampled_frames = 0U;
    bool every_short_transition_published_a_blended_frame = true;
    bool every_short_transition_reached_its_target = true;
    std::vector<std::uint64_t> missed_short_transition_indices;
    std::vector<std::uint64_t> stalled_short_transition_indices;
    for (std::uint64_t index = 0U; index < 20U; ++index) {
        const auto target = index % 2U == 0U ? "scene-blue" : "scene-red";
        const auto prepare_sequence = 2'000U + index * 2U;
        const auto prepared = graph.prepare(
            OutputBus::virtual_camera, target, 1U,
            "continuity-" + std::to_string(index), prepare_sequence,
            {.kind = SceneTransitionKind::dissolve, .duration_ms = 50U});
        graph.take(prepared, 1U, prepare_sequence + 1U);
        if (index == 0U) {
            // A latest-frame consumer can be descheduled beyond the minimum
            // transition duration. The compositor must retain a body frame
            // until the required Program egress has observed it.
            std::this_thread::sleep_for(250ms);
        }
        bool published_blended_frame = false;
        bool reached_target = false;
        // The correctness contract is causal ordering: a required Program
        // consumer must observe a body frame before the destination endpoint.
        // Keep only a generous deadlock watchdog here; runner scheduling is not
        // part of the transition's requested 50 ms presentation duration.
        const auto deadline = std::chrono::steady_clock::now() + 2s;
        while (std::chrono::steady_clock::now() < deadline) {
            const auto frame = wait_for_program_center(renderer, last_sequence, 100ms);
            if (!frame.has_value()) {
                continue;
            }
            last_sequence = frame->sequence;
            minimum_dissolve_luma =
                (std::min)(minimum_dissolve_luma, frame->yuv[0]);
            const bool red_endpoint =
                near_channel(frame->yuv[0], 63U, 5U) &&
                near_channel(frame->yuv[1], 102U, 8U) &&
                near_channel(frame->yuv[2], 240U, 8U);
            const bool blue_endpoint =
                near_channel(frame->yuv[0], 32U, 5U) &&
                near_channel(frame->yuv[1], 240U, 8U) &&
                near_channel(frame->yuv[2], 118U, 8U);
            published_blended_frame =
                published_blended_frame || (!red_endpoint && !blue_endpoint);
            ++sampled_frames;
            const bool target_endpoint =
                target == std::string_view{"scene-blue"}
                    ? blue_endpoint
                    : red_endpoint;
            if (target_endpoint) {
                reached_target = true;
                break;
            }
        }
        every_short_transition_published_a_blended_frame =
            every_short_transition_published_a_blended_frame &&
            published_blended_frame;
        every_short_transition_reached_its_target =
            every_short_transition_reached_its_target && reached_target;
        if (!published_blended_frame) {
            missed_short_transition_indices.push_back(index);
        }
        if (!reached_target) {
            stalled_short_transition_indices.push_back(index);
        }
    }
    expect(sampled_frames >= 40U,
           "rapid real transitions expose enough Program frames for continuity checks");
    if (!every_short_transition_published_a_blended_frame) {
        std::cerr << "Missed minimum-duration transition indices:";
        for (const auto index : missed_short_transition_indices) {
            std::cerr << ' ' << index;
        }
        std::cerr << '\n';
    }
    expect(every_short_transition_published_a_blended_frame,
           "every minimum-duration Program transition publishes a blended NV12 frame");
    if (!every_short_transition_reached_its_target) {
        std::cerr << "Stalled minimum-duration transition indices:";
        for (const auto index : stalled_short_transition_indices) {
            std::cerr << ' ' << index;
        }
        std::cerr << '\n';
    }
    expect(every_short_transition_reached_its_target,
           "every minimum-duration Program transition reaches its requested endpoint");
    expect(minimum_dissolve_luma >= 24U,
           "Dissolve never publishes a transient black/blank Program frame");
}

void test_scene_renderer_composes_nested_scene_to_shared_d3d11_frame(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto renderer = media_runtime.scene_renderer();
    expect(renderer != nullptr, "the D3D11 probe exposes a scene renderer");
    if (renderer == nullptr) {
        return;
    }
    test_renderer_hydration_and_output_updates_drive_graph_demand(renderer);
    test_route_change_cannot_be_lost_before_graph_wait_registration(renderer);
    test_output_cursor_rebases_when_the_active_graph_changes(renderer);
    test_terminal_graph_wait_is_rate_limited(renderer);
    renderer->set_system_memory_output_enabled(
        solin::media_engine::OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel,
        true);
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
        graph.take(preparation, 1U, 3U);
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
            graph.take(prepared, 1U, mutation_sequence++);
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
    solin::media_engine::SceneOutputFrameCursor virtual_camera_cursor{};
    const auto camera_deadline = std::chrono::steady_clock::now() + 5s;
    while (std::chrono::steady_clock::now() < camera_deadline) {
        if (const auto sequence = renderer->visit_latest_frame(
                solin::media_engine::OutputBus::virtual_camera,
                virtual_camera_cursor,
                [&virtual_camera_frame](
                    const solin::media_engine::VideoFrameView& frame) {
                    virtual_camera_frame = solin::media_engine::copy_video_frame(frame);
                });
            sequence.has_value()) {
            virtual_camera_cursor = sequence.value();
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
    test_program_transitions_render_real_synthetic_frames(graph, renderer);
    renderer->set_system_memory_output_enabled(
        solin::media_engine::OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel,
        false);
}

void test_failed_source_does_not_block_healthy_compositor_layers(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto renderer = media_runtime.scene_renderer();
    expect(renderer != nullptr, "the degraded-source test has a scene renderer");
    if (renderer == nullptr) {
        return;
    }
    auto factory = std::make_shared<SelectiveFailureFactory>(
        media_runtime.source_runtime_factory());
    solin::media_engine::SceneGraphRuntime graph{factory, renderer};
    graph.hydrate(compositor_with_failed_camera_snapshot(), 1U);

    std::shared_ptr<const solin::media_engine::SourceFrame> frame;
    bool rendered_healthy_source = false;
    const auto deadline = std::chrono::steady_clock::now() + 3s;
    while (std::chrono::steady_clock::now() < deadline) {
        frame = renderer->latest_frame(solin::media_engine::OutputBus::media_windows);
        const auto sample = solin::media_engine::gstreamer_sample(frame);
        if (sample && sample_center_is_test_color(sample.sample)) {
            rendered_healthy_source = true;
            break;
        }
        std::this_thread::sleep_for(10ms);
    }
    expect(frame != nullptr && rendered_healthy_source,
           "a source that fails before its first frame does not hold back healthy layers");
}

void test_program_cadence_recovers_when_media_arrives_before_the_startup_camera(
    solin::media_engine::MediaRuntime& media_runtime) {
    using solin::media_engine::OutputBus;
    using solin::media_engine::SceneTransitionKind;

    const auto renderer = media_runtime.scene_renderer();
    expect(renderer != nullptr,
           "the deferred startup cadence test has a scene renderer");
    if (renderer == nullptr) {
        return;
    }

    auto factory = std::make_shared<StartupGateFactory>(
        media_runtime.source_runtime_factory());
    solin::media_engine::SceneGraphRuntime graph{factory, renderer};
    graph.hydrate(startup_deferred_camera_snapshot(), 10'000U);

    const auto camera = factory->camera();
    expect(camera != nullptr,
           "the startup fixture gates the camera source before its first frame");
    expect(renderer->latest_frame(OutputBus::virtual_camera) == nullptr,
           "Program remains deferred while its initial camera has no frame");
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel, true);
    if (camera == nullptr) {
        renderer->set_system_memory_output_enabled(
            OutputBus::virtual_camera,
            solin::media_engine::SystemMemoryOutputConsumer::frame_channel, false);
        return;
    }

    const auto prepared = graph.prepare(
        OutputBus::virtual_camera, "startup-media-scene", 1U,
        "startup-media-before-camera", 10'001U,
        {.kind = SceneTransitionKind::fade_to_black, .duration_ms = 200U});
    graph.take(prepared, 1U, 10'002U);
    std::this_thread::sleep_for(3'500ms);
    camera->release();

    solin::media_engine::SceneOutputFrameCursor cursor{};
    std::size_t frame_count = 0U;
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    while (std::chrono::steady_clock::now() < deadline) {
        const auto sequence = renderer->visit_latest_frame(
            OutputBus::virtual_camera, cursor,
            [](const solin::media_engine::VideoFrameView&) {});
        if (sequence.has_value()) {
            cursor = sequence.value();
            ++frame_count;
        }
        static_cast<void>(renderer->wait_for_frame(
            OutputBus::virtual_camera, cursor, std::stop_token{},
            (std::min)(deadline, std::chrono::steady_clock::now() + 20ms)));
    }
    expect(frame_count >= 45U,
           "Program recovers its negotiated cadence when media starts before the camera's first frame");
    expect(frame_count <= 75U,
           "Program coalesces simultaneous source revisions to its negotiated cadence");

    graph.shutdown();
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel, false);
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

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot transition_snapshot() {
    auto value = compositor_snapshot();
    value.document_id = "gstreamer-transition-test";
    value.sources = {
        {.id = "color-red",
         .kind = solin::media_engine::SceneSourceKind::color,
         .enabled = true,
         .configuration = solin::media_engine::ColorSourceConfiguration{"#FF0000FF"}},
        {.id = "color-blue",
         .kind = solin::media_engine::SceneSourceKind::color,
         .enabled = true,
         .configuration = solin::media_engine::ColorSourceConfiguration{"#0000FFFF"}},
    };
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
    auto pip_geometry = geometry;
    pip_geometry.x = 0.72;
    pip_geometry.y = 0.72;
    pip_geometry.width = 0.24;
    pip_geometry.height = 0.24;
    value.scenes = {
        {.id = "scene-red",
         .layers = {{.id = "red-layer",
                     .source_id = "color-red",
                     .geometry = geometry}}},
        {.id = "scene-blue",
         .layers = {{.id = "blue-layer",
                     .source_id = "color-blue",
                     .geometry = geometry}}},
        {.id = "scene-red-pip",
         .layers = {
             {.id = "pip-background-layer",
              .source_id = "color-red",
              .geometry = geometry},
             {.id = "pip-overlay-layer",
              .source_id = "color-blue",
              .geometry = pip_geometry},
         }},
    };
    value.outputs[0].default_scene_id = "scene-red";
    value.outputs[1].default_scene_id = "scene-red";
    value.active_scene_ids = {"scene-red", "scene-red"};
    value.render_enabled = {false, true};
    value.output_enabled = {false, true};
    return value;
}

[[nodiscard]] std::optional<CenterNv12Frame> wait_for_program_center(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer,
    const std::uint64_t after_sequence, const std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
        std::optional<solin::media_engine::PackedVideoFrame> packed;
        const auto sequence = renderer->visit_latest_frame(
            solin::media_engine::OutputBus::virtual_camera,
            solin::media_engine::SceneOutputFrameCursor{
                .frame_sequence = after_sequence,
            },
            [&packed](const solin::media_engine::VideoFrameView& frame) {
                packed = solin::media_engine::copy_video_frame(frame);
            });
        if (sequence.has_value() && packed.has_value() &&
            packed->pixel_format == solin::media_engine::VideoFramePixelFormat::nv12) {
            const auto x = packed->width / 2U;
            const auto y = packed->height / 2U;
            const auto y_index = packed->plane_offsets[0] +
                                 static_cast<std::size_t>(y) *
                                     packed->plane_strides[0] +
                                 x;
            const auto uv_index = packed->plane_offsets[1] +
                                  static_cast<std::size_t>(y / 2U) *
                                      packed->plane_strides[1] +
                                  static_cast<std::size_t>(x / 2U) * 2U;
            if (uv_index + 1U < packed->bytes.size()) {
                return CenterNv12Frame{
                    .sequence = sequence->frame_sequence,
                    .yuv = {packed->bytes[y_index], packed->bytes[uv_index],
                            packed->bytes[uv_index + 1U]},
                    .bytes = std::move(packed->bytes),
                };
            }
        }
        static_cast<void>(renderer->wait_for_frame(
            solin::media_engine::OutputBus::virtual_camera,
            solin::media_engine::SceneOutputFrameCursor{
                .frame_sequence = after_sequence,
            },
            std::stop_token{}, deadline));
    }
    return std::nullopt;
}

class RouteChangeRaceGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    void set_direct_output_enabled(const bool enabled) noexcept override {
        direct_output_enabled = enabled;
        ++direct_output_changes;
    }

    void set_rendering_enabled(const bool enabled) noexcept override {
        rendering_enabled = enabled;
        ++rendering_changes;
    }

    [[nodiscard]] bool wait_for_frame(
        std::uint64_t, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        std::unique_lock lock{mutex_};
        wait_entered_ = true;
        wakeup_.notify_all();
        wakeup_.wait(lock, [this] { return allow_wait_registration_; });
        const auto wake_generation = wake_generation_;
        static_cast<void>(wakeup_.wait_until(
            lock, stop_token, deadline,
            [this, wake_generation] {
                return wake_generation_ != wake_generation;
            }));
        return false;
    }

    void wake_frame_waiters() noexcept override {
        std::scoped_lock lock{mutex_};
        ++wake_generation_;
        wakeup_.notify_all();
    }

    [[nodiscard]] bool wait_until_entered(
        const std::chrono::milliseconds timeout) const {
        std::unique_lock lock{mutex_};
        return wakeup_.wait_for(lock, timeout,
                                [this] { return wait_entered_; });
    }

    void allow_wait_registration() noexcept {
        std::scoped_lock lock{mutex_};
        allow_wait_registration_ = true;
        wakeup_.notify_all();
    }

    bool direct_output_enabled{true};
    bool rendering_enabled{true};
    std::uint64_t direct_output_changes{0U};
    std::uint64_t rendering_changes{0U};

  private:
    mutable std::mutex mutex_{};
    mutable std::condition_variable_any wakeup_{};
    mutable bool wait_entered_{false};
    mutable bool allow_wait_registration_{false};
    mutable std::uint64_t wake_generation_{0U};
};

class FixedFrameGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    FixedFrameGraph(const std::uint64_t sequence, const std::uint8_t value) {
        const auto layout = solin::media_engine::packed_video_frame_layout(
            4U, 2U, solin::media_engine::VideoFramePixelFormat::nv12);
        frame_ = {
            .sequence = sequence,
            .duration_ns = 16'666'667U,
            .width = layout.width,
            .height = layout.height,
            .pixel_format = layout.pixel_format,
            .plane_strides = layout.plane_strides,
            .plane_offsets = layout.plane_offsets,
            .bytes = std::vector<std::uint8_t>(layout.payload_size, value),
        };
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(
        const std::uint64_t after_sequence,
        const solin::media_engine::VideoFrameVisitor& visitor) const noexcept override {
        if (frame_.sequence <= after_sequence) {
            return std::nullopt;
        }
        visitor(solin::media_engine::video_frame_view(frame_));
        return frame_.sequence;
    }

  private:
    solin::media_engine::PackedVideoFrame frame_{};
};

class ImmediateFailureGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    [[nodiscard]] bool wait_for_frame(
        std::uint64_t, std::stop_token,
        std::chrono::steady_clock::time_point) const noexcept override {
        return false;
    }
};

// Hold only the transition's input feeder. GStreamer's streaming threads and
// output clock remain free to render the already selected, retained endpoints.
class TransitionFeederGateGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    explicit TransitionFeederGateGraph(
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> graph)
        : graph_(std::move(graph)) {}

    void arm() {
        std::scoped_lock lock{mutex_};
        armed_ = true;
    }

    void set_direct_output_enabled(const bool enabled) noexcept override {
        std::scoped_lock lock{mutex_};
        if (!enabled && armed_) {
            gated_ = true;
        }
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame() const noexcept override {
        return graph_->latest_frame();
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame() const noexcept override {
        std::unique_lock lock{mutex_};
        if (gated_ && calls_after_take_++ != 0U) {
            blocked_ = true;
            wakeup_.notify_all();
            wakeup_.wait(lock, [this] { return released_; });
        }
        return graph_->latest_gpu_frame();
    }

    [[nodiscard]] bool wait_until_blocked() const {
        std::unique_lock lock{mutex_};
        return wakeup_.wait_for(lock, 500ms, [this] { return blocked_; });
    }

    void release() {
        std::scoped_lock lock{mutex_};
        released_ = true;
        wakeup_.notify_all();
    }

  private:
    std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> graph_;
    mutable std::mutex mutex_{};
    mutable std::condition_variable wakeup_{};
    bool armed_{false};
    bool gated_{false};
    bool released_{false};
    mutable bool blocked_{false};
    mutable unsigned calls_after_take_{0U};
};

void test_program_transition_clock_survives_a_descheduled_feeder(
    solin::media_engine::MediaRuntime& media_runtime) {
    using solin::media_engine::OutputBus;
    const auto renderer = media_runtime.scene_renderer();
    const auto snapshot = transition_snapshot();
    const auto document = solin::media_engine::compile_scene_document(snapshot);
    solin::media_engine::SourceRegistry registry{media_runtime.source_runtime_factory()};
    registry.replace_snapshot(snapshot);
    const auto prepare = [&](const char* scene, const char* source) {
        auto resources =
            std::make_shared<solin::media_engine::SceneRenderResources>();
        resources->source_leases.push_back(registry.acquire(source, scene));
        const auto& lease = resources->source_leases.front();
        expect(wait_for_frame(lease.runtime(), 5s) != nullptr,
               "the gated transition fixture has a retained source frame");
        auto graph = renderer->prepare({
            .bus = OutputBus::virtual_camera,
            .document_revision = snapshot.document_revision,
            .output = snapshot.outputs[1],
            .graph = document->scenes.at(scene),
            .sources = {{.source_id = source,
                         .generation = lease.generation(),
                         .runtime = &lease.runtime()}},
            .resources = std::move(resources),
        });
        graph->set_direct_output_enabled(true);
        expect(graph->wait_for_frame(0U, {}, std::chrono::steady_clock::now() + 1s),
               "the gated transition fixture primes CPU and GPU output");
        return graph;
    };
    const auto red = prepare("scene-red", "color-red");
    const auto blue = prepare("scene-blue", "color-blue");
    auto outgoing = std::make_shared<TransitionFeederGateGraph>(red);
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel, true);
    renderer->commit_hydration({nullptr, outgoing}, {false, true}, 1U);
    const auto transition = renderer->prepare_transition(
        OutputBus::virtual_camera, blue,
        {.kind = solin::media_engine::SceneTransitionKind::dissolve,
         .duration_ms = 50U});
    expect(transition.render_output != nullptr && !transition.fallback_applied,
           "the gated transition prepares the production D3D11 effect");
    if (transition.render_output == nullptr) {
        return;
    }
    transition.render_output->set_direct_output_enabled(true);
    const auto origin = outgoing->latest_frame();
    const auto prime_deadline = std::chrono::steady_clock::now() + 1s;
    while (transition.render_output->latest_frame() == origin &&
           std::chrono::steady_clock::now() < prime_deadline) {
        std::this_thread::sleep_for(1ms);
    }
    const auto primed = transition.render_output->latest_frame();
    expect(primed != nullptr && primed != origin,
           "the gated transition publishes its own pre-Take endpoint");
    if (primed == nullptr || primed == origin) {
        return;
    }
    std::mutex visitor_mutex;
    std::condition_variable_any visitor_wakeup;
    bool endpoint_mapped = false;
    bool release_endpoint = false;
    // This reader maps the origin before Take, then returns after body output
    // has arrived. Its successful visit must acknowledge that origin PTS only.
    std::jthread endpoint_reader{[&](const std::stop_token stop) {
        static_cast<void>(transition.render_output->visit_latest_frame(
            0U, [&](const solin::media_engine::VideoFrameView&) {
                std::unique_lock lock{visitor_mutex};
                endpoint_mapped = true;
                visitor_wakeup.notify_all();
                visitor_wakeup.wait(lock, stop, [&] { return release_endpoint; });
            }));
    }};
    {
        std::unique_lock lock{visitor_mutex};
        expect(visitor_wakeup.wait_for(lock, 1s, [&] { return endpoint_mapped; }),
               "the delayed reader maps the origin before Take");
    }
    outgoing->arm();
    renderer->commit_take(OutputBus::virtual_camera, blue,
                          transition.render_output, 2U);
    expect(outgoing->wait_until_blocked(),
           "the transition feeder is descheduled after its first on-air submission");
    const auto body_deadline = std::chrono::steady_clock::now() + 120ms;
    auto retained = transition.render_output->latest_frame();
    const auto body_timestamp = primed->presentation_timestamp_ns +
        2U * static_cast<std::uint64_t>(
            solin::media_engine::scene_transition_frame_interval(
                snapshot.outputs[1].video_format).count());
    while (retained != nullptr && retained->presentation_timestamp_ns < body_timestamp &&
           std::chrono::steady_clock::now() < body_deadline) {
        static_cast<void>(transition.render_output->wait_for_frame(
            retained->sequence, {}, body_deadline));
        retained = transition.render_output->latest_frame();
    }
    {
        std::scoped_lock lock{visitor_mutex};
        release_endpoint = true;
    }
    visitor_wakeup.notify_all();
    endpoint_reader.join();
    // Let the next output selection react to the delayed endpoint visit before
    // the real Program consumer is allowed to acknowledge a body frame.
    if (retained != nullptr) {
        static_cast<void>(transition.render_output->wait_for_frame(
            retained->sequence, {}, std::chrono::steady_clock::now() + 120ms));
    }
    bool blended = false;
    std::uint64_t cursor = 0U;
    const auto deadline = std::chrono::steady_clock::now() + 120ms;
    while (std::chrono::steady_clock::now() < deadline && !blended) {
        const auto frame = wait_for_program_center(renderer, cursor, 40ms);
        if (frame.has_value()) {
            cursor = frame->sequence;
            blended = near_channel(frame->yuv[1], 171U, 40U) &&
                      near_channel(frame->yuv[2], 179U, 40U);
        }
    }
    // Release before asserting or destroying the renderer, even on failure.
    outgoing->release();
    expect(blended,
           "the Program output clock renders a real blended frame while its feeder is descheduled");
    renderer->shutdown();
    red->stop();
    blue->stop();
}

void test_renderer_hydration_and_output_updates_drive_graph_demand(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer) {
    using solin::media_engine::OutputBus;
    auto preview = std::make_shared<RouteChangeRaceGraph>();
    auto program = std::make_shared<RouteChangeRaceGraph>();

    renderer->commit_hydration({preview, program}, {false, false}, 1U);
    expect(!preview->direct_output_enabled && !preview->rendering_enabled &&
               !program->direct_output_enabled && !program->rendering_enabled,
           "hydration suspends every graph whose aggregate render demand is false");
    expect(preview->direct_output_changes == 1U &&
               preview->rendering_changes == 1U &&
               program->direct_output_changes == 1U &&
               program->rendering_changes == 1U,
           "hydration applies demand exactly once to each committed graph");

    renderer->set_output_enabled(OutputBus::virtual_camera, true, 2U);
    expect(!program->direct_output_enabled && program->rendering_enabled,
           "render demand alone resumes Program without opening CPU egress");
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::virtual_camera,
        true);
    expect(program->direct_output_enabled && program->rendering_enabled,
           "a system-memory consumer opens Program CPU egress on demand");
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel,
        true);
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::virtual_camera,
        false);
    expect(program->direct_output_enabled && program->rendering_enabled,
           "releasing one consumer keeps CPU egress open for the other");
    renderer->set_system_memory_output_enabled(
        OutputBus::virtual_camera,
        solin::media_engine::SystemMemoryOutputConsumer::frame_channel,
        false);
    expect(!program->direct_output_enabled && program->rendering_enabled,
           "releasing the last consumer leaves only GPU Program rendering active");
    renderer->set_output_enabled(OutputBus::virtual_camera, false, 3U);
    expect(!program->direct_output_enabled && !program->rendering_enabled,
           "disabling Program closes its direct output and suspends rendering");
}

void test_ephemeral_transition_graph_requires_causal_output(
    solin::media_engine::MediaRuntime& media_runtime) {
    const auto factory = media_runtime.source_runtime_factory();
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(transition_snapshot());
    auto red_lease =
        registry.acquire("color-red", "transition-readiness-red-test");
    auto blue_lease =
        registry.acquire("color-blue", "transition-readiness-blue-test");
    const auto red = wait_for_frame(red_lease.runtime(), 5s);
    const auto blue = wait_for_frame(blue_lease.runtime(), 5s);
    expect(red != nullptr && blue != nullptr,
           "the transition readiness fixture has two distinct GPU frames");
    if (red == nullptr || blue == nullptr) {
        return;
    }
    for (std::size_t attempt = 0U; attempt < 5U; ++attempt) {
        const auto input_count = static_cast<std::uint8_t>(attempt % 2U + 1U);
        auto device = solin::media_engine::gstreamer_d3d11_device(red);
        const bool d3d11 =
            red->memory == solin::media_engine::SourceFrameMemory::d3d11 &&
            device != nullptr;
        solin::media_engine::GStreamerFrameTransitionPipeline transition{
            d3d11, std::move(device), red->width, red->height, input_count};
        std::uint64_t revision = 0U;
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> output{
            nullptr, &gst_sample_unref};
        std::this_thread::sleep_for(50ms);
        auto unsolicited = transition.output_after(revision);
        expect(unsolicited.sample == nullptr,
               "a fresh ephemeral transition cannot acknowledge itself with a background frame");
        if (unsolicited.sample != nullptr) {
            gst_sample_unref(unsolicited.sample);
        }
        std::uint64_t previous_submission = 0U;
        for (std::size_t submission_index = 0U; submission_index < 6U;
             ++submission_index) {
            const auto& submitted =
                submission_index % 2U == 0U ? red : blue;
            const auto expected =
                submission_index % 2U == 0U
                    ? std::array<guint8, 4U>{0x00U, 0x00U, 0xFFU, 0xFFU}
                    : std::array<guint8, 4U>{0xFFU, 0x00U, 0x00U, 0xFFU};
            output.reset();
            expect(transition.render(
                       submitted, input_count == 2U ? submitted : nullptr,
                       {.outgoing = 1.0, .incoming = 0.0}),
                   "the ephemeral transition accepts one bounded submission");
            const auto awaited_submission = transition.submission();
            expect(awaited_submission > previous_submission,
                   "successive transition submissions have distinct identities");
            const auto deadline = std::chrono::steady_clock::now() + 1s;
            while (std::chrono::steady_clock::now() < deadline && output == nullptr) {
                auto candidate = transition.output_after(revision);
                if (candidate.sample != nullptr) {
                    revision = candidate.revision;
                    if (candidate.submission == awaited_submission) {
                        output.reset(candidate.sample);
                        break;
                    }
                    gst_sample_unref(candidate.sample);
                }
                std::this_thread::sleep_for(16ms);
            }
            expect(output != nullptr,
                   "each submitted input set receives its own causal output acknowledgement");
            expect(output != nullptr &&
                       sample_center_is_color(output.get(), expected),
                   "each acknowledgement carries the pixels from its exact submission");
            previous_submission = awaited_submission;
            std::this_thread::sleep_for(50ms);
        }
    }
}

void test_route_change_cannot_be_lost_before_graph_wait_registration(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer) {
    using solin::media_engine::OutputBus;
    auto outgoing = std::make_shared<RouteChangeRaceGraph>();
    auto incoming = std::make_shared<RouteChangeRaceGraph>();
    renderer->commit_hydration({nullptr, outgoing}, {false, true}, 1U);

    std::chrono::steady_clock::duration elapsed{};
    std::thread waiter{[&] {
        const auto started = std::chrono::steady_clock::now();
        static_cast<void>(renderer->wait_for_frame(
            OutputBus::virtual_camera, {}, std::stop_token{}, started + 600ms));
        elapsed = std::chrono::steady_clock::now() - started;
    }};
    const auto entered = outgoing->wait_until_entered(1s);
    expect(entered, "the route-race fixture entered the outgoing graph wait");
    if (entered) {
        renderer->commit_take(OutputBus::virtual_camera, incoming, nullptr, 2U);
    }
    // Reproduce the critical ordering: the outgoing graph observes its own wake
    // before it registers the generation on which it is about to block.
    outgoing->allow_wait_registration();
    waiter.join();
    expect(elapsed < 200ms,
           "a route change interrupts an outgoing graph wait without waiting for the heartbeat deadline");
    expect(!outgoing->rendering_enabled,
           "a cut suspends the retired graph as soon as the route changes");
}

void test_output_cursor_rebases_when_the_active_graph_changes(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer) {
    using solin::media_engine::OutputBus;
    auto outgoing = std::make_shared<FixedFrameGraph>(100U, 0x20U);
    auto incoming = std::make_shared<FixedFrameGraph>(1U, 0xE0U);
    renderer->commit_hydration({nullptr, outgoing}, {false, true}, 1U);

    solin::media_engine::SceneOutputFrameCursor cursor{};
    std::uint8_t observed = 0U;
    const auto outgoing_cursor = renderer->visit_latest_frame(
        OutputBus::virtual_camera, cursor,
        [&observed](const solin::media_engine::VideoFrameView& frame) {
            observed = frame.planes[0].front();
        });
    expect(outgoing_cursor.has_value() && observed == 0x20U,
           "the output cursor observes the active outgoing graph");
    if (!outgoing_cursor.has_value()) {
        return;
    }
    cursor = outgoing_cursor.value();

    renderer->commit_take(OutputBus::virtual_camera, incoming, nullptr, 2U);
    const auto incoming_cursor = renderer->visit_latest_frame(
        OutputBus::virtual_camera, cursor,
        [&observed](const solin::media_engine::VideoFrameView& frame) {
            observed = frame.planes[0].front();
        });
    expect(incoming_cursor.has_value() && observed == 0xE0U &&
               incoming_cursor->route_generation > cursor.route_generation &&
               incoming_cursor->frame_sequence == 1U,
           "a new route publishes its static frame even when its internal sequence is lower");
}

void test_terminal_graph_wait_is_rate_limited(
    const std::shared_ptr<solin::media_engine::SceneRenderer>& renderer) {
    using solin::media_engine::OutputBus;
    renderer->commit_hydration(
        {nullptr, std::make_shared<ImmediateFailureGraph>()}, {false, true}, 1U);
    const auto started = std::chrono::steady_clock::now();
    const auto delivered = renderer->wait_for_frame(
        OutputBus::virtual_camera, {}, std::stop_token{}, started + 250ms);
    const auto elapsed = std::chrono::steady_clock::now() - started;
    expect(!delivered && elapsed >= 40ms,
           "a terminal active graph cannot turn an event-driven consumer into a busy retry loop");
}

[[nodiscard]] bool near_channel(const std::uint8_t value, const std::uint8_t expected,
                                const std::uint8_t tolerance) {
    const auto difference = value > expected ? value - expected : expected - value;
    return difference <= tolerance;
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
    test_gstreamer_diagnostics_extract_only_the_native_hresult();
    test_automatic_camera_format_is_bounded_and_deterministic();
    {
        solin::media_engine::MediaRuntime media_runtime;
        const auto probe = media_runtime.initialize();
        expect(probe.initialized, "the pinned GStreamer runtime initializes");
        if (probe.initialized) {
            test_local_camera_caps_preserve_exact_native_frame_rates();
            test_local_camera_caps_reject_invalid_native_frame_rates();
            test_ephemeral_transition_graph_requires_causal_output(media_runtime);
            test_color_source_publishes_bounded_latest_d3d11_frames(media_runtime);
            test_rtsp_source_decodes_to_the_same_bounded_frame_contract(media_runtime);
            if (probe.d3d11_compositor) {
                test_program_transition_clock_survives_a_descheduled_feeder(
                    media_runtime);
                test_scene_renderer_composes_nested_scene_to_shared_d3d11_frame(
                    media_runtime);
                test_failed_source_does_not_block_healthy_compositor_layers(media_runtime);
                test_program_cadence_recovers_when_media_arrives_before_the_startup_camera(
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
