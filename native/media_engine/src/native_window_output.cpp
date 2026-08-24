#include "solin/media_engine/native_window_output.hpp"
#include "solin/media_engine/presentation_transition.hpp"

#include "gstreamer_frame_transition.hpp"
#include "gstreamer_source_runtime.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <ranges>
#include <stdexcept>
#include <thread>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/video/video-info.h>
#include <gst/video/videooverlay.h>
#endif

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace solin::media_engine {
namespace {

using namespace std::chrono_literals;

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER

void release_pipeline(GstElement* pipeline) noexcept {
    if (pipeline != nullptr) {
        static_cast<void>(gst_element_set_state(pipeline, GST_STATE_NULL));
        gst_object_unref(pipeline);
    }
}

void require_link(GstElement* source, GstElement* destination) {
    if (gst_element_link(source, destination) == FALSE) {
        throw std::runtime_error("native_window_output_link_failed");
    }
}

GstElement* add_element(GstElement* pipeline, const char* factory) {
    auto* element = gst_element_factory_make(factory, nullptr);
    if (element == nullptr || gst_bin_add(GST_BIN(pipeline), element) == FALSE) {
        if (element != nullptr) {
            gst_object_unref(element);
        }
        throw std::runtime_error("native_window_output_unavailable");
    }
    return element;
}

constexpr auto kTransitionFrameInterval = 16ms;

#ifdef _WIN32

constexpr wchar_t kNativeChildWindowClass[] = L"SolinNativeVideoSurface";

LRESULT CALLBACK native_child_window_proc(const HWND window, const UINT message,
                                          const WPARAM w_param, const LPARAM l_param) noexcept {
    if (message == WM_LBUTTONDOWN || message == WM_LBUTTONUP || message == WM_LBUTTONDBLCLK) {
        const auto parent = GetParent(window);
        if (parent != nullptr) {
            static_cast<void>(PostMessageW(parent, message, w_param, l_param));
        }
        return 0;
    }
    if (message == WM_ERASEBKGND) {
        RECT area{};
        if (GetClientRect(window, &area) != FALSE) {
            FillRect(reinterpret_cast<HDC>(w_param), &area,
                     static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
        }
        return 1;
    }
    return DefWindowProcW(window, message, w_param, l_param);
}

void register_native_child_window_class() {
    static std::once_flag registration;
    std::call_once(registration, [] {
        WNDCLASSEXW definition{};
        definition.cbSize = sizeof(definition);
        definition.style = CS_DBLCLKS | CS_HREDRAW | CS_VREDRAW;
        definition.lpfnWndProc = &native_child_window_proc;
        definition.hInstance = GetModuleHandleW(nullptr);
        definition.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
        definition.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
        definition.lpszClassName = kNativeChildWindowClass;
        if (RegisterClassExW(&definition) == 0U && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            throw std::runtime_error("native_window_class_unavailable");
        }
    });
}

class NativeChildWindow final {
  public:
    explicit NativeChildWindow(const OutputWindowConfiguration& target) {
        parent_ = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(target.native_handle));
        if (parent_ == nullptr || IsWindow(parent_) == FALSE) {
            throw std::runtime_error("native_window_handle_invalid");
        }
        register_native_child_window_class();
        RECT bounds{};
        if (GetClientRect(parent_, &bounds) == FALSE) {
            throw std::runtime_error("native_window_handle_invalid");
        }
        width_ = std::max<LONG>(1, bounds.right - bounds.left);
        height_ = std::max<LONG>(1, bounds.bottom - bounds.top);
        window_ = CreateWindowExW(WS_EX_NOPARENTNOTIFY | WS_EX_NOACTIVATE, kNativeChildWindowClass,
                                  L"", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS, 0, 0, width_,
                                  height_, parent_, nullptr, GetModuleHandleW(nullptr), nullptr);
        if (window_ == nullptr) {
            throw std::runtime_error("native_window_child_unavailable");
        }
        // The presenter is a rendering plane.  Keeping it at the bottom of
        // its Qt host allows app-process interaction/decoration planes to sit
        // above it without coupling the engine to a particular target id.
        if (SetWindowPos(window_, HWND_BOTTOM, 0, 0, width_, height_,
                         SWP_NOACTIVATE | SWP_NOOWNERZORDER) == FALSE) {
            DestroyWindow(window_);
            window_ = nullptr;
            throw std::runtime_error("native_window_z_order_unavailable");
        }
    }

    ~NativeChildWindow() {
        if (window_ != nullptr && IsWindow(window_) != FALSE) {
            DestroyWindow(window_);
        }
    }

    NativeChildWindow(const NativeChildWindow&) = delete;
    NativeChildWindow& operator=(const NativeChildWindow&) = delete;

    [[nodiscard]] HWND handle() const noexcept { return window_; }

    [[nodiscard]] bool pump() noexcept {
        if (parent_ == nullptr || IsWindow(parent_) == FALSE || window_ == nullptr ||
            IsWindow(window_) == FALSE) {
            return false;
        }
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0U, 0U, PM_REMOVE) != FALSE) {
            if (message.message == WM_QUIT) {
                return false;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        RECT bounds{};
        if (GetClientRect(parent_, &bounds) == FALSE) {
            return false;
        }
        const auto width = std::max<LONG>(1, bounds.right - bounds.left);
        const auto height = std::max<LONG>(1, bounds.bottom - bounds.top);
        if (width != width_ || height != height_) {
            if (SetWindowPos(window_, HWND_BOTTOM, 0, 0, width, height,
                             SWP_NOACTIVATE | SWP_NOOWNERZORDER) == FALSE) {
                return false;
            }
            width_ = width;
            height_ = height;
        }
        return true;
    }

  private:
    HWND parent_{nullptr};
    HWND window_{nullptr};
    LONG width_{1};
    LONG height_{1};
};

#endif

class NativeTargetPipeline final {
  public:
    explicit NativeTargetPipeline(const OutputWindowConfiguration& target)
        : target_id_(target.target_id), transition_(kMediaPresentationTransition) {
#ifdef _WIN32
        window_ = std::make_unique<NativeChildWindow>(target);
#else
        static_cast<void>(target);
        throw std::runtime_error("native_window_output_unavailable");
#endif
        auto* pipeline = gst_pipeline_new(nullptr);
        if (pipeline == nullptr) {
            throw std::runtime_error("native_window_output_unavailable");
        }
        std::unique_ptr<GstElement, decltype(&release_pipeline)> candidate{pipeline,
                                                                           &release_pipeline};
#ifdef _WIN32
        auto* source = add_element(pipeline, "appsrc");
        auto* queue = add_element(pipeline, "queue");
        auto* upload = add_element(pipeline, "d3d11upload");
        auto* convert = add_element(pipeline, "d3d11convert");
        auto* sink = add_element(pipeline, "d3d11videosink");
#else
        auto* source = add_element(pipeline, "appsrc");
        auto* queue = add_element(pipeline, "queue");
        auto* upload = add_element(pipeline, "videoconvert");
        auto* convert = add_element(pipeline, "videoconvert");
        auto* sink = add_element(pipeline, "autovideosink");
#endif
        g_object_set(source, "is-live", TRUE, "format", GST_FORMAT_TIME,
                     "do-timestamp", TRUE, "block", FALSE, nullptr);
        gst_app_src_set_max_buffers(GST_APP_SRC(source), 1U);
        gst_app_src_set_leaky_type(GST_APP_SRC(source),
                                   GST_APP_LEAKY_TYPE_DOWNSTREAM);
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U,
                     "max-size-time", static_cast<guint64>(0U), "leaky", 2, nullptr);
        require_link(source, queue);
        require_link(queue, upload);
        require_link(upload, convert);
        require_link(convert, sink);
        g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE, "force-aspect-ratio", TRUE,
                     nullptr);
        if (GST_IS_VIDEO_OVERLAY(sink) == FALSE) {
            throw std::runtime_error("native_window_overlay_unavailable");
        }
#ifdef _WIN32
        gst_video_overlay_set_window_handle(GST_VIDEO_OVERLAY(sink),
                                            reinterpret_cast<guintptr>(window_->handle()));
#endif
        gst_video_overlay_handle_events(GST_VIDEO_OVERLAY(sink), FALSE);
        if (gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
            throw std::runtime_error("native_window_output_start_failed");
        }
        auto* bus = gst_element_get_bus(pipeline);
        if (bus == nullptr) {
            throw std::runtime_error("native_window_output_start_failed");
        }
        pipeline_ = candidate.release();
        source_ = GST_APP_SRC(source);
        bus_ = bus;
        request(target);
    }

    ~NativeTargetPipeline() {
        transition_pipeline_.reset();
        if (bus_ != nullptr) {
            gst_object_unref(bus_);
        }
        release_pipeline(pipeline_);
    }

    NativeTargetPipeline(const NativeTargetPipeline&) = delete;
    NativeTargetPipeline& operator=(const NativeTargetPipeline&) = delete;

    void request(const OutputWindowConfiguration& target) noexcept {
        const auto previous = transition_.desired();
        transition_.request(
            PresentationIdentity{
                .bus = target.bus,
                // Raw media changes are already resolved once in the
                // canonical content source. A physical presenter owns only
                // the Raw<->Program surface handoff.
                .media_epoch = 0U,
            },
            std::chrono::steady_clock::now());
        if (previous != transition_.desired()) {
            frozen_outgoing_ = latest_presented_frame_;
            reset_transition_pipeline();
            direct_push_ = {};
            last_transition_tick_ = std::chrono::steady_clock::now();
        }
    }

    [[nodiscard]] bool render(
        const std::array<std::shared_ptr<const SourceFrame>, 2U>& frames,
        const std::chrono::steady_clock::time_point now) noexcept {
        try {
            if (source_ == nullptr || bus_ == nullptr) {
                return false;
            }
            auto* error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
            if (error != nullptr) {
                gst_message_unref(error);
                return false;
            }
            const auto desired = transition_.desired();
            const auto incoming = matching_frame(desired, frames);
            if (transition_pipeline_ != nullptr) {
                static_cast<void>(consume_transition_output());
                if (transition_awaiting_output_ &&
                    last_transition_tick_.time_since_epoch().count() != 0) {
                    transition_.delay(now - last_transition_tick_);
                }
            }
            const auto allow_incoming =
                !(last_transition_phase_ ==
                      PresentationTransitionPhase::waiting_at_black &&
                  transition_awaiting_output_);
            const auto transition_sample =
                transition_.sample(now, allow_incoming && incoming != nullptr);
            last_transition_tick_ = now;
            last_transition_phase_ = transition_sample.phase;

            if (transition_sample.phase == PresentationTransitionPhase::stable) {
                reset_transition_pipeline();
                frozen_outgoing_.reset();
                if (incoming != nullptr) {
                    if (!push_frame(incoming, false)) {
                        return false;
                    }
                    latest_presented_frame_ = incoming;
                }
                return true;
            }
            if (transition_sample.phase ==
                PresentationTransitionPhase::waiting_for_first_frame) {
                return true;
            }

            TransitionStage stage = TransitionStage::blend;
            std::shared_ptr<const SourceFrame> outgoing;
            std::shared_ptr<const SourceFrame> transition_incoming;
            std::shared_ptr<const SourceFrame> format_reference;
            switch (transition_sample.phase) {
            case PresentationTransitionPhase::fading_out:
            case PresentationTransitionPhase::waiting_at_black:
                stage = TransitionStage::outgoing_to_black;
                outgoing = frozen_outgoing_;
                format_reference = outgoing;
                break;
            case PresentationTransitionPhase::fading_in:
                stage = TransitionStage::incoming_from_black;
                transition_incoming = incoming;
                format_reference = transition_incoming;
                break;
            case PresentationTransitionPhase::blending:
                stage = TransitionStage::blend;
                outgoing = frozen_outgoing_;
                transition_incoming = incoming;
                format_reference = transition_incoming;
                break;
            default:
                return true;
            }
            if (transition_sample.phase ==
                    PresentationTransitionPhase::waiting_at_black &&
                outgoing == nullptr) {
                return true;
            }
            if (format_reference == nullptr ||
                !ensure_transition_pipeline(stage, format_reference)) {
                return recover_as_cut(incoming, now);
            }
            const auto checkpoint = transition_pipeline_->revision();
            if (!transition_pipeline_->render(
                    outgoing, transition_incoming, transition_sample.weights)) {
                return recover_as_cut(incoming, now);
            }
            transition_awaited_revision_ = checkpoint;
            transition_awaiting_output_ = true;
            if (!consume_transition_output()) {
                return true;
            }
            if (transition_sample.phase ==
                    PresentationTransitionPhase::waiting_at_black &&
                !transition_awaiting_output_) {
                reset_transition_pipeline();
                frozen_outgoing_.reset();
            }
            return true;
        } catch (...) {
            return false;
        }
    }

    [[nodiscard]] bool pump_window() noexcept {
#ifdef _WIN32
        return window_ != nullptr && window_->pump();
#else
        return false;
#endif
    }

    [[nodiscard]] const std::string& target_id() const noexcept { return target_id_; }
    [[nodiscard]] PresentationIdentity desired() const noexcept {
        return transition_.desired();
    }
    [[nodiscard]] bool animation_active() const noexcept {
        return transition_.animation_active() || transition_awaiting_output_;
    }

  private:
    struct PushIdentity final {
        std::uint64_t sequence{0U};
        std::uint64_t stream_epoch{0U};
        std::uint64_t media_epoch{0U};
    };

    enum class TransitionStage : std::uint8_t {
        none,
        outgoing_to_black,
        incoming_from_black,
        blend,
    };

    class TransitionFramePayload final : public GStreamerSamplePayload {
      public:
        explicit TransitionFramePayload(GstSample* sample) : sample_(sample) {
            if (sample_ == nullptr) {
                throw std::invalid_argument("native transition sample is required");
            }
        }
        ~TransitionFramePayload() override { gst_sample_unref(sample_); }
        [[nodiscard]] GstSample* sample() const noexcept override { return sample_; }

      private:
        GstSample* sample_{nullptr};
    };

    void reset_transition_pipeline() noexcept {
        transition_pipeline_.reset();
        transition_stage_ = TransitionStage::none;
        transition_width_ = 0U;
        transition_height_ = 0U;
        transition_output_revision_ = 0U;
        transition_awaiting_output_ = false;
        transition_awaited_revision_ = 0U;
    }

    [[nodiscard]] bool ensure_transition_pipeline(
        const TransitionStage stage,
        const std::shared_ptr<const SourceFrame>& reference) noexcept {
        if (reference == nullptr || reference->width == 0U || reference->height == 0U) {
            return false;
        }
        if (transition_pipeline_ != nullptr && transition_stage_ == stage &&
            transition_width_ == reference->width &&
            transition_height_ == reference->height) {
            return true;
        }
        reset_transition_pipeline();
        try {
            auto device = gstreamer_d3d11_device(reference);
            const bool use_d3d11 =
                reference->memory == SourceFrameMemory::d3d11 && device != nullptr;
            transition_pipeline_ =
                std::make_unique<GStreamerFrameTransitionPipeline>(
                    use_d3d11, std::move(device), reference->width,
                    reference->height);
            transition_stage_ = stage;
            transition_width_ = reference->width;
            transition_height_ = reference->height;
            return true;
        } catch (...) {
            reset_transition_pipeline();
            return false;
        }
    }

    [[nodiscard]] static std::shared_ptr<const SourceFrame> matching_frame(
        const PresentationIdentity presentation,
        const std::array<std::shared_ptr<const SourceFrame>, 2U>& frames) noexcept {
        const auto& frame = frames[static_cast<std::size_t>(presentation.bus)];
        if (frame == nullptr) {
            return {};
        }
        return frame;
    }

    [[nodiscard]] bool push_frame(const std::shared_ptr<const SourceFrame>& frame,
                                  const bool force) noexcept {
        const PushIdentity identity{
            .sequence = frame->sequence,
            .stream_epoch = frame->stream_epoch,
            .media_epoch = frame->media_epoch,
        };
        if (!force && identity.sequence == direct_push_.sequence &&
            identity.stream_epoch == direct_push_.stream_epoch &&
            identity.media_epoch == direct_push_.media_epoch) {
            return true;
        }
        const auto sample = gstreamer_sample(frame);
        if (!sample) {
            return false;
        }
        if (!push_sample(sample.sample)) {
            return false;
        }
        direct_push_ = identity;
        return true;
    }

    [[nodiscard]] bool push_sample(GstSample* sample) noexcept {
        if (source_ == nullptr || sample == nullptr) {
            return false;
        }
        auto* buffer = gst_sample_get_buffer(sample);
        auto* caps = gst_sample_get_caps(sample);
        if (buffer == nullptr || caps == nullptr) {
            return false;
        }
        auto* copy = gst_buffer_copy(buffer);
        if (copy == nullptr) {
            return false;
        }
        GST_BUFFER_PTS(copy) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_DTS(copy) = GST_CLOCK_TIME_NONE;
        auto* pushed = gst_sample_new(copy, caps, nullptr, nullptr);
        gst_buffer_unref(copy);
        if (pushed == nullptr) {
            return false;
        }
        const auto flow = gst_app_src_push_sample(source_, pushed);
        gst_sample_unref(pushed);
        return flow == GST_FLOW_OK;
    }

    [[nodiscard]] std::shared_ptr<const SourceFrame>
    transition_frame(GstSample* sample) noexcept {
        if (sample == nullptr) {
            return {};
        }
        try {
            auto* caps = gst_sample_get_caps(sample);
            auto* buffer = gst_sample_get_buffer(sample);
            GstVideoInfo info{};
            if (caps == nullptr || buffer == nullptr ||
                gst_video_info_from_caps(&info, caps) == FALSE) {
                return {};
            }
            const auto* format =
                gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info));
            if (format == nullptr) {
                return {};
            }
            const auto* features = gst_caps_get_features(caps, 0U);
            const bool d3d11 =
                features != nullptr &&
                gst_caps_features_contains(features, "memory:D3D11Memory") != FALSE;
            auto payload =
                std::make_shared<TransitionFramePayload>(gst_sample_ref(sample));
            return std::make_shared<SourceFrame>(SourceFrame{
                .sequence = ++transition_frame_sequence_,
                .stream_epoch = 1U,
                .media_epoch = 0U,
                .discontinuity = false,
                .presentation_timestamp_ns =
                    GST_CLOCK_TIME_IS_VALID(GST_BUFFER_PTS(buffer))
                        ? static_cast<std::uint64_t>(GST_BUFFER_PTS(buffer))
                        : 0U,
                .duration_ns =
                    GST_CLOCK_TIME_IS_VALID(GST_BUFFER_DURATION(buffer))
                        ? static_cast<std::uint64_t>(GST_BUFFER_DURATION(buffer))
                        : 0U,
                .received_monotonic_ns = static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        std::chrono::steady_clock::now().time_since_epoch())
                        .count()),
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

    [[nodiscard]] bool consume_transition_output() noexcept {
        if (transition_pipeline_ == nullptr) {
            return false;
        }
        auto output =
            transition_pipeline_->output_after(transition_output_revision_);
        if (output.sample == nullptr) {
            return false;
        }
        std::unique_ptr<GstSample, decltype(&gst_sample_unref)> guard{
            output.sample, &gst_sample_unref};
        const auto frame = transition_frame(guard.get());
        if (frame == nullptr || !push_sample(guard.get())) {
            return false;
        }
        transition_output_revision_ = output.revision;
        if (transition_awaiting_output_ &&
            output.revision > transition_awaited_revision_) {
            transition_awaiting_output_ = false;
        }
        latest_presented_frame_ = frame;
        return true;
    }

    [[nodiscard]] bool recover_as_cut(
        const std::shared_ptr<const SourceFrame>& incoming,
        const std::chrono::steady_clock::time_point now) noexcept {
        const auto desired = transition_.desired();
        reset_transition_pipeline();
        transition_ = PresentationTransition{kMediaPresentationTransition};
        transition_.request(desired, now);
        frozen_outgoing_.reset();
        direct_push_ = {};
        if (incoming == nullptr) {
            return true;
        }
        static_cast<void>(transition_.sample(now, true));
        if (!push_frame(incoming, true)) {
            return false;
        }
        latest_presented_frame_ = incoming;
        return true;
    }

    std::string target_id_{};
    PresentationTransition transition_;
    PresentationTransitionPhase last_transition_phase_{
        PresentationTransitionPhase::waiting_for_first_frame};
    std::chrono::steady_clock::time_point last_transition_tick_{};
    std::shared_ptr<const SourceFrame> latest_presented_frame_{};
    std::shared_ptr<const SourceFrame> frozen_outgoing_{};
    std::unique_ptr<GStreamerFrameTransitionPipeline> transition_pipeline_{};
    TransitionStage transition_stage_{TransitionStage::none};
    std::uint32_t transition_width_{0U};
    std::uint32_t transition_height_{0U};
    std::uint64_t transition_output_revision_{0U};
    std::uint64_t transition_frame_sequence_{0U};
    bool transition_awaiting_output_{false};
    std::uint64_t transition_awaited_revision_{0U};
    PushIdentity direct_push_{};
    GstElement* pipeline_{nullptr};
    GstAppSrc* source_{nullptr};
    GstBus* bus_{nullptr};
#ifdef _WIN32
    std::unique_ptr<NativeChildWindow> window_{};
#endif
};

