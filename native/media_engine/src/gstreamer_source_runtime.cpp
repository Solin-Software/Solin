#include "gstreamer_source_runtime.hpp"

#include "solin/media_engine/frame_channel.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/d3d11/gstd3d11.h>
#include <gst/gst.h>
#include <gst/video/video-info.h>
#include <gst/video/gstvideometa.h>
#endif

namespace solin::media_engine {

std::uint64_t GStreamerFrameSignal::revision() const noexcept {
    return revision_.load(std::memory_order_acquire);
}

void GStreamerFrameSignal::notify() noexcept {
    revision_.fetch_add(1U, std::memory_order_release);
    wakeup_.notify_all();
}

std::uint64_t
GStreamerFrameSignal::wait_after(const std::uint64_t revision) const noexcept {
    try {
        std::unique_lock lock{mutex_};
        wakeup_.wait(lock, [this, revision] {
            return revision_.load(std::memory_order_acquire) != revision;
        });
    } catch (...) {
    }
    return revision_.load(std::memory_order_acquire);
}

bool GStreamerFrameSignal::wait_after(
    const std::uint64_t revision, const std::stop_token stop_token,
    const std::chrono::steady_clock::time_point deadline) const noexcept {
    try {
        std::unique_lock lock{mutex_};
        return wakeup_.wait_until(lock, stop_token, deadline, [this, revision] {
            return revision_.load(std::memory_order_acquire) != revision;
        });
    } catch (...) {
        return false;
    }
}

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
namespace {

using namespace std::chrono_literals;

constexpr auto kBusPollInterval = 100ms;
constexpr auto kContentFrameInterval = 33'333'333ns;
constexpr auto kInitialReconnectDelay = 250ms;
constexpr auto kMaximumReconnectDelay = 4'000ms;
constexpr auto kFreshFrameThreshold = 2s;
constexpr auto kMinimumHealthyRun = 5s;
constexpr std::uint64_t kMinimumHealthyFrames = 30U;
constexpr std::uint32_t kMaximumSourceDimension = 3'840U;
constexpr std::uint64_t kMaximumSourcePixels = 3'840ULL * 2'160ULL;
constexpr std::uint32_t kMaximumSourceShortEdge = 2'160U;
constexpr std::uint32_t kMaximumSourceFramesPerSecond = 60U;
constexpr std::string_view kD3d11MemoryFeature = "memory:D3D11Memory";

void release_pipeline(GstElement* pipeline) noexcept {
    if (pipeline != nullptr) {
        static_cast<void>(gst_element_set_state(pipeline, GST_STATE_NULL));
        gst_object_unref(pipeline);
    }
}

void release_bus(GstBus* bus) noexcept {
    if (bus != nullptr) {
        gst_object_unref(bus);
    }
}

void release_caps(GstCaps* caps) noexcept {
    if (caps != nullptr) {
        gst_caps_unref(caps);
    }
}

[[nodiscard]] std::unique_ptr<GstCaps, decltype(&release_caps)> bounded_raw_caps(const bool d3d11) {
    const std::string prefix =
        d3d11 ? "video/x-raw(memory:D3D11Memory),format=BGRA,pixel-aspect-ratio=1/1"
              : "video/x-raw,format=BGRA,pixel-aspect-ratio=1/1";
    const auto serialized = prefix + ",width=[1,3840],height=[1,2160],framerate=[0/1,60/1];" +
                            prefix + ",width=[1,2160],height=[1,3840],framerate=[0/1,60/1]";
    return {
        gst_caps_from_string(serialized.c_str()),
        &release_caps,
    };
}

[[nodiscard]] std::unique_ptr<GstCaps, decltype(&release_caps)> exact_raw_caps(
    const bool d3d11, const std::uint32_t width, const std::uint32_t height) {
    // SourceFrame dimensions describe the visible raster. Pinning both the
    // raster and square PAR prevents converters from retaining the previous
    // size by introducing anamorphic pixels or embedded letterboxing.
    auto* caps = gst_caps_new_simple(
        "video/x-raw", "format", G_TYPE_STRING, "BGRA", "width", G_TYPE_INT,
        static_cast<gint>(width), "height", G_TYPE_INT, static_cast<gint>(height),
        "pixel-aspect-ratio", GST_TYPE_FRACTION, 1, 1, nullptr);
    if (caps != nullptr && d3d11) {
        gst_caps_set_features(caps, 0U,
                              gst_caps_features_new("memory:D3D11Memory", nullptr));
    }
    return {caps, &release_caps};
}

[[nodiscard]] std::unique_ptr<GstCaps, decltype(&release_caps)> bounded_decoded_caps() {
    return {
        gst_caps_from_string("video/x-raw,width=[1,3840],height=[1,2160],framerate=[0/1,60/1];"
                             "video/x-raw,width=[1,2160],height=[1,3840],framerate=[0/1,60/1]"),
        &release_caps,
    };
}

[[nodiscard]] std::unique_ptr<GstCaps, decltype(&release_caps)> bounded_local_input_caps() {
    return {
        gst_caps_from_string("video/x-raw,width=[1,3840],height=[1,2160],framerate=[1/1,60/1];"
                             "video/x-raw,width=[1,2160],height=[1,3840],framerate=[1/1,60/1];"
                             "image/jpeg,width=[1,3840],height=[1,2160],framerate=[1/1,60/1];"
                             "image/jpeg,width=[1,2160],height=[1,3840],framerate=[1/1,60/1];"
                             "video/x-h264,width=[1,3840],height=[1,2160],framerate=[1/1,60/1];"
                             "video/x-h264,width=[1,2160],height=[1,3840],framerate=[1/1,60/1]"),
        &release_caps,
    };
}

[[nodiscard]] bool is_bounded_video_caps(const GstCaps* caps) {
    GstVideoInfo info{};
    if (caps == nullptr || gst_caps_is_fixed(caps) == FALSE ||
        gst_video_info_from_caps(&info, caps) == FALSE) {
        return false;
    }
    const auto width = static_cast<std::uint64_t>(GST_VIDEO_INFO_WIDTH(&info));
    const auto height = static_cast<std::uint64_t>(GST_VIDEO_INFO_HEIGHT(&info));
    const auto fps_numerator = static_cast<std::uint64_t>(GST_VIDEO_INFO_FPS_N(&info));
    const auto fps_denominator = static_cast<std::uint64_t>(GST_VIDEO_INFO_FPS_D(&info));
    return width > 0U && height > 0U && width <= kMaximumSourceDimension &&
           height <= kMaximumSourceDimension && width * height <= kMaximumSourcePixels &&
           (std::min)(width, height) <= kMaximumSourceShortEdge && fps_denominator > 0U &&
           fps_numerator <=
               static_cast<std::uint64_t>(kMaximumSourceFramesPerSecond) * fps_denominator;
}

class GStreamerFramePayload final : public GStreamerSamplePayload {
  public:
    GStreamerFramePayload(GstSample* sample, std::shared_ptr<void> runtime_lifetime,
                          std::shared_ptr<GstD3D11Device> d3d11_device)
        : sample_(sample), runtime_lifetime_(std::move(runtime_lifetime)),
          d3d11_device_(std::move(d3d11_device)) {
        if (sample_ == nullptr) {
            throw std::invalid_argument("GStreamer frame sample is required");
        }
    }

