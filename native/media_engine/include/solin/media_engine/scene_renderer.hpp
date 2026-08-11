#pragma once

#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace solin::media_engine {

struct CompiledSceneGraph;

using VideoFrameVisitor = std::function<void(const VideoFrameView&)>;

struct SceneRenderSourceBinding {
    std::string source_id{};
    std::uint64_t generation{0U};
    // The matching SourceLease owns this runtime for the entire prepared-graph
    // lifetime. Renderers must not retain the pointer beyond that graph.
    SourceRuntime* runtime{nullptr};
};

struct SceneRenderPreparation {
    OutputBus bus{OutputBus::media_windows};
    std::uint64_t document_revision{0U};
    SceneOutputDefinition output{};
    std::shared_ptr<const CompiledSceneGraph> graph{};
    std::vector<SceneRenderSourceBinding> sources{};
};

class PreparedSceneRenderGraph {
  public:
    virtual ~PreparedSceneRenderGraph() = default;

    PreparedSceneRenderGraph(const PreparedSceneRenderGraph&) = delete;
    PreparedSceneRenderGraph& operator=(const PreparedSceneRenderGraph&) = delete;
    PreparedSceneRenderGraph(PreparedSceneRenderGraph&&) = delete;
    PreparedSceneRenderGraph& operator=(PreparedSceneRenderGraph&&) = delete;

    // Editor-only geometry updates are applied to the already prepared graph.
    // They deliberately do not mutate the authoritative scene document; the
    // following document commit remains the durability boundary.
    [[nodiscard]] virtual bool
    preview_layer_geometry(std::string_view scene_id, std::string_view layer_id,
                           const SceneLayerGeometry& geometry) noexcept {
        static_cast<void>(scene_id);
        static_cast<void>(layer_id);
        static_cast<void>(geometry);
        return false;
    }

  protected:
    PreparedSceneRenderGraph() = default;
};

class SceneRendererError final : public std::runtime_error {
  public:
    SceneRendererError(std::string error_code, std::string message);

    [[nodiscard]] const std::string& error_code() const noexcept;

  private:
    std::string error_code_{};
};

class SceneRenderer {
  public:
    virtual ~SceneRenderer() = default;

    SceneRenderer(const SceneRenderer&) = delete;
    SceneRenderer& operator=(const SceneRenderer&) = delete;
    SceneRenderer(SceneRenderer&&) = delete;
    SceneRenderer& operator=(SceneRenderer&&) = delete;

    [[nodiscard]] virtual std::shared_ptr<PreparedSceneRenderGraph>
    prepare(const SceneRenderPreparation& preparation) = 0;

    virtual void commit_hydration(
        const std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U>& graphs,
        const std::array<bool, 2U>& enabled, std::uint64_t sequence) noexcept = 0;
    virtual void commit_take(OutputBus bus,
                             std::shared_ptr<PreparedSceneRenderGraph> graph,
                             std::uint64_t sequence) noexcept = 0;
    virtual void set_output_enabled(OutputBus bus, bool enabled,
                                    std::uint64_t sequence) noexcept = 0;
    [[nodiscard]] virtual std::shared_ptr<const SourceFrame>
    latest_frame(OutputBus bus) const noexcept = 0;
    [[nodiscard]] virtual std::optional<std::uint64_t>
    visit_latest_frame(OutputBus bus, std::uint64_t after_sequence,
                       const VideoFrameVisitor& visitor) const noexcept {
        static_cast<void>(bus);
        static_cast<void>(after_sequence);
        static_cast<void>(visitor);
        return std::nullopt;
    }
    virtual void shutdown() noexcept = 0;

  protected:
    SceneRenderer() = default;
};

} // namespace solin::media_engine
