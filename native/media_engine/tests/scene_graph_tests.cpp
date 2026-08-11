#include "solin/media_engine/scene_graph.hpp"

#include <atomic>
#include <cstdint>
#include <exception>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (condition) {
        return;
    }
    ++failures;
    std::cerr << "FAILED: " << description << '\n';
}

template <typename Callback>
void expect_error(Callback&& callback, const std::string_view error_code,
                  const char* description) {
    try {
        std::forward<Callback>(callback)();
        expect(false, description);
    } catch (const solin::media_engine::SceneGraphError& error) {
        expect(error.error_code() == error_code, description);
    } catch (const std::exception&) {
        expect(false, description);
    }
}

struct RuntimeCounters final {
    std::atomic_uint64_t created{0U};
    std::atomic_uint64_t started{0U};
    std::atomic_uint64_t stopped{0U};
};

class FakeRuntime final : public solin::media_engine::SourceRuntime {
  public:
    explicit FakeRuntime(std::shared_ptr<RuntimeCounters> counters)
        : counters_(std::move(counters)) {}

    void start() override {
        ++counters_->started;
        status_.store(solin::media_engine::SourceRuntimeStatus::ready);
    }

    void stop() noexcept override {
        if (!stopped_.exchange(true)) {
            ++counters_->stopped;
            status_.store(solin::media_engine::SourceRuntimeStatus::stopped);
        }
    }

    [[nodiscard]] solin::media_engine::SourceRuntimeHealth health() const override {
        return {.status = status_.load()};
    }

  private:
    std::shared_ptr<RuntimeCounters> counters_{};
    std::atomic<solin::media_engine::SourceRuntimeStatus> status_{
        solin::media_engine::SourceRuntimeStatus::starting};
    std::atomic_bool stopped_{false};
};

class FakeRuntimeFactory final : public solin::media_engine::SourceRuntimeFactory {
  public:
    explicit FakeRuntimeFactory(std::shared_ptr<RuntimeCounters> counters)
        : counters_(std::move(counters)) {}

    [[nodiscard]] std::shared_ptr<solin::media_engine::SourceRuntime>
    create(const solin::media_engine::SceneSource& source, std::uint64_t) override {
        ++counters_->created;
        if (source.id == failing_source_id_) {
            throw std::runtime_error("synthetic source failure");
        }
        if (source.kind != solin::media_engine::SceneSourceKind::color &&
            source.kind != solin::media_engine::SceneSourceKind::local_camera &&
            source.kind != solin::media_engine::SceneSourceKind::rtsp_camera) {
            throw std::runtime_error("source_runtime_not_implemented");
        }
        return std::make_shared<FakeRuntime>(counters_);
    }

    void fail_source(std::string source_id) { failing_source_id_ = std::move(source_id); }

  private:
    std::shared_ptr<RuntimeCounters> counters_{};
    std::string failing_source_id_{};
};

class FakePreparedRenderGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    FakePreparedRenderGraph(std::string scene_id_value,
                            const solin::media_engine::OutputBus bus_value)
        : scene_id(std::move(scene_id_value)), bus(bus_value) {}

    [[nodiscard]] bool preview_layer_geometry(
        const std::string_view edited_scene_id,
        const std::string_view edited_layer_id,
        const solin::media_engine::SceneLayerGeometry& geometry) noexcept override {
        if (edited_scene_id != scene_id) {
            return false;
        }
        preview_layer_id = edited_layer_id;
        preview_geometry = geometry;
        ++preview_count;
        return true;
    }

    std::string scene_id{};
    solin::media_engine::OutputBus bus{solin::media_engine::OutputBus::media_windows};
    std::string preview_layer_id{};
    solin::media_engine::SceneLayerGeometry preview_geometry{};
    std::uint64_t preview_count{0U};
};

class FakeRenderer final : public solin::media_engine::SceneRenderer {
  public:
    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    prepare(const solin::media_engine::SceneRenderPreparation& preparation) override {
        if (preparation.graph == nullptr || preparation.output.video_format.width == 0U ||
            preparation.output.video_format.height == 0U) {
            throw std::runtime_error("invalid renderer preparation");
        }
        if (preparation.graph->scene_id == failing_scene_id_) {
            throw solin::media_engine::SceneRendererError{
                "renderer_resource_exhausted", "Synthetic renderer failure"};
        }
        for (const auto& source : preparation.sources) {
            if (source.runtime == nullptr || source.source_id.empty() ||
                source.generation == 0U) {
                throw std::runtime_error("invalid renderer source binding");
            }
        }
        ++prepare_count;
        return std::make_shared<FakePreparedRenderGraph>(preparation.graph->scene_id,
                                                         preparation.bus);
    }