    ~GStreamerFramePayload() override { gst_sample_unref(sample_); }

    [[nodiscard]] GstSample* sample() const noexcept override { return sample_; }

  private:
    GstSample* sample_{nullptr};
    std::shared_ptr<void> runtime_lifetime_{};
    std::shared_ptr<GstD3D11Device> d3d11_device_{};
};

[[nodiscard]] GstElement* add_element(GstElement* pipeline, const char* factory_name,
                                      const char* element_name) {
    auto* element = gst_element_factory_make(factory_name, element_name);
    if (element == nullptr) {
        throw std::runtime_error("required source pipeline element is unavailable");
    }
    if (gst_bin_add(GST_BIN(pipeline), element) == FALSE) {
        gst_object_unref(element);
        throw std::runtime_error("source pipeline element could not be added");
    }
    return element;
}

void require_link(GstElement* source, GstElement* sink) {
    if (gst_element_link(source, sink) == FALSE) {
        throw std::runtime_error("source pipeline elements could not be linked");
    }
}

[[nodiscard]] bool has_factory(const char* name) {
    auto* factory = gst_element_factory_find(name);
    if (factory == nullptr) {
        return false;
    }
    gst_object_unref(factory);
    return true;
}

[[nodiscard]] std::uint64_t monotonic_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                          std::chrono::steady_clock::now().time_since_epoch())
                                          .count());
}

[[nodiscard]] std::uint64_t valid_clock_time(const GstClockTime value) noexcept {
    return GST_CLOCK_TIME_IS_VALID(value) ? static_cast<std::uint64_t>(value) : 0U;
}

struct PadLinkContext final {
    explicit PadLinkContext(GstElement* target_value)
        : target(GST_ELEMENT(gst_object_ref(target_value))) {}
    ~PadLinkContext() { gst_object_unref(target); }

    GstElement* target{nullptr};
};

void destroy_pad_link_context(gpointer data, GClosure*) {
    delete static_cast<PadLinkContext*>(data);
}

void link_decoded_video_pad(GstElement*, GstPad* source_pad, gpointer data) {
    const auto* context = static_cast<PadLinkContext*>(data);
    auto* target_pad = gst_element_get_static_pad(context->target, "sink");
    if (target_pad == nullptr || gst_pad_is_linked(target_pad) != FALSE) {
        if (target_pad != nullptr) {
            gst_object_unref(target_pad);
        }
        return;
    }
    std::unique_ptr<GstCaps, decltype(&release_caps)> caps{gst_pad_get_current_caps(source_pad),
                                                           &release_caps};
    if (caps == nullptr) {
        caps.reset(gst_pad_query_caps(source_pad, nullptr));
    }
    const std::unique_ptr<GstCaps, decltype(&release_caps)> raw_video_caps{
        gst_caps_from_string("video/x-raw"), &release_caps};
    const bool is_video = caps != nullptr && raw_video_caps != nullptr &&
                          !gst_caps_is_empty(caps.get()) &&
                          gst_caps_can_intersect(caps.get(), raw_video_caps.get()) != FALSE;
    const bool within_budget = caps != nullptr && (gst_caps_is_fixed(caps.get()) == FALSE ||
                                                   is_bounded_video_caps(caps.get()));
    if (is_video && within_budget) {
        static_cast<void>(gst_pad_link(source_pad, target_pad));
    }
    gst_object_unref(target_pad);
}

struct RtspSetupContext final {
    bool use_tcp{true};
    std::uint32_t latency_ms{200U};
};

void configure_rtsp_source(GstElement*, GstElement* source, gpointer data) {
    const auto* context = static_cast<RtspSetupContext*>(data);
    auto* factory = gst_element_get_factory(source);
    if (factory == nullptr ||
        std::string_view{gst_plugin_feature_get_name(GST_PLUGIN_FEATURE(factory))} != "rtspsrc") {
        return;
    }
    constexpr guint udp_transport = 1U;
    constexpr guint tcp_transport = 4U;
    g_object_set(source, "latency", context->latency_ms, "drop-on-latency", TRUE, "protocols",
                 context->use_tcp ? tcp_transport : udp_transport, "tcp-timeout",
                 static_cast<guint64>(5'000'000U), "timeout", static_cast<guint64>(5'000'000U),
                 "teardown-timeout", static_cast<guint64>(100'000'000U), "retry", 3U, nullptr);
}

void destroy_rtsp_setup_context(gpointer data, GClosure*) {
    delete static_cast<RtspSetupContext*>(data);
}

[[nodiscard]] bool is_d3d11_failure_origin(const GstMessage* message) {
    if (message == nullptr || GST_MESSAGE_SRC(message) == nullptr ||
        GST_IS_ELEMENT(GST_MESSAGE_SRC(message)) == FALSE) {
        return false;
    }
    auto* element = GST_ELEMENT(GST_MESSAGE_SRC(message));
    auto* factory = gst_element_get_factory(element);
    const auto* factory_name =
        factory == nullptr ? nullptr : gst_plugin_feature_get_name(GST_PLUGIN_FEATURE(factory));
    if (factory_name != nullptr && std::string_view{factory_name}.starts_with("d3d11")) {
        return true;
    }
    const auto* element_name = GST_OBJECT_NAME(element);
    return element_name != nullptr && (std::string_view{element_name} == "canonical-input" ||
                                       std::string_view{element_name} == "canonical-converter" ||
                                       std::string_view{element_name} == "canonical-format");
}

[[nodiscard]] std::uint32_t gstreamer_color(const std::string& value) {
    const auto component = [&value](const std::size_t offset) {
        return static_cast<std::uint32_t>(std::stoul(value.substr(offset, 2U), nullptr, 16));
    };
    const auto red = component(1U);
    const auto green = component(3U);
    const auto blue = component(5U);
    const auto alpha = component(7U);
    return (alpha << 24U) | (red << 16U) | (green << 8U) | blue;
}

