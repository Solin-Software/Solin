#include "solin/media_engine/scene_graph.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <utility>

namespace solin::media_engine {
namespace {

constexpr std::size_t kOutputBusCount = 2U;
constexpr auto kRetiredGraphReleaseDelay = std::chrono::seconds{3};
constexpr std::size_t kMaximumRetiredGraphs = 16U;

[[nodiscard]] std::size_t bus_index(const OutputBus bus) noexcept {
    return static_cast<std::size_t>(bus);
}

[[nodiscard]] bool visible_source_layer(const SceneLayerDefinition& layer,
                                        const SceneSource& source) noexcept {
    return source.enabled && layer.geometry.visible && layer.geometry.opacity > 0.0 &&
           layer.geometry.width > 0.0 && layer.geometry.height > 0.0;
}

[[nodiscard]] bool same_document_definition(const SceneHydrationSnapshot& left,
                                            const SceneHydrationSnapshot& right) {
    return left.document_id == right.document_id && left.sources == right.sources &&
           left.scenes == right.scenes && left.outputs == right.outputs;
}

class SceneCompiler final {
  public:
    explicit SceneCompiler(const SceneHydrationSnapshot& snapshot) {
        for (const auto& source : snapshot.sources) {
            const auto [_, inserted] = sources_.emplace(source.id, source);
            if (!inserted) {
                throw SceneGraphError{"invalid_scene_graph",
                                      "The scene graph contains duplicate sources"};
            }
        }
        for (const auto& scene : snapshot.scenes) {
            const auto [_, inserted] = definitions_.emplace(scene.id, &scene);
            if (!inserted) {
                throw SceneGraphError{"invalid_scene_graph",
                                      "The scene graph contains duplicate scenes"};
            }
        }
    }

    [[nodiscard]] std::shared_ptr<const CompiledSceneDocument>
    compile(const SceneHydrationSnapshot& snapshot) {
        auto document = std::make_shared<CompiledSceneDocument>();
        document->document_id = snapshot.document_id;
        document->document_revision = snapshot.document_revision;
        document->sources = sources_;
        for (const auto& [scene_id, _] : definitions_) {
            document->scenes.emplace(scene_id, compile_scene(scene_id));
        }
        return document;
    }

  private:
    [[nodiscard]] std::shared_ptr<const CompiledSceneGraph>
    compile_scene(const std::string& scene_id) {
        const auto existing = compiled_.find(scene_id);
        if (existing != compiled_.end()) {
            return existing->second;
        }
        const auto state = states_[scene_id];
        if (state == 1U) {
            throw SceneGraphError{"scene_reference_cycle",
                                  "Scene references contain a cycle"};
        }
        const auto definition = definitions_.find(scene_id);
        if (definition == definitions_.end()) {
            throw SceneGraphError{"scene_not_found", "The configured scene does not exist"};
        }
        states_[scene_id] = 1U;
        auto graph = std::make_shared<CompiledSceneGraph>();
        graph->scene_id = scene_id;
        std::set<std::string, std::less<>> active_sources;
        graph->layers.reserve(definition->second->layers.size());
        for (const auto& layer : definition->second->layers) {
            const auto source = sources_.find(layer.source_id);
            if (source == sources_.end()) {
                throw SceneGraphError{"source_not_found",
                                      "A scene layer references a missing source"};
            }
            CompiledSceneLayer compiled_layer{
                .layer = layer,
                .source_kind = source->second.kind,
                .source_enabled = source->second.enabled,
            };
            if (source->second.kind == SceneSourceKind::scene_reference) {
                const auto* reference = std::get_if<SceneReferenceSourceConfiguration>(
                    &source->second.configuration);
                if (reference == nullptr) {
                    throw SceneGraphError{"invalid_scene_graph",
                                          "A scene reference has invalid configuration"};
                }
                compiled_layer.referenced_scene = compile_scene(reference->target_scene_id);
                if (visible_source_layer(layer, source->second)) {
                    active_sources.insert(
                        compiled_layer.referenced_scene->active_leaf_source_ids.begin(),
                        compiled_layer.referenced_scene->active_leaf_source_ids.end());
                }
            } else if (visible_source_layer(layer, source->second)) {
                active_sources.insert(layer.source_id);
            }
            graph->layers.push_back(std::move(compiled_layer));
        }
        graph->active_leaf_source_ids.assign(active_sources.begin(), active_sources.end());
        states_[scene_id] = 2U;
        compiled_.emplace(scene_id, graph);
        return graph;
    }

