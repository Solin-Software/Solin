#include "solin/media_engine/native_window_output.hpp"

#include "gstreamer_source_runtime.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <future>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
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
        : render_bus_(target.bus) {
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
        auto* source = gst_element_factory_make("appsrc", nullptr);
        auto* queue = gst_element_factory_make("queue", nullptr);
#ifdef _WIN32
        auto* sink = gst_element_factory_make("d3d11videosink", nullptr);
#else
        auto* sink = gst_element_factory_make("autovideosink", nullptr);
#endif
        if (source == nullptr || queue == nullptr || sink == nullptr) {
            if (source != nullptr) {
                gst_object_unref(source);
            }
            if (queue != nullptr) {
                gst_object_unref(queue);
            }
            if (sink != nullptr) {
                gst_object_unref(sink);
            }
            throw std::runtime_error("native_window_output_unavailable");
        }
        gst_bin_add_many(GST_BIN(pipeline), source, queue, sink, nullptr);
        g_object_set(source, "is-live", TRUE, "format", GST_FORMAT_TIME, "do-timestamp", TRUE,
                     "block", FALSE, nullptr);
        gst_app_src_set_max_buffers(GST_APP_SRC(source), 1U);
        gst_app_src_set_leaky_type(GST_APP_SRC(source), GST_APP_LEAKY_TYPE_DOWNSTREAM);
        g_object_set(queue, "max-size-buffers", 1U, "max-size-bytes", 0U, "max-size-time",
                     static_cast<guint64>(0U), "leaky", 2, nullptr);
        g_object_set(sink, "sync", FALSE, "enable-last-sample", FALSE, "force-aspect-ratio", TRUE,
                     nullptr);
        require_link(source, queue);
        require_link(queue, sink);
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
    }

    ~NativeTargetPipeline() {
        if (bus_ != nullptr) {
            gst_object_unref(bus_);
        }
        release_pipeline(pipeline_);
    }

    NativeTargetPipeline(const NativeTargetPipeline&) = delete;
    NativeTargetPipeline& operator=(const NativeTargetPipeline&) = delete;

    [[nodiscard]] bool push(const std::shared_ptr<const SourceFrame>& frame) noexcept {
        try {
            if (frame == nullptr || source_ == nullptr || bus_ == nullptr) {
                return false;
            }
            auto* error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
            if (error != nullptr) {
                gst_message_unref(error);
                return false;
            }
            const auto sample = gstreamer_sample(frame);
            if (!sample) {
                return false;
            }
            auto* buffer = gst_sample_get_buffer(sample.sample);
            auto* caps = gst_sample_get_caps(sample.sample);
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

    [[nodiscard]] OutputBus bus() const noexcept { return render_bus_; }

  private:
    OutputBus render_bus_{OutputBus::media_windows};
    GstElement* pipeline_{nullptr};
    GstAppSrc* source_{nullptr};
    GstBus* bus_{nullptr};
#ifdef _WIN32
    std::unique_ptr<NativeChildWindow> window_{};
#endif
};

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
            const auto current_generation = content_source_.has_value()
                                                ? content_source_->generation()
                                                : 0U;
            const auto next_generation = content_source.has_value()
                                             ? content_source->generation()
                                             : 0U;
            if (targets_ == targets && current_generation == next_generation) {
                return !desired_enabled_ || worker_running_.load() || targets_.empty();
            }
            stop_locked();
            targets_ = targets;
            content_source_ = std::move(content_source);
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
        auto* content_runtime = content_source_.has_value()
                                    ? &content_source_->runtime()
                                    : nullptr;
        worker_ =
            std::jthread([renderer = renderer_, targets = targets_, content_runtime,
                          started = std::move(started), running = &worker_running_](
                             const std::stop_token stop_token) mutable {
                std::vector<std::unique_ptr<NativeTargetPipeline>> pipelines;
                const auto rebuild_pipelines = [&targets, &pipelines] {
                    std::vector<std::unique_ptr<NativeTargetPipeline>> next;
                    next.reserve(targets.size());
                    for (const auto& target : targets) {
                        if (target.visible) {
                            next.push_back(std::make_unique<NativeTargetPipeline>(target));
                        }
                    }
                    pipelines = std::move(next);
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
                std::array<std::uint64_t, 2U> last_sequences{};
                const auto recover_pipelines = [&] {
                    pipelines.clear();
                    std::this_thread::sleep_for(250ms);
                    try {
                        rebuild_pipelines();
                        last_sequences.fill(0U);
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
                    for (std::size_t index = 0U; index < frames.size(); ++index) {
                        const auto& frame = frames[index];
                        if (frame == nullptr || frame->sequence <= last_sequences[index]) {
                            continue;
                        }
                        const auto bus = static_cast<OutputBus>(index);
                        for (const auto& pipeline : pipelines) {
                            if (pipeline->bus() == bus) {
                                healthy = pipeline->push(frame) && healthy;
                            }
                        }
                        last_sequences[index] = frame->sequence;
                    }
                    if (!healthy) {
                        recover_pipelines();
                        continue;
                    }
                    const auto has_content_target = std::ranges::any_of(
                        pipelines, [](const auto& pipeline) {
                            return pipeline->bus() == OutputBus::media_windows;
                        });
                    const auto has_program_target = std::ranges::any_of(
                        pipelines, [](const auto& pipeline) {
                            return pipeline->bus() == OutputBus::virtual_camera;
                        });
                    const auto deadline = std::chrono::steady_clock::now() +
                                          (has_program_target ? 16ms : 100ms);
                    if (has_content_target && content_runtime != nullptr) {
                        static_cast<void>(content_runtime->wait_for_frame(
                            last_sequences[static_cast<std::size_t>(
                                OutputBus::media_windows)],
                            stop_token, deadline));
                    } else if (has_program_target) {
                        static_cast<void>(renderer->wait_for_gpu_frame(
                            OutputBus::virtual_camera,
                            last_sequences[static_cast<std::size_t>(
                                OutputBus::virtual_camera)],
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
    }

    std::shared_ptr<SceneRenderer> renderer_{};
    std::mutex mutex_{};
    std::vector<OutputWindowConfiguration> targets_{};
    std::optional<SourceLease> content_source_{};
    bool desired_enabled_{false};
    std::atomic_bool worker_running_{false};
    std::jthread worker_{};
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
