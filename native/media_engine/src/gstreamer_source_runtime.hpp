#pragma once

#include "solin/media_engine/source_registry.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string_view>

typedef struct _GstSample GstSample;
typedef struct _GstD3D11Device GstD3D11Device;

namespace solin::media_engine {

using LocalCameraFormatResolver = std::function<
    std::optional<LocalCameraSourceConfiguration>(std::string_view)>;

class GStreamerSamplePayload : public SourceFramePayload {
  public:
    ~GStreamerSamplePayload() override = default;

    [[nodiscard]] virtual GstSample* sample() const noexcept = 0;

  protected:
    GStreamerSamplePayload() = default;
};

// One process-local activity signal is shared by all runtimes created by a
// factory and every compositor graph consuming them. It carries no pixels and
// no queue: consumers wake, then read the latest frame from their own sources.
class GStreamerFrameSignal final {
  public:
    [[nodiscard]] std::uint64_t revision() const noexcept;
    void notify() noexcept;
    [[nodiscard]] std::uint64_t wait_after(std::uint64_t revision) const noexcept;
    [[nodiscard]] bool wait_after(
        std::uint64_t revision, std::stop_token stop_token,
        std::chrono::steady_clock::time_point deadline) const noexcept;

  private:
    std::atomic_uint64_t revision_{0U};
    mutable std::mutex mutex_{};
    mutable std::condition_variable_any wakeup_{};
};

[[nodiscard]] std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(std::shared_ptr<void> runtime_lifetime);
[[nodiscard]] std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(std::shared_ptr<void> runtime_lifetime,
                                      bool allow_d3d11);
[[nodiscard]] std::shared_ptr<SourceRuntimeFactory>
make_gstreamer_source_runtime_factory(
    std::shared_ptr<void> runtime_lifetime, bool allow_d3d11,
    LocalCameraFormatResolver camera_format_resolver);

struct GStreamerSampleLease final {
    std::shared_ptr<const SourceFrame> frame{};
    GstSample* sample{nullptr};

    [[nodiscard]] explicit operator bool() const noexcept {
        return frame != nullptr && sample != nullptr;
    }
};

struct GStreamerD3d11DeviceLease final {
    std::shared_ptr<SourceRuntimeFactory> factory{};
    std::shared_ptr<GstD3D11Device> owner{};
    GstD3D11Device* device{nullptr};

    [[nodiscard]] explicit operator bool() const noexcept {
        return factory != nullptr && owner != nullptr && device == owner.get();
    }
};

[[nodiscard]] GStreamerSampleLease
gstreamer_sample(std::shared_ptr<const SourceFrame> frame) noexcept;
[[nodiscard]] GStreamerD3d11DeviceLease
gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory> factory) noexcept;
[[nodiscard]] std::shared_ptr<GStreamerFrameSignal>
gstreamer_frame_signal(std::shared_ptr<SourceRuntimeFactory> factory) noexcept;
[[nodiscard]] bool
invalidate_gstreamer_d3d11_device(std::shared_ptr<SourceRuntimeFactory> factory) noexcept;

} // namespace solin::media_engine