enum class RuntimeLifecycle : std::uint8_t {
    idle,
    running,
    stopping,
    stopped,
};

class D3d11PipelineError final : public std::runtime_error {
  public:
    explicit D3d11PipelineError(const char* message) : std::runtime_error(message) {}
};

class GStreamerD3d11DeviceManager final {
  public:
    explicit GStreamerD3d11DeviceManager(const bool allow_d3d11) {
        if (!allow_d3d11 || !has_factory("d3d11upload") || !has_factory("d3d11convert")) {
            return;
        }
        device_ = std::shared_ptr<GstD3D11Device>(gst_d3d11_device_new(0U, 0U),
                                                  [](GstD3D11Device* device) {
                                                      if (device != nullptr) {
                                                          gst_object_unref(device);
                                                      }
                                                  });
    }

    [[nodiscard]] std::shared_ptr<GstD3D11Device> acquire() const noexcept {
        std::scoped_lock lock{mutex_};
        return device_;
    }

    [[nodiscard]] bool is_current(const std::shared_ptr<GstD3D11Device>& device) const noexcept {
        std::scoped_lock lock{mutex_};
        return device_ != nullptr && device_ == device;
    }

    bool invalidate(const std::shared_ptr<GstD3D11Device>& expected = {}) noexcept {
        std::scoped_lock lock{mutex_};
        if (device_ == nullptr || (expected != nullptr && expected != device_)) {
            return false;
        }
        device_.reset();
        return true;
    }

  private:
    mutable std::mutex mutex_{};
    std::shared_ptr<GstD3D11Device> device_{};
};

class GStreamerSourceRuntime final : public SourceRuntime {
  public:
    GStreamerSourceRuntime(SceneSource source, const std::uint64_t,
                           std::shared_ptr<void> runtime_lifetime,
                           std::shared_ptr<GStreamerD3d11DeviceManager> d3d11_manager,
                           std::shared_ptr<GStreamerFrameSignal> frame_signal,
                           LocalCameraFormatResolver camera_format_resolver)
        : source_(std::move(source)), runtime_lifetime_(std::move(runtime_lifetime)),
          d3d11_manager_(std::move(d3d11_manager)), d3d11_device_(d3d11_manager_->acquire()),
          use_d3d11_(d3d11_device_ != nullptr),
          frame_signal_(std::move(frame_signal)),
          camera_format_resolver_(std::move(camera_format_resolver)) {
        if (frame_signal_ == nullptr) {
            throw std::invalid_argument("GStreamer frame signal is required");
        }
        if (source_.kind == SceneSourceKind::solin_content) {
            if (!source_.frame_channel.has_value()) {
                throw std::runtime_error("content_ingress_unavailable");
            }
            frame_channel_reader_ = make_frame_channel_reader(source_.frame_channel.value());
        }
    }

    ~GStreamerSourceRuntime() override { stop(); }

    void start() override {
        {
            std::scoped_lock lock{lifecycle_mutex_};
            if (lifecycle_ != RuntimeLifecycle::idle) {
                throw std::logic_error("source runtime was already started");
            }
            lifecycle_ = RuntimeLifecycle::running;
        }
        stop_requested_.store(false);
        try {
            worker_ = std::thread([this] { run(); });
        } catch (...) {
            {
                std::scoped_lock lock{lifecycle_mutex_};
                lifecycle_ = RuntimeLifecycle::idle;
            }
            lifecycle_changed_.notify_all();
            throw;
        }
    }

    // Concurrent callers wait for the same worker join; none can observe a half-stop.
    void stop() noexcept override {
        {
            std::unique_lock lock{lifecycle_mutex_};
            if (lifecycle_ == RuntimeLifecycle::stopping) {
                lifecycle_changed_.wait(lock,
                                        [this] { return lifecycle_ == RuntimeLifecycle::stopped; });
                return;
            }
            if (lifecycle_ == RuntimeLifecycle::stopped) {
                return;
            }
            if (lifecycle_ == RuntimeLifecycle::idle) {
                lifecycle_ = RuntimeLifecycle::stopped;
                lifecycle_changed_.notify_all();
                return;
            }
            if (lifecycle_ != RuntimeLifecycle::running) {
                return;
            }
            lifecycle_ = RuntimeLifecycle::stopping;
        }
        stop_requested_.store(true);
        if (frame_channel_reader_ != nullptr) {
            frame_channel_reader_->wake();
        }
        reconnect_wakeup_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
        release_content_frame();
        {
            std::scoped_lock lock{state_mutex_};
            health_.status = SourceRuntimeStatus::stopped;
            health_.error_code.clear();
        }
        frame_signal_->notify();
        {
            std::scoped_lock lock{lifecycle_mutex_};
            lifecycle_ = RuntimeLifecycle::stopped;
        }
        lifecycle_changed_.notify_all();
    }

