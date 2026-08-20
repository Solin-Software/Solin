#include "gstreamer_scene_renderer.hpp"

#include "gstreamer_source_runtime.hpp"
#include "solin/media_engine/render_geometry.hpp"
#include "solin/media_engine/scene_graph.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/base/gstaggregator.h>
#include <gst/d3d11/gstd3d11.h>
#include <gst/gst.h>
#include <gst/video/video-info.h>
#endif

namespace solin::media_engine {

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
namespace {

using namespace std::chrono_literals;

constexpr auto kPrepareTimeout = 3s;
constexpr std::uint32_t kMaximumOutputDimension = 3'840U;
constexpr std::uint64_t kMaximumOutputPixels = 3'840ULL * 2'160ULL;
constexpr std::uint32_t kMaximumOutputFramesPerSecond = 60U;

void release_pipeline(GstElement* pipeline) noexcept {
    if (pipeline != nullptr) {
        static_cast<void>(gst_element_set_state(pipeline, GST_STATE_NULL));
        gst_object_unref(pipeline);
    }
}

void release_caps(GstCaps* caps) noexcept {
    if (caps != nullptr) {
        gst_caps_unref(caps);
    }
}

void release_bus(GstBus* bus) noexcept {
    if (bus != nullptr) {
        gst_object_unref(bus);
    }
}

[[nodiscard]] std::uint64_t monotonic_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

[[nodiscard]] std::uint64_t valid_clock_time(const GstClockTime value) noexcept {
    return GST_CLOCK_TIME_IS_VALID(value) ? static_cast<std::uint64_t>(value) : 0U;
}

[[nodiscard]] std::size_t bus_index(const OutputBus bus) noexcept {
    return static_cast<std::size_t>(bus);
}

[[nodiscard]] GstElement* add_element(GstElement* pipeline, const char* factory_name) {
    auto* element = gst_element_factory_make(factory_name, nullptr);
    if (element == nullptr) {
        throw SceneRendererError{"renderer_dependency_unavailable",
                                 "A required compositor element is unavailable"};
    }
    if (gst_bin_add(GST_BIN(pipeline), element) == FALSE) {
        gst_object_unref(element);
        throw SceneRendererError{"renderer_pipeline_failed",
                                 "A compositor element could not be added"};
    }
    return element;
}

[[nodiscard]] GstElement* add_live_compositor(GstElement* pipeline) {
    auto* compositor =
        gst_element_factory_make_full("d3d11compositor", "force-live", TRUE, nullptr);
    if (compositor == nullptr) {
        throw SceneRendererError{"renderer_dependency_unavailable",
                                 "The D3D11 compositor is unavailable"};
    }
    if (gst_bin_add(GST_BIN(pipeline), compositor) == FALSE) {
        gst_object_unref(compositor);
        throw SceneRendererError{"renderer_pipeline_failed",
                                 "The D3D11 compositor could not be added"};
    }
    g_object_set(compositor, "ignore-inactive-pads", TRUE, "latency",
                 static_cast<guint64>(0U), nullptr);
    return compositor;
}

void require_link(GstElement* source, GstElement* sink) {
    if (gst_element_link(source, sink) == FALSE) {
        throw SceneRendererError{"renderer_pipeline_failed",
                                 "Compositor elements could not be linked"};
    }
}

void connect_tee(GstElement* tee, GstElement* target) {
    auto* source_pad = gst_element_request_pad_simple(tee, "src_%u");
    auto* target_pad = gst_element_get_static_pad(target, "sink");
    if (source_pad == nullptr || target_pad == nullptr ||
        gst_pad_link(source_pad, target_pad) != GST_PAD_LINK_OK) {
        if (source_pad != nullptr) {
            gst_object_unref(source_pad);
        }
        if (target_pad != nullptr) {
            gst_object_unref(target_pad);
        }
        throw SceneRendererError{"renderer_pipeline_failed",
                                 "A compositor branch could not be linked"};
    }
    gst_object_unref(source_pad);
    gst_object_unref(target_pad);
}

struct ReadinessGate final {
    GstElement* valve{nullptr};
    GstElement* output_tee{nullptr};
};

[[nodiscard]] ReadinessGate add_readiness_gate(GstElement* pipeline,
                                               GstElement* source_tee) {
    auto* queue = add_element(pipeline, "queue");
    auto* valve = add_element(pipeline, "valve");
    auto* output_tee = add_element(pipeline, "tee");
    g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                 "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
    // Keep downstream clocks, sticky events, and state changes moving while
    // withholding visual buffers. Appsinks ignore GAP events, so the previous
    // Program frame remains visible until this graph is genuinely ready.
    g_object_set(valve, "drop", TRUE, nullptr);
    gst_util_set_object_arg(G_OBJECT(valve), "drop-mode", "transform-to-gap");
    connect_tee(source_tee, queue);
    require_link(queue, valve);
    require_link(valve, output_tee);
    return {.valve = valve, .output_tee = output_tee};
}

void open_readiness_gate(GstElement* valve) noexcept {
    try {
        if (valve != nullptr) {
            g_object_set(valve, "drop", FALSE, nullptr);
        }
    } catch (...) {
    }
}

[[nodiscard]] std::unique_ptr<GstCaps, decltype(&release_caps)> output_caps(
    const OutputVideoFormat& format) {
    auto* caps = gst_caps_new_simple(
        "video/x-raw", "format", G_TYPE_STRING, "BGRA", "width", G_TYPE_INT,
        static_cast<gint>(format.width), "height", G_TYPE_INT,
        static_cast<gint>(format.height), "framerate", GST_TYPE_FRACTION,
        static_cast<gint>(format.fps_numerator),
        static_cast<gint>(format.fps_denominator), "pixel-aspect-ratio",
        GST_TYPE_FRACTION, 1, 1, nullptr);
    if (caps != nullptr) {
        gst_caps_set_features(caps, 0U,
                              gst_caps_features_new("memory:D3D11Memory", nullptr));
    }
    return {caps, &release_caps};
}

void validate_preparation(const SceneRenderPreparation& preparation) {
    const auto& format = preparation.output.video_format;
    const auto pixels = static_cast<std::uint64_t>(format.width) * format.height;
    if (preparation.graph == nullptr || format.width == 0U || format.height == 0U ||
        format.width > kMaximumOutputDimension ||
        format.height > kMaximumOutputDimension || pixels > kMaximumOutputPixels ||
        (std::min)(format.width, format.height) > 2'160U ||
        format.fps_numerator == 0U || format.fps_denominator == 0U ||
        format.fps_numerator >
            kMaximumOutputFramesPerSecond * format.fps_denominator ||
        (format.pixel_format != "bgra" && format.pixel_format != "nv12")) {
        throw SceneRendererError{"renderer_format_unsupported",
                                 "The compositor output format is unsupported"};
    }
}

[[nodiscard]] gint pixel_position(const double value) noexcept {
    const auto rounded = std::llround(value);
    return static_cast<gint>(std::clamp<std::int64_t>(
        rounded, (std::numeric_limits<gint>::min)(),
        (std::numeric_limits<gint>::max)()));
}

[[nodiscard]] gint pixel_size(const double value) noexcept {
    return (std::max)(1, pixel_position(value));
}

class VideoFrameMap final {
  public:
    VideoFrameMap(const GstVideoInfo& info, GstBuffer* buffer) {
        mapped_ = gst_video_frame_map(&frame_, &info, buffer, GST_MAP_READ) != FALSE;
    }
    ~VideoFrameMap() {
        if (mapped_) {
            gst_video_frame_unmap(&frame_);
        }
    }

    VideoFrameMap(const VideoFrameMap&) = delete;
    VideoFrameMap& operator=(const VideoFrameMap&) = delete;

    [[nodiscard]] explicit operator bool() const noexcept { return mapped_; }
    [[nodiscard]] const GstVideoFrame& get() const noexcept { return frame_; }

  private:
    GstVideoFrame frame_{};
    bool mapped_{false};
};

[[nodiscard]] std::optional<std::uint64_t>
visit_gstreamer_frame(const std::shared_ptr<const SourceFrame>& source,
                      const std::uint64_t after_sequence,
                      const VideoFrameVisitor& visitor) {
    if (source == nullptr || source->sequence <= after_sequence) {
        return std::nullopt;
    }
    const auto lease = gstreamer_sample(source);
    if (!lease) {
        return std::nullopt;
    }
    auto* caps = gst_sample_get_caps(lease.sample);
    auto* buffer = gst_sample_get_buffer(lease.sample);
    GstVideoInfo info{};
    if (caps == nullptr || buffer == nullptr ||
        gst_video_info_from_caps(&info, caps) == FALSE) {
        return std::nullopt;
    }
    VideoFramePixelFormat pixel_format{};
    switch (GST_VIDEO_INFO_FORMAT(&info)) {
    case GST_VIDEO_FORMAT_BGRA:
        pixel_format = VideoFramePixelFormat::bgra;
        break;
    case GST_VIDEO_FORMAT_NV12:
        pixel_format = VideoFramePixelFormat::nv12;
        break;
    case GST_VIDEO_FORMAT_YUY2:
        pixel_format = VideoFramePixelFormat::yuy2;
        break;
    default:
        return std::nullopt;
    }
    const auto layout = packed_video_frame_layout(source->width, source->height,
                                                   pixel_format);
    VideoFrameMap mapped{info, buffer};
    if (!mapped) {
        return std::nullopt;
    }
    const auto& frame = mapped.get();
    VideoFrameView view{
        .sequence = source->sequence,
        .presentation_timestamp_ns = source->presentation_timestamp_ns,
        .duration_ns = source->duration_ns,
        .produced_monotonic_ns = source->received_monotonic_ns,
        .discontinuity = source->discontinuity,
        .width = source->width,
        .height = source->height,
        .pixel_format = pixel_format,
    };
    for (std::uint32_t plane = 0U; plane < layout.plane_count; ++plane) {
        const auto stride = GST_VIDEO_FRAME_PLANE_STRIDE(&frame, plane);
        const auto absolute_stride = static_cast<std::size_t>(
            stride < 0 ? -static_cast<std::int64_t>(stride) : stride);
        const auto rows = plane == 1U && pixel_format == VideoFramePixelFormat::nv12
                              ? layout.height / 2U
                              : layout.height;
        const auto size = rows == 0U
                              ? 0U
                              : absolute_stride * (rows - 1U) +
                                    layout.plane_strides[plane];
        view.planes[plane] = {
            static_cast<const std::uint8_t*>(
                GST_VIDEO_FRAME_PLANE_DATA(&frame, plane)),
            size,
        };
        view.plane_strides[plane] = stride;
    }
    validate_video_frame_view(view, layout);
    visitor(view);
    return source->sequence;
}

class GStreamerRenderedFramePayload final : public GStreamerSamplePayload {
  public:
    GStreamerRenderedFramePayload(GstSample* sample,
                                  std::shared_ptr<GstD3D11Device> device)
        : sample_(sample), device_(std::move(device)) {
        if (sample_ == nullptr || device_ == nullptr) {
            throw std::invalid_argument("Rendered GStreamer sample ownership is required");
        }
    }