    std::map<std::string, SceneSource, std::less<>> sources_{};
    std::map<std::string, const SceneGraphDefinition*, std::less<>> definitions_{};
    std::map<std::string, std::uint8_t, std::less<>> states_{};
    std::map<std::string, std::shared_ptr<const CompiledSceneGraph>, std::less<>> compiled_{};
};

[[noreturn]] void map_source_failure(const std::exception& error) {
    const std::string_view code{error.what()};
    if (code == "source_runtime_not_implemented") {
        throw SceneGraphError{"source_not_implemented",
                              "A source used by this scene is not implemented"};
    }
    if (code.find("capacity") != std::string_view::npos ||
        code.find("budget") != std::string_view::npos) {
        throw SceneGraphError{"media_resource_budget_exceeded",
                              "The scene exceeds the configured media resource budget"};
    }
    if (code == "source is disabled") {
        throw SceneGraphError{"source_disabled", "A source used by this scene is disabled"};
    }
    throw SceneGraphError{"source_unavailable",
                          "A source used by this scene could not be prepared"};
}

} // namespace

std::shared_ptr<const CompiledSceneDocument>
compile_scene_document(const SceneHydrationSnapshot& snapshot) {
    return SceneCompiler{snapshot}.compile(snapshot);
}

SceneGraphError::SceneGraphError(std::string error_code, std::string message)
    : std::runtime_error(std::move(message)), error_code_(std::move(error_code)) {
    if (error_code_.empty()) {
        throw std::invalid_argument("scene graph error code is required");
    }
}

const std::string& SceneGraphError::error_code() const noexcept { return error_code_; }

class SceneGraphRuntime::Impl final {
  public:
    struct PreparedScene final {
        ScenePreparationReceipt receipt{};
        std::string request_id{};
        std::uint64_t document_revision{0U};
        std::shared_ptr<const CompiledSceneGraph> graph{};
        std::shared_ptr<SceneRenderResources> render_resources{};
        std::shared_ptr<PreparedSceneRenderGraph> render_graph{};
        std::shared_ptr<PreparedSceneRenderGraph> transition_output{};
    };

    class DeferredReleaseQueue final {
      public:
        DeferredReleaseQueue() : worker_([this] { run(); }) {}
        ~DeferredReleaseQueue() { close(); }

        DeferredReleaseQueue(const DeferredReleaseQueue&) = delete;
        DeferredReleaseQueue& operator=(const DeferredReleaseQueue&) = delete;

        void defer(PreparedScene scene) {
            {
                std::scoped_lock lock{mutex_};
                if (closed_) {
                    throw std::logic_error("scene release queue is closed");
                }
                entries_.push_back({
                    .release_at = std::chrono::steady_clock::now() +
                                  kRetiredGraphReleaseDelay,
                    .scene = std::move(scene),
                });
                if (entries_.size() > kMaximumRetiredGraphs) {
                    entries_.front().release_at =
                        std::chrono::steady_clock::time_point::min();
                }
            }
            wakeup_.notify_one();
        }

        [[nodiscard]] std::optional<PreparedScene>
        reclaim(const OutputBus bus, const std::string_view scene_id,
                const std::uint64_t document_revision) {
            std::optional<PreparedScene> reclaimed;
            {
                std::scoped_lock lock{mutex_};
                const auto candidate = std::ranges::find_if(
                    entries_, [bus, scene_id, document_revision](const Entry& entry) {
                        return entry.scene.receipt.bus == bus &&
                               entry.scene.receipt.scene_id == scene_id &&
                               entry.scene.document_revision == document_revision;
                    });
                if (candidate == entries_.end()) {
                    return {};
                }
                reclaimed.emplace(std::move(candidate->scene));
                entries_.erase(candidate);
            }
            wakeup_.notify_one();
            return reclaimed;
        }

        void close() noexcept {
            {
                std::scoped_lock lock{mutex_};
                if (closed_) {
                    return;
                }
                closed_ = true;
            }
            wakeup_.notify_all();
            if (worker_.joinable()) {
                worker_.join();
            }
        }