    void commit_hydration(
        const std::array<std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>,
                         2U>& graphs,
        const std::array<bool, 2U>& enabled,
        const std::uint64_t sequence) noexcept override {
        active = graphs;
        output_enabled = enabled;
        hydration_sequence = sequence;
        ++hydration_commit_count;
    }

    void commit_take(
        const solin::media_engine::OutputBus bus,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> graph,
        const std::uint64_t sequence) noexcept override {
        active[static_cast<std::size_t>(bus)] = std::move(graph);
        take_sequence = sequence;
        ++take_commit_count;
    }

    void set_output_enabled(const solin::media_engine::OutputBus bus,
                            const bool enabled,
                            const std::uint64_t sequence) noexcept override {
        output_enabled[static_cast<std::size_t>(bus)] = enabled;
        output_sequence = sequence;
        ++output_change_count;
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    void shutdown() noexcept override {
        active = {};
        ++shutdown_count;
    }

    void fail_scene(std::string scene_id) { failing_scene_id_ = std::move(scene_id); }

    [[nodiscard]] std::string active_scene(const solin::media_engine::OutputBus bus) const {
        const auto graph = std::dynamic_pointer_cast<FakePreparedRenderGraph>(
            active[static_cast<std::size_t>(bus)]);
        return graph == nullptr ? std::string{} : graph->scene_id;
    }

    std::uint64_t prepare_count{0U};
    std::uint64_t hydration_commit_count{0U};
    std::uint64_t take_commit_count{0U};
    std::uint64_t output_change_count{0U};
    std::uint64_t shutdown_count{0U};
    std::uint64_t hydration_sequence{0U};
    std::uint64_t take_sequence{0U};
    std::uint64_t output_sequence{0U};
    std::array<std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>, 2U>
        active{};
    std::array<bool, 2U> output_enabled{};

  private:
    std::string failing_scene_id_{};
};

[[nodiscard]] solin::media_engine::SceneLayerGeometry visible_geometry() {
    return {
        .x = 0.0,
        .y = 0.0,
        .width = 1.0,
        .height = 1.0,
        .opacity = 1.0,
        .fit_mode = "cover",
        .visible = true,
    };
}

[[nodiscard]] solin::media_engine::SceneLayerDefinition
layer(std::string id, std::string source_id, const bool visible = true) {
    auto geometry = visible_geometry();
    geometry.visible = visible;
    return {
        .id = std::move(id),
        .source_id = std::move(source_id),
        .geometry = std::move(geometry),
    };
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot snapshot(
    const std::uint64_t revision = 1U) {
    solin::media_engine::SceneHydrationSnapshot value{
        .document_id = "document-1",
        .document_revision = revision,
        .sources = {
            {
                .id = "color-1",
                .kind = solin::media_engine::SceneSourceKind::color,
                .enabled = true,
                .configuration =
                    solin::media_engine::ColorSourceConfiguration{"#000000FF"},
            },
            {
                .id = "color-2",
                .kind = solin::media_engine::SceneSourceKind::color,
                .enabled = true,
                .configuration =
                    solin::media_engine::ColorSourceConfiguration{"#FFFFFFFF"},
            },
            {
                .id = "reference-1",
                .kind = solin::media_engine::SceneSourceKind::scene_reference,
                .enabled = true,
                .configuration = solin::media_engine::SceneReferenceSourceConfiguration{
                    "scene-b"},
            },
        },
        .scenes = {
            {.id = "scene-a", .layers = {layer("layer-a", "reference-1")}},
            {.id = "scene-b", .layers = {layer("layer-b", "color-1")}},
            {.id = "scene-c", .layers = {layer("layer-c", "color-2")}},
        },
        .active_scene_ids = {"scene-a", "scene-a"},
        .render_enabled = {true, false},
        .output_enabled = {true, false},
    };
    value.outputs[0].bus = solin::media_engine::OutputBus::media_windows;
    value.outputs[0].default_scene_id = "scene-a";
    value.outputs[0].video_format = {
        .width = 1920U,
        .height = 1080U,
        .fps_numerator = 60U,
        .fps_denominator = 1U,
        .pixel_format = "bgra",
        .color_space = "bt709",
        .color_range = "full",
    };
    value.outputs[1].bus = solin::media_engine::OutputBus::virtual_camera;
    value.outputs[1].default_scene_id = "scene-a";
    value.outputs[1].video_format = value.outputs[0].video_format;
    return value;
}

void test_compiler_preserves_reference_groups_without_expanding_the_graph() {
    const auto compiled = solin::media_engine::compile_scene_document(snapshot());
    const auto scene = compiled->scenes.at("scene-a");
    expect(scene->layers.size() == 1U && scene->layers[0].referenced_scene != nullptr,
           "scene references compile into reusable nested graph nodes");
    expect(scene->layers[0].referenced_scene->scene_id == "scene-b",
           "the compiled reference targets the configured scene");
    expect(scene->active_leaf_source_ids == std::vector<std::string>{"color-1"},
           "reference compilation deduplicates active leaf sources");

    auto cyclic = snapshot();
    cyclic.scenes[1].layers = {layer("cycle", "reference-1")};
    expect_error(
        [&cyclic] {
            static_cast<void>(solin::media_engine::compile_scene_document(cyclic));
        },
        "scene_reference_cycle", "the compiler independently rejects reference cycles");
}

void test_hydrate_prepare_cancel_and_take_are_transactional() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SceneGraphRuntime runtime{factory};
    runtime.hydrate(snapshot(), 1U);

    expect(runtime.document_revision() == 1U &&
               runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
                   "scene-a" &&
               runtime.active_scene(solin::media_engine::OutputBus::virtual_camera) ==
                   "scene-a",
           "hydration atomically installs both active scene graphs");
    expect(counters->created == 1U && counters->started == 1U,
           "both buses share one source runtime during hydration");
    expect(runtime.output_enabled(solin::media_engine::OutputBus::media_windows) &&
               !runtime.output_enabled(solin::media_engine::OutputBus::virtual_camera),
           "hydration installs output enablement with the graphs");

    auto cancelled = runtime.prepare(solin::media_engine::OutputBus::media_windows,
                                     "scene-c", 1U, "prepare-cancelled", 2U);
    expect(runtime.pending_preparation_count() == 1U,
           "preparation retains its source leases until commit or cancellation");
    runtime.cancel("prepare-cancelled");
    expect(runtime.pending_preparation_count() == 0U,
           "cancellation removes the matching preparation");
    expect_error(
        [&runtime, &cancelled] { runtime.take(cancelled, "cut", 0U, 1U, 3U); },
        "preparation_not_found", "a cancelled token cannot be committed");

    const auto prepared = runtime.prepare(solin::media_engine::OutputBus::media_windows,
                                          "scene-c", 1U, "prepare-committed", 4U);
    runtime.take(prepared, "cut", 0U, 1U, 5U);
    expect(runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
               "scene-c" &&
               runtime.active_scene(solin::media_engine::OutputBus::virtual_camera) ==
                   "scene-a",
           "Take changes exactly one output bus atomically");
    expect(runtime.pending_preparation_count() == 0U,
           "Take consumes its preparation token");
    expect_error(
        [&runtime, &prepared] { runtime.take(prepared, "cut", 0U, 1U, 6U); },
        "preparation_not_found", "a consumed preparation cannot be replayed");

    const auto superseded = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-a", 1U,
        "prepare-superseded", 7U);
    const auto newest = runtime.prepare(solin::media_engine::OutputBus::media_windows,
                                        "scene-b", 1U, "prepare-newest", 8U);
    expect_error(
        [&runtime, &superseded] { runtime.take(superseded, "cut", 0U, 1U, 9U); },
        "preparation_not_found", "a newer preparation invalidates an older bus token");
    runtime.take(newest, "cut", 0U, 1U, 10U);