    ~GStreamerRenderedFramePayload() override { gst_sample_unref(sample_); }

    [[nodiscard]] GstSample* sample() const noexcept override { return sample_; }

  private:
    GstSample* sample_{nullptr};
    std::shared_ptr<GstD3D11Device> device_{};
};

class PreparedGStreamerSceneGraph final : public PreparedSceneRenderGraph {
  public:
    PreparedGStreamerSceneGraph(const SceneRenderPreparation& preparation,
                                GStreamerD3d11DeviceLease device,
                                std::shared_ptr<std::atomic_uint64_t> frame_sequence,
                                std::shared_ptr<GStreamerFrameSignal> source_frame_signal)
        : output_(preparation.output.video_format), bus_id_(preparation.bus),
          device_(std::move(device)), frame_sequence_(std::move(frame_sequence)),
          source_frame_signal_(std::move(source_frame_signal)),
          resources_(preparation.resources) {
        validate_preparation(preparation);
        if (!device_ || frame_sequence_ == nullptr || source_frame_signal_ == nullptr) {
            throw SceneRendererError{"renderer_device_unavailable",
                                     "The shared D3D11 device is unavailable"};
        }
        auto* raw_pipeline = gst_pipeline_new(nullptr);
        if (raw_pipeline == nullptr) {
            throw SceneRendererError{"renderer_pipeline_failed",
                                     "The compositor pipeline could not be created"};
        }
        std::unique_ptr<GstElement, decltype(&release_pipeline)> pipeline{
            raw_pipeline, &release_pipeline};
        auto* context = gst_d3d11_context_new(device_.device);
        if (context == nullptr) {
            throw SceneRendererError{"renderer_device_unavailable",
                                     "The compositor D3D11 context is unavailable"};
        }
        gst_element_set_context(pipeline.get(), context);
        gst_context_unref(context);

        pipeline_ = pipeline.get();
        for (const auto& binding : preparation.sources) {
            if (binding.runtime == nullptr || binding.source_id.empty()) {
                throw SceneRendererError{"renderer_source_invalid",
                                         "A compositor source binding is invalid"};
            }
            source_bindings_.emplace(binding.source_id, binding.runtime);
        }
        auto* root_tee = build_scene(preparation.graph, true);
        const auto readiness = add_readiness_gate(pipeline_, root_tee);
        output_readiness_valve_ = readiness.valve;
        add_output_branch(readiness.output_tee);
        if (sources_.empty()) {
            output_ready_ = true;
            open_readiness_gate(output_readiness_valve_);
        }
        pipeline_ = pipeline.release();
        bus_ = gst_element_get_bus(pipeline_);
        if (bus_ == nullptr) {
            stop();
            throw SceneRendererError{"renderer_pipeline_failed",
                                     "The compositor message bus is unavailable"};
        }
        gst_bus_set_sync_handler(bus_, &PreparedGStreamerSceneGraph::on_bus_message,
                                 source_frame_signal_.get(), nullptr);

        const auto state_change = gst_element_set_state(pipeline_, GST_STATE_PLAYING);
        if (state_change == GST_STATE_CHANGE_FAILURE) {
            stop();
            throw SceneRendererError{"renderer_preroll_failed",
                                     "The compositor pipeline could not start"};
        }
        try {
            feeder_ = std::thread([this] { feed_sources(); });
        } catch (...) {
            stop();
            throw SceneRendererError{"renderer_worker_failed",
                                     "The compositor feeder could not start"};
        }
        GstState state = GST_STATE_NULL;
        const auto state_result = gst_element_get_state(
            pipeline_, &state, nullptr,
            static_cast<GstClockTime>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(kPrepareTimeout)
                    .count()));
        if (state_result == GST_STATE_CHANGE_FAILURE || state < GST_STATE_PAUSED) {
            stop();
            throw SceneRendererError{"renderer_preroll_failed",
                                     "The compositor pipeline did not preroll"};
        }
    }

    ~PreparedGStreamerSceneGraph() override { stop(); }