    [[nodiscard]] SourceRuntimeHealth health() const override {
        std::scoped_lock lock{state_mutex_};
        return health_;
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame> latest_frame() const override {
        std::scoped_lock lock{state_mutex_};
        return latest_frame_;
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        const auto ready = [this, after_sequence] {
            const auto frame = latest_frame();
            return frame != nullptr && frame->sequence > after_sequence;
        };
        if (ready()) {
            return true;
        }
        const auto revision = frame_signal_->revision();
        if (ready()) {
            return true;
        }
        static_cast<void>(frame_signal_->wait_after(revision, stop_token, deadline));
        return ready();
    }

  private:
    struct PipelineTail final {
        GstElement* input{nullptr};
        GstElement* caps_filter{nullptr};
        GstElement* sink{nullptr};
    };

    [[nodiscard]] PipelineTail add_tail(GstElement* pipeline) {
        try {
            auto* input = add_element(pipeline, use_d3d11_ ? "d3d11upload" : "videoconvert",
                                      "canonical-input");
            GstElement* previous = input;
            if (use_d3d11_) {
                auto* converter = add_element(pipeline, "d3d11convert", "canonical-converter");
                require_link(previous, converter);
                previous = converter;
            }
            auto* caps_filter = add_element(pipeline, "capsfilter", "canonical-format");
            auto* sink = add_element(pipeline, "appsink", "latest-frame");
            require_link(previous, caps_filter);
            require_link(caps_filter, sink);

            const auto caps = bounded_raw_caps(use_d3d11_);
            if (caps == nullptr) {
                throw std::runtime_error("canonical source caps are unavailable");
            }
            g_object_set(caps_filter, "caps", caps.get(), nullptr);
            g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE, "wait-on-eos", FALSE,
                         nullptr);
            gst_app_sink_set_max_buffers(GST_APP_SINK(sink), 1U);
            gst_app_sink_set_leaky_type(GST_APP_SINK(sink), GST_APP_LEAKY_TYPE_DOWNSTREAM);
            GstAppSinkCallbacks callbacks{};
            callbacks.new_sample = &GStreamerSourceRuntime::on_new_sample;
            gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, this, nullptr);
            return {.input = input, .caps_filter = caps_filter, .sink = sink};
        } catch (const std::exception&) {
            if (use_d3d11_) {
                throw D3d11PipelineError{"D3D11 source tail could not be created"};
            }
            throw;
        }
    }

    [[nodiscard]] GstCaps*
    local_camera_caps(const LocalCameraSourceConfiguration& configuration) const {
        const auto pixels = static_cast<std::uint64_t>(configuration.width) *
                            static_cast<std::uint64_t>(configuration.height);
        if (configuration.width == 0U || configuration.height == 0U ||
            configuration.width > kMaximumSourceDimension ||
            configuration.height > kMaximumSourceDimension || pixels > kMaximumSourcePixels ||
            configuration.fps_numerator == 0U || configuration.fps_denominator == 0U ||
            configuration.fps_numerator >
                kMaximumSourceFramesPerSecond * configuration.fps_denominator ||
            configuration.media_type.empty() || configuration.pixel_format.empty()) {
            throw std::runtime_error("local_camera_format_unsupported");
        }
        auto* caps =
            gst_caps_new_simple(configuration.media_type.c_str(), "width", G_TYPE_INT,
                                static_cast<gint>(configuration.width), "height", G_TYPE_INT,
                                static_cast<gint>(configuration.height), "framerate",
                                GST_TYPE_FRACTION, static_cast<gint>(configuration.fps_numerator),
                                static_cast<gint>(configuration.fps_denominator), nullptr);
        if (caps == nullptr) {
            throw std::runtime_error("local camera caps could not be created");
        }
        if (configuration.media_type == "video/x-raw") {
            gst_caps_set_simple(caps, "format", G_TYPE_STRING, configuration.pixel_format.c_str(),
                                nullptr);
        }
        return caps;
    }

    void add_local_camera(GstElement* pipeline, GstElement* tail_input,
                          const LocalCameraSourceConfiguration& configuration) {
        auto resolved_configuration = configuration;
        if (configuration.width == 0U && camera_format_resolver_) {
            const auto resolved = camera_format_resolver_(configuration.device_id);
            if (resolved.has_value() &&
                resolved->device_id == configuration.device_id) {
                resolved_configuration = resolved.value();
                resolved_configuration.has_ptz_binding =
                    configuration.has_ptz_binding;
                resolved_configuration.keep_active = configuration.keep_active;
            }
        }
        auto* source = add_element(pipeline, "mfvideosrc", "local-camera");
        g_object_set(source, "do-timestamp", TRUE, nullptr);
        if (!resolved_configuration.device_id.empty()) {
            g_object_set(source, "device-path", resolved_configuration.device_id.c_str(),
                         nullptr);
        }
        const bool automatic_format = resolved_configuration.width == 0U;
        if (automatic_format) {
            auto* caps_filter = add_element(pipeline, "capsfilter", "camera-format-budget");
            auto* decoder = add_element(pipeline, "decodebin3", "camera-decoder");
            const auto input_caps = bounded_local_input_caps();
            const auto caps = bounded_decoded_caps();
            if (input_caps == nullptr || caps == nullptr) {
                throw std::runtime_error("automatic camera caps are unavailable");
            }
            g_object_set(caps_filter, "caps", input_caps.get(), nullptr);
            g_object_set(decoder, "caps", caps.get(), nullptr);
            require_link(source, caps_filter);
            require_link(caps_filter, decoder);
            g_signal_connect_data(decoder, "pad-added", G_CALLBACK(link_decoded_video_pad),
                                  new PadLinkContext{tail_input}, destroy_pad_link_context,
                                  static_cast<GConnectFlags>(0));
            return;
        }
        auto* caps_filter = add_element(pipeline, "capsfilter", "camera-format");
        const std::unique_ptr<GstCaps, decltype(&release_caps)> caps{
            local_camera_caps(resolved_configuration), &release_caps};
        g_object_set(caps_filter, "caps", caps.get(), nullptr);
        require_link(source, caps_filter);
        if (resolved_configuration.media_type == "video/x-raw") {
            require_link(caps_filter, tail_input);
            return;
        }
        auto* decoder = add_element(pipeline, "decodebin3", "camera-decoder");
        const auto decoded_caps = bounded_decoded_caps();
        if (decoded_caps == nullptr) {
            throw std::runtime_error("decoded camera caps are unavailable");
        }
        g_object_set(decoder, "caps", decoded_caps.get(), nullptr);
        require_link(caps_filter, decoder);
        g_signal_connect_data(decoder, "pad-added", G_CALLBACK(link_decoded_video_pad),
                              new PadLinkContext{tail_input}, destroy_pad_link_context,
                              static_cast<GConnectFlags>(0));
    }

    void add_rtsp_camera(GstElement* pipeline, GstElement* tail_input,
                         const RtspCameraSourceConfiguration& configuration) {
        auto* decoder = add_element(pipeline, "uridecodebin3", "rtsp-camera");
        const auto decoded_caps = bounded_decoded_caps();
        if (decoded_caps == nullptr) {
            throw std::runtime_error("decoded RTSP caps are unavailable");
        }
        g_object_set(decoder, "caps", decoded_caps.get(), nullptr);
        g_object_set(decoder, "uri", configuration.uri.c_str(), nullptr);
        g_signal_connect_data(decoder, "source-setup", G_CALLBACK(configure_rtsp_source),
                              new RtspSetupContext{configuration.use_tcp, configuration.latency_ms},
                              destroy_rtsp_setup_context, static_cast<GConnectFlags>(0));
        g_signal_connect_data(decoder, "pad-added", G_CALLBACK(link_decoded_video_pad),
                              new PadLinkContext{tail_input}, destroy_pad_link_context,
                              static_cast<GConnectFlags>(0));
    }

    void add_color_source(GstElement* pipeline, GstElement* tail_input,
                          const ColorSourceConfiguration& configuration) {
        auto* source = add_element(pipeline, "videotestsrc", "color-source");
        gst_util_set_object_arg(G_OBJECT(source), "pattern", "solid-color");
        g_object_set(source, "is-live", TRUE, "do-timestamp", TRUE, "foreground-color",
                     gstreamer_color(configuration.color), nullptr);
        require_link(source, tail_input);
    }

    void add_solin_content(GstElement* pipeline, GstElement* tail_input) {
        auto* source = add_element(pipeline, "appsrc", "content-ingress");
        g_object_set(source, "is-live", TRUE, "format", GST_FORMAT_TIME, "do-timestamp", TRUE,
                     "block", FALSE, nullptr);
        gst_app_src_set_max_buffers(GST_APP_SRC(source), 1U);
        gst_app_src_set_leaky_type(GST_APP_SRC(source), GST_APP_LEAKY_TYPE_DOWNSTREAM);
        require_link(source, tail_input);
        content_appsrc_ = GST_APP_SRC(source);
    }

    void push_content_frame() {
        if (content_appsrc_ == nullptr || frame_channel_reader_ == nullptr) {
            throw std::runtime_error("content_ingress_unavailable");
        }
        auto frame_lease = frame_channel_reader_->read_latest(content_pipeline_sequence_);
        if (!frame_lease.has_value()) {
            return;
        }
        const auto frame = frame_lease->frame();
        GstBuffer* buffer = nullptr;
        if (use_d3d11_) {
            auto lifetime =
                std::make_unique<FrameChannelFrameLease>(std::move(frame_lease.value()));
            buffer = gst_buffer_new_wrapped_full(
                GST_MEMORY_FLAG_READONLY,
                const_cast<std::uint8_t*>(lifetime->frame().bytes.data()),
                lifetime->frame().bytes.size(), 0U, lifetime->frame().bytes.size(),
                lifetime.get(), [](gpointer value) {
                    delete static_cast<FrameChannelFrameLease*>(value);
                });
            if (buffer != nullptr) {
                static_cast<void>(lifetime.release());
            }
        } else {
            buffer = gst_buffer_new_memdup(frame.bytes.data(), frame.bytes.size());
        }
        if (buffer == nullptr) {
            throw std::runtime_error("content ingress buffer allocation failed");
        }
        const auto video_format = frame.pixel_format == VideoFramePixelFormat::nv12
                                      ? GST_VIDEO_FORMAT_NV12
                                      : GST_VIDEO_FORMAT_BGRA;
        const auto plane_count = frame.pixel_format == VideoFramePixelFormat::nv12 ? 2U : 1U;
        gsize plane_offsets[GST_VIDEO_MAX_PLANES]{};
        gint plane_strides[GST_VIDEO_MAX_PLANES]{};
        for (guint index = 0U; index < plane_count; ++index) {
            if (frame.plane_strides[index] >
                static_cast<std::uint32_t>((std::numeric_limits<gint>::max)())) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content ingress stride is invalid");
            }
            plane_offsets[index] = static_cast<gsize>(frame.plane_offsets[index]);
            plane_strides[index] = static_cast<gint>(frame.plane_strides[index]);
        }
        if (gst_buffer_add_video_meta_full(
                buffer, GST_VIDEO_FRAME_FLAG_NONE, video_format, frame.width, frame.height,
                plane_count, plane_offsets, plane_strides) == nullptr) {
            gst_buffer_unref(buffer);
            throw std::runtime_error("content ingress video metadata allocation failed");
        }
        if (content_frame_caps_ == nullptr || content_frame_width_ != frame.width ||
            content_frame_height_ != frame.height ||
            content_frame_pixel_format_ != frame.pixel_format) {
            if (content_canonical_caps_filter_ == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content canonical caps are unavailable");
            }
            const auto canonical_caps = exact_raw_caps(use_d3d11_, frame.width, frame.height);
            if (canonical_caps == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content canonical caps could not be created");
            }
            g_object_set(content_canonical_caps_filter_, "caps", canonical_caps.get(), nullptr);
            const auto* pixel_format =
                frame.pixel_format == VideoFramePixelFormat::nv12 ? "NV12" : "BGRA";
            auto* replacement_caps = gst_caps_new_simple(
                "video/x-raw", "format", G_TYPE_STRING, pixel_format,
                "pixel-aspect-ratio", GST_TYPE_FRACTION, 1, 1, "width", G_TYPE_INT,
                static_cast<gint>(frame.width), "height", G_TYPE_INT,
                static_cast<gint>(frame.height), "framerate", GST_TYPE_FRACTION, 0, 1,
                nullptr);
            if (replacement_caps == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content ingress caps could not be created");
            }
            if (frame.pixel_format == VideoFramePixelFormat::nv12) {
                gst_caps_set_simple(replacement_caps, "colorimetry", G_TYPE_STRING,
                                    "bt709", nullptr);
            }
            if (content_frame_caps_ != nullptr) {
                gst_caps_unref(content_frame_caps_);
            }
            content_frame_caps_ = replacement_caps;
            content_frame_width_ = frame.width;
            content_frame_height_ = frame.height;
            content_frame_pixel_format_ = frame.pixel_format;
        }
        GST_BUFFER_PTS(buffer) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_DTS(buffer) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_DURATION(buffer) = static_cast<GstClockTime>(kContentFrameInterval.count());
        if (last_content_sequence_ == 0U ||
            frame.sequence != last_content_sequence_ + 1U) {
            GST_BUFFER_FLAG_SET(buffer, GST_BUFFER_FLAG_DISCONT);
        }
        auto* sample = gst_sample_new(buffer, content_frame_caps_, nullptr, nullptr);
        gst_buffer_unref(buffer);
        if (sample == nullptr) {
            throw std::runtime_error("content ingress sample allocation failed");
        }
        const auto flow = gst_app_src_push_sample(content_appsrc_, sample);
        gst_sample_unref(sample);
        if (flow != GST_FLOW_OK) {
            throw std::runtime_error("content ingress rejected a frame");
        }
        content_pipeline_sequence_ = frame.sequence;
        last_content_sequence_ = frame.sequence;
    }

    void release_content_frame() noexcept {
        if (content_frame_caps_ != nullptr) {
            gst_caps_unref(content_frame_caps_);
            content_frame_caps_ = nullptr;
        }
        content_frame_width_ = 0U;
        content_frame_height_ = 0U;
        content_frame_pixel_format_ = VideoFramePixelFormat::bgra;
        content_pipeline_sequence_ = 0U;
        last_content_sequence_ = 0U;
    }

    [[nodiscard]] GstElement* build_pipeline() {
        content_appsrc_ = nullptr;
        content_canonical_caps_filter_ = nullptr;
        content_pipeline_sequence_ = 0U;
        auto* raw_pipeline = gst_pipeline_new(nullptr);
        if (raw_pipeline == nullptr) {
            throw std::runtime_error("source pipeline could not be created");
        }
        std::unique_ptr<GstElement, decltype(&release_pipeline)> pipeline{raw_pipeline,
                                                                          &release_pipeline};
        if (use_d3d11_ && d3d11_device_ != nullptr) {
            auto* context = gst_d3d11_context_new(d3d11_device_.get());
            if (context == nullptr) {
                throw D3d11PipelineError{"D3D11 source context could not be created"};
            }
            gst_element_set_context(pipeline.get(), context);
            gst_context_unref(context);
        }
        const auto tail = add_tail(pipeline.get());
        switch (source_.kind) {
        case SceneSourceKind::solin_content:
            content_canonical_caps_filter_ = tail.caps_filter;
            add_solin_content(pipeline.get(), tail.input);
            break;
        case SceneSourceKind::local_camera:
            add_local_camera(pipeline.get(), tail.input,
                             std::get<LocalCameraSourceConfiguration>(source_.configuration));
            break;
        case SceneSourceKind::rtsp_camera:
            add_rtsp_camera(pipeline.get(), tail.input,
                            std::get<RtspCameraSourceConfiguration>(source_.configuration));
            break;
        case SceneSourceKind::color:
            add_color_source(pipeline.get(), tail.input,
                             std::get<ColorSourceConfiguration>(source_.configuration));
            break;
        default:
            throw std::runtime_error("source runtime kind is not implemented");
        }
        return pipeline.release();
    }

    [[nodiscard]] const char* stream_error_code() const noexcept {
        switch (source_.kind) {
        case SceneSourceKind::solin_content:
            return "content_ingress_failed";
        case SceneSourceKind::local_camera:
            return "local_camera_stream_failed";
        case SceneSourceKind::rtsp_camera:
            return "rtsp_stream_failed";
        case SceneSourceKind::color:
            return "generated_source_failed";
        default:
            return "source_stream_failed";
        }
    }

    [[nodiscard]] std::chrono::nanoseconds frame_timeout() const noexcept {
        if (source_.kind == SceneSourceKind::rtsp_camera) {
            const auto& configuration =
                std::get<RtspCameraSourceConfiguration>(source_.configuration);
            return (std::max)(5'000ms,
                              std::chrono::milliseconds{configuration.latency_ms * 3U} + 2s);
        }
        return source_.kind == SceneSourceKind::color ? 2s : 5s;
    }

    void publish_failure(const char* error_code) {
        {
            std::scoped_lock lock{state_mutex_};
            health_.status =
                ever_ready_ ? SourceRuntimeStatus::degraded : SourceRuntimeStatus::failed;
            health_.error_code = error_code;
        }
        frame_signal_->notify();
    }

    void publish_attempt_started() {
        {
            std::scoped_lock lock{state_mutex_};
            health_.status =
                ever_ready_ ? SourceRuntimeStatus::degraded : SourceRuntimeStatus::starting;
            // Retain the last actionable error while reconnecting. Clearing it at the
            // beginning of every retry made observers flicker between healthy and failed
            // even though no frame had recovered yet. The first delivered frame is the
            // authoritative recovery boundary and clears the error below.
        }
        frame_signal_->notify();
    }

    void run() noexcept {
        auto reconnect_delay = kInitialReconnectDelay;
        while (!stop_requested_.load()) {
            if (use_d3d11_ && !d3d11_manager_->is_current(d3d11_device_)) {
                use_d3d11_ = false;
                d3d11_device_.reset();
                std::scoped_lock lock{state_mutex_};
                latest_frame_.reset();
            }
            static_cast<void>(stream_epoch_.fetch_add(1U));
            last_sink_dropped_frames_.store(0U);
            publish_attempt_started();
            bool pipeline_failed = false;
            bool use_system_memory_fallback = false;
            try {
                std::unique_ptr<GstElement, decltype(&release_pipeline)> pipeline{
                    build_pipeline(), &release_pipeline};
                const std::unique_ptr<GstBus, decltype(&release_bus)> bus{
                    gst_element_get_bus(pipeline.get()), &release_bus};
                if (bus == nullptr) {
                    throw std::runtime_error("source pipeline could not start");
                }
                const auto state_change = gst_element_set_state(pipeline.get(), GST_STATE_PLAYING);
                if (state_change == GST_STATE_CHANGE_FAILURE) {
                    auto* state_error = gst_bus_timed_pop_filtered(bus.get(), 100U * GST_MSECOND,
                                                                   GST_MESSAGE_ERROR);
                    use_system_memory_fallback = use_d3d11_ && is_d3d11_failure_origin(state_error);
                    if (state_error != nullptr) {
                        gst_message_unref(state_error);
                    }
                    throw std::runtime_error("source pipeline could not start");
                }
                const auto sequence_before_attempt = frame_sequence_.load();
                const auto attempt_started = monotonic_nanoseconds();
                while (!stop_requested_.load()) {
                    if (use_d3d11_ && !d3d11_manager_->is_current(d3d11_device_)) {
                        pipeline_failed = true;
                        use_system_memory_fallback = true;
                        break;
                    }
                    if (source_.kind == SceneSourceKind::solin_content) {
                        push_content_frame();
                    }
                    GstMessage* message = nullptr;
                    if (source_.kind == SceneSourceKind::solin_content) {
                        static_cast<void>(
                            frame_channel_reader_->wait_for_frame(kBusPollInterval));
                        message = gst_bus_pop_filtered(
                            bus.get(), static_cast<GstMessageType>(GST_MESSAGE_ERROR |
                                                                  GST_MESSAGE_EOS));
                    } else {
                        message = gst_bus_timed_pop_filtered(
                            bus.get(),
                            static_cast<GstClockTime>(kBusPollInterval.count()) *
                                GST_MSECOND,
                            static_cast<GstMessageType>(GST_MESSAGE_ERROR |
                                                        GST_MESSAGE_EOS));
                    }
                    if (message == nullptr) {
                        const auto current_sequence = frame_sequence_.load();
                        const auto last_frame = last_frame_received_ns_.load();
                        const auto reference = current_sequence > sequence_before_attempt
                                                   ? last_frame
                                                   : attempt_started;
                        const auto now = monotonic_nanoseconds();
                        const auto retained_content_frame =
                            source_.kind == SceneSourceKind::solin_content &&
                            content_pipeline_sequence_ != 0U &&
                            current_sequence > sequence_before_attempt;
                        if (!retained_content_frame && reference <= now &&
                            now - reference > static_cast<std::uint64_t>(frame_timeout().count())) {
                            pipeline_failed = true;
                            publish_failure("source_frame_timeout");
                            break;
                        }
                        continue;
                    }
                    pipeline_failed = true;
                    use_system_memory_fallback = use_d3d11_ && is_d3d11_failure_origin(message);
                    gst_message_unref(message);
                    break;
                }
                const auto frames_in_attempt = frame_sequence_.load() - sequence_before_attempt;
                const auto last_frame = last_frame_received_ns_.load();
                const auto now = monotonic_nanoseconds();
                const auto minimum_healthy_run = static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(kMinimumHealthyRun)
                        .count());
                if (frames_in_attempt >= kMinimumHealthyFrames && attempt_started <= now &&
                    now - attempt_started >= minimum_healthy_run && last_frame <= now &&
                    now - last_frame <= static_cast<std::uint64_t>(
                                            std::chrono::duration_cast<std::chrono::nanoseconds>(
                                                kFreshFrameThreshold)
                                                .count())) {
                    reconnect_delay = kInitialReconnectDelay;
                }
            } catch (const D3d11PipelineError&) {
                pipeline_failed = true;
                use_system_memory_fallback = use_d3d11_;
            } catch (const std::exception&) {
                pipeline_failed = true;
            }
            content_appsrc_ = nullptr;
            content_canonical_caps_filter_ = nullptr;
            if (stop_requested_.load()) {
                break;
            }
            use_system_memory_fallback = use_system_memory_fallback ||
                                         (use_d3d11_ && !d3d11_manager_->is_current(d3d11_device_));
            if (pipeline_failed) {
                const auto current_health = health();
                if (current_health.error_code != "source_frame_timeout") {
                    publish_failure(stream_error_code());
                }
            }
            if (use_system_memory_fallback) {
                // A source-local D3D11 negotiation failure does not imply that the
                // shared compositor device was lost. Invalidating it here made every
                // later scene preparation fail with renderer_device_unavailable even
                // though the active compositor was still rendering successfully.
                // Keep the shared device alive and degrade only this source pipeline
                // to system memory; d3d11upload remains the renderer boundary.
                use_d3d11_ = false;
                d3d11_device_.reset();
                std::scoped_lock lock{state_mutex_};
                latest_frame_.reset();
            }
            {
                std::scoped_lock lock{state_mutex_};
                ++health_.reconnect_count;
            }
            if (use_system_memory_fallback) {
                reconnect_delay = kInitialReconnectDelay;
                continue;
            }
            std::unique_lock lock{reconnect_mutex_};
            reconnect_wakeup_.wait_for(lock, reconnect_delay,
                                       [this] { return stop_requested_.load(); });
            reconnect_delay = (std::min)(reconnect_delay * 2, kMaximumReconnectDelay);
        }
    }

    static GstFlowReturn on_new_sample(GstAppSink* sink, gpointer data) {
        return static_cast<GStreamerSourceRuntime*>(data)->receive_sample(sink);
    }

    GstFlowReturn receive_sample(GstAppSink* sink) noexcept {
        auto* sample = gst_app_sink_pull_sample(sink);
        if (sample == nullptr) {
            return GST_FLOW_EOS;
        }
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> sample_guard{sample,
                                                                             &gst_sample_unref};
        try {
            const auto* caps = gst_sample_get_caps(sample);
            const auto* buffer = gst_sample_get_buffer(sample);
            GstVideoInfo info{};
            if (caps == nullptr || buffer == nullptr ||
                gst_video_info_from_caps(&info, caps) == FALSE) {
                return GST_FLOW_ERROR;
            }
            const auto* features = gst_caps_get_features(caps, 0U);
            const bool d3d11 =
                features != nullptr &&
                gst_caps_features_contains(features, kD3d11MemoryFeature.data()) != FALSE;
            if (d3d11 && !d3d11_manager_->is_current(d3d11_device_)) {
                return GST_FLOW_ERROR;
            }
            const auto stream_epoch = stream_epoch_.load();
            const auto* raw_pixel_format = gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info));
            if (raw_pixel_format == nullptr) {
                return GST_FLOW_ERROR;
            }
            if (!is_bounded_video_caps(caps)) {
                return GST_FLOW_ERROR;
            }
            const auto sequence = frame_sequence_.fetch_add(1U) + 1U;
            auto payload = std::make_shared<GStreamerFramePayload>(
                sample_guard.get(), runtime_lifetime_, d3d11_device_);
            static_cast<void>(sample_guard.release());
            const auto received_at = monotonic_nanoseconds();
            bool discontinuity = false;
            {
                std::scoped_lock lock{state_mutex_};
                discontinuity = stream_epoch != last_published_epoch_;
                last_published_epoch_ = stream_epoch;
            }
            auto frame = std::make_shared<SourceFrame>(SourceFrame{
                .sequence = sequence,
                .stream_epoch = stream_epoch,
                .discontinuity = discontinuity,
                .presentation_timestamp_ns = valid_clock_time(GST_BUFFER_PTS(buffer)),
                .duration_ns = valid_clock_time(GST_BUFFER_DURATION(buffer)),
                .received_monotonic_ns = received_at,
                .width = static_cast<std::uint32_t>(GST_VIDEO_INFO_WIDTH(&info)),
                .height = static_cast<std::uint32_t>(GST_VIDEO_INFO_HEIGHT(&info)),
                .pixel_format = raw_pixel_format,
                .memory = d3d11 ? SourceFrameMemory::d3d11 : SourceFrameMemory::system_memory,
                .payload = std::move(payload),
            });
            {
                std::scoped_lock lock{state_mutex_};
                latest_frame_ = frame;
                ever_ready_ = true;
                health_.status = SourceRuntimeStatus::ready;
                health_.error_code.clear();
                health_.frame_sequence = sequence;
                guint64 dropped_frames = 0U;
                g_object_get(sink, "dropped", &dropped_frames, nullptr);
                const auto current_dropped = static_cast<std::uint64_t>(dropped_frames);
                const auto previous_dropped = last_sink_dropped_frames_.exchange(current_dropped);
                dropped_frames_total_.fetch_add(current_dropped >= previous_dropped
                                                    ? current_dropped - previous_dropped
                                                    : current_dropped);
                health_.dropped_frames = dropped_frames_total_.load();
            }
            last_frame_received_ns_.store(received_at);
            frame_signal_->notify();
            return GST_FLOW_OK;
        } catch (const std::exception&) {
            return GST_FLOW_ERROR;
        }
    }

    SceneSource source_{};
    std::atomic_bool stop_requested_{false};
    std::atomic_uint64_t frame_sequence_{0U};
    std::atomic_uint64_t last_frame_received_ns_{0U};
    std::atomic_uint64_t stream_epoch_{0U};
    std::atomic_uint64_t last_sink_dropped_frames_{0U};
    std::atomic_uint64_t dropped_frames_total_{0U};
    std::thread worker_{};
    mutable std::mutex lifecycle_mutex_{};
    std::condition_variable lifecycle_changed_{};
    RuntimeLifecycle lifecycle_{RuntimeLifecycle::idle};
    mutable std::mutex state_mutex_{};
    SourceRuntimeHealth health_{.status = SourceRuntimeStatus::starting};
    std::shared_ptr<const SourceFrame> latest_frame_{};
    bool ever_ready_{false};
    std::uint64_t last_published_epoch_{0U};
    std::shared_ptr<void> runtime_lifetime_{};
    std::shared_ptr<GStreamerD3d11DeviceManager> d3d11_manager_{};
    std::shared_ptr<GstD3D11Device> d3d11_device_{};
    bool use_d3d11_{false};
    std::shared_ptr<GStreamerFrameSignal> frame_signal_{};
    LocalCameraFormatResolver camera_format_resolver_{};
    std::unique_ptr<FrameChannelReader> frame_channel_reader_{};
    GstAppSrc* content_appsrc_{nullptr};
    GstElement* content_canonical_caps_filter_{nullptr};
    GstCaps* content_frame_caps_{nullptr};
    std::uint32_t content_frame_width_{0U};
    std::uint32_t content_frame_height_{0U};
    VideoFramePixelFormat content_frame_pixel_format_{VideoFramePixelFormat::bgra};
    std::uint64_t last_content_sequence_{0U};
    std::uint64_t content_pipeline_sequence_{0U};
    std::mutex reconnect_mutex_{};
    std::condition_variable reconnect_wakeup_{};
};