      private:
        struct Entry final {
            std::chrono::steady_clock::time_point release_at{};
            PreparedScene scene{};
        };

        void run() noexcept {
            while (true) {
                std::optional<Entry> released;
                {
                    std::unique_lock lock{mutex_};
                    if (entries_.empty() && !closed_) {
                        wakeup_.wait(lock,
                                     [this] { return closed_ || !entries_.empty(); });
                    }
                    if (entries_.empty() && closed_) {
                        return;
                    }
                    if (!closed_) {
                        const auto deadline = entries_.front().release_at;
                        if (std::chrono::steady_clock::now() < deadline) {
                            wakeup_.wait_until(lock, deadline);
                            continue;
                        }
                    }
                    released.emplace(std::move(entries_.front()));
                    entries_.pop_front();
                }
                // Prepared render graphs must be destroyed before their source
                // leases. PreparedScene's member order guarantees that here,
                // outside both the scene control lock and this queue lock.
                released.reset();
            }
        }

        std::mutex mutex_{};
        std::condition_variable wakeup_{};
        std::deque<Entry> entries_{};
        bool closed_{false};
        std::thread worker_{};
    };

    Impl(std::shared_ptr<SourceRuntimeFactory> source_factory,
         std::shared_ptr<SceneRenderer> renderer_value)
        : registry(std::move(source_factory),
                   SourceRegistryLimits{
                       .maximum_retained_idle_cameras = 4U,
                   }),
          renderer(std::move(renderer_value)) {}

    void defer_release(std::optional<PreparedScene>& scene) {
        if (!scene.has_value()) {
            return;
        }
        if (scene->transition_output != nullptr) {
            scene->transition_output->stop();
        }
        deferred_releases.defer(std::move(scene.value()));
        scene.reset();
    }

    void defer_all_pending() {
        for (auto& [_, scene] : pending_by_token) {
            if (scene.transition_output != nullptr) {
                scene.transition_output->stop();
            }
            deferred_releases.defer(std::move(scene));
        }
        pending_by_token.clear();
        token_by_request.clear();
    }

    [[nodiscard]] std::string next_token() {
        if (next_preparation_token == 0U) {
            throw SceneGraphError{"preparation_token_exhausted",
                                  "Scene preparation identities are exhausted"};
        }
        std::ostringstream value;
        value << "preparation-" << next_preparation_token++;
        return value.str();
    }