    void stop() noexcept override {
        if (stopped_.exchange(true)) {
            return;
        }
        wake_frame_waiters();
        source_frame_signal_->notify();
        if (feeder_.joinable()) {
            feeder_.join();
        }
        auto* pipeline = std::exchange(pipeline_, nullptr);
        auto* bus = std::exchange(bus_, nullptr);
        if (bus != nullptr) {
            gst_bus_set_sync_handler(bus, nullptr, nullptr, nullptr);
        }
        release_bus(bus);
        release_pipeline(pipeline);
        std::scoped_lock lock{frame_mutex_};
        latest_frame_.reset();
        latest_gpu_frame_.reset();
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    latest_frame() const noexcept override {
        if (failed_.load()) {
            return {};
        }
        try {
            std::scoped_lock lock{frame_mutex_};
            return latest_frame_;
        } catch (...) {
            return {};
        }
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(const std::uint64_t after_sequence,
                       const VideoFrameVisitor& visitor) const noexcept override {
        try {
            return visit_gstreamer_frame(latest_frame(), after_sequence, visitor);
        } catch (...) {
            return std::nullopt;
        }
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        try {
            std::unique_lock lock{frame_mutex_};
            const auto ready = [&] {
                return stopped_.load() || failed_.load() ||
                       (latest_frame_ != nullptr &&
                        latest_frame_->sequence > after_sequence);
            };
            if (ready()) {
                return latest_frame_ != nullptr &&
                       latest_frame_->sequence > after_sequence;
            }
            const auto wakeup_generation = frame_wakeup_generation_;
            static_cast<void>(frame_wakeup_.wait_until(
                lock, stop_token, deadline, [&] {
                    return ready() ||
                           frame_wakeup_generation_ != wakeup_generation;
                }));
            return latest_frame_ != nullptr &&
                   latest_frame_->sequence > after_sequence;
        } catch (...) {
            return false;
        }
    }

    void wake_frame_waiters() noexcept override {
        try {
            {
                std::scoped_lock lock{frame_mutex_};
                ++frame_wakeup_generation_;
            }
            frame_wakeup_.notify_all();
        } catch (...) {
        }
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    latest_gpu_frame() const noexcept override {
        if (failed_.load()) {
            return {};
        }
        try {
            std::scoped_lock lock{frame_mutex_};
            return latest_gpu_frame_;
        } catch (...) {
            return {};
        }
    }

    [[nodiscard]] const OutputVideoFormat& output_format() const noexcept { return output_; }

    void set_direct_output_enabled(const bool enabled) noexcept override {
        try {
            if (direct_output_valve_ != nullptr) {
                g_object_set(direct_output_valve_, "drop", enabled ? FALSE : TRUE,
                             nullptr);
            }
        } catch (...) {
        }
    }

    [[nodiscard]] bool preview_layer_geometry(
        const std::string_view scene_id, const std::string_view layer_id,
        const SceneLayerGeometry& geometry) noexcept override {
        try {
            std::scoped_lock lock{geometry_mutex_};
            const auto key = std::pair{std::string{scene_id}, std::string{layer_id}};
            const auto existing = layer_branches_.find(key);
            if (existing == layer_branches_.end()) {
                return false;
            }
            auto& branch = existing->second;
            branch.geometry = geometry;
            const auto dimensions = configure_branch(
                branch, branch.source_width, branch.source_height);
            for (auto& [_, layers] : compositor_layers_) {
                for (auto& layer : layers) {
                    if (layer.key != key) {
                        continue;
                    }
                    apply_compositor_geometry(layer.pad, geometry,
                                              dimensions.first, dimensions.second);
                    layer.selected_width = dimensions.first;
                    layer.selected_height = dimensions.second;
                }
            }
            return true;
        } catch (...) {
            return false;
        }
    }

  private:
    using LayerKey = std::pair<std::string, std::string>;

    struct LayerBranch final {
        LayerKey key{};
        SceneLayerGeometry geometry{};
        GstElement* crop{nullptr};
        GstElement* transform{nullptr};
        GstElement* caps_filter{nullptr};
        std::uint32_t source_width{0U};
        std::uint32_t source_height{0U};
    };

    struct CompositorLayerState final {
        LayerKey key{};
        GstPad* pad{nullptr};
        std::uint32_t selected_width{0U};
        std::uint32_t selected_height{0U};
    };

    struct SourceInput final {
        SourceRuntime* runtime{nullptr};
        GstElement* app_source{nullptr};
        GstElement* tee{nullptr};
        std::uint64_t last_stream_epoch{0U};
        std::uint64_t last_sequence{0U};
        std::uint32_t configured_width{0U};
        std::uint32_t configured_height{0U};
        std::vector<LayerKey> branches{};
    };

    [[nodiscard]] GstElement* build_scene(
        const std::shared_ptr<const CompiledSceneGraph>& graph, const bool root) {
        const auto existing = scene_outputs_.find(graph.get());
        if (existing != scene_outputs_.end()) {
            return existing->second;
        }
        auto* compositor = add_live_compositor(pipeline_);
        g_object_set(compositor, "background", root ? 1 : 3, nullptr);
        if (root) {
            root_compositor_ = compositor;
        }
        compositor_layers_.try_emplace(compositor);
        g_object_set(compositor, "emit-signals", TRUE, nullptr);
        g_signal_connect(compositor, "samples-selected",
                         G_CALLBACK(&PreparedGStreamerSceneGraph::on_samples_selected),
                         this);
        auto* caps_filter = add_element(pipeline_, "capsfilter");
        auto* tee = add_element(pipeline_, "tee");
        const auto caps = output_caps(output_);
        if (caps == nullptr) {
            throw SceneRendererError{"renderer_format_unsupported",
                                     "The compositor output caps are unavailable"};
        }
        g_object_set(caps_filter, "caps", caps.get(), nullptr);
        require_link(compositor, caps_filter);
        require_link(caps_filter, tee);
        scene_outputs_.emplace(graph.get(), tee);

        for (std::size_t index = 0U; index < graph->layers.size(); ++index) {
            const auto& layer = graph->layers[index];
            if (!layer.source_enabled || !layer.layer.geometry.visible ||
                layer.layer.geometry.opacity <= 0.0 ||
                layer.layer.geometry.width <= 0.0 ||
                layer.layer.geometry.height <= 0.0) {
                continue;
            }
            if (layer.layer.geometry.border_width > 0.0 ||
                layer.layer.geometry.corner_radius > 0.0) {
                throw SceneRendererError{
                    "renderer_layer_feature_unsupported",
                    "Border and corner radius require the Solin shader compositor"};
            }
            if (layer.referenced_scene != nullptr) {
                auto* input = build_scene(layer.referenced_scene, false);
                add_layer_branch(input, compositor, graph->scene_id, layer.layer.id,
                                 layer.layer.geometry, output_.width, output_.height,
                                 static_cast<guint>(index), nullptr);
                continue;
            }
            auto& source = source_input(layer.layer.source_id);
            add_layer_branch(source.tee, compositor, graph->scene_id, layer.layer.id,
                             layer.layer.geometry, output_.width, output_.height,
                             static_cast<guint>(index), &source);
        }
        return tee;
    }

    [[nodiscard]] SourceInput& source_input(const std::string& source_id) {
        const auto existing = sources_.find(source_id);
        if (existing != sources_.end()) {
            return existing->second;
        }
        const auto binding = source_bindings_.find(source_id);
        if (binding == source_bindings_.end()) {
            throw SceneRendererError{"renderer_source_invalid",
                                     "A compositor source binding is missing"};
        }
        SourceInput input{
            .runtime = binding->second,
            .app_source = add_element(pipeline_, "appsrc"),
            .tee = add_element(pipeline_, "tee"),
        };
        g_object_set(input.app_source, "is-live", TRUE, "do-timestamp", TRUE,
                     "format", GST_FORMAT_TIME, "block", FALSE, "emit-signals", FALSE,
                     "max-buffers", static_cast<guint64>(1U), "max-bytes",
                     static_cast<guint64>(0U), "max-time", static_cast<guint64>(0U),
                     "leaky-type", GST_APP_LEAKY_TYPE_DOWNSTREAM, nullptr);
        require_link(input.app_source, input.tee);
        const auto result = sources_.emplace(source_id, std::move(input));
        return result.first->second;
    }

    void add_layer_branch(GstElement* input_tee, GstElement* compositor,
                          const std::string& scene_id, const std::string& layer_id,
                          const SceneLayerGeometry& geometry,
                          const std::uint32_t source_width,
                          const std::uint32_t source_height, const guint zorder,
                          SourceInput* dynamic_source) {
        auto* queue = add_element(pipeline_, "queue");
        auto* crop = add_element(pipeline_, "videocrop");
        auto* upload = add_element(pipeline_, "d3d11upload");
        auto* transform = add_element(pipeline_, "d3d11convert");
        auto* caps_filter = add_element(pipeline_, "capsfilter");
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        g_object_set(transform, "ortho", TRUE, "rotation-z",
                     static_cast<gfloat>(geometry.rotation_degrees), "scale-x",
                     static_cast<gfloat>(geometry.mirror_x ? -1.0 : 1.0), "scale-y",
                     static_cast<gfloat>(geometry.mirror_y ? -1.0 : 1.0), nullptr);
        const std::unique_ptr<GstCaps, decltype(&release_caps)> caps{
            gst_caps_from_string(
                "video/x-raw(memory:D3D11Memory),format=BGRA,pixel-aspect-ratio=1/1"),
            &release_caps};
        if (caps == nullptr) {
            throw SceneRendererError{"renderer_format_unsupported",
                                     "Canonical compositor caps are unavailable"};
        }
        g_object_set(caps_filter, "caps", caps.get(), nullptr);
        connect_tee(input_tee, queue);
        require_link(queue, crop);
        require_link(crop, upload);
        require_link(upload, transform);
        require_link(transform, caps_filter);

        auto* source_pad = gst_element_get_static_pad(caps_filter, "src");
        auto* compositor_pad =
            gst_element_request_pad_simple(compositor, "sink_%u");
        if (source_pad == nullptr || compositor_pad == nullptr ||
            gst_pad_link(source_pad, compositor_pad) != GST_PAD_LINK_OK) {
            if (source_pad != nullptr) {
                gst_object_unref(source_pad);
            }
            if (compositor_pad != nullptr) {
                gst_object_unref(compositor_pad);
            }
            throw SceneRendererError{"renderer_pipeline_failed",
                                     "A layer could not be linked to its scene"};
        }
        gst_object_unref(source_pad);
        g_object_set(compositor_pad, "zorder", zorder, "sizing-policy", 0, nullptr);
        LayerBranch branch{
            .key = {scene_id, layer_id},
            .geometry = geometry,
            .crop = crop,
            .transform = transform,
            .caps_filter = caps_filter,
            .source_width = source_width,
            .source_height = source_height,
        };
        const auto visible_dimensions =
            configure_branch(branch, source_width, source_height);
        apply_compositor_geometry(compositor_pad, geometry,
                                  visible_dimensions.first,
                                  visible_dimensions.second);
        compositor_layers_.at(compositor).push_back(CompositorLayerState{
            .key = branch.key,
            .pad = compositor_pad,
            .selected_width = visible_dimensions.first,
            .selected_height = visible_dimensions.second,
        });
        const auto [stored, inserted] =
            layer_branches_.emplace(branch.key, std::move(branch));
        if (!inserted) {
            throw SceneRendererError{"renderer_pipeline_failed",
                                     "A rendered layer identity is duplicated"};
        }
        if (dynamic_source != nullptr) {
            dynamic_source->branches.push_back(stored->first);
        }
        gst_object_unref(compositor_pad);
    }

    [[nodiscard]] std::pair<std::uint32_t, std::uint32_t>
    configure_branch(LayerBranch& branch, const std::uint32_t source_width,
                     const std::uint32_t source_height) const {
        branch.source_width = source_width;
        branch.source_height = source_height;
        g_object_set(branch.transform, "ortho", TRUE, "rotation-z",
                     static_cast<gfloat>(branch.geometry.rotation_degrees), "scale-x",
                     static_cast<gfloat>(branch.geometry.mirror_x ? -1.0 : 1.0), "scale-y",
                     static_cast<gfloat>(branch.geometry.mirror_y ? -1.0 : 1.0), nullptr);
        const auto transform = compute_render_layer_transform(
            branch.geometry, source_width, source_height, output_.width, output_.height);
        auto left = static_cast<gint>(
            std::llround(transform.source_uv.left * source_width));
        auto right = static_cast<gint>(
            std::llround((1.0 - transform.source_uv.right) * source_width));
        auto top = static_cast<gint>(
            std::llround(transform.source_uv.top * source_height));
        auto bottom = static_cast<gint>(
            std::llround((1.0 - transform.source_uv.bottom) * source_height));
        left = std::clamp(left, 0, static_cast<gint>(source_width - 1U));
        right = std::clamp(right, 0,
                           static_cast<gint>(source_width - 1U) - left);
        top = std::clamp(top, 0, static_cast<gint>(source_height - 1U));
        bottom = std::clamp(bottom, 0,
                            static_cast<gint>(source_height - 1U) - top);
        g_object_set(branch.crop, "left", left, "right", right, "top", top,
                     "bottom", bottom, nullptr);
        const auto visible_width =
            static_cast<gint>(source_width) - left - right;
        const auto visible_height =
            static_cast<gint>(source_height) - top - bottom;
        // Keep the transform's negotiated raster synchronized with the crop.
        // Otherwise d3d11convert can preserve its previous dimensions and add
        // padding before the compositor applies the layer fit a second time.
        const std::unique_ptr<GstCaps, decltype(&release_caps)> branch_caps{
            gst_caps_new_simple(
                "video/x-raw", "format", G_TYPE_STRING, "BGRA", "width",
                G_TYPE_INT, visible_width, "height", G_TYPE_INT, visible_height,
                "pixel-aspect-ratio", GST_TYPE_FRACTION, 1, 1, nullptr),
            &release_caps};
        if (branch_caps == nullptr) {
            throw SceneRendererError{"renderer_pipeline_failed",
                                     "Layer dimensions could not be configured"};
        }
        gst_caps_set_features(branch_caps.get(), 0U,
                              gst_caps_features_new("memory:D3D11Memory", nullptr));
        g_object_set(branch.caps_filter, "caps", branch_caps.get(), nullptr);
        return {
            static_cast<std::uint32_t>(visible_width),
            static_cast<std::uint32_t>(visible_height),
        };
    }

    void apply_compositor_geometry(GstPad* pad, SceneLayerGeometry geometry,
                                   const std::uint32_t selected_width,
                                   const std::uint32_t selected_height) const {
        // The selected sample is already cropped by the upstream branch. Computing
        // its destination with the authored crop again would distort contain layers.
        geometry.crop_left = 0.0;
        geometry.crop_top = 0.0;
        geometry.crop_right = 0.0;
        geometry.crop_bottom = 0.0;
        const auto transform = compute_render_layer_transform(
            geometry, selected_width, selected_height, output_.width, output_.height);
        g_object_set(pad, "xpos", pixel_position(transform.content_bounds.x), "ypos",
                     pixel_position(transform.content_bounds.y), "width",
                     pixel_size(transform.content_bounds.width), "height",
                     pixel_size(transform.content_bounds.height), "alpha",
                     transform.opacity, nullptr);
    }

    static void on_samples_selected(GstAggregator* aggregator, GstSegment*, guint64,
                                    guint64, guint64, GstStructure*,
                                    gpointer user_data) noexcept {
        static_cast<PreparedGStreamerSceneGraph*>(user_data)
            ->synchronize_compositor_geometry(aggregator);
    }

    void synchronize_compositor_geometry(GstAggregator* aggregator) noexcept {
        try {
            std::scoped_lock lock{geometry_mutex_};
            const auto compositor = compositor_layers_.find(GST_ELEMENT(aggregator));
            if (compositor == compositor_layers_.end()) {
                return;
            }
            bool all_inputs_ready = !compositor->second.empty();
            for (auto& layer : compositor->second) {
                const auto branch = layer_branches_.find(layer.key);
                if (branch == layer_branches_.end()) {
                    continue;
                }
                std::unique_ptr<GstSample, decltype(&gst_sample_unref)> sample{
                    gst_aggregator_peek_next_sample(
                        aggregator, GST_AGGREGATOR_PAD(layer.pad)),
                    &gst_sample_unref};
                if (sample == nullptr) {
                    all_inputs_ready = false;
                    continue;
                }
                auto* caps = gst_sample_get_caps(sample.get());
                GstVideoInfo info{};
                if (caps == nullptr ||
                    gst_video_info_from_caps(&info, caps) == FALSE) {
                    continue;
                }
                const auto width =
                    static_cast<std::uint32_t>(GST_VIDEO_INFO_WIDTH(&info));
                const auto height =
                    static_cast<std::uint32_t>(GST_VIDEO_INFO_HEIGHT(&info));
                if (width == 0U || height == 0U ||
                    (width == layer.selected_width &&
                     height == layer.selected_height)) {
                    continue;
                }
                apply_compositor_geometry(layer.pad, branch->second.geometry, width, height);
                layer.selected_width = width;
                layer.selected_height = height;
            }
            if (GST_ELEMENT(aggregator) == root_compositor_ && all_inputs_ready &&
                !output_ready_.exchange(true)) {
                open_readiness_gate(output_readiness_valve_);
            }
        } catch (...) {
            failed_.store(true);
        }
    }

    void add_output_branch(GstElement* root_tee) {
        if (bus_id_ == OutputBus::virtual_camera) {
            add_gpu_transition_tap(root_tee);
        }
        auto* queue = add_element(pipeline_, "queue");
        auto* sink = add_element(pipeline_, "appsink");
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE,
                     "wait-on-eos", FALSE, nullptr);
        gst_app_sink_set_max_buffers(GST_APP_SINK(sink), 1U);
        gst_app_sink_set_leaky_type(GST_APP_SINK(sink),
                                    GST_APP_LEAKY_TYPE_DOWNSTREAM);
        GstAppSinkCallbacks callbacks{};
        callbacks.new_sample = &PreparedGStreamerSceneGraph::on_new_sample;
        gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, this, nullptr);
        connect_tee(root_tee, queue);
        if (bus_id_ == OutputBus::virtual_camera || output_.pixel_format == "nv12") {
            auto* convert = add_element(pipeline_, "d3d11convert");
            auto* gpu_caps_filter = add_element(pipeline_, "capsfilter");
            const auto nv12_output =
                bus_id_ == OutputBus::virtual_camera ||
                output_.pixel_format == "nv12";
            const auto* output_format = nv12_output ? "NV12" : "BGRA";
            const auto* colorimetry = nv12_output ? ",colorimetry=bt709" : "";
            const auto gpu_caps_text =
                std::string{"video/x-raw(memory:D3D11Memory),format="} +
                output_format + ",pixel-aspect-ratio=1/1" + colorimetry;
            const std::unique_ptr<GstCaps, decltype(&release_caps)> gpu_caps{
                gst_caps_from_string(gpu_caps_text.c_str()),
                &release_caps};
            if (gpu_caps == nullptr) {
                throw SceneRendererError{"renderer_format_unsupported",
                                         "Output pixel-format caps are unavailable"};
            }
            g_object_set(gpu_caps_filter, "caps", gpu_caps.get(), nullptr);
            if (bus_id_ == OutputBus::virtual_camera) {
                direct_output_valve_ = add_element(pipeline_, "valve");
                require_link(queue, direct_output_valve_);
                require_link(direct_output_valve_, convert);
            } else {
                require_link(queue, convert);
            }
            require_link(convert, gpu_caps_filter);
            if (bus_id_ == OutputBus::virtual_camera) {
                auto* download = add_element(pipeline_, "d3d11download");
                auto* cpu_caps_filter = add_element(pipeline_, "capsfilter");
                const auto cpu_caps_text =
                    std::string{"video/x-raw,format="} + output_format +
                    ",pixel-aspect-ratio=1/1" + colorimetry;
                const std::unique_ptr<GstCaps, decltype(&release_caps)> cpu_caps{
                    gst_caps_from_string(cpu_caps_text.c_str()), &release_caps};
                if (cpu_caps == nullptr) {
                    throw SceneRendererError{
                        "renderer_format_unsupported",
                        "System-memory output caps are unavailable"};
                }
                g_object_set(cpu_caps_filter, "caps", cpu_caps.get(), nullptr);
                require_link(gpu_caps_filter, download);
                require_link(download, cpu_caps_filter);
                require_link(cpu_caps_filter, sink);
            } else {
                require_link(gpu_caps_filter, sink);
            }
        } else {
            require_link(queue, sink);
        }
    }

    void add_gpu_transition_tap(GstElement* root_tee) {
        auto* queue = add_element(pipeline_, "queue");
        auto* sink = add_element(pipeline_, "appsink");
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE,
                     "wait-on-eos", FALSE, nullptr);
        gst_app_sink_set_max_buffers(GST_APP_SINK(sink), 1U);
        gst_app_sink_set_leaky_type(GST_APP_SINK(sink),
                                    GST_APP_LEAKY_TYPE_DOWNSTREAM);
        GstAppSinkCallbacks callbacks{};
        callbacks.new_sample = &PreparedGStreamerSceneGraph::on_new_gpu_sample;
        gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, this, nullptr);
        connect_tee(root_tee, queue);
        require_link(queue, sink);
    }

