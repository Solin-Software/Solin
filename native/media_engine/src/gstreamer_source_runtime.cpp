#include "gstreamer_source_runtime.hpp"
#include "gstreamer_error_diagnostics.hpp"
#include "gstreamer_frame_transition.hpp"

#include "solin/media_engine/content_image_framing.hpp"
#include "solin/media_engine/frame_channel.hpp"
#include "solin/media_engine/local_camera_discovery.hpp"
#include "solin/media_engine/presentation_transition.hpp"
#ifdef _WIN32
#include "windows_d3d11_frame_channel.hpp"
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <numeric>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

#include <nlohmann/json.hpp>

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
constexpr auto kContentTransitionFrameInterval = 16ms;
constexpr std::uint64_t kTransitionSubmissionFps = 60U;
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

class ContentGpuTransitionPipeline final {
  public:
    struct Output final {
        std::uint64_t revision{0U};
        std::uint64_t submission{0U};
        GstSample* sample{nullptr};
    };

    ContentGpuTransitionPipeline(const bool use_d3d11,
                                 std::shared_ptr<GstD3D11Device> device,
                                 const std::uint32_t width,
                                 const std::uint32_t height,
                                 const std::uint8_t input_count)
        : use_d3d11_(use_d3d11), device_(std::move(device)),
          input_count_(input_count) {
        if (width == 0U || height == 0U || input_count_ == 0U ||
            input_count_ > sources_.size() || (use_d3d11_ && device_ == nullptr)) {
            throw std::invalid_argument("content transition output is invalid");
        }
        build(width, height);
    }

    ~ContentGpuTransitionPipeline() { close(); }

    ContentGpuTransitionPipeline(const ContentGpuTransitionPipeline&) = delete;
    ContentGpuTransitionPipeline& operator=(const ContentGpuTransitionPipeline&) = delete;

    [[nodiscard]] bool render(const std::shared_ptr<const SourceFrame>& outgoing,
                              const std::shared_ptr<const SourceFrame>& incoming,
                              const SceneTransitionWeights weights) noexcept {
        try {
            if (pipeline_ == nullptr || bus_ == nullptr || sources_[0] == nullptr ||
                pads_[0] == nullptr) {
                return false;
            }
            if (auto* error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
                error != nullptr) {
                gst_message_unref(error);
                return false;
            }
            const bool two_inputs = outgoing != nullptr && incoming != nullptr;
            if (two_inputs && input_count_ < 2U) {
                return false;
            }
            const auto submission = submission_sequence_.fetch_add(1U) + 1U;
            const auto alphas = scene_transition_source_over_alphas(weights);
            g_object_set(pads_[0], "alpha",
                         outgoing != nullptr ? alphas.outgoing : alphas.incoming,
                         nullptr);
            if (pads_[1] != nullptr) {
                g_object_set(pads_[1], "alpha", two_inputs ? alphas.incoming : 0.0,
                             nullptr);
            }
            if (outgoing != nullptr &&
                !push(sources_[0], outgoing, submission)) {
                return false;
            }
            if (incoming != nullptr &&
                !push(sources_[two_inputs ? 1U : 0U], incoming, submission)) {
                return false;
            }
            latest_submission_.store(submission, std::memory_order_release);
            return outgoing != nullptr || incoming != nullptr;
        } catch (...) {
            return false;
        }
    }

    [[nodiscard]] bool render_framed(
        const std::shared_ptr<const SourceFrame>& frame,
        const ContentImagePlacement placement) noexcept {
        try {
            if (pipeline_ == nullptr || bus_ == nullptr || sources_[0] == nullptr ||
                pads_[0] == nullptr || frame == nullptr) {
                return false;
            }
            if (!resume()) {
                return false;
            }
            if (auto* error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
                error != nullptr) {
                gst_message_unref(error);
                return false;
            }
            const auto visible = placement.visible();
            const auto submission = submission_sequence_.fetch_add(1U) + 1U;
            g_object_set(
                pads_[0], "xpos", static_cast<gint>(placement.destination_x),
                "ypos", static_cast<gint>(placement.destination_y), "width",
                static_cast<gint>(placement.destination_width), "height",
                static_cast<gint>(placement.destination_height), "alpha",
                visible ? 1.0 : 0.0, "sizing-policy", 0, nullptr);
            if (pads_[1] != nullptr) {
                g_object_set(pads_[1], "alpha", 0.0, nullptr);
            }
            if (!push(sources_[0], frame, submission,
                      visible ? &placement : nullptr)) {
                return false;
            }
            latest_submission_.store(submission, std::memory_order_release);
            return true;
        } catch (...) {
            return false;
        }
    }

    [[nodiscard]] std::uint64_t revision() const noexcept {
        return output_revision_.load(std::memory_order_acquire);
    }

    [[nodiscard]] std::uint64_t submission() const noexcept {
        return latest_submission_.load(std::memory_order_acquire);
    }

    [[nodiscard]] Output output_after(const std::uint64_t revision) const noexcept {
        try {
            std::scoped_lock lock{output_mutex_};
            const auto current = output_revision_.load(std::memory_order_acquire);
            if (current <= revision || latest_output_ == nullptr) {
                return {};
            }
            return {
                .revision = current,
                .submission = latest_output_submission_,
                .sample = gst_sample_ref(latest_output_),
            };
        } catch (...) {
            return {};
        }
    }

  private:
    void close() noexcept {
        release_pipeline(pipeline_);
        pipeline_ = nullptr;
        running_ = false;
        for (auto*& pad : pads_) {
            if (pad != nullptr) {
                gst_object_unref(pad);
                pad = nullptr;
            }
        }
        if (bus_ != nullptr) {
            gst_object_unref(bus_);
            bus_ = nullptr;
        }
        std::scoped_lock lock{output_mutex_};
        if (latest_output_ != nullptr) {
            gst_sample_unref(latest_output_);
            latest_output_ = nullptr;
        }
    }
    [[nodiscard]] GstElement* add_input(GstElement* compositor,
                                        const std::size_t index) {
        auto* source = add_element(pipeline_, "appsrc", nullptr);
        auto* queue = add_element(pipeline_, "queue", nullptr);
        auto* upload = add_element(
            pipeline_, use_d3d11_ ? "d3d11upload" : "videoconvert", nullptr);
        GstElement* convert = upload;
        if (use_d3d11_) {
            convert = add_element(pipeline_, "d3d11convert", nullptr);
        }
        // This graph advances only when its owner submits a complete frame set.
        // A shared explicit timestamp across every input makes the compositor
        // wait for that exact set and turns the output PTS into a causal ACK.
        g_object_set(source, "is-live", FALSE, "format", GST_FORMAT_TIME,
                     "do-timestamp", FALSE, "block", FALSE, nullptr);
        gst_app_src_set_max_buffers(GST_APP_SRC(source), 1U);
        gst_app_src_set_leaky_type(GST_APP_SRC(source), GST_APP_LEAKY_TYPE_DOWNSTREAM);
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        require_link(source, queue);
        require_link(queue, upload);
        if (convert != upload) {
            require_link(upload, convert);
        }
        auto* source_pad = gst_element_get_static_pad(convert, "src");
        auto* compositor_pad = gst_element_request_pad_simple(compositor, "sink_%u");
        if (source_pad == nullptr || compositor_pad == nullptr ||
            gst_pad_link(source_pad, compositor_pad) != GST_PAD_LINK_OK) {
            if (source_pad != nullptr) {
                gst_object_unref(source_pad);
            }
            if (compositor_pad != nullptr) {
                gst_object_unref(compositor_pad);
            }
            throw std::runtime_error("content transition input could not be linked");
        }
        gst_object_unref(source_pad);
        g_object_set(compositor_pad, "alpha", 0.0,
                     "zorder", static_cast<guint>(index), nullptr);
        pads_[index] = compositor_pad;
        sources_[index] = GST_APP_SRC(source);
        return source;
    }