struct NativeWindowRoutingState final {
    std::mutex mutex{};
    std::vector<OutputWindowConfiguration> targets{};
    std::shared_ptr<SourceLease> content_source{};
    std::uint64_t revision{0U};
};

[[nodiscard]] bool same_window_topology(
    const std::vector<OutputWindowConfiguration>& current,
    const std::vector<OutputWindowConfiguration>& next) {
    if (current.size() != next.size()) {
        return false;
    }
    return std::ranges::all_of(current, [&next](const auto& target) {
        const auto match = std::ranges::find_if(next, [&target](const auto& candidate) {
            return candidate.target_id == target.target_id;
        });
        return match != next.end() && match->native_handle == target.native_handle &&
               match->visible == target.visible;
    });
}

#endif

} // namespace

class NativeWindowOutputController::Impl final {
  public:
    explicit Impl(std::shared_ptr<SceneRenderer> renderer) : renderer_(std::move(renderer)) {
        if (renderer_ == nullptr) {
            throw std::invalid_argument("native_window_output_renderer_required");
        }
    }

    ~Impl() { shutdown(); }

    [[nodiscard]] bool configure(
        const std::vector<OutputWindowConfiguration>& targets,
        std::optional<SourceLease> content_source) noexcept {
        try {
            std::scoped_lock lock{mutex_};
            const auto content_required = std::ranges::any_of(
                targets, [](const auto& target) {
                    return target.visible && target.bus == OutputBus::media_windows;
                });
            if (content_required && !content_source.has_value()) {
                return false;
            }
            const auto current_generation = content_source_ != nullptr
                                                ? content_source_->generation()
                                                : 0U;
            const auto next_generation = content_source.has_value()
                                             ? content_source->generation()
                                             : 0U;
            if (targets_ == targets && current_generation == next_generation) {
                return !desired_enabled_ || worker_running_.load() || targets_.empty();
            }
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
            auto next_content_source = content_source.has_value()
                                           ? std::make_shared<SourceLease>(
                                                 std::move(content_source.value()))
                                           : nullptr;
            if (worker_running_.load() && routing_state_ != nullptr &&
                same_window_topology(targets_, targets)) {
                targets_ = targets;
                content_source_ = next_content_source;
                {
                    std::scoped_lock routing_lock{routing_state_->mutex};
                    routing_state_->targets = targets;
                    routing_state_->content_source = next_content_source;
                    ++routing_state_->revision;
                }
                if (content_source_ != nullptr) {
                    content_source_->runtime().wake_frame_waiters();
                }
                return true;
            }
#endif
            stop_locked();
            targets_ = targets;
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
            content_source_ = std::move(next_content_source);
#else
            content_source_ = content_source.has_value()
                                  ? std::make_shared<SourceLease>(
                                        std::move(content_source.value()))
                                  : nullptr;
#endif
            return !desired_enabled_ || targets_.empty() || start_locked();
        } catch (...) {
            return false;
        }
    }