    static GstFlowReturn on_new_sample(GstAppSink* sink, gpointer user_data) noexcept {
        return static_cast<PreparedGStreamerSceneGraph*>(user_data)
            ->publish_sample(sink, false);
    }

    static GstFlowReturn on_new_gpu_sample(GstAppSink* sink,
                                           gpointer user_data) noexcept {
        return static_cast<PreparedGStreamerSceneGraph*>(user_data)
            ->publish_sample(sink, true);
    }

    GstFlowReturn publish_sample(GstAppSink* sink, const bool gpu_tap) noexcept {
        auto* sample = gst_app_sink_pull_sample(sink);
        if (sample == nullptr) {
            return GST_FLOW_EOS;
        }
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> sample_guard{
            sample, &gst_sample_unref};
        try {
            auto* caps = gst_sample_get_caps(sample);
            auto* buffer = gst_sample_get_buffer(sample);
            GstVideoInfo info{};
            if (caps == nullptr || buffer == nullptr ||
                gst_video_info_from_caps(&info, caps) == FALSE) {
                return GST_FLOW_ERROR;
            }
            const auto sequence = frame_sequence_->fetch_add(1U) + 1U;
            auto payload = std::make_shared<GStreamerRenderedFramePayload>(
                sample_guard.release(), device_.owner);
            const auto* format_name =
                gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info));
            if (format_name == nullptr) {
                return GST_FLOW_ERROR;
            }
            const auto* features = gst_caps_get_features(caps, 0U);
            const auto memory_kind =
                features != nullptr &&
                        gst_caps_features_contains(features, "memory:D3D11Memory") != FALSE
                    ? SourceFrameMemory::d3d11
                    : SourceFrameMemory::system_memory;
            auto frame = std::make_shared<SourceFrame>(SourceFrame{
                .sequence = sequence,
                .stream_epoch = 1U,
                .discontinuity = sequence == 1U,
                .presentation_timestamp_ns = valid_clock_time(GST_BUFFER_PTS(buffer)),
                .duration_ns = valid_clock_time(GST_BUFFER_DURATION(buffer)),
                .received_monotonic_ns = monotonic_nanoseconds(),
                .width = static_cast<std::uint32_t>(GST_VIDEO_INFO_WIDTH(&info)),
                .height = static_cast<std::uint32_t>(GST_VIDEO_INFO_HEIGHT(&info)),
                .pixel_format = format_name,
                .memory = memory_kind,
                .payload = std::move(payload),
            });
            {
                std::scoped_lock lock{frame_mutex_};
                if (gpu_tap) {
                    latest_gpu_frame_ = std::move(frame);
                } else {
                    latest_frame_ = std::move(frame);
                    ++frame_wakeup_generation_;
                }
            }
            if (!gpu_tap) {
                frame_wakeup_.notify_all();
            }
            return GST_FLOW_OK;
        } catch (...) {
            return GST_FLOW_ERROR;
        }
    }

    static GstBusSyncReply on_bus_message(GstBus*, GstMessage* message,
                                          gpointer user_data) noexcept {
        if (message != nullptr && GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR &&
            user_data != nullptr) {
            static_cast<GStreamerFrameSignal*>(user_data)->notify();
        }
        return GST_BUS_PASS;
    }

    void feed_sources() noexcept {
        auto source_revision = source_frame_signal_->revision();
        while (!stopped_.load()) {
            auto* message = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
            if (message != nullptr) {
                gst_message_unref(message);
                failed_.store(true);
                wake_frame_waiters();
                break;
            }
            for (auto& [_, source] : sources_) {
                push_latest_frame(source);
            }
            open_readiness_gate_when_sources_settle();
            source_revision = source_frame_signal_->wait_after(source_revision);
        }
    }

    void open_readiness_gate_when_sources_settle() noexcept {
        if (output_ready_.load()) {
            return;
        }
        try {
            const auto settled = std::ranges::all_of(
                sources_, [](const auto& item) {
                    const auto& source = item.second;
                    if (source.last_sequence != 0U) {
                        return true;
                    }
                    const auto health = source.runtime->health();
                    return health.status == SourceRuntimeStatus::failed ||
                           health.status == SourceRuntimeStatus::stopped ||
                           (health.status == SourceRuntimeStatus::degraded &&
                            !health.error_code.empty());
                });
            if (settled && !output_ready_.exchange(true)) {
                open_readiness_gate(output_readiness_valve_);
            }
        } catch (...) {
            failed_.store(true);
        }
    }

    void push_latest_frame(SourceInput& source) noexcept {
        try {
            auto frame = source.runtime->latest_frame();
            if (frame == nullptr ||
                (frame->stream_epoch == source.last_stream_epoch &&
                 frame->sequence == source.last_sequence)) {
                return;
            }
            auto sample = gstreamer_sample(frame);
            if (!sample) {
                return;
            }
            if (source.configured_width != frame->width ||
                source.configured_height != frame->height) {
                std::scoped_lock lock{geometry_mutex_};
                for (const auto& key : source.branches) {
                    const auto branch = layer_branches_.find(key);
                    if (branch != layer_branches_.end()) {
                        static_cast<void>(configure_branch(
                            branch->second, frame->width, frame->height));
                    }
                }
                source.configured_width = frame->width;
                source.configured_height = frame->height;
            }
            auto* original_buffer = gst_sample_get_buffer(sample.sample);
            auto* caps = gst_sample_get_caps(sample.sample);
            if (original_buffer == nullptr || caps == nullptr) {
                return;
            }
            auto* buffer = gst_buffer_copy(original_buffer);
            if (buffer == nullptr) {
                return;
            }
            GST_BUFFER_PTS(buffer) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DTS(buffer) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DURATION(buffer) = GST_CLOCK_TIME_NONE;
            auto* retimed = gst_sample_new(buffer, caps, nullptr, nullptr);
            gst_buffer_unref(buffer);
            if (retimed == nullptr) {
                return;
            }
            const auto flow =
                gst_app_src_push_sample(GST_APP_SRC(source.app_source), retimed);
            gst_sample_unref(retimed);
            if (flow == GST_FLOW_OK) {
                source.last_stream_epoch = frame->stream_epoch;
                source.last_sequence = frame->sequence;
            }
        } catch (...) {
        }
    }

    OutputVideoFormat output_{};
    OutputBus bus_id_{OutputBus::media_windows};
    GStreamerD3d11DeviceLease device_{};
    GstElement* pipeline_{nullptr};
    GstBus* bus_{nullptr};
    GstElement* root_compositor_{nullptr};
    GstElement* output_readiness_valve_{nullptr};
    GstElement* direct_output_valve_{nullptr};
    std::map<std::string, SourceRuntime*, std::less<>> source_bindings_{};
    std::map<std::string, SourceInput, std::less<>> sources_{};
    std::map<const CompiledSceneGraph*, GstElement*> scene_outputs_{};
    std::map<LayerKey, LayerBranch> layer_branches_{};
    std::map<GstElement*, std::vector<CompositorLayerState>> compositor_layers_{};
    mutable std::mutex geometry_mutex_{};
    std::atomic_bool stopped_{false};
    std::atomic_bool failed_{false};
    std::atomic_bool output_ready_{false};
    std::thread feeder_{};
    std::shared_ptr<std::atomic_uint64_t> frame_sequence_{};
    std::shared_ptr<GStreamerFrameSignal> source_frame_signal_{};
    std::shared_ptr<SceneRenderResources> resources_{};
    mutable std::mutex frame_mutex_{};
    mutable std::condition_variable_any frame_wakeup_{};
    std::uint64_t frame_wakeup_generation_{0U};
    std::shared_ptr<const SourceFrame> latest_frame_{};
    std::shared_ptr<const SourceFrame> latest_gpu_frame_{};
};