    runtime.set_output_enabled(solin::media_engine::OutputBus::virtual_camera, true,
                               1U, 11U);
    expect(runtime.output_enabled(solin::media_engine::OutputBus::virtual_camera),
           "output state changes use the applied document revision");
    expect_error(
        [&runtime] {
            runtime.set_output_enabled(solin::media_engine::OutputBus::virtual_camera,
                                       false, 1U, 11U);
        },
        "stale_command_sequence", "stale output commands are rejected");
}

void test_failed_or_stale_prepare_preserves_the_previous_preparation() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SceneGraphRuntime runtime{factory};
    auto initial = snapshot();
    initial.active_scene_ids = {"scene-b", "scene-b"};
    runtime.hydrate(initial, 1U);

    const auto prepared = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-a", 1U,
        "prepare-preserved", 2U);
    factory->fail_source("color-2");
    expect_error(
        [&runtime] {
            static_cast<void>(runtime.prepare(
                solin::media_engine::OutputBus::media_windows, "scene-c", 1U,
                "prepare-failing", 3U));
        },
        "source_unavailable", "a failing replacement reports its source error");
    expect(runtime.pending_preparation_count() == 1U,
           "a failing replacement preserves the previously prepared scene");

    expect_error(
        [&runtime] {
            static_cast<void>(runtime.prepare(
                solin::media_engine::OutputBus::media_windows, "scene-b", 1U,
                "prepare-stale", 1U));
        },
        "stale_command_sequence", "an older preparation cannot replace a newer one");
    runtime.take(prepared, "cut", 0U, 1U, 4U);
    expect(runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
               "scene-a",
           "the preserved preparation remains consumable after rejected replacements");

    auto newer_snapshot = snapshot(2U);
    expect_error([&runtime, &newer_snapshot] { runtime.hydrate(newer_snapshot, 3U); },
                 "stale_command_sequence",
                 "hydration cannot roll state back behind a newer Take sequence");

    auto conflicting_snapshot = snapshot();
    conflicting_snapshot.scenes[2].layers = {
        layer("conflicting-layer", "color-1")};
    expect_error(
        [&runtime, &conflicting_snapshot] {
            runtime.hydrate(conflicting_snapshot, 5U);
        },
        "conflicting_document_revision",
        "the same revision cannot install a different scene document");
    expect(runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
               "scene-a",
           "a conflicting revision preserves the applied graph");
}