class GStreamerSourceRuntimeFactory final : public SourceRuntimeFactory {
  public:
    GStreamerSourceRuntimeFactory(std::shared_ptr<void> runtime_lifetime, const bool allow_d3d11,
                                  LocalCameraFormatResolver camera_format_resolver)
        : runtime_lifetime_(std::move(runtime_lifetime)),
          d3d11_manager_(std::make_shared<GStreamerD3d11DeviceManager>(allow_d3d11)),
          frame_signal_(std::make_shared<GStreamerFrameSignal>()),
          camera_format_resolver_(std::move(camera_format_resolver)) {
        if (runtime_lifetime_ == nullptr) {
            throw std::invalid_argument("GStreamer runtime lifetime is required");
        }
    }

    [[nodiscard]] std::shared_ptr<SourceRuntime> create(const SceneSource& source,
                                                        const std::uint64_t generation) override {
        if (source.kind != SceneSourceKind::solin_content &&
            source.kind != SceneSourceKind::local_camera &&
            source.kind != SceneSourceKind::rtsp_camera && source.kind != SceneSourceKind::color) {
            throw std::runtime_error("source_runtime_not_implemented");
        }
        return std::make_shared<GStreamerSourceRuntime>(source, generation, runtime_lifetime_,
                                                        d3d11_manager_, frame_signal_,
                                                        camera_format_resolver_);
    }