    [[nodiscard]] bool set_enabled(const bool enabled) noexcept {
        try {
            std::scoped_lock lock{mutex_};
            desired_enabled_ = enabled;
            if (!enabled || targets_.empty()) {
                stop_locked();
                return true;
            }
            return worker_running_.load() || start_locked();
        } catch (...) {
            return false;
        }
    }

    void shutdown() noexcept {
        try {
            std::scoped_lock lock{mutex_};
            desired_enabled_ = false;
            stop_locked();
            targets_.clear();
            content_source_.reset();
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
            routing_state_.reset();
#endif
        } catch (...) {
        }
    }

  private:
    [[nodiscard]] bool start_locked() {
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        if (worker_.joinable()) {
            stop_locked();
        }
        const auto has_visible_target = std::any_of(
            targets_.begin(), targets_.end(), [](const auto& target) { return target.visible; });
        if (!has_visible_target) {
            return true;
        }
        std::promise<bool> started;
        auto started_future = started.get_future();
        routing_state_ = std::make_shared<NativeWindowRoutingState>();
        routing_state_->targets = targets_;
        routing_state_->content_source = content_source_;
        routing_state_->revision = 1U;
        worker_ =
            std::jthread([renderer = renderer_, routing = routing_state_,
                          started = std::move(started), running = &worker_running_](
                             const std::stop_token stop_token) mutable {
                std::vector<std::unique_ptr<NativeTargetPipeline>> pipelines;
                std::uint64_t applied_revision = 0U;
                const auto routing_snapshot = [&routing] {
                    std::scoped_lock lock{routing->mutex};
                    return std::tuple{
                        routing->targets,
                        routing->content_source,
                        routing->revision,
                    };
                };
                const auto rebuild_pipelines = [&routing_snapshot, &pipelines,
                                                &applied_revision] {
                    const auto [targets, content_source, revision] = routing_snapshot();
                    static_cast<void>(content_source);
                    std::vector<std::unique_ptr<NativeTargetPipeline>> next;
                    next.reserve(targets.size());
                    for (const auto& target : targets) {
                        if (target.visible) {
                            next.push_back(std::make_unique<NativeTargetPipeline>(target));
                        }
                    }
                    pipelines = std::move(next);
                    applied_revision = revision;
                };
                const auto apply_routing = [&routing_snapshot, &pipelines,
                                           &applied_revision] {
                    const auto [targets, content_source, revision] = routing_snapshot();
                    static_cast<void>(content_source);
                    if (revision == applied_revision) {
                        return;
                    }
                    for (const auto& target : targets) {
                        const auto pipeline = std::ranges::find_if(
                            pipelines, [&target](const auto& candidate) {
                                return candidate->target_id() == target.target_id;
                            });
                        if (pipeline != pipelines.end()) {
                            (*pipeline)->request(target);
                        }
                    }
                    applied_revision = revision;
                };
                try {
                    rebuild_pipelines();
                    running->store(!pipelines.empty());
                    started.set_value(running->load());
                } catch (...) {
                    running->store(false);
                    started.set_value(false);
                    return;
                }
                const auto recover_pipelines = [&] {
                    pipelines.clear();
                    std::this_thread::sleep_for(250ms);
                    try {
                        rebuild_pipelines();
                    } catch (...) {
                    }
                };
                while (!stop_token.stop_requested()) {
                    if (pipelines.empty()) {
                        recover_pipelines();
                        continue;
                    }
                    bool healthy = true;
                    for (const auto& pipeline : pipelines) {
                        healthy = pipeline->pump_window() && healthy;
                    }
                    if (!healthy) {
                        recover_pipelines();
                        continue;
                    }
                    apply_routing();
                    const auto [routed_targets, content_source, routed_revision] =
                        routing_snapshot();
                    static_cast<void>(routed_targets);
                    static_cast<void>(routed_revision);
                    auto* content_runtime = content_source != nullptr
                                                ? &content_source->runtime()
                                                : nullptr;
                    std::array<std::shared_ptr<const SourceFrame>, 2U> frames{};
                    frames[static_cast<std::size_t>(OutputBus::media_windows)] =
                        content_runtime == nullptr ? nullptr : content_runtime->latest_frame();
                    auto program_frame =
                        renderer->latest_gpu_frame(OutputBus::virtual_camera);
                    if (program_frame == nullptr) {
                        // Device recovery and non-D3D builds retain the existing
                        // system-memory route as a bounded compatibility fallback.
                        program_frame =
                            renderer->latest_frame(OutputBus::virtual_camera);
                    }
                    frames[static_cast<std::size_t>(OutputBus::virtual_camera)] =
                        std::move(program_frame);
                    const auto now = std::chrono::steady_clock::now();
                    for (const auto& pipeline : pipelines) {
                        healthy = pipeline->render(frames, now) && healthy;
                    }
                    if (!healthy) {
                        recover_pipelines();
                        continue;
                    }
                    const auto has_content_target = std::ranges::any_of(
                        pipelines, [](const auto& pipeline) {
                            return pipeline->desired().bus == OutputBus::media_windows;
                        });
                    const auto has_program_target = std::ranges::any_of(
                        pipelines, [](const auto& pipeline) {
                            return pipeline->desired().bus == OutputBus::virtual_camera;
                        });
                    const auto has_active_transition = std::ranges::any_of(
                        pipelines, [](const auto& pipeline) {
                            return pipeline->animation_active();
                        });
                    const auto deadline = std::chrono::steady_clock::now() +
                                          (has_active_transition ||
                                                   (has_content_target && has_program_target)
                                               ? kTransitionFrameInterval
                                               : 100ms);
                    if (has_content_target && content_runtime != nullptr) {
                        const auto content_sequence =
                            frames[static_cast<std::size_t>(OutputBus::media_windows)] == nullptr
                                ? 0U
                                : frames[static_cast<std::size_t>(OutputBus::media_windows)]
                                      ->sequence;
                        static_cast<void>(content_runtime->wait_for_frame(
                            content_sequence,
                            stop_token, deadline));
                    } else if (has_program_target) {
                        const auto program_sequence =
                            frames[static_cast<std::size_t>(OutputBus::virtual_camera)] == nullptr
                                ? 0U
                                : frames[static_cast<std::size_t>(OutputBus::virtual_camera)]
                                      ->sequence;
                        static_cast<void>(renderer->wait_for_gpu_frame(
                            OutputBus::virtual_camera,
                            program_sequence,
                            stop_token, deadline));
                    } else {
                        std::this_thread::sleep_until(deadline);
                    }
                }
                running->store(false);
            });
        if (started_future.wait_for(2s) != std::future_status::ready || !started_future.get()) {
            stop_locked();
            return false;
        }
        return true;
#else
        return false;
#endif
    }