    void build(const std::uint32_t width, const std::uint32_t height) {
        pipeline_ = gst_pipeline_new(nullptr);
        if (pipeline_ == nullptr) {
            throw std::runtime_error("content transition pipeline could not be created");
        }
        try {
            if (use_d3d11_) {
                auto* context = gst_d3d11_context_new(device_.get());
                if (context == nullptr) {
                    throw D3d11PipelineError{
                        "D3D11 content transition context could not be created"};
                }
                gst_element_set_context(pipeline_, context);
                gst_context_unref(context);
            }
            auto* compositor = gst_element_factory_make_full(
                use_d3d11_ ? "d3d11compositor" : "compositor",
                "force-live", FALSE, nullptr);
            if (compositor == nullptr ||
                gst_bin_add(GST_BIN(pipeline_), compositor) == FALSE) {
                if (compositor != nullptr) {
                    gst_object_unref(compositor);
                }
                throw std::runtime_error(
                    "content transition compositor could not be created");
            }
            g_object_set(compositor, "background", 1, "latency",
                         static_cast<guint64>(0U), nullptr);
            if (g_object_class_find_property(G_OBJECT_GET_CLASS(compositor),
                                             "ignore-inactive-pads") != nullptr) {
                g_object_set(compositor, "ignore-inactive-pads", FALSE, nullptr);
            }
            for (std::size_t index = 0U; index < input_count_; ++index) {
                static_cast<void>(add_input(compositor, index));
            }
            auto* caps_filter = add_element(pipeline_, "capsfilter", nullptr);
            const auto caps = exact_raw_caps(use_d3d11_, width, height);
            if (caps == nullptr) {
                throw std::runtime_error("content transition caps could not be created");
            }
            gst_caps_set_simple(caps.get(), "framerate", GST_TYPE_FRACTION, 60, 1,
                                nullptr);
            g_object_set(caps_filter, "caps", caps.get(), nullptr);
            auto* sink = add_element(pipeline_, "appsink", nullptr);
            g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE,
                         "wait-on-eos", FALSE, nullptr);
            gst_app_sink_set_max_buffers(GST_APP_SINK(sink), 1U);
            gst_app_sink_set_leaky_type(GST_APP_SINK(sink),
                                        GST_APP_LEAKY_TYPE_DOWNSTREAM);
            GstAppSinkCallbacks callbacks{};
            callbacks.new_preroll =
                &ContentGpuTransitionPipeline::on_new_preroll;
            callbacks.new_sample = &ContentGpuTransitionPipeline::on_new_sample;
            gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, this, nullptr);
            require_link(compositor, caps_filter);
            require_link(caps_filter, sink);
            bus_ = gst_element_get_bus(pipeline_);
            if (bus_ == nullptr ||
                gst_element_set_state(pipeline_, GST_STATE_PLAYING) ==
                    GST_STATE_CHANGE_FAILURE) {
                throw std::runtime_error("content transition pipeline could not start");
            }
            running_ = true;
        } catch (...) {
            close();
            throw;
        }
    }

    [[nodiscard]] static bool push(
        GstAppSrc* source, const std::shared_ptr<const SourceFrame>& frame,
        const std::uint64_t submission,
        const ContentImagePlacement* placement = nullptr) noexcept {
        const auto lease = gstreamer_sample(frame);
        if (!lease || source == nullptr) {
            return false;
        }
        auto* original = gst_sample_get_buffer(lease.sample);
        auto* caps = gst_sample_get_caps(lease.sample);
        if (original == nullptr || caps == nullptr) {
            return false;
        }
        auto* buffer = gst_buffer_copy(original);
        if (buffer == nullptr) {
            return false;
        }
        if (placement != nullptr) {
            auto* crop = gst_buffer_get_video_crop_meta(buffer);
            if (crop == nullptr) {
                crop = gst_buffer_add_video_crop_meta(buffer);
            }
            if (crop == nullptr) {
                gst_buffer_unref(buffer);
                return false;
            }
            crop->x = placement->source_x;
            crop->y = placement->source_y;
            crop->width = placement->source_width;
            crop->height = placement->source_height;
        }
        const auto timestamp = gst_util_uint64_scale(
            submission - 1U, GST_SECOND, kTransitionSubmissionFps);
        const auto next_timestamp = gst_util_uint64_scale(
            submission, GST_SECOND, kTransitionSubmissionFps);
        GST_BUFFER_PTS(buffer) = timestamp;
        GST_BUFFER_DTS(buffer) = timestamp;
        GST_BUFFER_DURATION(buffer) = next_timestamp - timestamp;
        GST_BUFFER_OFFSET(buffer) = submission;
        GST_BUFFER_OFFSET_END(buffer) = submission;
        auto* sample = gst_sample_new(buffer, caps, nullptr, nullptr);
        gst_buffer_unref(buffer);
        if (sample == nullptr) {
            return false;
        }
        const auto flow = gst_app_src_push_sample(source, sample);
        gst_sample_unref(sample);
        return flow == GST_FLOW_OK;
    }

    [[nodiscard]] bool resume() noexcept {
        if (running_) {
            return true;
        }
        if (pipeline_ == nullptr ||
            gst_element_set_state(pipeline_, GST_STATE_PLAYING) ==
                GST_STATE_CHANGE_FAILURE) {
            return false;
        }
        running_ = true;
        return true;
    }

    static GstFlowReturn on_new_sample(GstAppSink* sink, gpointer data) noexcept {
        return static_cast<ContentGpuTransitionPipeline*>(data)->receive(
            gst_app_sink_pull_sample(sink));
    }

    static GstFlowReturn on_new_preroll(GstAppSink* sink,
                                        gpointer data) noexcept {
        return static_cast<ContentGpuTransitionPipeline*>(data)->receive(
            gst_app_sink_pull_preroll(sink));
    }

    GstFlowReturn receive(GstSample* sample) noexcept {
        if (sample == nullptr) {
            return GST_FLOW_EOS;
        }
        try {
            auto* buffer = gst_sample_get_buffer(sample);
            const auto timestamp =
                buffer == nullptr ? GST_CLOCK_TIME_NONE : GST_BUFFER_PTS(buffer);
            if (!GST_CLOCK_TIME_IS_VALID(timestamp)) {
                gst_sample_unref(sample);
                return GST_FLOW_OK;
            }
            const auto acknowledged_submission = gst_util_uint64_scale_round(
                                                     timestamp,
                                                     kTransitionSubmissionFps,
                                                     GST_SECOND) +
                                                 1U;
            const auto expected_timestamp = gst_util_uint64_scale(
                acknowledged_submission - 1U, GST_SECOND,
                kTransitionSubmissionFps);
            if (timestamp != expected_timestamp) {
                gst_sample_unref(sample);
                return GST_FLOW_OK;
            }
            std::scoped_lock lock{output_mutex_};
            if (latest_output_ != nullptr) {
                gst_sample_unref(latest_output_);
            }
            latest_output_ = sample;
            latest_output_submission_ = acknowledged_submission;
            output_revision_.fetch_add(1U, std::memory_order_release);
            return GST_FLOW_OK;
        } catch (...) {
            gst_sample_unref(sample);
            return GST_FLOW_ERROR;
        }
    }

    bool use_d3d11_{false};
    std::shared_ptr<GstD3D11Device> device_{};
    std::size_t input_count_{0U};
    GstElement* pipeline_{nullptr};
    bool running_{false};
    std::array<GstAppSrc*, 2U> sources_{};
    std::array<GstPad*, 2U> pads_{};
    GstBus* bus_{nullptr};
    mutable std::mutex output_mutex_{};
    GstSample* latest_output_{nullptr};
    std::uint64_t latest_output_submission_{0U};
    std::atomic_uint64_t output_revision_{0U};
    std::atomic_uint64_t submission_sequence_{0U};
    std::atomic_uint64_t latest_submission_{0U};
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
            const auto& channel = source_.frame_channel.value();
#ifdef _WIN32
            if (channel.transport == "d3d11_shared_texture") {
                if (!use_d3d11_ || d3d11_device_ == nullptr) {
                    throw std::runtime_error("content_d3d11_ingress_unavailable");
                }
                d3d11_frame_channel_reader_ = make_d3d11_frame_channel_reader(
                    channel,
                    gst_d3d11_device_get_device_handle(d3d11_device_.get()));
            } else
#endif
            {
                frame_channel_reader_ = make_frame_channel_reader(channel);
            }
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
#ifdef _WIN32
        if (d3d11_frame_channel_reader_ != nullptr) {
            d3d11_frame_channel_reader_->wake();
        }