class PreparedGStreamerTransition final : public PreparedSceneRenderGraph {
  public:
    PreparedGStreamerTransition(
        std::shared_ptr<PreparedSceneRenderGraph> outgoing,
        std::shared_ptr<PreparedGStreamerSceneGraph> incoming,
        const SceneTransitionSpec transition, GStreamerD3d11DeviceLease device,
        std::shared_ptr<std::atomic_uint64_t> frame_sequence)
        : incoming_(std::move(incoming)), transition_(transition),
          device_(std::move(device)), frame_sequence_(std::move(frame_sequence)) {
        validate_scene_transition(transition_);
        if (transition_.kind == SceneTransitionKind::cut || outgoing == nullptr ||
            incoming_ == nullptr || !device_ || frame_sequence_ == nullptr) {
            throw SceneRendererError{"transition_preparation_failed",
                                     "The transition inputs are unavailable"};
        }
        outgoing_control_ = outgoing;
        if (outgoing->is_transition_output()) {
            frozen_outgoing_gpu_ = outgoing->latest_gpu_frame();
            frozen_outgoing_cpu_ = outgoing->latest_frame();
            if (frozen_outgoing_gpu_ == nullptr) {
                outgoing_ = outgoing->transition_origin();
                if (outgoing_ == nullptr) {
                    outgoing_ = std::move(outgoing);
                }
            }
        } else {
            outgoing_ = std::move(outgoing);
        }
        if (frozen_outgoing_gpu_ == nullptr && outgoing_ == nullptr) {
            throw SceneRendererError{"transition_origin_unavailable",
                                     "The transition origin has no stable frame source"};
        }
        const auto& output_format = incoming_->output_format();
        frame_interval_ = scene_transition_frame_interval(output_format);
        build_pipeline(output_format);
    }

    ~PreparedGStreamerTransition() override { stop(); }

    [[nodiscard]] bool is_transition_output() const noexcept override { return true; }

    [[nodiscard]] std::shared_ptr<PreparedSceneRenderGraph>
    transition_target() const noexcept override {
        return incoming_;
    }

    [[nodiscard]] std::shared_ptr<PreparedSceneRenderGraph>
    transition_origin() const noexcept override {
        return outgoing_;
    }

    void set_direct_output_enabled(const bool enabled) noexcept override {
        if (incoming_ != nullptr) {
            incoming_->set_direct_output_enabled(enabled);
        }
    }

    void start_transition() noexcept override {
        if (outgoing_control_ != nullptr) {
            outgoing_control_->set_direct_output_enabled(false);
            outgoing_control_.reset();
        }
        incoming_->set_direct_output_enabled(false);
        start_requested_.store(true);
        wakeup_.notify_all();
    }