void test_failed_hydration_preserves_the_previous_applied_graph() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SceneGraphRuntime runtime{factory};
    auto initial = snapshot();
    initial.active_scene_ids = {"scene-b", "scene-b"};
    runtime.hydrate(initial, 1U);

    factory->fail_source("color-2");
    auto rejected = snapshot(2U);
    rejected.active_scene_ids = {"scene-c", "scene-c"};
    expect_error([&runtime, &rejected] { runtime.hydrate(rejected, 2U); },
                 "source_unavailable",
                 "hydration reports a sanitized source preparation failure");
    expect(runtime.document_revision() == 1U &&
               runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
                   "scene-b" &&
               runtime.active_scene(solin::media_engine::OutputBus::virtual_camera) ==
                   "scene-b",
           "failed hydration leaves the prior document and both active graphs applied");

    auto unsupported = snapshot(3U);
    unsupported.sources.push_back({
        .id = "image-1",
        .kind = solin::media_engine::SceneSourceKind::image,
        .enabled = true,
        .configuration = solin::media_engine::ImageSourceConfiguration{"asset-1"},
    });
    unsupported.scenes.push_back(
        {.id = "scene-image", .layers = {layer("image-layer", "image-1")}});
    unsupported.active_scene_ids = {"scene-image", "scene-image"};
    expect_error([&runtime, &unsupported] { runtime.hydrate(unsupported, 3U); },
                 "source_not_implemented",
                 "unsupported graph leaves fail with an explicit capability error");
    expect(runtime.document_revision() == 1U,
           "an unsupported source cannot partially advance the applied document");
}

void test_preview_geometry_updates_the_active_renderer_without_rebuilding_it() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);
    const auto prepared_before = renderer->prepare_count;
    auto edited = layer("layer-a", "reference-1");
    edited.geometry.x = 0.2;
    edited.geometry.y = 0.1;
    edited.geometry.width = 0.5;

    runtime.preview_layer_geometry(
        solin::media_engine::OutputBus::media_windows, "scene-a", edited, 1U, 2U);

    const auto active = std::dynamic_pointer_cast<FakePreparedRenderGraph>(
        renderer->active[static_cast<std::size_t>(
            solin::media_engine::OutputBus::media_windows)]);
    expect(active != nullptr && active->preview_count == 1U &&
               active->preview_layer_id == "layer-a" &&
               active->preview_geometry.x == 0.2 &&
               active->preview_geometry.width == 0.5,
           "preview geometry reaches the active compositor graph");
    expect(renderer->prepare_count == prepared_before && runtime.document_revision() == 1U,
           "preview geometry neither rebuilds nor mutates the durable document");
    expect_error(
        [&runtime, &edited] {
            runtime.preview_layer_geometry(
                solin::media_engine::OutputBus::media_windows, "scene-a", edited,
                2U, 3U);
        },
        "stale_document_revision",
        "preview geometry is fenced by the authoritative document revision");
}

void test_new_document_identity_accepts_an_independent_revision_sequence() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SceneGraphRuntime runtime{factory};
    auto initial = snapshot(9U);
    runtime.hydrate(initial, 1U);

    auto next = snapshot(0U);
    next.document_id = "document-2";
    next.active_scene_ids = {"scene-b", "scene-b"};
    runtime.hydrate(next, 2U);

    expect(runtime.document_revision() == 0U &&
               runtime.active_scene(solin::media_engine::OutputBus::virtual_camera) ==
                   "scene-b",
           "a different scene document may start at revision zero");
}

