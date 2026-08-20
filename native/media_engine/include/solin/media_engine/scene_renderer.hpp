#pragma once

#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/source_registry.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <array>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <stop_token>
#include <stdexcept>
#include <string>
#include <vector>

namespace solin::media_engine {

enum class SceneTransitionKind : std::uint8_t {
    cut,
    dissolve,
    fade_to_black,
};

struct SceneTransitionSpec {
    SceneTransitionKind kind{SceneTransitionKind::cut};
    std::uint32_t duration_ms{0U};

    bool operator==(const SceneTransitionSpec&) const = default;
};

struct SceneTransitionWeights {
    double outgoing{0.0};
    double incoming{1.0};

    bool operator==(const SceneTransitionWeights&) const = default;
};

using BgraPixel = std::array<std::uint8_t, 4U>;

inline constexpr std::uint32_t kMinimumAnimatedTransitionDurationMs = 50U;
inline constexpr std::uint32_t kMaximumAnimatedTransitionDurationMs = 10'000U;

[[nodiscard]] SceneTransitionKind scene_transition_kind_from_text(std::string_view value);
[[nodiscard]] std::string_view scene_transition_kind_text(SceneTransitionKind kind) noexcept;
void validate_scene_transition(const SceneTransitionSpec& transition);
[[nodiscard]] SceneTransitionWeights
scene_transition_weights(const SceneTransitionSpec& transition, double progress) noexcept;
[[nodiscard]] BgraPixel
scene_transition_blend_pixel(const SceneTransitionSpec& transition, double progress,
                             const BgraPixel& outgoing,
                             const BgraPixel& incoming) noexcept;
[[nodiscard]] std::chrono::nanoseconds
scene_transition_frame_interval(const OutputVideoFormat& format);

struct CompiledSceneGraph;

using VideoFrameVisitor = std::function<void(const VideoFrameView&)>;

struct SceneRenderSourceBinding {
    std::string source_id{};
    std::uint64_t generation{0U};
    // The matching SourceLease owns this runtime for the entire prepared-graph
    // lifetime. Renderers must not retain the pointer beyond that graph.
    SourceRuntime* runtime{nullptr};
};

// Render graphs share ownership of their source leases with the scene runtime.
// This prevents a transition from outliving the non-owning SourceRuntime pointers
// embedded in either its outgoing or incoming graph.
struct SceneRenderResources {
    std::vector<SourceLease> source_leases{};
};

struct SceneRenderPreparation {
    OutputBus bus{OutputBus::media_windows};
    std::uint64_t document_revision{0U};
    SceneOutputDefinition output{};
    std::shared_ptr<const CompiledSceneGraph> graph{};
    std::vector<SceneRenderSourceBinding> sources{};
    std::shared_ptr<SceneRenderResources> resources{};
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

    [[nodiscard]] virtual std::shared_ptr<const SourceFrame>
    latest_frame() const noexcept {
        return {};
    }
    [[nodiscard]] virtual std::shared_ptr<const SourceFrame>
    latest_gpu_frame() const noexcept {
        return {};
    }
    [[nodiscard]] virtual std::optional<std::uint64_t>
    visit_latest_frame(std::uint64_t after_sequence,
                       const VideoFrameVisitor& visitor) const noexcept {
        static_cast<void>(after_sequence);
        static_cast<void>(visitor);
        return std::nullopt;
    }
    [[nodiscard]] virtual bool wait_for_frame(
        std::uint64_t after_sequence, std::stop_token stop_token,
        std::chrono::steady_clock::time_point deadline) const noexcept {
        static_cast<void>(after_sequence);
        static_cast<void>(stop_token);
        static_cast<void>(deadline);
        return false;
    }
    virtual void wake_frame_waiters() noexcept {}
    virtual void start_transition() noexcept {}
    virtual void stop() noexcept {}
    virtual void set_direct_output_enabled(bool enabled) noexcept {
        static_cast<void>(enabled);
    }
    [[nodiscard]] virtual bool is_transition_output() const noexcept { return false; }
    [[nodiscard]] virtual std::shared_ptr<PreparedSceneRenderGraph>
    transition_target() const noexcept {
        return {};
    }
    [[nodiscard]] virtual std::shared_ptr<PreparedSceneRenderGraph>
    transition_origin() const noexcept {
        return {};
    }

  protected:
    PreparedSceneRenderGraph() = default;
};

struct SceneRenderTransitionPreparation {
    SceneTransitionSpec effective_transition{};
    std::shared_ptr<PreparedSceneRenderGraph> render_output{};
    bool fallback_applied{false};
    std::string fallback_reason{};
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

    [[nodiscard]] virtual SceneRenderTransitionPreparation
    prepare_transition(OutputBus bus,
                       const std::shared_ptr<PreparedSceneRenderGraph>& incoming,
                       const SceneTransitionSpec& transition) = 0;

    virtual void commit_hydration(
        const std::array<std::shared_ptr<PreparedSceneRenderGraph>, 2U>& graphs,
        const std::array<bool, 2U>& enabled, std::uint64_t sequence) noexcept = 0;
    virtual void commit_take(OutputBus bus,
                             std::shared_ptr<PreparedSceneRenderGraph> target_graph,
                             std::shared_ptr<PreparedSceneRenderGraph> render_output,
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
    [[nodiscard]] virtual bool wait_for_frame(
        OutputBus bus, std::uint64_t after_sequence, std::stop_token stop_token,
        std::chrono::steady_clock::time_point deadline) const noexcept {
        static_cast<void>(bus);
        static_cast<void>(after_sequence);
        static_cast<void>(stop_token);
        static_cast<void>(deadline);
        return false;
    }
    virtual void shutdown() noexcept = 0;

  protected:
    SceneRenderer() = default;
};

} // namespace solin::media_engine