    void stop() noexcept override {
        if (stopped_.exchange(true)) {
            return;
        }
        wake_frame_waiters();
        wakeup_.notify_all();
        if (feeder_.joinable() && feeder_.get_id() != std::this_thread::get_id()) {
            feeder_.join();
        }
        release_transition_pipeline();
        std::scoped_lock lock{frame_mutex_};
        latest_frame_.reset();
        latest_gpu_frame_.reset();
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    latest_frame() const noexcept override {
        try {
            if (completed_.load()) {
                return incoming_->latest_frame();
            }
            std::scoped_lock lock{frame_mutex_};
            if (latest_frame_ != nullptr) {
                return latest_frame_;
            }
            if (frozen_outgoing_cpu_ != nullptr) {
                return frozen_outgoing_cpu_;
            }
            return outgoing_ == nullptr ? nullptr : outgoing_->latest_frame();
        } catch (...) {
            return incoming_ == nullptr ? nullptr : incoming_->latest_frame();
        }
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    latest_gpu_frame() const noexcept override {
        try {
            if (completed_.load()) {
                return incoming_->latest_gpu_frame();
            }
            std::scoped_lock lock{frame_mutex_};
            if (latest_gpu_frame_ != nullptr) {
                return latest_gpu_frame_;
            }
            if (frozen_outgoing_gpu_ != nullptr) {
                return frozen_outgoing_gpu_;
            }
            return outgoing_ == nullptr ? nullptr : outgoing_->latest_gpu_frame();
        } catch (...) {
            return incoming_ == nullptr ? nullptr : incoming_->latest_gpu_frame();
        }
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(const std::uint64_t after_sequence,
                       const VideoFrameVisitor& visitor) const noexcept override {
        try {
            return visit_gstreamer_frame(latest_frame(), after_sequence, visitor);
        } catch (...) {
            return std::nullopt;
        }
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        try {
            if (completed_.load()) {
                return incoming_ != nullptr &&
                       incoming_->wait_for_frame(after_sequence, stop_token, deadline);
            }
            if (const auto frame = latest_frame();
                frame != nullptr && frame->sequence > after_sequence) {
                return true;
            }
            std::unique_lock lock{frame_mutex_};
            const auto wakeup_generation = frame_wakeup_generation_;
            static_cast<void>(frame_wakeup_.wait_until(
                lock, stop_token, deadline, [&] {
                    return stopped_.load() || completed_.load() ||
                           frame_wakeup_generation_ != wakeup_generation ||
                           (latest_frame_ != nullptr &&
                            latest_frame_->sequence > after_sequence);
                }));
            if (latest_frame_ != nullptr &&
                latest_frame_->sequence > after_sequence) {
                return true;
            }
            lock.unlock();
            const auto frame = latest_frame();
            return frame != nullptr && frame->sequence > after_sequence;
        } catch (...) {
            return false;
        }
    }

    void wake_frame_waiters() noexcept override {
        try {
            {
                std::scoped_lock lock{frame_mutex_};
                ++frame_wakeup_generation_;
            }
            frame_wakeup_.notify_all();
        } catch (...) {
        }
    }

  private:
    void build_pipeline(const OutputVideoFormat& format) {
        auto* raw_pipeline = gst_pipeline_new(nullptr);
        if (raw_pipeline == nullptr) {
            throw SceneRendererError{"transition_preparation_failed",
                                     "The transition pipeline could not be created"};
        }
        std::unique_ptr<GstElement, decltype(&release_pipeline)> pipeline{
            raw_pipeline, &release_pipeline};
        auto* context = gst_d3d11_context_new(device_.device);
        if (context == nullptr) {
            throw SceneRendererError{"renderer_device_unavailable",
                                     "The transition D3D11 context is unavailable"};
        }
        gst_element_set_context(pipeline.get(), context);
        gst_context_unref(context);
        pipeline_ = pipeline.get();

        outgoing_source_ = add_transition_input(pipeline.get());
        incoming_source_ = add_transition_input(pipeline.get());
        auto* compositor = add_live_compositor(pipeline.get());
        g_object_set(compositor, "background", 1, nullptr);
        outgoing_pad_ = link_transition_input(outgoing_source_, compositor, 0U);
        incoming_pad_ = link_transition_input(incoming_source_, compositor, 1U);
        apply_weights({.outgoing = 1.0, .incoming = 0.0});
        g_object_set(compositor, "emit-signals", TRUE, nullptr);
        g_signal_connect(compositor, "samples-selected",
                         G_CALLBACK(&PreparedGStreamerTransition::on_samples_selected),
                         this);

        auto* caps_filter = add_element(pipeline.get(), "capsfilter");
        const auto caps = output_caps(format);
        if (caps == nullptr) {
            throw SceneRendererError{"renderer_format_unsupported",
                                     "Transition output caps are unavailable"};
        }
        g_object_set(caps_filter, "caps", caps.get(), nullptr);
        auto* source_tee = add_element(pipeline.get(), "tee");
        require_link(compositor, caps_filter);
        require_link(caps_filter, source_tee);
        const auto readiness = add_readiness_gate(pipeline.get(), source_tee);
        output_readiness_valve_ = readiness.valve;
        add_gpu_output(readiness.output_tee);
        add_cpu_output(readiness.output_tee);

        pipeline_ = pipeline.release();
        bus_ = gst_element_get_bus(pipeline_);
        if (bus_ == nullptr ||
            gst_element_set_state(pipeline_, GST_STATE_PLAYING) ==
                GST_STATE_CHANGE_FAILURE) {
            stop();
            throw SceneRendererError{"transition_preparation_failed",
                                     "The transition pipeline could not start"};
        }
        try {
            feeder_ = std::thread([this] { feed(); });
        } catch (...) {
            stop();
            throw SceneRendererError{"renderer_worker_failed",
                                     "The transition worker could not start"};
        }
    }

    [[nodiscard]] static GstElement* add_transition_input(GstElement* pipeline) {
        auto* source = add_element(pipeline, "appsrc");
        g_object_set(source, "is-live", TRUE, "do-timestamp", TRUE, "format",
                     GST_FORMAT_TIME, "block", FALSE, "emit-signals", FALSE,
                     "max-buffers", static_cast<guint64>(1U), "max-bytes",
                     static_cast<guint64>(0U), "max-time", static_cast<guint64>(0U),
                     "leaky-type", 2, nullptr);
        return source;
    }

    [[nodiscard]] static GstPad* link_transition_input(GstElement* source,
                                                        GstElement* compositor,
                                                        const guint zorder) {
        auto* parent = gst_element_get_parent(source);
        if (parent == nullptr) {
            throw SceneRendererError{"transition_preparation_failed",
                                     "The transition input has no pipeline"};
        }
        auto* queue = add_element(GST_ELEMENT(parent), "queue");
        gst_object_unref(parent);
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        require_link(source, queue);
        auto* source_pad = gst_element_get_static_pad(queue, "src");
        auto* compositor_pad = gst_element_request_pad_simple(compositor, "sink_%u");
        if (source_pad == nullptr || compositor_pad == nullptr ||
            gst_pad_link(source_pad, compositor_pad) != GST_PAD_LINK_OK) {
            if (source_pad != nullptr) {
                gst_object_unref(source_pad);
            }
            if (compositor_pad != nullptr) {
                gst_object_unref(compositor_pad);
            }
            throw SceneRendererError{"transition_preparation_failed",
                                     "A transition input could not be linked"};
        }
        gst_object_unref(source_pad);
        g_object_set(compositor_pad, "zorder", zorder, nullptr);
        return compositor_pad;
    }

    void add_gpu_output(GstElement* tee) {
        auto* queue = add_element(pipeline_, "queue");
        auto* sink = add_element(pipeline_, "appsink");
        configure_sink(queue, sink, &PreparedGStreamerTransition::on_gpu_sample);
        connect_tee(tee, queue);
        require_link(queue, sink);
    }

    void add_cpu_output(GstElement* tee) {
        auto* queue = add_element(pipeline_, "queue");
        auto* convert = add_element(pipeline_, "d3d11convert");
        auto* gpu_caps_filter = add_element(pipeline_, "capsfilter");
        const std::unique_ptr<GstCaps, decltype(&release_caps)> gpu_caps{
            gst_caps_from_string(
                "video/x-raw(memory:D3D11Memory),format=NV12,"
                "pixel-aspect-ratio=1/1,colorimetry=bt709"),
            &release_caps};
        auto* download = add_element(pipeline_, "d3d11download");
        auto* cpu_caps_filter = add_element(pipeline_, "capsfilter");
        const std::unique_ptr<GstCaps, decltype(&release_caps)> cpu_caps{
            gst_caps_from_string(
                "video/x-raw,format=NV12,pixel-aspect-ratio=1/1,colorimetry=bt709"),
            &release_caps};
        auto* sink = add_element(pipeline_, "appsink");
        if (gpu_caps == nullptr || cpu_caps == nullptr) {
            throw SceneRendererError{"renderer_format_unsupported",
                                     "Transition conversion caps are unavailable"};
        }
        g_object_set(gpu_caps_filter, "caps", gpu_caps.get(), nullptr);
        g_object_set(cpu_caps_filter, "caps", cpu_caps.get(), nullptr);
        configure_sink(queue, sink, &PreparedGStreamerTransition::on_cpu_sample);
        connect_tee(tee, queue);
        require_link(queue, convert);
        require_link(convert, gpu_caps_filter);
        require_link(gpu_caps_filter, download);
        require_link(download, cpu_caps_filter);
        require_link(cpu_caps_filter, sink);
    }

    void configure_sink(GstElement* queue, GstElement* sink,
                        GstFlowReturn (*callback)(GstAppSink*, gpointer)) {
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE,
                     "wait-on-eos", FALSE, nullptr);
        gst_app_sink_set_max_buffers(GST_APP_SINK(sink), 1U);
        gst_app_sink_set_leaky_type(GST_APP_SINK(sink),
                                    GST_APP_LEAKY_TYPE_DOWNSTREAM);
        GstAppSinkCallbacks callbacks{};
        callbacks.new_sample = callback;
        gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, this, nullptr);
    }

    static GstFlowReturn on_gpu_sample(GstAppSink* sink, gpointer user_data) noexcept {
        return static_cast<PreparedGStreamerTransition*>(user_data)
            ->publish_sample(sink, true);
    }

    static GstFlowReturn on_cpu_sample(GstAppSink* sink, gpointer user_data) noexcept {
        return static_cast<PreparedGStreamerTransition*>(user_data)
            ->publish_sample(sink, false);
    }

    GstFlowReturn publish_sample(GstAppSink* sink, const bool gpu) noexcept {
        auto* sample = gst_app_sink_pull_sample(sink);
        if (sample == nullptr) {
            return GST_FLOW_EOS;
        }
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> sample_guard{
            sample, &gst_sample_unref};
        try {
            auto* caps = gst_sample_get_caps(sample);
            auto* buffer = gst_sample_get_buffer(sample);
            GstVideoInfo info{};
            if (caps == nullptr || buffer == nullptr ||
                gst_video_info_from_caps(&info, caps) == FALSE) {
                return GST_FLOW_ERROR;
            }
            const auto sequence = frame_sequence_->fetch_add(1U) + 1U;
            auto payload = std::make_shared<GStreamerRenderedFramePayload>(
                sample_guard.release(), device_.owner);
            const auto* format =
                gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info));
            if (format == nullptr) {
                return GST_FLOW_ERROR;
            }
            auto frame = std::make_shared<SourceFrame>(SourceFrame{
                .sequence = sequence,
                .stream_epoch = 1U,
                .discontinuity = false,
                .presentation_timestamp_ns = valid_clock_time(GST_BUFFER_PTS(buffer)),
                .duration_ns = valid_clock_time(GST_BUFFER_DURATION(buffer)),
                .received_monotonic_ns = monotonic_nanoseconds(),
                .width = static_cast<std::uint32_t>(GST_VIDEO_INFO_WIDTH(&info)),
                .height = static_cast<std::uint32_t>(GST_VIDEO_INFO_HEIGHT(&info)),
                .pixel_format = format,
                .memory = gpu ? SourceFrameMemory::d3d11
                              : SourceFrameMemory::system_memory,
                .payload = std::move(payload),
            });
            {
                std::scoped_lock lock{frame_mutex_};
                if (gpu) {
                    latest_gpu_frame_ = std::move(frame);
                } else {
                    latest_frame_ = std::move(frame);
                    ++frame_wakeup_generation_;
                }
            }
            if (!gpu) {
                frame_wakeup_.notify_all();
            }
            return GST_FLOW_OK;
        } catch (...) {
            return GST_FLOW_ERROR;
        }
    }

    [[nodiscard]] bool push_frame(GstElement* source,
                                  const std::shared_ptr<const SourceFrame>& frame) noexcept {
        try {
            const auto sample = gstreamer_sample(frame);
            if (!sample) {
                return false;
            }
            auto* original = gst_sample_get_buffer(sample.sample);
            auto* caps = gst_sample_get_caps(sample.sample);
            if (original == nullptr || caps == nullptr) {
                return false;
            }
            auto* buffer = gst_buffer_copy(original);
            if (buffer == nullptr) {
                return false;
            }
            GST_BUFFER_PTS(buffer) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DTS(buffer) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DURATION(buffer) = GST_CLOCK_TIME_NONE;
            auto* retimed = gst_sample_new(buffer, caps, nullptr, nullptr);
            gst_buffer_unref(buffer);
            if (retimed == nullptr) {
                return false;
            }
            const auto flow =
                gst_app_src_push_sample(GST_APP_SRC(source), retimed);
            gst_sample_unref(retimed);
            return flow == GST_FLOW_OK;
        } catch (...) {
            return false;
        }
    }

    void apply_weights(const SceneTransitionWeights weights) noexcept {
        g_object_set(outgoing_pad_, "alpha", weights.outgoing, nullptr);
        g_object_set(incoming_pad_, "alpha", weights.incoming, nullptr);
    }

    static void on_samples_selected(GstAggregator* aggregator, GstSegment*, guint64,
                                    guint64, guint64, GstStructure*,
                                    gpointer user_data) noexcept {
        static_cast<PreparedGStreamerTransition*>(user_data)
            ->mark_inputs_selected(aggregator);
    }

    void mark_inputs_selected(GstAggregator* aggregator) noexcept {
        try {
            std::unique_ptr<GstSample, decltype(&gst_sample_unref)> outgoing{
                gst_aggregator_peek_next_sample(
                    aggregator, GST_AGGREGATOR_PAD(outgoing_pad_)),
                &gst_sample_unref};
            std::unique_ptr<GstSample, decltype(&gst_sample_unref)> incoming{
                gst_aggregator_peek_next_sample(
                    aggregator, GST_AGGREGATOR_PAD(incoming_pad_)),
                &gst_sample_unref};
            if (outgoing != nullptr && incoming != nullptr &&
                !output_ready_.exchange(true)) {
                open_readiness_gate(output_readiness_valve_);
                wakeup_.notify_all();
            }
        } catch (...) {
        }
    }

    void feed() noexcept {
        std::optional<std::chrono::steady_clock::time_point> started_at;
        while (!stopped_.load()) {
            auto* error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
            if (error != nullptr) {
                gst_message_unref(error);
                break;
            }
            const auto outgoing =
                frozen_outgoing_gpu_ != nullptr
                    ? frozen_outgoing_gpu_
                    : (outgoing_ == nullptr ? nullptr
                                            : outgoing_->latest_gpu_frame());
            const auto incoming = incoming_->latest_gpu_frame();
            const bool have_outgoing = outgoing != nullptr &&
                                       push_frame(outgoing_source_, outgoing);
            const bool have_incoming = incoming != nullptr &&
                                       push_frame(incoming_source_, incoming);
            if (start_requested_.load() && have_outgoing && have_incoming &&
                output_ready_.load() && !started_at.has_value()) {
                started_at = std::chrono::steady_clock::now();
            }
            if (started_at.has_value()) {
                const auto elapsed = std::chrono::steady_clock::now() - *started_at;
                const auto duration = std::chrono::milliseconds{transition_.duration_ms};
                const auto progress =
                    std::chrono::duration<double>(elapsed).count() /
                    std::chrono::duration<double>(duration).count();
                apply_weights(scene_transition_weights(transition_, progress));
                if (progress >= 1.0) {
                    break;
                }
            }
            std::unique_lock lock{wakeup_mutex_};
            wakeup_.wait_for(lock, frame_interval_,
                             [this] { return stopped_.load(); });
        }
        if (!stopped_.load()) {
            incoming_->set_direct_output_enabled(true);
            outgoing_.reset();
            frozen_outgoing_gpu_.reset();
            frozen_outgoing_cpu_.reset();
            completed_.store(true);
            wake_frame_waiters();
            release_transition_pipeline();
        }
    }

    void release_transition_pipeline() noexcept {
        try {
            std::scoped_lock lock{pipeline_mutex_};
            auto* pipeline = std::exchange(pipeline_, nullptr);
            auto* bus = std::exchange(bus_, nullptr);
            release_bus(bus);
            // Stop and join GStreamer streaming callbacks before releasing the
            // request-pad references used by samples-selected.
            release_pipeline(pipeline);
            auto* outgoing_pad = std::exchange(outgoing_pad_, nullptr);
            auto* incoming_pad = std::exchange(incoming_pad_, nullptr);
            if (outgoing_pad != nullptr) {
                gst_object_unref(outgoing_pad);
            }
            if (incoming_pad != nullptr) {
                gst_object_unref(incoming_pad);
            }
        } catch (...) {
        }
    }

    std::shared_ptr<PreparedSceneRenderGraph> outgoing_{};
    std::shared_ptr<PreparedSceneRenderGraph> outgoing_control_{};
    std::shared_ptr<PreparedGStreamerSceneGraph> incoming_{};
    std::shared_ptr<const SourceFrame> frozen_outgoing_gpu_{};
    std::shared_ptr<const SourceFrame> frozen_outgoing_cpu_{};
    SceneTransitionSpec transition_{};
    GStreamerD3d11DeviceLease device_{};
    std::shared_ptr<std::atomic_uint64_t> frame_sequence_{};
    GstElement* pipeline_{nullptr};
    GstBus* bus_{nullptr};
    GstElement* output_readiness_valve_{nullptr};
    GstElement* outgoing_source_{nullptr};
    GstElement* incoming_source_{nullptr};
    GstPad* outgoing_pad_{nullptr};
    GstPad* incoming_pad_{nullptr};
    std::mutex pipeline_mutex_{};
    std::atomic_bool start_requested_{false};
    std::atomic_bool stopped_{false};
    std::atomic_bool completed_{false};
    std::atomic_bool output_ready_{false};
    std::thread feeder_{};
    std::mutex wakeup_mutex_{};
    std::condition_variable wakeup_{};
    std::chrono::nanoseconds frame_interval_{33'333'333};
    mutable std::mutex frame_mutex_{};
    mutable std::condition_variable_any frame_wakeup_{};
    std::uint64_t frame_wakeup_generation_{0U};
    std::shared_ptr<const SourceFrame> latest_frame_{};
    std::shared_ptr<const SourceFrame> latest_gpu_frame_{};
};