    [[nodiscard]] std::shared_ptr<GstD3D11Device> d3d11_device() const noexcept {
        return d3d11_manager_->acquire();
    }

    bool invalidate_d3d11_device() noexcept { return d3d11_manager_->invalidate(); }

    [[nodiscard]] std::shared_ptr<GStreamerFrameSignal> frame_signal() const noexcept {
        return frame_signal_;
    }

  private:
    std::shared_ptr<void> runtime_lifetime_{};
    std::shared_ptr<GStreamerD3d11DeviceManager> d3d11_manager_{};
    std::shared_ptr<GStreamerFrameSignal> frame_signal_{};
    LocalCameraFormatResolver camera_format_resolver_{};
};

} // namespace

std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(std::shared_ptr<void> runtime_lifetime) {
    return make_gstreamer_source_runtime_factory(std::move(runtime_lifetime), true);
}

std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(std::shared_ptr<void> runtime_lifetime,
                                      const bool allow_d3d11) {
    return make_gstreamer_source_runtime_factory(std::move(runtime_lifetime), allow_d3d11, {});
}

std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(
    std::shared_ptr<void> runtime_lifetime, const bool allow_d3d11,
    LocalCameraFormatResolver camera_format_resolver) {
    return std::make_shared<GStreamerSourceRuntimeFactory>(std::move(runtime_lifetime),
                                                           allow_d3d11,
                                                           std::move(camera_format_resolver));
}