    [[nodiscard]] PreparedScene prepare_graph(
        const OutputBus bus, const std::string_view scene_id,
        const std::uint64_t expected_document_revision, const std::string_view request_id,
        const std::shared_ptr<const CompiledSceneDocument>& compiled_document,
        const SceneOutputDefinition& output,
        const SceneTransitionSpec& requested_transition = {},
        SourceRegistryUpdate* source_update = nullptr) {
        if (compiled_document == nullptr ||
            expected_document_revision != compiled_document->document_revision) {
            throw SceneGraphError{"stale_document_revision",
                                  "The scene document revision is stale"};
        }
        const auto graph = compiled_document->scenes.find(scene_id);
        if (graph == compiled_document->scenes.end()) {
            throw SceneGraphError{"scene_not_found", "The configured scene does not exist"};
        }
        auto token = next_token();
        if (source_update == nullptr) {
            if (auto retained = deferred_releases.reclaim(
                    bus, scene_id, expected_document_revision);
                retained.has_value()) {
                retained->receipt.preparation_token = token;
                retained->request_id = std::string{request_id};
                retained->graph = graph->second;
                retained->receipt.effective_transition = {};
                retained->receipt.fallback_applied = false;
                retained->receipt.fallback_reason.clear();
                retained->transition_output.reset();
                if (requested_transition.kind != SceneTransitionKind::cut) {
                    if (renderer == nullptr) {
                        retained->receipt.fallback_applied = true;
                        retained->receipt.fallback_reason =
                            "transition_renderer_unavailable";
                    } else {
                        try {
                            auto transition = renderer->prepare_transition(
                                bus, retained->render_graph, requested_transition);
                            retained->receipt.effective_transition =
                                transition.effective_transition;
                            retained->receipt.fallback_applied =
                                transition.fallback_applied;
                            retained->receipt.fallback_reason =
                                std::move(transition.fallback_reason);
                            retained->transition_output =
                                std::move(transition.render_output);
                        } catch (const SceneRendererError& error) {
                            retained->receipt.fallback_applied = true;
                            retained->receipt.fallback_reason = error.error_code();
                        } catch (...) {
                            retained->receipt.fallback_applied = true;
                            retained->receipt.fallback_reason =
                                "transition_preparation_failed";
                        }
                    }
                }
                return std::move(retained.value());
            }
        }
        PreparedScene prepared{
            .receipt = {.bus = bus,
                        .scene_id = std::string{scene_id},
                        .preparation_token = token},
            .request_id = std::string{request_id},
            .document_revision = expected_document_revision,
            .graph = graph->second,
            .render_resources = std::make_shared<SceneRenderResources>(),
        };
        prepared.render_resources->source_leases.reserve(
            graph->second->active_leaf_source_ids.size());
        try {
            for (const auto& source_id : graph->second->active_leaf_source_ids) {
                prepared.render_resources->source_leases.push_back(
                    source_update == nullptr
                        ? registry.acquire(source_id, token)
                        : source_update->acquire(source_id, token));
            }
        } catch (const SceneGraphError&) {
            throw;
        } catch (const std::exception& error) {
            map_source_failure(error);
        }
        if (renderer != nullptr) {
            SceneRenderPreparation render_preparation{
                .bus = bus,
                .document_revision = expected_document_revision,
                .output = output,
                .graph = prepared.graph,
                .resources = prepared.render_resources,
            };
            render_preparation.sources.reserve(
                prepared.render_resources->source_leases.size());
            for (const auto& lease : prepared.render_resources->source_leases) {
                render_preparation.sources.push_back({
                    .source_id = lease.source().id,
                    .generation = lease.generation(),
                    .runtime = &lease.runtime(),
                });
            }
            try {
                prepared.render_graph = renderer->prepare(render_preparation);
            } catch (const SceneRendererError& error) {
                throw SceneGraphError{error.error_code(), error.what()};
            } catch (const std::exception&) {
                throw SceneGraphError{"renderer_preparation_failed",
                                      "The scene renderer could not prepare the graph"};
            }
            if (prepared.render_graph == nullptr) {
                throw SceneGraphError{"renderer_preparation_failed",
                                      "The scene renderer returned an invalid graph"};
            }
        }
        prepared.receipt.effective_transition = {};
        if (requested_transition.kind != SceneTransitionKind::cut) {
            if (renderer == nullptr) {
                prepared.receipt.fallback_applied = true;
                prepared.receipt.fallback_reason = "transition_renderer_unavailable";
            } else {
                try {
                    auto transition = renderer->prepare_transition(
                        bus, prepared.render_graph, requested_transition);
                    prepared.receipt.effective_transition =
                        transition.effective_transition;
                    prepared.receipt.fallback_applied = transition.fallback_applied;
                    prepared.receipt.fallback_reason =
                        std::move(transition.fallback_reason);
                    prepared.transition_output = std::move(transition.render_output);
                } catch (const SceneRendererError& error) {
                    prepared.receipt.fallback_applied = true;
                    prepared.receipt.fallback_reason = error.error_code();
                } catch (...) {
                    prepared.receipt.fallback_applied = true;
                    prepared.receipt.fallback_reason = "transition_preparation_failed";
                }
            }
        }
        return prepared;
    }

    void erase_pending_for_bus(const OutputBus bus) {
        for (auto iterator = pending_by_token.begin(); iterator != pending_by_token.end();) {
            if (iterator->second.receipt.bus != bus) {
                ++iterator;
                continue;
            }
            token_by_request.erase(iterator->second.request_id);
            if (iterator->second.transition_output != nullptr) {
                iterator->second.transition_output->stop();
            }
            deferred_releases.defer(std::move(iterator->second));
            iterator = pending_by_token.erase(iterator);
        }
    }