    void stop_locked() noexcept {
        if (worker_.joinable()) {
            worker_.request_stop();
            worker_.join();
        }
        worker_running_.store(false);
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
        routing_state_.reset();
#endif
    }

    std::shared_ptr<SceneRenderer> renderer_{};
    std::mutex mutex_{};
    std::vector<OutputWindowConfiguration> targets_{};
    std::shared_ptr<SourceLease> content_source_{};
    bool desired_enabled_{false};
    std::atomic_bool worker_running_{false};
    std::jthread worker_{};
#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
    std::shared_ptr<NativeWindowRoutingState> routing_state_{};
#endif
};

NativeWindowOutputController::NativeWindowOutputController(std::shared_ptr<SceneRenderer> renderer)
    : impl_(std::make_unique<Impl>(std::move(renderer))) {}

NativeWindowOutputController::~NativeWindowOutputController() = default;

bool NativeWindowOutputController::configure(
    const std::vector<OutputWindowConfiguration>& targets,
    std::optional<SourceLease> content_source) noexcept {
    return impl_->configure(targets, std::move(content_source));
}

bool NativeWindowOutputController::set_enabled(const bool enabled) noexcept {
    return impl_->set_enabled(enabled);
}

void NativeWindowOutputController::shutdown() noexcept { impl_->shutdown(); }

} // namespace solin::media_engine