GStreamerSampleLease gstreamer_sample(std::shared_ptr<const SourceFrame> frame) noexcept {
    const auto* payload = frame == nullptr
                              ? nullptr
                              : dynamic_cast<const GStreamerSamplePayload*>(frame->payload.get());
    return {.frame = std::move(frame), .sample = payload == nullptr ? nullptr : payload->sample()};
}

GStreamerD3d11DeviceLease
gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory> factory) noexcept {
    const auto* typed = dynamic_cast<const GStreamerSourceRuntimeFactory*>(factory.get());
    auto owner = typed == nullptr ? nullptr : typed->d3d11_device();
    return {.factory = std::move(factory), .owner = owner, .device = owner.get()};
}

std::shared_ptr<GStreamerFrameSignal>
gstreamer_frame_signal(std::shared_ptr<SourceRuntimeFactory> factory) noexcept {
    const auto* typed = dynamic_cast<const GStreamerSourceRuntimeFactory*>(factory.get());
    return typed == nullptr ? nullptr : typed->frame_signal();
}

bool invalidate_gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory> factory) noexcept {
    auto* typed = dynamic_cast<GStreamerSourceRuntimeFactory*>(factory.get());
    return typed != nullptr && typed->invalidate_d3d11_device();
}

#else

std::shared_ptr<SourceRuntimeFactory> make_gstreamer_source_runtime_factory(std::shared_ptr<void>) {
    throw std::runtime_error("media_runtime_unavailable");
}

std::shared_ptr<SourceRuntimeFactory> make_gstreamer_source_runtime_factory(std::shared_ptr<void>,
                                                                            bool) {
    throw std::runtime_error("media_runtime_unavailable");
}

GStreamerSampleLease gstreamer_sample(std::shared_ptr<const SourceFrame> frame) noexcept {
    return {.frame = std::move(frame), .sample = nullptr};
}

GStreamerD3d11DeviceLease
gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory> factory) noexcept {
    return {.factory = std::move(factory), .owner = nullptr, .device = nullptr};
}

std::shared_ptr<GStreamerFrameSignal>
gstreamer_frame_signal(std::shared_ptr<SourceRuntimeFactory>) noexcept {
    return {};
}

bool invalidate_gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory>) noexcept {
    return false;
}

#endif

} // namespace solin::media_engine
