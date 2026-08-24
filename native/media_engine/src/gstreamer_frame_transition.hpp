#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/source_registry.hpp"

#include <cstdint>
#include <memory>

typedef struct _GstSample GstSample;
typedef struct _GstD3D11Device GstD3D11Device;

namespace solin::media_engine {

// A short-lived, readiness-observable GPU graph. It is deliberately not a
// long-lived presentation owner: callers create it for one transition, drive
// both source frames until output is acknowledged, retain the resulting
// GstSample, then destroy the graph and return to their direct stable route.
class GStreamerFrameTransitionPipeline final {
  public:
    struct Output final {
        std::uint64_t revision{0U};
        // A new reference owned by the caller. Unref it after wrapping or use.
        GstSample* sample{nullptr};
    };

    GStreamerFrameTransitionPipeline(bool use_d3d11,
                                     std::shared_ptr<GstD3D11Device> device,
                                     std::uint32_t width,
                                     std::uint32_t height);
    ~GStreamerFrameTransitionPipeline();

    GStreamerFrameTransitionPipeline(const GStreamerFrameTransitionPipeline&) = delete;
    GStreamerFrameTransitionPipeline&
    operator=(const GStreamerFrameTransitionPipeline&) = delete;

    [[nodiscard]] bool render(
        const std::shared_ptr<const SourceFrame>& outgoing,
        const std::shared_ptr<const SourceFrame>& incoming,
        SceneTransitionWeights weights) noexcept;
    [[nodiscard]] std::uint64_t revision() const noexcept;
    [[nodiscard]] Output output_after(std::uint64_t revision) const noexcept;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_{};
};

} // namespace solin::media_engine