class GStreamerSceneRenderer final : public SceneRenderer {
  public:
    explicit GStreamerSceneRenderer(std::shared_ptr<SourceRuntimeFactory> source_factory)
        : source_factory_(std::move(source_factory)),
          source_frame_signal_(gstreamer_frame_signal(source_factory_)) {
        if (source_factory_ == nullptr || source_frame_signal_ == nullptr) {
            throw std::invalid_argument("Source runtime factory is required");
        }
    }

    [[nodiscard]] std::shared_ptr<PreparedSceneRenderGraph>
    prepare(const SceneRenderPreparation& preparation) override {
        if (closed_.load()) {
            throw SceneRendererError{"renderer_stopped", "The scene renderer is stopped"};
        }
        auto graph = std::make_shared<PreparedGStreamerSceneGraph>(
            preparation, gstreamer_d3d11_device(source_factory_),
            frame_sequences_[bus_index(preparation.bus)], source_frame_signal_);
        if (closed_.load()) {
            graph->stop();
            throw SceneRendererError{"renderer_stopped", "The scene renderer is stopped"};
        }
        if (preparation.bus == OutputBus::virtual_camera) {
            graph->set_direct_output_enabled(false);
        }
        return graph;
    }

    [[nodiscard]] SceneRenderTransitionPreparation
    prepare_transition(
        const OutputBus bus,
        const std::shared_ptr<PreparedSceneRenderGraph>& incoming,
        const SceneTransitionSpec& transition) override {
        validate_scene_transition(transition);
        if (bus == OutputBus::media_windows ||
            transition.kind == SceneTransitionKind::cut) {
            return {.effective_transition = {}};
        }
        const auto target =
            std::dynamic_pointer_cast<PreparedGStreamerSceneGraph>(incoming);
        if (target == nullptr) {
            return {
                .effective_transition = {},
                .fallback_applied = true,
                .fallback_reason = "transition_destination_unavailable",
            };
        }
        std::shared_ptr<PreparedSceneRenderGraph> outgoing;
        {
            std::scoped_lock lock{mutex_};
            outgoing = active_[bus_index(bus)];
        }
        if (outgoing == nullptr) {
            return {
                .effective_transition = {},
                .fallback_applied = true,
                .fallback_reason = "transition_origin_unavailable",
            };
        }
        auto transition_output = std::make_shared<PreparedGStreamerTransition>(
            std::move(outgoing), target, transition,
            gstreamer_d3d11_device(source_factory_),
            frame_sequences_[bus_index(bus)]);
        return {
            .effective_transition = transition,
            .render_output = std::move(transition_output),
        };
    }