void test_invisible_reference_groups_do_not_start_unused_sources() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto value = snapshot();
    value.scenes[0].layers = {layer("hidden-reference", "reference-1", false)};
    solin::media_engine::SceneGraphRuntime runtime{factory};
    runtime.hydrate(value, 1U);
    expect(counters->created == 0U,
           "hidden reference groups do not activate their descendant source pipelines");
}

void test_renderer_prepare_and_commits_follow_scene_transactions() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    expect(renderer->prepare_count == 2U && renderer->hydration_commit_count == 1U &&
               renderer->hydration_sequence == 1U &&
               renderer->active_scene(
                   solin::media_engine::OutputBus::media_windows) == "scene-a" &&
               renderer->active_scene(
                   solin::media_engine::OutputBus::virtual_camera) == "scene-a",
           "hydration prepares and atomically commits both renderer graphs");

    const auto prepared = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-c", 1U,
        "prepare-renderer", 2U);
    expect(renderer->prepare_count == 3U && renderer->take_commit_count == 0U &&
               renderer->active_scene(
                   solin::media_engine::OutputBus::media_windows) == "scene-a",
           "prepare prerolls a renderer graph without changing active output");
    runtime.take(prepared, "cut", 0U, 1U, 3U);
    expect(renderer->take_commit_count == 1U && renderer->take_sequence == 3U &&
               renderer->active_scene(
                   solin::media_engine::OutputBus::media_windows) == "scene-c",
           "Take commits the already prepared renderer graph");

    runtime.set_output_enabled(solin::media_engine::OutputBus::virtual_camera, true,
                               1U, 4U);
    expect(renderer->output_change_count == 1U && renderer->output_sequence == 4U &&
               renderer->output_enabled[1],
           "output enablement is forwarded to the renderer transaction");
    runtime.shutdown();
    expect(renderer->shutdown_count == 1U,
           "scene graph shutdown stops the renderer exactly once");
}

void test_recent_scene_graph_is_reused_for_a_return_cut() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    const auto away = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-c", 1U,
        "prepare-away", 2U);
    runtime.take(away, "cut", 0U, 1U, 3U);
    expect(renderer->prepare_count == 3U,
           "the first cut prepares the destination graph once");

    const auto back = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-a", 1U,
        "prepare-back", 4U);
    expect(renderer->prepare_count == 3U,
           "a prompt return reuses the retained graph without another preroll");
    expect(back.preparation_token != away.preparation_token,
           "a reused graph receives a fresh transaction identity");
    runtime.take(back, "cut", 0U, 1U, 5U);
    expect(renderer->active_scene(
               solin::media_engine::OutputBus::media_windows) == "scene-a",
           "the retained graph remains committable as the active scene");
}

void test_renderer_failure_preserves_applied_and_pending_graphs() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);
    const auto preserved = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-b", 1U,
        "renderer-preserved", 2U);

    renderer->fail_scene("scene-c");
    expect_error(
        [&runtime] {
            static_cast<void>(runtime.prepare(
                solin::media_engine::OutputBus::media_windows, "scene-c", 1U,
                "renderer-failing", 3U));
        },
        "renderer_resource_exhausted",
        "renderer preparation errors retain their stable error code");
    expect(runtime.pending_preparation_count() == 1U &&
               renderer->active_scene(
                   solin::media_engine::OutputBus::media_windows) == "scene-a",
           "a renderer failure preserves active and previously pending graphs");
    runtime.take(preserved, "cut", 0U, 1U, 4U);
    expect(renderer->active_scene(
               solin::media_engine::OutputBus::media_windows) == "scene-b",
           "the preserved renderer graph remains committable");
}

} // namespace

int main() {
    test_compiler_preserves_reference_groups_without_expanding_the_graph();
    test_hydrate_prepare_cancel_and_take_are_transactional();
    test_preview_geometry_updates_the_active_renderer_without_rebuilding_it();
    test_failed_or_stale_prepare_preserves_the_previous_preparation();
    test_failed_hydration_preserves_the_previous_applied_graph();
    test_new_document_identity_accepts_an_independent_revision_sequence();
    test_invisible_reference_groups_do_not_start_unused_sources();
    test_renderer_prepare_and_commits_follow_scene_transactions();
    test_recent_scene_graph_is_reused_for_a_return_cut();
    test_renderer_failure_preserves_applied_and_pending_graphs();
    if (failures != 0) {
        std::cerr << failures << " scene graph test(s) failed\n";
        return 1;
    }
    return 0;
}