    mutable std::mutex mutex{};
    bool closed{false};
    SourceRegistry registry;
    std::shared_ptr<SceneRenderer> renderer{};
    DeferredReleaseQueue deferred_releases{};
    std::shared_ptr<const CompiledSceneDocument> document{};
    std::shared_ptr<const SceneHydrationSnapshot> applied_snapshot{};
    std::array<std::optional<PreparedScene>, kOutputBusCount> active{};
    std::array<bool, kOutputBusCount> outputs_enabled{};
    std::map<std::string, PreparedScene, std::less<>> pending_by_token{};
    std::map<std::string, std::string, std::less<>> token_by_request{};
    std::uint64_t last_mutation_sequence{0U};
    std::uint64_t next_preparation_token{1U};
};

SceneGraphRuntime::SceneGraphRuntime(std::shared_ptr<SourceRuntimeFactory> source_factory,
                                     std::shared_ptr<SceneRenderer> renderer)
    : impl_(std::make_unique<Impl>(std::move(source_factory), std::move(renderer))) {}

SceneGraphRuntime::~SceneGraphRuntime() { shutdown(); }

void SceneGraphRuntime::hydrate(const SceneHydrationSnapshot& snapshot,
                                const std::uint64_t sequence) {
    auto next_document = compile_scene_document(snapshot);
    auto next_snapshot = std::make_shared<SceneHydrationSnapshot>(snapshot);
    std::scoped_lock lock{impl_->mutex};
    if (impl_->closed) {
        throw SceneGraphError{"media_graph_stopped", "The scene graph is stopped"};
    }
    if (impl_->document != nullptr && sequence <= impl_->last_mutation_sequence) {
        throw SceneGraphError{"stale_command_sequence",
                              "The scene command sequence is stale"};
    }
    const bool same_document =
        impl_->applied_snapshot != nullptr &&
        snapshot.document_id == impl_->applied_snapshot->document_id;
    if (same_document &&
        snapshot.document_revision < impl_->applied_snapshot->document_revision) {
        throw SceneGraphError{"stale_document_revision",
                              "The scene document revision is stale"};
    }
    if (same_document &&
        snapshot.document_revision == impl_->applied_snapshot->document_revision &&
        !same_document_definition(snapshot, *impl_->applied_snapshot)) {
        throw SceneGraphError{"conflicting_document_revision",
                              "The scene document revision conflicts with the applied graph"};
    }
    std::optional<SourceRegistryUpdate> source_update;
    try {
        source_update.emplace(impl_->registry.stage_snapshot(snapshot));
    } catch (const std::exception& error) {
        map_source_failure(error);
    }

    std::array<std::optional<Impl::PreparedScene>, kOutputBusCount> next_active{};
    for (std::size_t index = 0U; index < kOutputBusCount; ++index) {
        const auto bus = static_cast<OutputBus>(index);
        next_active[index].emplace(impl_->prepare_graph(
            bus, snapshot.active_scene_ids[index], snapshot.document_revision,
            index == 0U ? "hydrate-media-windows" : "hydrate-virtual-camera", next_document,
            snapshot.outputs[index], {}, &source_update.value()));
    }
    try {
        source_update->commit();
    } catch (const std::exception& error) {
        map_source_failure(error);
    }
    if (impl_->renderer != nullptr) {
        std::array<std::shared_ptr<PreparedSceneRenderGraph>, kOutputBusCount>
            render_graphs{};
        for (std::size_t index = 0U; index < kOutputBusCount; ++index) {
            render_graphs[index] = next_active[index]->render_graph;
        }
        impl_->renderer->commit_hydration(render_graphs, snapshot.render_enabled, sequence);
    }
    impl_->defer_all_pending();
    for (auto& active : impl_->active) {
        impl_->defer_release(active);
    }
    impl_->active = std::move(next_active);
    impl_->outputs_enabled = snapshot.render_enabled;
    impl_->document = std::move(next_document);
    impl_->applied_snapshot = std::move(next_snapshot);
    impl_->last_mutation_sequence = sequence;
}

ScenePreparationReceipt SceneGraphRuntime::prepare(
    const OutputBus bus, const std::string_view scene_id,
    const std::uint64_t document_revision, const std::string_view request_id,
    const std::uint64_t sequence, const SceneTransitionSpec& transition) {
    try {
        validate_scene_transition(transition);
    } catch (const std::invalid_argument& error) {
        throw SceneGraphError{"invalid_transition", error.what()};
    }
    std::scoped_lock lock{impl_->mutex};
    if (impl_->closed) {
        throw SceneGraphError{"media_graph_stopped", "The scene graph is stopped"};
    }
    if (sequence <= impl_->last_mutation_sequence) {
        throw SceneGraphError{"stale_command_sequence",
                              "The scene command sequence is stale"};
    }
    if (impl_->token_by_request.contains(request_id)) {
        throw SceneGraphError{"duplicate_preparation_request",
                              "The scene preparation request already exists"};
    }
    const auto index = bus_index(bus);
    const auto effective_request = bus == OutputBus::media_windows
                                       ? SceneTransitionSpec{}
                                       : transition;
    auto prepared = impl_->prepare_graph(
        bus, scene_id, document_revision, request_id, impl_->document,
        impl_->applied_snapshot->outputs[index], effective_request);
    const auto receipt = prepared.receipt;
    impl_->erase_pending_for_bus(bus);
    impl_->token_by_request.emplace(prepared.request_id,
                                    prepared.receipt.preparation_token);
    impl_->pending_by_token.emplace(prepared.receipt.preparation_token,
                                    std::move(prepared));
    impl_->last_mutation_sequence = sequence;
    return receipt;
}

void SceneGraphRuntime::take(const ScenePreparationReceipt& preparation,
                             const std::uint64_t document_revision,
                             const std::uint64_t sequence) {
    take(preparation.bus, preparation.scene_id, preparation.preparation_token,
         document_revision, sequence);
}

void SceneGraphRuntime::take(const OutputBus bus, const std::string_view scene_id,
                             const std::string_view preparation_token,
                             const std::uint64_t document_revision,
                             const std::uint64_t sequence) {
    std::scoped_lock lock{impl_->mutex};
    if (impl_->closed) {
        throw SceneGraphError{"media_graph_stopped", "The scene graph is stopped"};
    }
    if (impl_->document == nullptr ||
        document_revision != impl_->document->document_revision) {
        throw SceneGraphError{"stale_document_revision",
                              "The scene document revision is stale"};
    }
    const auto pending = impl_->pending_by_token.find(preparation_token);
    if (pending == impl_->pending_by_token.end()) {
        throw SceneGraphError{"preparation_not_found",
                              "The scene preparation does not exist"};
    }
    if (pending->second.receipt.bus != bus || pending->second.receipt.scene_id != scene_id ||
        pending->second.document_revision != document_revision) {
        throw SceneGraphError{"preparation_mismatch",
                              "The scene preparation does not match the Take request"};
    }
    const auto index = bus_index(bus);
    if (sequence <= impl_->last_mutation_sequence) {
        throw SceneGraphError{"stale_command_sequence",
                              "The scene command sequence is stale"};
    }
    auto committed = std::move(pending->second);
    impl_->pending_by_token.erase(pending);
    impl_->token_by_request.erase(committed.request_id);
    impl_->defer_release(impl_->active[index]);
    if (impl_->renderer != nullptr) {
        impl_->renderer->commit_take(bus, committed.render_graph,
                                     committed.transition_output, sequence);
    }
    impl_->active[index] = std::move(committed);
    impl_->last_mutation_sequence = sequence;
}

void SceneGraphRuntime::cancel(const std::string_view request_id) noexcept {
    if (impl_ == nullptr) {
        return;
    }
    try {
        std::scoped_lock lock{impl_->mutex};
        const auto token = impl_->token_by_request.find(request_id);
        if (token == impl_->token_by_request.end()) {
            return;
        }
        const auto pending = impl_->pending_by_token.find(token->second);
        if (pending != impl_->pending_by_token.end()) {
            if (pending->second.transition_output != nullptr) {
                pending->second.transition_output->stop();
            }
            impl_->deferred_releases.defer(std::move(pending->second));
            impl_->pending_by_token.erase(pending);
        }
        impl_->token_by_request.erase(token);
    } catch (...) {
    }
}

void SceneGraphRuntime::set_output_enabled(const OutputBus bus, const bool enabled,
                                           const std::uint64_t document_revision,
                                           const std::uint64_t sequence) {
    std::scoped_lock lock{impl_->mutex};
    if (impl_->closed) {
        throw SceneGraphError{"media_graph_stopped", "The scene graph is stopped"};
    }
    if (impl_->document == nullptr ||
        document_revision != impl_->document->document_revision) {
        throw SceneGraphError{"stale_document_revision",
                              "The scene document revision is stale"};
    }
    const auto index = bus_index(bus);
    if (sequence <= impl_->last_mutation_sequence) {
        throw SceneGraphError{"stale_command_sequence",
                              "The scene command sequence is stale"};
    }
    if (impl_->renderer != nullptr) {
        impl_->renderer->set_output_enabled(bus, enabled, sequence);
    }
    impl_->outputs_enabled[index] = enabled;
    impl_->last_mutation_sequence = sequence;
}

void SceneGraphRuntime::preview_layer_geometry(
    const OutputBus bus, const std::string_view scene_id,
    const SceneLayerDefinition& layer, const std::uint64_t document_revision,
    const std::uint64_t sequence) {
    std::scoped_lock lock{impl_->mutex};
    if (impl_->closed) {
        throw SceneGraphError{"media_graph_stopped", "The scene graph is stopped"};
    }
    if (impl_->document == nullptr || impl_->applied_snapshot == nullptr ||
        document_revision != impl_->document->document_revision) {
        throw SceneGraphError{"stale_document_revision",
                              "The scene document revision is stale"};
    }
    if (sequence <= impl_->last_mutation_sequence) {
        throw SceneGraphError{"stale_command_sequence",
                              "The scene command sequence is stale"};
    }
    const auto scene = std::ranges::find_if(
        impl_->applied_snapshot->scenes,
        [scene_id](const SceneGraphDefinition& candidate) {
            return candidate.id == scene_id;
        });
    if (scene == impl_->applied_snapshot->scenes.end()) {
        throw SceneGraphError{"scene_not_found", "The edited scene does not exist"};
    }
    const auto authored_layer = std::ranges::find_if(
        scene->layers, [&layer](const SceneLayerDefinition& candidate) {
            return candidate.id == layer.id;
        });
    if (authored_layer == scene->layers.end() ||
        authored_layer->source_id != layer.source_id) {
        throw SceneGraphError{"layer_not_found", "The edited layer does not exist"};
    }
    const auto& active = impl_->active[bus_index(bus)];
    if (active.has_value() && active->render_graph != nullptr) {
        static_cast<void>(active->render_graph->preview_layer_geometry(
            scene_id, layer.id, layer.geometry));
    }
    impl_->last_mutation_sequence = sequence;
}

void SceneGraphRuntime::shutdown() noexcept {
    if (impl_ == nullptr) {
        return;
    }
    try {
        std::shared_ptr<SceneRenderer> renderer;
        {
            std::scoped_lock lock{impl_->mutex};
            if (impl_->closed) {
                return;
            }
            impl_->closed = true;
            impl_->defer_all_pending();
            renderer = impl_->renderer;
        }
        // Active leases stay alive until the renderer has stopped every graph
        // which may still be consuming their non-owning runtime bindings.
        if (renderer != nullptr) {
            renderer->shutdown();
        }
        {
            std::scoped_lock lock{impl_->mutex};
            for (auto& active : impl_->active) {
                impl_->defer_release(active);
            }
            impl_->document.reset();
            impl_->applied_snapshot.reset();
            impl_->renderer.reset();
        }
        impl_->deferred_releases.close();
        impl_->registry.shutdown();
    } catch (...) {
    }
}

std::uint64_t SceneGraphRuntime::document_revision() const noexcept {
    if (impl_ == nullptr) {
        return 0U;
    }
    std::scoped_lock lock{impl_->mutex};
    return impl_->document == nullptr ? 0U : impl_->document->document_revision;
}

std::string SceneGraphRuntime::active_scene(const OutputBus bus) const {
    std::scoped_lock lock{impl_->mutex};
    const auto& active = impl_->active[bus_index(bus)];
    return active.has_value() ? active->receipt.scene_id : std::string{};
}

bool SceneGraphRuntime::output_enabled(const OutputBus bus) const {
    std::scoped_lock lock{impl_->mutex};
    return impl_->outputs_enabled[bus_index(bus)];
}

std::size_t SceneGraphRuntime::pending_preparation_count() const noexcept {
    if (impl_ == nullptr) {
        return 0U;
    }
    std::scoped_lock lock{impl_->mutex};
    return impl_->pending_by_token.size();
}

std::shared_ptr<const CompiledSceneDocument> SceneGraphRuntime::compiled_document() const {
    std::scoped_lock lock{impl_->mutex};
    return impl_->document;
}

} // namespace solin::media_engine
