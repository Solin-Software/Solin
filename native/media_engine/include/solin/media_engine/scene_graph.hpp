#pragma once

#include "solin/media_engine/scene_renderer.hpp"
#include "solin/media_engine/source_registry.hpp"

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

struct CompiledSceneGraph;

struct CompiledSceneLayer {
    SceneLayerDefinition layer{};
    SceneSourceKind source_kind{SceneSourceKind::solin_content};
    bool source_enabled{false};
    std::shared_ptr<const CompiledSceneGraph> referenced_scene{};
};

struct CompiledSceneGraph {
    std::string scene_id{};
    std::vector<CompiledSceneLayer> layers{};
    std::vector<std::string> active_leaf_source_ids{};
};

struct CompiledSceneDocument {
    std::string document_id{};
    std::uint64_t document_revision{0U};
    std::map<std::string, SceneSource, std::less<>> sources{};
    std::map<std::string, std::shared_ptr<const CompiledSceneGraph>, std::less<>> scenes{};
};

[[nodiscard]] std::shared_ptr<const CompiledSceneDocument>
compile_scene_document(const SceneHydrationSnapshot& snapshot);

class SceneGraphError final : public std::runtime_error {
  public:
    SceneGraphError(std::string error_code, std::string message);

    [[nodiscard]] const std::string& error_code() const noexcept;

  private:
    std::string error_code_{};
};

struct ScenePreparationReceipt {
    OutputBus bus{OutputBus::media_windows};
    std::string scene_id{};
    std::string preparation_token{};
    SceneTransitionSpec effective_transition{};
    bool fallback_applied{false};
    std::string fallback_reason{};
};

class SceneGraphRuntime final {
  public:
    explicit SceneGraphRuntime(std::shared_ptr<SourceRuntimeFactory> source_factory,
                               std::shared_ptr<SceneRenderer> renderer = {});
    ~SceneGraphRuntime();

    SceneGraphRuntime(const SceneGraphRuntime&) = delete;
    SceneGraphRuntime& operator=(const SceneGraphRuntime&) = delete;
    SceneGraphRuntime(SceneGraphRuntime&&) = delete;
    SceneGraphRuntime& operator=(SceneGraphRuntime&&) = delete;

    void hydrate(const SceneHydrationSnapshot& snapshot, std::uint64_t sequence);
    [[nodiscard]] ScenePreparationReceipt
    prepare(OutputBus bus, std::string_view scene_id, std::uint64_t document_revision,
            std::string_view request_id, std::uint64_t sequence,
            const SceneTransitionSpec& transition = {});
    void take(const ScenePreparationReceipt& preparation,
              std::uint64_t document_revision, std::uint64_t sequence);
    void take(OutputBus bus, std::string_view scene_id, std::string_view preparation_token,
              std::uint64_t document_revision, std::uint64_t sequence);
    void cancel(std::string_view request_id) noexcept;
    void set_output_enabled(OutputBus bus, bool enabled, std::uint64_t document_revision,
                            std::uint64_t sequence);
    void preview_layer_geometry(OutputBus bus, std::string_view scene_id,
                                const SceneLayerDefinition& layer,
                                std::uint64_t document_revision,
                                std::uint64_t sequence);
    void shutdown() noexcept;

    [[nodiscard]] std::uint64_t document_revision() const noexcept;
    [[nodiscard]] std::string active_scene(OutputBus bus) const;
    [[nodiscard]] bool output_enabled(OutputBus bus) const;
    [[nodiscard]] std::size_t pending_preparation_count() const noexcept;
    [[nodiscard]] std::shared_ptr<const CompiledSceneDocument> compiled_document() const;

  private:
    class Impl;
    std::unique_ptr<Impl> impl_{};
};

} // namespace solin::media_engine