#endif
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

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    activation_frame(
        const std::optional<std::uint64_t> expected_media_epoch) const override {
        const auto frame = latest_frame();
        if (source_.kind != SceneSourceKind::solin_content) {
            return frame;
        }
        try {
            auto requested_epoch = expected_media_epoch;
            if (!requested_epoch.has_value()) {
#ifdef _WIN32
                if (d3d11_frame_channel_reader_ != nullptr) {
                    requested_epoch = d3d11_frame_channel_reader_->media_epoch();
                } else
#endif
                if (frame_channel_reader_ != nullptr) {
                    requested_epoch = frame_channel_reader_->media_epoch();
                }
            }
            if (!requested_epoch.has_value()) {
                return {};
            }
            return frame != nullptr && frame->media_epoch == *requested_epoch &&
                           frame->activation_ready
                       ? frame
                       : std::shared_ptr<const SourceFrame>{};
        } catch (...) {
            return {};
        }
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, const std::stop_token stop_token,
        const std::chrono::steady_clock::time_point deadline) const noexcept override {
        const auto ready = [this, after_sequence] {
            const auto frame = latest_frame();
            return frame != nullptr && frame->sequence > after_sequence;
        };
        const auto stopped = [this] {
            return health().status == SourceRuntimeStatus::stopped;
        };
        while (!stop_token.stop_requested() &&
               std::chrono::steady_clock::now() < deadline) {
            if (ready()) {
                return true;
            }
            if (stopped()) {
                return false;
            }
            const auto revision = frame_signal_->revision();
            if (ready()) {
                return true;
            }
            if (stopped()) {
                return false;
            }
            if (!frame_signal_->wait_after(revision, stop_token, deadline)) {
                break;
            }
        }
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
            gstreamer_local_camera_caps(resolved_configuration), &release_caps};
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

    void wake_frame_waiters() noexcept override {
        try {
            frame_signal_->notify();
        } catch (...) {
        }
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

    struct ContentIngressMetadata final {
        std::uint64_t sequence{0U};
        std::uint64_t media_epoch{0U};
        std::uint32_t width{0U};
        std::uint32_t height{0U};
        VideoFramePixelFormat pixel_format{VideoFramePixelFormat::bgra};
        std::array<std::uint32_t, 2U> plane_strides{};
        std::array<std::uint64_t, 2U> plane_offsets{};
        bool d3d11_memory{false};
    };

    void update_content_control_state() {
        std::optional<std::uint64_t> requested_epoch;
        std::optional<FrameChannelImageTransform> requested_transform;
#ifdef _WIN32
        if (d3d11_frame_channel_reader_ != nullptr) {
            requested_epoch = d3d11_frame_channel_reader_->media_epoch();
            requested_transform = d3d11_frame_channel_reader_->image_transform();
        } else
#endif
        if (frame_channel_reader_ != nullptr) {
            requested_epoch = frame_channel_reader_->media_epoch();
            requested_transform = frame_channel_reader_->image_transform();
        }
        if (requested_epoch.has_value()) {
            content_requested_media_epoch_.store(requested_epoch.value(),
                                                 std::memory_order_release);
        }
        if (requested_transform.has_value() &&
            requested_transform->revision >
                content_requested_image_transform_.revision) {
            content_requested_image_transform_ = requested_transform.value();
        }
    }

    void push_content_buffer(GstBuffer* buffer,
                             const ContentIngressMetadata& frame) {
        if (buffer == nullptr) {
            throw std::runtime_error("content ingress buffer allocation failed");
        }
        const auto video_format = frame.pixel_format == VideoFramePixelFormat::nv12
                                      ? GST_VIDEO_FORMAT_NV12
                                      : GST_VIDEO_FORMAT_BGRA;
        const auto plane_count =
            frame.pixel_format == VideoFramePixelFormat::nv12 ? 2U : 1U;
        gsize plane_offsets[GST_VIDEO_MAX_PLANES]{};
        gint plane_strides[GST_VIDEO_MAX_PLANES]{};
        for (guint index = 0U; index < plane_count; ++index) {
            if (frame.plane_strides[index] >
                static_cast<std::uint32_t>((std::numeric_limits<gint>::max)())) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content ingress stride is invalid");
            }
            plane_offsets[index] =
                static_cast<gsize>(frame.plane_offsets[index]);
            plane_strides[index] =
                static_cast<gint>(frame.plane_strides[index]);
        }
        if (gst_buffer_add_video_meta_full(
                buffer, GST_VIDEO_FRAME_FLAG_NONE, video_format, frame.width,
                frame.height, plane_count, plane_offsets, plane_strides) ==
            nullptr) {
            gst_buffer_unref(buffer);
            throw std::runtime_error(
                "content ingress video metadata allocation failed");
        }
        if (content_frame_caps_ == nullptr ||
            content_frame_width_ != frame.width ||
            content_frame_height_ != frame.height ||
            content_frame_pixel_format_ != frame.pixel_format ||
            content_frame_d3d11_ != frame.d3d11_memory) {
            if (content_canonical_caps_filter_ == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content canonical caps are unavailable");
            }
            const auto canonical_caps =
                exact_raw_caps(use_d3d11_, frame.width, frame.height);
            if (canonical_caps == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error(
                    "content canonical caps could not be created");
            }
            g_object_set(content_canonical_caps_filter_, "caps",
                         canonical_caps.get(), nullptr);
            const auto* pixel_format =
                frame.pixel_format == VideoFramePixelFormat::nv12 ? "NV12"
                                                                  : "BGRA";
            auto* replacement_caps = gst_caps_new_simple(
                "video/x-raw", "format", G_TYPE_STRING, pixel_format,
                "pixel-aspect-ratio", GST_TYPE_FRACTION, 1, 1, "width",
                G_TYPE_INT, static_cast<gint>(frame.width), "height", G_TYPE_INT,
                static_cast<gint>(frame.height), "framerate", GST_TYPE_FRACTION,
                0, 1, nullptr);
            if (replacement_caps == nullptr) {
                gst_buffer_unref(buffer);
                throw std::runtime_error("content ingress caps could not be created");
            }
            if (frame.d3d11_memory) {
                gst_caps_set_features(
                    replacement_caps, 0U,
                    gst_caps_features_new(
                        GST_CAPS_FEATURE_MEMORY_D3D11_MEMORY, nullptr));
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
            content_frame_d3d11_ = frame.d3d11_memory;
        }
        GST_BUFFER_PTS(buffer) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_DTS(buffer) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_OFFSET(buffer) = frame.media_epoch;
        // Carry the ingress sequence through negotiation and conversion so the
        // producer can distinguish an accepted appsrc push from a frame that
        // actually reached the appsink. A lone still-image sample may otherwise
        // be lost while dynamic caps are renegotiated and never retried.
        GST_BUFFER_OFFSET_END(buffer) = frame.sequence;
        GST_BUFFER_DURATION(buffer) =
            static_cast<GstClockTime>(kContentFrameInterval.count());
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
        content_submitted_sequence_ = frame.sequence;
        last_content_sequence_ = frame.sequence;
    }

    void push_system_content_frame() {
        auto frame_lease = frame_channel_reader_->read_latest(
            content_submitted_sequence_);
        const auto confirmed_sequence =
            content_confirmed_sequence_.load(std::memory_order_acquire);
        if (!frame_lease.has_value() &&
            confirmed_sequence < content_submitted_sequence_) {
            frame_lease =
                frame_channel_reader_->read_latest(confirmed_sequence);
        }
        if (!frame_lease.has_value()) {
            return;
        }
        const auto frame = frame_lease->frame();
        GstBuffer* buffer = nullptr;
        if (use_d3d11_) {
            auto lifetime = std::make_unique<FrameChannelFrameLease>(
                std::move(frame_lease.value()));
            buffer = gst_buffer_new_wrapped_full(
                GST_MEMORY_FLAG_READONLY,
                const_cast<std::uint8_t*>(lifetime->frame().bytes.data()),
                lifetime->frame().bytes.size(), 0U,
                lifetime->frame().bytes.size(), lifetime.get(),
                [](gpointer value) {
                    delete static_cast<FrameChannelFrameLease*>(value);
                });
            if (buffer != nullptr) {
                static_cast<void>(lifetime.release());
            }
        } else {
            buffer = gst_buffer_new_memdup(frame.bytes.data(), frame.bytes.size());
        }
        push_content_buffer(
            buffer,
            {.sequence = frame.sequence,
             .media_epoch = frame.media_epoch,
             .width = frame.width,
             .height = frame.height,
             .pixel_format = frame.pixel_format,
             .plane_strides = frame.plane_strides,
             .plane_offsets = frame.plane_offsets,
             .d3d11_memory = false});
    }

#ifdef _WIN32
    void push_d3d11_content_frame() {
        auto frame_lease = d3d11_frame_channel_reader_->read_latest(
            content_submitted_sequence_);
        const auto confirmed_sequence =
            content_confirmed_sequence_.load(std::memory_order_acquire);
        if (!frame_lease.has_value() &&
            confirmed_sequence < content_submitted_sequence_) {
            frame_lease = d3d11_frame_channel_reader_->read_latest(
                confirmed_sequence);
        }
        if (!frame_lease.has_value()) {
            return;
        }
        const auto frame = frame_lease->frame();
        const bool resource_changed =
            content_d3d11_layout_generation_ != frame.resource_generation;
        auto lifetime = std::make_unique<D3d11FrameChannelFrameLease>(
            std::move(frame_lease.value()));
        auto* memory = gst_d3d11_allocator_alloc_wrapped(
            nullptr, d3d11_device_.get(), lifetime->frame().texture,
            resource_changed ? 0U : content_d3d11_mapped_size_, lifetime.get(),
            [](gpointer value) {
                delete static_cast<D3d11FrameChannelFrameLease*>(value);
            });
        if (memory == nullptr) {
            throw std::runtime_error(
                "content D3D11 memory wrapping failed");
        }
        static_cast<void>(lifetime.release());
        if (resource_changed) {
            gsize memory_offset = 0U;
            gsize mapped_size = 0U;
            static_cast<void>(
                gst_memory_get_sizes(memory, &memory_offset, &mapped_size));
            guint resource_stride = 0U;
            if (memory_offset != 0U || mapped_size == 0U ||
                gst_d3d11_memory_get_resource_stride(
                    GST_D3D11_MEMORY_CAST(memory), &resource_stride) == FALSE ||
                resource_stride == 0U) {
                gst_memory_unref(memory);
                throw std::runtime_error(
                    "content D3D11 mapped layout is invalid");
            }
            const auto minimum_size =
                static_cast<std::uint64_t>(resource_stride) * frame.height *
                (frame.pixel_format == VideoFramePixelFormat::nv12 ? 3U : 1U);
            const auto denominator =
                frame.pixel_format == VideoFramePixelFormat::nv12 ? 2U : 1U;
            if (minimum_size / denominator > mapped_size) {
                gst_memory_unref(memory);
                throw std::runtime_error(
                    "content D3D11 mapped layout is too small");
            }
            content_d3d11_layout_generation_ = frame.resource_generation;
            content_d3d11_mapped_size_ = mapped_size;
            content_d3d11_stride_ = resource_stride;
        }
        auto* buffer = gst_buffer_new();
        if (buffer == nullptr) {
            gst_memory_unref(memory);
            throw std::runtime_error("content ingress buffer allocation failed");
        }
        gst_buffer_append_memory(buffer, memory);
        const auto second_offset =
            frame.pixel_format == VideoFramePixelFormat::nv12
                ? static_cast<std::uint64_t>(content_d3d11_stride_) * frame.height
                : 0U;
        push_content_buffer(
            buffer,
            {.sequence = frame.sequence,
             .media_epoch = frame.media_epoch,
             .width = frame.width,
             .height = frame.height,
             .pixel_format = frame.pixel_format,
             .plane_strides = {content_d3d11_stride_,
                               frame.pixel_format == VideoFramePixelFormat::nv12
                                   ? content_d3d11_stride_
                                   : 0U},
             .plane_offsets = {0U, second_offset},
             .d3d11_memory = true});
    }
#endif

    void push_content_frame() {
        if (content_appsrc_ == nullptr ||
            (frame_channel_reader_ == nullptr
#ifdef _WIN32
             && d3d11_frame_channel_reader_ == nullptr
#endif
             )) {
            throw std::runtime_error("content_ingress_unavailable");
        }
        update_content_control_state();
#ifdef _WIN32
        if (d3d11_frame_channel_reader_ != nullptr) {
            push_d3d11_content_frame();
            return;
        }
#endif
        push_system_content_frame();
    }

    void release_content_frame() noexcept {
        reset_content_transition_pipeline();
        reset_content_image_framing();
        if (content_frame_caps_ != nullptr) {
            gst_caps_unref(content_frame_caps_);
            content_frame_caps_ = nullptr;
        }
        content_frame_width_ = 0U;
        content_frame_height_ = 0U;
        content_frame_pixel_format_ = VideoFramePixelFormat::bgra;
        content_frame_d3d11_ = false;
        content_d3d11_layout_generation_ = 0U;
        content_d3d11_mapped_size_ = 0U;
        content_d3d11_stride_ = 0U;
        content_submitted_sequence_ = 0U;
        content_confirmed_sequence_.store(0U, std::memory_order_release);
        last_content_sequence_ = 0U;
        content_requested_media_epoch_.store(0U, std::memory_order_release);
        content_transition_ = PresentationTransition{kMediaPresentationTransition};
        content_transition_requested_ = false;
        content_observed_requested_media_epoch_ = 0U;
        content_requested_image_transform_ = {};
        content_last_transition_phase_ =
            PresentationTransitionPhase::waiting_for_first_frame;
        content_last_transition_tick_ = {};
        content_frozen_outgoing_.reset();
        content_transition_bypass_epoch_.reset();
        content_transition_black_output_observed_ = false;
        content_transition_first_frame_watchdog_.acknowledge();
        std::scoped_lock lock{state_mutex_};
        content_decoded_frame_.reset();
    }

    enum class ContentTransitionStage : std::uint8_t {
        none,
        outgoing_to_black,
        incoming_from_black,
        blend,
    };

    void reset_content_framing_pipeline() noexcept {
        content_framing_pipeline_.reset();
        content_framing_width_ = 0U;
        content_framing_height_ = 0U;
        content_framing_output_revision_ = 0U;
        content_framing_awaiting_output_ = false;
        content_framing_awaited_revision_ = 0U;
        content_framing_awaited_submission_ = 0U;
        content_framing_requested_input_sequence_ = 0U;
        content_framing_output_watchdog_.acknowledge();
    }

    void reset_content_image_framing() noexcept {
        reset_content_framing_pipeline();
        content_image_transform_animation_.reset();
        content_observed_image_transform_revision_ = 0U;
        content_applied_image_transform_media_epoch_ = 0U;
        content_framed_input_sequence_ = 0U;
        content_framed_frame_.reset();
        content_last_rendered_image_transform_ = {};
        content_has_last_rendered_image_transform_ = false;
        content_framing_last_tick_ = {};
        content_failed_image_transform_revision_ = 0U;
    }

    [[nodiscard]] bool ensure_content_framing_pipeline(
        const std::uint32_t width, const std::uint32_t height) noexcept {
        if (content_framing_pipeline_ != nullptr &&
            content_framing_width_ == width &&
            content_framing_height_ == height) {
            return true;
        }
        reset_content_framing_pipeline();
        try {
            content_framing_pipeline_ =
                std::make_unique<ContentGpuTransitionPipeline>(
                    use_d3d11_, d3d11_device_, width, height, 1U);
            content_framing_width_ = width;
            content_framing_height_ = height;
            return true;
        } catch (...) {
            reset_content_framing_pipeline();
            return false;
        }
    }

    void reset_content_transition_pipeline() noexcept {
        content_transition_pipeline_.reset();
        content_transition_stage_ = ContentTransitionStage::none;
        content_transition_width_ = 0U;
        content_transition_height_ = 0U;
        content_transition_output_revision_ = 0U;
        content_transition_awaiting_output_ = false;
        content_transition_awaited_revision_ = 0U;
        content_transition_awaited_submission_ = 0U;
        content_transition_output_watchdog_.acknowledge();
    }

    [[nodiscard]] bool ensure_content_transition_pipeline(
        const ContentTransitionStage stage, const std::uint32_t width,
        const std::uint32_t height) noexcept {
        if (content_transition_pipeline_ != nullptr &&
            content_transition_stage_ == stage &&
            content_transition_width_ == width &&
            content_transition_height_ == height) {
            return true;
        }
        reset_content_transition_pipeline();
        try {
            content_transition_pipeline_ =
                std::make_unique<ContentGpuTransitionPipeline>(
                    use_d3d11_, d3d11_device_, width, height,
                    stage == ContentTransitionStage::blend ? 2U : 1U);
            content_transition_stage_ = stage;
            content_transition_width_ = width;
            content_transition_height_ = height;
            return true;
        } catch (...) {
            reset_content_transition_pipeline();
            return false;
        }
    }

    void publish_content_frame(std::shared_ptr<SourceFrame> frame) noexcept {
        if (frame == nullptr) {
            return;
        }
        try {
            {
                std::scoped_lock lock{state_mutex_};
                if (latest_frame_ != nullptr &&
                    latest_frame_->sequence == frame->sequence &&
                    latest_frame_->stream_epoch == frame->stream_epoch &&
                    latest_frame_->media_epoch == frame->media_epoch) {
                    return;
                }
                frame->discontinuity = frame->stream_epoch != last_published_epoch_ ||
                                       frame->media_epoch != last_published_media_epoch_;
                last_published_epoch_ = frame->stream_epoch;
                last_published_media_epoch_ = frame->media_epoch;
                latest_frame_ = frame;
                ever_ready_ = true;
                health_.status = SourceRuntimeStatus::ready;
                health_.error_code.clear();
                health_.frame_sequence = frame->sequence;
            }
            frame_signal_->notify();
        } catch (...) {
        }
    }

    [[nodiscard]] std::shared_ptr<SourceFrame> content_gpu_output_frame(
        ContentGpuTransitionPipeline::Output output,
        const std::uint64_t media_epoch,
        const bool activation_ready = true) noexcept {
        if (output.sample == nullptr) {
            return {};
        }
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> sample_guard{
            output.sample, &gst_sample_unref};
        try {
            auto* caps = gst_sample_get_caps(sample_guard.get());
            auto* buffer = gst_sample_get_buffer(sample_guard.get());
            GstVideoInfo info{};
            if (caps == nullptr || buffer == nullptr ||
                gst_video_info_from_caps(&info, caps) == FALSE ||
                !is_bounded_video_caps(caps)) {
                return {};
            }
            const auto* features = gst_caps_get_features(caps, 0U);
            const bool d3d11 =
                features != nullptr &&
                gst_caps_features_contains(features, kD3d11MemoryFeature.data()) != FALSE;
            const auto* format =
                gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info));
            if (format == nullptr ||
                (d3d11 && !d3d11_manager_->is_current(d3d11_device_))) {
                return {};
            }
            const auto sequence = frame_sequence_.fetch_add(1U) + 1U;
            auto payload = std::make_shared<GStreamerFramePayload>(
                sample_guard.release(), runtime_lifetime_, d3d11_device_);
            return std::make_shared<SourceFrame>(SourceFrame{
                .sequence = sequence,
                .stream_epoch = stream_epoch_.load(),
                .media_epoch = media_epoch,
                .activation_ready = activation_ready,
                .discontinuity = false,
                .presentation_timestamp_ns = valid_clock_time(GST_BUFFER_PTS(buffer)),
                .duration_ns = valid_clock_time(GST_BUFFER_DURATION(buffer)),
                .received_monotonic_ns = monotonic_nanoseconds(),
                .width = static_cast<std::uint32_t>(GST_VIDEO_INFO_WIDTH(&info)),
                .height = static_cast<std::uint32_t>(GST_VIDEO_INFO_HEIGHT(&info)),
                .pixel_format = format,
                .memory = d3d11 ? SourceFrameMemory::d3d11
                                : SourceFrameMemory::system_memory,
                .payload = std::move(payload),
            });
        } catch (...) {
            return {};
        }
    }

    void publish_content_transition_output(
        ContentGpuTransitionPipeline::Output output,
        const std::uint64_t media_epoch,
        const bool activation_ready) noexcept {
        publish_content_frame(content_gpu_output_frame(
            std::move(output), media_epoch, activation_ready));
    }

    void consume_content_transition_output(const std::uint64_t media_epoch) noexcept {
        if (content_transition_pipeline_ == nullptr) {
            return;
        }
        auto output = content_transition_pipeline_->output_after(
            content_transition_output_revision_);
        if (output.sample == nullptr) {
            return;
        }
        content_transition_output_revision_ = output.revision;
        if (content_transition_awaiting_output_ &&
            (output.revision <= content_transition_awaited_revision_ ||
             output.submission != content_transition_awaited_submission_)) {
            gst_sample_unref(output.sample);
            return;
        }
        if (content_transition_awaiting_output_) {
            content_transition_awaiting_output_ = false;
            content_transition_output_watchdog_.acknowledge();
        }
        publish_content_transition_output(
            std::move(output), media_epoch,
            content_transition_awaited_activation_ready_);
    }

    void consume_content_framing_output(const std::uint64_t media_epoch) noexcept {
        if (content_framing_pipeline_ == nullptr ||
            !content_framing_awaiting_output_) {
            return;
        }
        auto output = content_framing_pipeline_->output_after(
            content_framing_output_revision_);
        if (output.sample == nullptr) {
            return;
        }
        content_framing_output_revision_ = output.revision;
        if (output.revision <= content_framing_awaited_revision_ ||
            output.submission != content_framing_awaited_submission_) {
            gst_sample_unref(output.sample);
            return;
        }
        auto frame = content_gpu_output_frame(std::move(output), media_epoch);
        if (frame == nullptr) {
            return;
        }
        content_framing_awaiting_output_ = false;
        content_framing_output_watchdog_.acknowledge();
        content_framed_input_sequence_ =
            content_framing_requested_input_sequence_;
        content_framed_frame_ = std::move(frame);
    }

    [[nodiscard]] std::shared_ptr<SourceFrame> prepare_content_image_frame(
        const std::shared_ptr<SourceFrame>& decoded,
        const std::chrono::steady_clock::time_point now,
        bool& framing_active) noexcept {
        framing_active = false;
        if (decoded == nullptr) {
            return {};
        }
        const auto& requested = content_requested_image_transform_;
        if (requested.revision == 0U || requested.media_epoch != decoded->media_epoch) {
            if (content_framed_frame_ != nullptr &&
                content_applied_image_transform_media_epoch_ ==
                    decoded->media_epoch) {
                framing_active = content_framing_awaiting_output_ ||
                                 content_image_transform_animation_.active();
                return content_framed_frame_;
            }
            return decoded;
        }

        bool request_changed = false;
        if (requested.revision != content_observed_image_transform_revision_) {
            request_changed = true;
            const bool same_media =
                content_applied_image_transform_media_epoch_ == requested.media_epoch;
            content_observed_image_transform_revision_ = requested.revision;
            content_applied_image_transform_media_epoch_ = requested.media_epoch;
            content_failed_image_transform_revision_ = 0U;
            if (requested.enabled) {
                content_image_transform_animation_.request(
                    ContentImageTransform{
                        .zoom = requested.zoom,
                        .norm_x = requested.norm_x,
                        .norm_y = requested.norm_y,
                    },
                    std::chrono::milliseconds{requested.duration_ms},
                    requested.animate && same_media, now);
            } else {
                content_image_transform_animation_.reset();
            }
            if (!same_media) {
                reset_content_framing_pipeline();
                content_framed_input_sequence_ = 0U;
                content_framed_frame_.reset();
                content_has_last_rendered_image_transform_ = false;
            }
        }

        if (!requested.enabled) {
            reset_content_framing_pipeline();
            content_framed_input_sequence_ = 0U;
            content_framed_frame_.reset();
            content_has_last_rendered_image_transform_ = false;
            return decoded;
        }
        if (content_failed_image_transform_revision_ == requested.revision) {
            return content_framed_frame_ != nullptr ? content_framed_frame_ : decoded;
        }

        if (content_framing_pipeline_ != nullptr) {
            consume_content_framing_output(decoded->media_epoch);
            if (content_framing_awaiting_output_ &&
                content_framing_output_watchdog_.timed_out(now)) {
                content_failed_image_transform_revision_ = requested.revision;
                reset_content_framing_pipeline();
                record_content_readiness_timeout(
                    "image framing output", kPresentationOutputTimeout);
                return content_framed_frame_ != nullptr ? content_framed_frame_
                                                        : decoded;
            }
            if (content_framing_awaiting_output_ &&
                content_framing_last_tick_.time_since_epoch().count() != 0) {
                content_image_transform_animation_.delay(
                    now - content_framing_last_tick_);
            }
        }
        content_framing_last_tick_ = now;
        if (content_framing_awaiting_output_) {
            framing_active = true;
            return content_framed_frame_;
        }

        const auto transform = content_image_transform_animation_.sample(now);
        const bool needs_render =
            request_changed ||
            content_framed_input_sequence_ != decoded->sequence ||
            !content_has_last_rendered_image_transform_ ||
            transform != content_last_rendered_image_transform_;
        if (!needs_render) {
            framing_active = content_image_transform_animation_.active();
            return content_framed_frame_;
        }
        if (!ensure_content_framing_pipeline(requested.canvas_width,
                                             requested.canvas_height)) {
            content_failed_image_transform_revision_ = requested.revision;
            return content_framed_frame_ != nullptr ? content_framed_frame_ : decoded;
        }
        try {
            const auto geometry = compute_content_image_rect(
                decoded->width, decoded->height, requested.canvas_width,
                requested.canvas_height, transform);
            const auto placement = compute_content_image_placement(
                decoded->width, decoded->height, requested.canvas_width,
                requested.canvas_height, geometry);
            const auto checkpoint = content_framing_pipeline_->revision();
            if (!content_framing_pipeline_->render_framed(decoded, placement)) {
                content_failed_image_transform_revision_ = requested.revision;
                reset_content_framing_pipeline();
                return content_framed_frame_ != nullptr ? content_framed_frame_ : decoded;
            }
            content_last_rendered_image_transform_ = transform;
            content_has_last_rendered_image_transform_ = true;
            content_framing_requested_input_sequence_ = decoded->sequence;
            content_framing_awaited_revision_ = checkpoint;
            content_framing_awaited_submission_ =
                content_framing_pipeline_->submission();
            content_framing_awaiting_output_ = true;
            content_framing_output_watchdog_.arm(now);
            consume_content_framing_output(decoded->media_epoch);
            framing_active = content_image_transform_animation_.active() ||
                             content_framing_awaiting_output_;
            return content_framed_frame_;
        } catch (...) {
            content_failed_image_transform_revision_ = requested.revision;
            reset_content_framing_pipeline();
            return content_framed_frame_ != nullptr ? content_framed_frame_ : decoded;
        }
    }

    void recover_content_transition_as_cut(
        const std::shared_ptr<SourceFrame>& incoming,
        const std::chrono::steady_clock::time_point now) noexcept {
        reset_content_transition_pipeline();
        content_transition_bypass_epoch_.reset();
        content_transition_black_output_observed_ = false;
        content_transition_first_frame_watchdog_.acknowledge();
        content_transition_ = PresentationTransition{kMediaPresentationTransition};
        content_transition_requested_ = false;
        content_frozen_outgoing_.reset();
        if (incoming == nullptr) {
            return;
        }
        const PresentationIdentity desired{
            .bus = OutputBus::media_windows,
            .media_epoch = incoming->media_epoch,
        };
        content_transition_.request(desired, now);
        static_cast<void>(content_transition_.sample(now, true));
        content_transition_requested_ = true;
        content_observed_requested_media_epoch_ = incoming->media_epoch;
        publish_content_frame(incoming);
    }

    void record_content_readiness_timeout(
        const std::string_view stage,
        const std::chrono::steady_clock::duration timeout) noexcept {
        const auto dropped = dropped_frames_total_.fetch_add(1U) + 1U;
        {
            std::scoped_lock lock{state_mutex_};
            health_.dropped_frames = dropped;
        }
        try {
            std::cerr << "Native content " << stage
                      << " did not become ready within "
                      << std::chrono::duration_cast<std::chrono::milliseconds>(timeout)
                             .count()
                      << " ms; recovering through the direct frame route.\n";
        } catch (...) {
        }
    }

    [[nodiscard]] std::shared_ptr<SourceFrame> republish_content_frame(
        const std::shared_ptr<const SourceFrame>& frame) noexcept {
        if (frame == nullptr) {
            return {};
        }
        try {
            auto recovered = std::make_shared<SourceFrame>(*frame);
            recovered->sequence = frame_sequence_.fetch_add(1U) + 1U;
            recovered->received_monotonic_ns = monotonic_nanoseconds();
            publish_content_frame(recovered);
            return recovered;
        } catch (...) {
            return {};
        }
    }

    void restore_content_after_missing_first_frame(
        const std::uint64_t failed_epoch,
        const std::chrono::steady_clock::time_point now) noexcept {
        content_transition_bypass_epoch_ = failed_epoch;
        reset_content_transition_pipeline();
        content_transition_black_output_observed_ = false;
        content_transition_first_frame_watchdog_.acknowledge();
        std::shared_ptr<const SourceFrame> stable_failed_epoch;
        try {
            std::scoped_lock lock{state_mutex_};
            if (latest_frame_ != nullptr &&
                latest_frame_->media_epoch == failed_epoch) {
                stable_failed_epoch = latest_frame_;
            }
        } catch (...) {
        }
        // A watchdog recovery may stabilize only pixels already authored by the
        // failed projection epoch. Giving the outgoing video's pixels a fresh
        // sequence makes every downstream consumer accept stale content as the
        // new image/idle owner and can strand Program there indefinitely.
        content_frozen_outgoing_.reset();
        if (const auto recovered = republish_content_frame(stable_failed_epoch);
            recovered != nullptr) {
            content_transition_.restore_stable(PresentationIdentity{
                .bus = OutputBus::media_windows,
                .media_epoch = recovered->media_epoch,
            });
            content_frozen_outgoing_ = recovered;
            content_last_transition_phase_ =
                PresentationTransitionPhase::stable;
        } else {
            content_transition_ =
                PresentationTransition{kMediaPresentationTransition};
            content_last_transition_phase_ =
                PresentationTransitionPhase::waiting_for_first_frame;
        }
        content_last_transition_tick_ = now;
    }

    [[nodiscard]] bool render_content_presentation(
        const std::chrono::steady_clock::time_point now) noexcept {
        try {
            std::shared_ptr<SourceFrame> decoded;
            std::shared_ptr<SourceFrame> published;
            {
                std::scoped_lock lock{state_mutex_};
                decoded = content_decoded_frame_;
                published = std::const_pointer_cast<SourceFrame>(latest_frame_);
            }
            auto requested_epoch =
                content_requested_media_epoch_.load(std::memory_order_acquire);
            if (requested_epoch == 0U && decoded != nullptr) {
                requested_epoch = decoded->media_epoch;
            }
            bool framing_active = false;
            decoded = prepare_content_image_frame(decoded, now, framing_active);
            if (!content_transition_requested_ ||
                requested_epoch != content_observed_requested_media_epoch_) {
                const bool already_black =
                    content_transition_black_output_observed_ &&
                    content_last_transition_phase_ ==
                        PresentationTransitionPhase::waiting_at_black;
                if (!already_black) {
                    content_frozen_outgoing_ = std::move(published);
                }
                content_transition_.request(
                    PresentationIdentity{
                        .bus = OutputBus::media_windows,
                        .media_epoch = requested_epoch,
                    },
                    now);
                content_transition_requested_ = true;
                content_observed_requested_media_epoch_ = requested_epoch;
                content_transition_bypass_epoch_.reset();
                reset_content_transition_pipeline();
                content_transition_black_output_observed_ = already_black;
                content_transition_first_frame_watchdog_.acknowledge();
                if (already_black) {
                    content_transition_first_frame_watchdog_.arm(now);
                }
                content_last_transition_tick_ = now;
            }
            if (!content_transition_requested_) {
                return framing_active;
            }

            if (content_transition_bypass_epoch_.has_value()) {
                const auto deferred_epoch = *content_transition_bypass_epoch_;
                const auto deferred =
                    decoded != nullptr && decoded->media_epoch == deferred_epoch
                        ? decoded
                        : std::shared_ptr<SourceFrame>{};
                if (deferred == nullptr) {
                    return framing_active;
                }
                content_frozen_outgoing_ = std::move(published);
                content_transition_.request(
                    PresentationIdentity{
                        .bus = OutputBus::media_windows,
                        .media_epoch = deferred_epoch,
                    },
                    now);
                content_transition_bypass_epoch_.reset();
                reset_content_transition_pipeline();
                content_last_transition_tick_ = now;
                return true;
            }

            const auto desired = content_transition_.desired();
            const auto incoming =
                decoded != nullptr && decoded->media_epoch == desired.media_epoch
                    ? decoded
                    : std::shared_ptr<SourceFrame>{};
            if (content_transition_black_output_observed_) {
                if (incoming == nullptr) {
                    if (content_transition_first_frame_watchdog_.timed_out(now)) {
                        record_content_readiness_timeout(
                            "first frame", kPresentationFirstFrameTimeout);
                        restore_content_after_missing_first_frame(
                            desired.media_epoch, now);
                    }
                    return framing_active;
                }
                content_transition_black_output_observed_ = false;
                content_transition_first_frame_watchdog_.acknowledge();
            }
            if (content_transition_pipeline_ != nullptr) {
                consume_content_transition_output(desired.media_epoch);
                if (!content_transition_awaiting_output_ &&
                    content_last_transition_phase_ ==
                        PresentationTransitionPhase::waiting_at_black &&
                    incoming == nullptr) {
                    reset_content_transition_pipeline();
                    content_transition_black_output_observed_ = true;
                    content_transition_first_frame_watchdog_.arm(now);
                    return framing_active;
                }
                if (content_transition_awaiting_output_ &&
                    content_transition_output_watchdog_.timed_out(now)) {
                    content_transition_bypass_epoch_ = desired.media_epoch;
                    reset_content_transition_pipeline();
                    content_transition_black_output_observed_ = false;
                    content_transition_first_frame_watchdog_.acknowledge();
                    record_content_readiness_timeout(
                        "presentation transition output",
                        kPresentationOutputTimeout);
                    if (incoming != nullptr) {
                        recover_content_transition_as_cut(incoming, now);
                    } else if (content_frozen_outgoing_ != nullptr) {
                        restore_content_after_missing_first_frame(
                            desired.media_epoch, now);
                    }
                    return framing_active;
                }
                if (content_transition_awaiting_output_ &&
                    content_last_transition_tick_.time_since_epoch().count() != 0) {
                    content_transition_.delay(now - content_last_transition_tick_);
                }
                if (content_transition_awaiting_output_) {
                    content_last_transition_tick_ = now;
                    return true;
                }
            }
            const auto allow_incoming =
                !(content_last_transition_phase_ ==
                      PresentationTransitionPhase::waiting_at_black &&
                  content_transition_awaiting_output_);
            const auto transition_sample =
                content_transition_.sample(now, allow_incoming && incoming != nullptr);
            content_last_transition_tick_ = now;
            content_last_transition_phase_ = transition_sample.phase;

            if (transition_sample.phase == PresentationTransitionPhase::stable) {
                reset_content_transition_pipeline();
                content_transition_black_output_observed_ = false;
                content_transition_first_frame_watchdog_.acknowledge();
                content_frozen_outgoing_.reset();
                if (incoming != nullptr) {
                    publish_content_frame(incoming);
                }
                return framing_active;
            }
            if (transition_sample.phase ==
                PresentationTransitionPhase::waiting_for_first_frame) {
                return framing_active;
            }

            ContentTransitionStage stage = ContentTransitionStage::blend;
            std::shared_ptr<const SourceFrame> outgoing;
            std::shared_ptr<const SourceFrame> transition_incoming;
            std::uint32_t output_width = 0U;
            std::uint32_t output_height = 0U;
            switch (transition_sample.phase) {
            case PresentationTransitionPhase::fading_out:
            case PresentationTransitionPhase::waiting_at_black:
                stage = ContentTransitionStage::outgoing_to_black;
                outgoing = content_frozen_outgoing_;
                if (outgoing != nullptr) {
                    output_width = outgoing->width;
                    output_height = outgoing->height;
                }
                break;
            case PresentationTransitionPhase::fading_in:
                stage = ContentTransitionStage::incoming_from_black;
                transition_incoming = incoming;
                if (transition_incoming != nullptr) {
                    output_width = transition_incoming->width;
                    output_height = transition_incoming->height;
                }
                break;
            case PresentationTransitionPhase::blending:
                stage = ContentTransitionStage::blend;
                outgoing = content_frozen_outgoing_;
                transition_incoming = incoming;
                if (transition_incoming != nullptr) {
                    output_width = transition_incoming->width;
                    output_height = transition_incoming->height;
                }
                break;
            default:
                return framing_active;
            }
            if (transition_sample.phase ==
                    PresentationTransitionPhase::waiting_at_black &&
                outgoing == nullptr) {
                return framing_active;
            }
            if (output_width == 0U || output_height == 0U) {
                recover_content_transition_as_cut(incoming, now);
                return framing_active;
            }
            if (!ensure_content_transition_pipeline(stage, output_width,
                                                    output_height)) {
                recover_content_transition_as_cut(incoming, now);
                return framing_active;
            }
            const auto checkpoint = content_transition_pipeline_->revision();
            if (!content_transition_pipeline_->render(
                    outgoing, transition_incoming, transition_sample.weights)) {
                recover_content_transition_as_cut(incoming, now);
                return framing_active;
            }
            content_transition_awaited_revision_ = checkpoint;
            content_transition_awaited_submission_ =
                content_transition_pipeline_->submission();
            content_transition_awaited_activation_ready_ =
                transition_sample.weights.outgoing <= 0.0;
            content_transition_awaiting_output_ = true;
            content_transition_output_watchdog_.arm(now);
            consume_content_transition_output(desired.media_epoch);
            if (transition_sample.phase ==
                    PresentationTransitionPhase::waiting_at_black &&
                !content_transition_awaiting_output_) {
                // The black frame is now a standalone GPU sample, so the
                // ephemeral graph can be destroyed. Retain one bounded outgoing
                // lease until the incoming epoch becomes visible; a missing
                // first frame can then recover instead of making black the
                // permanent canonical output.
                reset_content_transition_pipeline();
                content_transition_black_output_observed_ = true;
                content_transition_first_frame_watchdog_.arm(now);
                return framing_active;
            }
            return framing_active || transition_sample.animation_active() ||
                   content_transition_awaiting_output_;
        } catch (...) {
            return false;
        }
    }

    [[nodiscard]] GstElement* build_pipeline() {
        content_appsrc_ = nullptr;
        content_canonical_caps_filter_ = nullptr;
        content_submitted_sequence_ = 0U;
        content_confirmed_sequence_.store(0U, std::memory_order_release);
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
#ifdef _WIN32
            if (d3d11_frame_channel_reader_ != nullptr) {
                return "content_d3d11_ingress_failed";
            }
#endif
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

    [[nodiscard]] bool requires_d3d11_ingress() const noexcept {
#ifdef _WIN32
        return d3d11_frame_channel_reader_ != nullptr;
#else
        return false;
#endif
    }

    void publish_failure(const char* error_code,
                         const std::string& native_error_code = {}) {
        bool log_camera_error = false;
        {
            std::scoped_lock lock{state_mutex_};
            health_.status =
                ever_ready_ ? SourceRuntimeStatus::degraded : SourceRuntimeStatus::failed;
            health_.error_code = error_code;
            log_camera_error =
                source_.kind == SceneSourceKind::local_camera &&
                (last_logged_camera_error_code_ != error_code ||
                 last_logged_camera_native_error_code_ != native_error_code);
            if (log_camera_error) {
                last_logged_camera_error_code_ = error_code;
                last_logged_camera_native_error_code_ = native_error_code;
            }
        }
        if (log_camera_error) {
            try {
                std::cerr << "camera_capture_error "
                          << nlohmann::json{
                                 {"source_id", source_.id},
                                 {"backend", "media_foundation"},
                                 {"stage", "pipeline"},
                                 {"error_code", error_code},
                                 {"native_error_code", native_error_code},
                             }
                                 .dump()
                          << '\n';
            } catch (...) {
                // Diagnostics must never interrupt source recovery.
            }
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
                if (requires_d3d11_ingress()) {
                    publish_failure("content_d3d11_device_lost");
                    std::unique_lock lock{reconnect_mutex_};
                    reconnect_wakeup_.wait_for(
                        lock, reconnect_delay,
                        [this] { return stop_requested_.load(); });
                    reconnect_delay =
                        (std::min)(reconnect_delay * 2, kMaximumReconnectDelay);
                    continue;
                }
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
            std::string pipeline_native_error;
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
                    use_system_memory_fallback =
                        use_d3d11_ && !requires_d3d11_ingress() &&
                        is_d3d11_failure_origin(state_error);
                    if (state_error != nullptr) {
                        pipeline_native_error = gstreamer_native_error_code(state_error);
                        gst_message_unref(state_error);
                    }
                    throw std::runtime_error("source pipeline could not start");
                }
                const auto sequence_before_attempt = frame_sequence_.load();
                const auto attempt_started = monotonic_nanoseconds();
                while (!stop_requested_.load()) {
                    if (use_d3d11_ && !d3d11_manager_->is_current(d3d11_device_)) {
                        pipeline_failed = true;
                        use_system_memory_fallback = !requires_d3d11_ingress();
                        break;
                    }
                    if (source_.kind == SceneSourceKind::solin_content) {
                        push_content_frame();
                    }
                    const auto content_transition_active =
                        source_.kind == SceneSourceKind::solin_content &&
                        render_content_presentation(
                            std::chrono::steady_clock::now());
                    GstMessage* message = nullptr;
                    if (source_.kind == SceneSourceKind::solin_content) {
#ifdef _WIN32
                        if (d3d11_frame_channel_reader_ != nullptr) {
                            static_cast<void>(
                                d3d11_frame_channel_reader_->wait_for_frame(
                                    content_transition_active
                                        ? kContentTransitionFrameInterval
                                        : kBusPollInterval));
                        } else
#endif
                        {
                            static_cast<void>(frame_channel_reader_->wait_for_frame(
                                content_transition_active
                                    ? kContentTransitionFrameInterval
                                    : kBusPollInterval));
                        }
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
                            content_submitted_sequence_ != 0U &&
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
                    use_system_memory_fallback =
                        use_d3d11_ && !requires_d3d11_ingress() &&
                        is_d3d11_failure_origin(message);
                    pipeline_native_error = gstreamer_native_error_code(message);
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
                use_system_memory_fallback =
                    use_d3d11_ && !requires_d3d11_ingress();
            } catch (const std::exception&) {
                pipeline_failed = true;
            }
            content_appsrc_ = nullptr;
            content_canonical_caps_filter_ = nullptr;
            if (stop_requested_.load()) {
                break;
            }
            use_system_memory_fallback =
                use_system_memory_fallback ||
                (use_d3d11_ && !requires_d3d11_ingress() &&
                 !d3d11_manager_->is_current(d3d11_device_));
            if (pipeline_failed) {
                const auto current_health = health();
                if (current_health.error_code != "source_frame_timeout") {
                    publish_failure(stream_error_code(), pipeline_native_error);
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
                reset_content_transition_pipeline();
                reset_content_image_framing();
                content_frozen_outgoing_.reset();
                std::scoped_lock lock{state_mutex_};
                latest_frame_.reset();
                content_decoded_frame_.reset();
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
            const auto media_epoch =
                source_.kind == SceneSourceKind::solin_content &&
                        GST_BUFFER_OFFSET(buffer) != GST_BUFFER_OFFSET_NONE
                    ? GST_BUFFER_OFFSET(buffer)
                    : 0U;
            const auto content_ingress_sequence =
                source_.kind == SceneSourceKind::solin_content &&
                        GST_BUFFER_OFFSET_END(buffer) != GST_BUFFER_OFFSET_NONE
                    ? GST_BUFFER_OFFSET_END(buffer)
                    : 0U;
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
            if (source_.kind != SceneSourceKind::solin_content) {
                std::scoped_lock lock{state_mutex_};
                discontinuity = stream_epoch != last_published_epoch_;
                last_published_epoch_ = stream_epoch;
            }
            auto frame = std::make_shared<SourceFrame>(SourceFrame{
                .sequence = sequence,
                .stream_epoch = stream_epoch,
                .media_epoch = media_epoch,
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
                if (source_.kind == SceneSourceKind::solin_content) {
                    content_decoded_frame_ = frame;
                    auto confirmed = content_confirmed_sequence_.load(
                        std::memory_order_relaxed);
                    while (content_ingress_sequence > confirmed &&
                           !content_confirmed_sequence_.compare_exchange_weak(
                               confirmed, content_ingress_sequence,
                               std::memory_order_release,
                               std::memory_order_relaxed)) {
                    }
                } else {
                    latest_frame_ = frame;
                }
                ever_ready_ = true;
                health_.status = SourceRuntimeStatus::ready;
                health_.error_code.clear();
                last_logged_camera_error_code_.clear();
                last_logged_camera_native_error_code_.clear();
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
            if (source_.kind == SceneSourceKind::solin_content &&
                frame_channel_reader_ != nullptr) {
                frame_channel_reader_->wake();
            }
#ifdef _WIN32
            if (source_.kind == SceneSourceKind::solin_content &&
                d3d11_frame_channel_reader_ != nullptr) {
                d3d11_frame_channel_reader_->wake();
            }
#endif
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
    std::string last_logged_camera_error_code_{};
    std::string last_logged_camera_native_error_code_{};
    std::shared_ptr<const SourceFrame> latest_frame_{};
    std::shared_ptr<SourceFrame> content_decoded_frame_{};
    bool ever_ready_{false};
    std::uint64_t last_published_epoch_{0U};
    std::uint64_t last_published_media_epoch_{0U};
    std::shared_ptr<void> runtime_lifetime_{};
    std::shared_ptr<GStreamerD3d11DeviceManager> d3d11_manager_{};
    std::shared_ptr<GstD3D11Device> d3d11_device_{};
    bool use_d3d11_{false};
    std::shared_ptr<GStreamerFrameSignal> frame_signal_{};
    LocalCameraFormatResolver camera_format_resolver_{};
    std::unique_ptr<FrameChannelReader> frame_channel_reader_{};
#ifdef _WIN32
    std::unique_ptr<D3d11FrameChannelReader> d3d11_frame_channel_reader_{};
#endif
    GstAppSrc* content_appsrc_{nullptr};
    GstElement* content_canonical_caps_filter_{nullptr};
    GstCaps* content_frame_caps_{nullptr};
    std::uint32_t content_frame_width_{0U};
    std::uint32_t content_frame_height_{0U};
    VideoFramePixelFormat content_frame_pixel_format_{VideoFramePixelFormat::bgra};
    bool content_frame_d3d11_{false};
#ifdef _WIN32
    std::uint64_t content_d3d11_layout_generation_{0U};
    gsize content_d3d11_mapped_size_{0U};
    guint content_d3d11_stride_{0U};
#endif
    std::atomic_uint64_t content_requested_media_epoch_{0U};
    FrameChannelImageTransform content_requested_image_transform_{};
    ContentImageTransformAnimation content_image_transform_animation_{};
    std::uint64_t content_observed_image_transform_revision_{0U};
    std::uint64_t content_applied_image_transform_media_epoch_{0U};
    std::uint64_t content_failed_image_transform_revision_{0U};
    std::unique_ptr<ContentGpuTransitionPipeline> content_framing_pipeline_{};
    std::uint32_t content_framing_width_{0U};
    std::uint32_t content_framing_height_{0U};
    std::uint64_t content_framing_output_revision_{0U};
    bool content_framing_awaiting_output_{false};
    std::uint64_t content_framing_awaited_revision_{0U};
    std::uint64_t content_framing_awaited_submission_{0U};
    PresentationReadinessWatchdog content_framing_output_watchdog_{};
    std::uint64_t content_framing_requested_input_sequence_{0U};
    std::uint64_t content_framed_input_sequence_{0U};
    std::shared_ptr<SourceFrame> content_framed_frame_{};
    ContentImageTransform content_last_rendered_image_transform_{};
    bool content_has_last_rendered_image_transform_{false};
    std::chrono::steady_clock::time_point content_framing_last_tick_{};
    PresentationTransition content_transition_{kMediaPresentationTransition};
    bool content_transition_requested_{false};
    std::uint64_t content_observed_requested_media_epoch_{0U};
    PresentationTransitionPhase content_last_transition_phase_{
        PresentationTransitionPhase::waiting_for_first_frame};
    std::chrono::steady_clock::time_point content_last_transition_tick_{};
    std::shared_ptr<SourceFrame> content_frozen_outgoing_{};
    std::unique_ptr<ContentGpuTransitionPipeline> content_transition_pipeline_{};
    ContentTransitionStage content_transition_stage_{ContentTransitionStage::none};
    std::uint32_t content_transition_width_{0U};
    std::uint32_t content_transition_height_{0U};
    std::uint64_t content_transition_output_revision_{0U};
    bool content_transition_awaiting_output_{false};
    std::uint64_t content_transition_awaited_revision_{0U};
    std::uint64_t content_transition_awaited_submission_{0U};
    bool content_transition_awaited_activation_ready_{false};
    PresentationReadinessWatchdog content_transition_output_watchdog_{};
    std::optional<std::uint64_t> content_transition_bypass_epoch_{};
    bool content_transition_black_output_observed_{false};
    PresentationReadinessWatchdog content_transition_first_frame_watchdog_{
        kPresentationFirstFrameTimeout};
    std::uint64_t last_content_sequence_{0U};
    std::uint64_t content_submitted_sequence_{0U};
    std::atomic_uint64_t content_confirmed_sequence_{0U};
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

GstCaps* gstreamer_local_camera_caps(const LocalCameraSourceConfiguration& configuration) {
    const auto pixels = static_cast<std::uint64_t>(configuration.width) *
                        static_cast<std::uint64_t>(configuration.height);
    if (configuration.width == 0U || configuration.height == 0U ||
        configuration.width > kMaximumSourceDimension ||
        configuration.height > kMaximumSourceDimension || pixels > kMaximumSourcePixels ||
        (std::min)(configuration.width, configuration.height) > kMaximumSourceShortEdge ||
        configuration.fps_numerator == 0U || configuration.fps_denominator == 0U ||
        configuration.fps_numerator > kMaximumCameraFpsComponent ||
        configuration.fps_denominator > kMaximumCameraFpsComponent ||
        static_cast<std::uint64_t>(configuration.fps_numerator) >
            static_cast<std::uint64_t>(kMaximumSourceFramesPerSecond) *
                configuration.fps_denominator ||
        std::gcd(configuration.fps_numerator, configuration.fps_denominator) != 1U ||
        (configuration.media_type != "video/x-raw" && configuration.media_type != "image/jpeg" &&
         configuration.media_type != "video/x-h264") ||
        configuration.pixel_format.empty()) {
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

class GStreamerFrameTransitionPipeline::Impl final {
  public:
    Impl(const bool use_d3d11, std::shared_ptr<GstD3D11Device> device,
         const std::uint32_t width, const std::uint32_t height,
         const std::uint8_t input_count)
        : pipeline(use_d3d11, std::move(device), width, height, input_count) {}

    ContentGpuTransitionPipeline pipeline;
};

GStreamerFrameTransitionPipeline::GStreamerFrameTransitionPipeline(
    const bool use_d3d11, std::shared_ptr<GstD3D11Device> device,
    const std::uint32_t width, const std::uint32_t height,
    const std::uint8_t input_count)
    : impl_(std::make_unique<Impl>(use_d3d11, std::move(device), width, height,
                                  input_count)) {}

GStreamerFrameTransitionPipeline::~GStreamerFrameTransitionPipeline() = default;

bool GStreamerFrameTransitionPipeline::render(
    const std::shared_ptr<const SourceFrame>& outgoing,
    const std::shared_ptr<const SourceFrame>& incoming,
    const SceneTransitionWeights weights) noexcept {
    return impl_ != nullptr && impl_->pipeline.render(outgoing, incoming, weights);
}

std::uint64_t GStreamerFrameTransitionPipeline::revision() const noexcept {
    return impl_ == nullptr ? 0U : impl_->pipeline.revision();
}

std::uint64_t GStreamerFrameTransitionPipeline::submission() const noexcept {
    return impl_ == nullptr ? 0U : impl_->pipeline.submission();
}

GStreamerFrameTransitionPipeline::Output
GStreamerFrameTransitionPipeline::output_after(const std::uint64_t revision) const noexcept {
    if (impl_ == nullptr) {
        return {};
    }
    const auto output = impl_->pipeline.output_after(revision);
    return {
        .revision = output.revision,
        .submission = output.submission,
        .sample = output.sample,
    };
}

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

std::shared_ptr<GstD3D11Device>
gstreamer_d3d11_device(const std::shared_ptr<const SourceFrame>& frame) noexcept {
    try {
        const auto lease = gstreamer_sample(frame);
        auto* buffer = lease ? gst_sample_get_buffer(lease.sample) : nullptr;
        auto* memory = buffer == nullptr || gst_buffer_n_memory(buffer) == 0U
                           ? nullptr
                           : gst_buffer_peek_memory(buffer, 0U);
        if (memory == nullptr || gst_is_d3d11_memory(memory) == FALSE) {
            return {};
        }
        auto* device = GST_D3D11_MEMORY_CAST(memory)->device;
        if (device == nullptr) {
            return {};
        }
        return std::shared_ptr<GstD3D11Device>(
            GST_D3D11_DEVICE(gst_object_ref(device)),
            [](GstD3D11Device* value) {
                if (value != nullptr) {
                    gst_object_unref(value);
                }
            });
    } catch (...) {
        return {};
    }
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

GstCaps* gstreamer_local_camera_caps(const LocalCameraSourceConfiguration&) {
    throw std::runtime_error("media_runtime_unavailable");
}

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

std::shared_ptr<GstD3D11Device>
gstreamer_d3d11_device(const std::shared_ptr<const SourceFrame>&) noexcept {
    return {};
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