    void commit_hydration(
        const std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U>& graphs,
        const std::array<bool, 2U>& enabled, std::uint64_t) noexcept override {
        try {
            std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U> previous;
            std::array<std::stop_source, 2U> previous_route_waits;
            {
                std::scoped_lock lock{mutex_};
                previous = active_;
                previous_route_waits = route_wait_cancellation_;
                active_ = graphs;
                enabled_ = enabled;
                for (std::size_t index = 0U; index < route_generation_.size();
                     ++index) {
                    ++route_generation_[index];
                    route_wait_cancellation_[index] = std::stop_source{};
                }
                if (active_[bus_index(OutputBus::virtual_camera)] != nullptr) {
                    active_[bus_index(OutputBus::virtual_camera)]
                        ->set_direct_output_enabled(true);
                }
            }
            for (auto& cancellation : previous_route_waits) {
                static_cast<void>(cancellation.request_stop());
            }
            for (auto& graph : previous) {
                if (graph != nullptr) {
                    graph->wake_frame_waiters();
                }
            }
            route_wakeup_.notify_all();
        } catch (...) {
        }
    }

    void commit_take(const OutputBus bus,
                     std::shared_ptr<PreparedSceneRenderGraph> target_graph,
                     std::shared_ptr<PreparedSceneRenderGraph> render_output,
        std::uint64_t) noexcept override {
        try {
            std::shared_ptr<PreparedSceneRenderGraph> previous;
            std::stop_source previous_route_wait;
            {
                std::scoped_lock lock{mutex_};
                const auto index = bus_index(bus);
                previous = active_[index];
                previous_route_wait = route_wait_cancellation_[index];
                if (render_output != nullptr) {
                    render_output->start_transition();
                    active_[index] = std::move(render_output);
                } else {
                    target_graph->set_direct_output_enabled(true);
                    active_[index] = std::move(target_graph);
                }
                ++route_generation_[index];
                route_wait_cancellation_[index] = std::stop_source{};
            }
            static_cast<void>(previous_route_wait.request_stop());
            if (previous != nullptr) {
                previous->wake_frame_waiters();
            }
            route_wakeup_.notify_all();
        } catch (...) {
        }
    }

    void set_output_enabled(const OutputBus bus, const bool enabled,
                            std::uint64_t) noexcept override {
        try {
            std::shared_ptr<PreparedSceneRenderGraph> wake_graph;
            std::stop_source previous_route_wait;
            {
                std::scoped_lock lock{mutex_};
                const auto index = bus_index(bus);
                previous_route_wait = route_wait_cancellation_[index];
                enabled_[index] = enabled;
                auto& active = active_[index];
                if (!enabled && active != nullptr &&
                    active->is_transition_output()) {
                    auto target = active->transition_target();
                    active->stop();
                    active = std::move(target);
                }
                if (active != nullptr && !active->is_transition_output()) {
                    active->set_direct_output_enabled(enabled);
                }
                wake_graph = active;
                ++route_generation_[index];
                route_wait_cancellation_[index] = std::stop_source{};
            }
            static_cast<void>(previous_route_wait.request_stop());
            if (wake_graph != nullptr) {
                wake_graph->wake_frame_waiters();
            }
            route_wakeup_.notify_all();
        } catch (...) {
        }
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    latest_frame(const OutputBus bus) const noexcept override {
        try {
            std::shared_ptr<PreparedSceneRenderGraph> graph;
            {
                std::scoped_lock lock{mutex_};
                if (!enabled_[bus_index(bus)]) {
                    return {};
                }
                graph = active_[bus_index(bus)];
            }
            return graph == nullptr ? nullptr : graph->latest_frame();
        } catch (...) {
            return {};
        }
    }

    [[nodiscard]] std::optional<std::uint64_t>
    visit_latest_frame(const OutputBus bus, const std::uint64_t after_sequence,
                       const VideoFrameVisitor& visitor) const noexcept override {
        try {
            std::shared_ptr<PreparedSceneRenderGraph> graph;
            {
                std::scoped_lock lock{mutex_};
                if (!enabled_[bus_index(bus)]) {
                    return std::nullopt;
                }
                graph = active_[bus_index(bus)];
            }
            return graph == nullptr
                       ? std::nullopt
                       : graph->visit_latest_frame(after_sequence, visitor);
        } catch (...) {
            return std::nullopt;
        }
    }

    [[nodiscard]] bool wait_for_frame(
        const OutputBus bus, const std::uint64_t after_sequence,
        const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        try {
            std::shared_ptr<PreparedSceneRenderGraph> graph;
            std::stop_token route_stop_token;
            {
                std::unique_lock lock{mutex_};
                const auto index = bus_index(bus);
                if (!enabled_[index] || active_[index] == nullptr) {
                    const auto route_generation = route_generation_[index];
                    static_cast<void>(route_wakeup_.wait_until(
                        lock, stop_token, deadline, [&] {
                            return closed_.load() ||
                                   route_generation_[index] != route_generation;
                        }));
                    if (closed_.load() || !enabled_[index] ||
                        active_[index] == nullptr) {
                        return false;
                    }
                }
                graph = active_[index];
                route_stop_token = route_wait_cancellation_[index].get_token();
            }
            if (graph == nullptr) {
                return false;
            }
            // A graph-level condition variable cannot atomically observe the
            // renderer's route mutex. Combine both cancellation domains so a
            // Take that lands between selecting the graph and entering its wait
            // remains observable even when the graph wake itself arrived early.
            std::stop_source combined_cancellation;
            std::stop_callback caller_cancel{
                stop_token, [&combined_cancellation] {
                    static_cast<void>(combined_cancellation.request_stop());
                }};
            std::stop_callback route_cancel{
                route_stop_token, [&combined_cancellation] {
                    static_cast<void>(combined_cancellation.request_stop());
                }};
            return graph->wait_for_frame(after_sequence,
                                         combined_cancellation.get_token(), deadline);
        } catch (...) {
            return false;
        }
    }

    void shutdown() noexcept override {
        if (closed_.exchange(true)) {
            return;
        }
        std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U> active;
        std::array<std::stop_source, 2U> route_waits;
        try {
            std::scoped_lock lock{mutex_};
            active.swap(active_);
            enabled_ = {};
            route_waits = route_wait_cancellation_;
            for (auto& generation : route_generation_) {
                ++generation;
            }
        } catch (...) {
            return;
        }
        for (auto& cancellation : route_waits) {
            static_cast<void>(cancellation.request_stop());
        }
        route_wakeup_.notify_all();
        for (auto& graph : active) {
            if (graph != nullptr) {
                graph->stop();
            }
        }
    }

  private:
    std::shared_ptr<SourceRuntimeFactory> source_factory_{};
    std::shared_ptr<GStreamerFrameSignal> source_frame_signal_{};
    std::array<std::shared_ptr<std::atomic_uint64_t>, 2U> frame_sequences_{
        std::make_shared<std::atomic_uint64_t>(0U),
        std::make_shared<std::atomic_uint64_t>(0U),
    };
    std::atomic_bool closed_{false};
    mutable std::mutex mutex_{};
    mutable std::condition_variable_any route_wakeup_{};
    std::array<std::uint64_t, 2U> route_generation_{};
    std::array<std::stop_source, 2U> route_wait_cancellation_{};
    std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U> active_{};
    std::array<bool, 2U> enabled_{};
};

} // namespace

std::shared_ptr<SceneRenderer> make_gstreamer_scene_renderer(
    std::shared_ptr<SourceRuntimeFactory> source_factory) {
    if (!gstreamer_d3d11_device(source_factory)) {
        throw SceneRendererError{"renderer_device_unavailable",
                                 "The shared D3D11 device is unavailable"};
    }
    return std::make_shared<GStreamerSceneRenderer>(std::move(source_factory));
}

#else

std::shared_ptr<SceneRenderer> make_gstreamer_scene_renderer(
    std::shared_ptr<SourceRuntimeFactory>) {
    throw SceneRendererError{"renderer_unavailable",
                             "The GStreamer scene renderer is unavailable"};
}

#endif

} // namespace solin::media_engine
