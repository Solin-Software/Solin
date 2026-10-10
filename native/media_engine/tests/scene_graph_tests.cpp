#include "solin/media_engine/scene_graph.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <exception>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
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

    void publish_activation_frame(const std::uint64_t media_epoch) noexcept {
        media_epoch_.store(media_epoch);
        frame_available_.store(true);
        static_cast<void>(sequence_.fetch_add(1U));
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame() const override {
        if (!frame_available_.load()) {
            return {};
        }
        return std::make_shared<solin::media_engine::SourceFrame>(
            solin::media_engine::SourceFrame{
                .sequence = sequence_.load(),
                .media_epoch = media_epoch_.load(),
                .activation_ready = true,
            });
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    activation_frame(
        const std::optional<std::uint64_t> expected_media_epoch) const override {
        const auto frame = latest_frame();
        return frame != nullptr &&
                       (!expected_media_epoch.has_value() ||
                        frame->media_epoch == expected_media_epoch.value())
                   ? frame
                   : std::shared_ptr<const solin::media_engine::SourceFrame>{};
    }

    [[nodiscard]] bool wait_for_frame(
        const std::uint64_t after_sequence, std::stop_token,
        std::chrono::steady_clock::time_point) const noexcept override {
        return sequence_.load() > after_sequence;
    }

  private:
    std::shared_ptr<RuntimeCounters> counters_{};
    std::atomic<solin::media_engine::SourceRuntimeStatus> status_{
        solin::media_engine::SourceRuntimeStatus::starting};
    std::atomic_bool stopped_{false};
    std::atomic_bool frame_available_{false};
    std::atomic_uint64_t sequence_{0U};
    std::atomic_uint64_t media_epoch_{0U};
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
            source.kind != solin::media_engine::SceneSourceKind::solin_content &&
            source.kind != solin::media_engine::SceneSourceKind::local_camera &&
            source.kind != solin::media_engine::SceneSourceKind::rtsp_camera) {
            throw std::runtime_error("source_runtime_not_implemented");
        }
        auto runtime = std::make_shared<FakeRuntime>(counters_);
        if (source.kind == solin::media_engine::SceneSourceKind::solin_content) {
            content_runtime_ = runtime;
        }
        return runtime;
    }

    void fail_source(std::string source_id) { failing_source_id_ = std::move(source_id); }
    [[nodiscard]] std::shared_ptr<FakeRuntime> content_runtime() const noexcept {
        return content_runtime_;
    }

  private:
    std::shared_ptr<RuntimeCounters> counters_{};
    std::string failing_source_id_{};
    std::shared_ptr<FakeRuntime> content_runtime_{};
};

class FakePreparedRenderGraph final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    FakePreparedRenderGraph(std::string scene_id_value,
                            const solin::media_engine::OutputBus bus_value,
                            const bool output_ready_value)
        : scene_id(std::move(scene_id_value)), bus(bus_value),
          output_ready(output_ready_value) {}

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

    void set_direct_output_enabled(const bool enabled) noexcept override {
        direct_output_enabled = enabled;
        ++direct_output_change_count;
    }

    void set_rendering_enabled(const bool enabled) noexcept override {
        rendering_enabled = enabled;
    }

    [[nodiscard]] bool wait_for_frame(
        std::uint64_t, std::stop_token,
        std::chrono::steady_clock::time_point) const noexcept override {
        return output_ready;
    }

    [[nodiscard]] bool wait_for_gpu_frame(
        std::uint64_t, std::stop_token,
        std::chrono::steady_clock::time_point) const noexcept override {
        return output_ready;
    }

    std::string scene_id{};
    solin::media_engine::OutputBus bus{solin::media_engine::OutputBus::media_windows};
    std::string preview_layer_id{};
    solin::media_engine::SceneLayerGeometry preview_geometry{};
    std::uint64_t preview_count{0U};
    bool direct_output_enabled{true};
    bool rendering_enabled{true};
    bool output_ready{true};
    std::uint64_t direct_output_change_count{0U};
};

struct TransitionCounters final {
    std::uint64_t started{0U};
    std::uint64_t stopped{0U};
    std::uint64_t active{0U};
    std::uint64_t maximum_active{0U};
    bool origin_rendering_enabled_at_start{false};
    bool overlapped_handoff{false};
};

class FakePreparedTransition final
    : public solin::media_engine::PreparedSceneRenderGraph {
  public:
    FakePreparedTransition(
        std::shared_ptr<TransitionCounters> counters,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> origin,
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> target)
        : counters_(std::move(counters)), origin_(std::move(origin)),
          target_(std::move(target)) {}

    [[nodiscard]] bool is_transition_output() const noexcept override { return true; }

    void start_transition() noexcept override {
        if (started_) {
            return;
        }
        started_ = true;
        counters_->overlapped_handoff =
            counters_->overlapped_handoff || counters_->active > 0U;
        ++counters_->started;
        ++counters_->active;
        counters_->maximum_active =
            (std::max)(counters_->maximum_active, counters_->active);
        if (const auto origin =
                std::dynamic_pointer_cast<FakePreparedRenderGraph>(origin_);
            origin != nullptr) {
            counters_->origin_rendering_enabled_at_start =
                origin->rendering_enabled;
        }
        if (origin_ != nullptr) {
            origin_->set_direct_output_enabled(false);
        }
        if (target_ != nullptr) {
            target_->set_direct_output_enabled(false);
        }
    }

    void stop() noexcept override {
        if (!started_ || stopped_) {
            return;
        }
        stopped_ = true;
        ++counters_->stopped;
        --counters_->active;
    }

    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    transition_origin() const noexcept override {
        return origin_;
    }

    [[nodiscard]] std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>
    transition_target() const noexcept override {
        return target_;
    }

    void set_direct_output_enabled(const bool enabled) noexcept override {
        if (target_ != nullptr) {
            target_->set_direct_output_enabled(enabled);
        }
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame() const noexcept override {
        return composed_frame_;
    }

    void publish_composed_frame() {
        composed_frame_ = std::make_shared<solin::media_engine::SourceFrame>();
    }

    void complete() {
        if (target_ != nullptr) {
            target_->set_direct_output_enabled(true);
        }
        origin_.reset();
    }

  private:
    std::shared_ptr<TransitionCounters> counters_{};
    std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> origin_{};
    std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> target_{};
    std::shared_ptr<const solin::media_engine::SourceFrame> composed_frame_{};
    bool started_{false};
    bool stopped_{false};
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
        return std::make_shared<FakePreparedRenderGraph>(
            preparation.graph->scene_id, preparation.bus,
            prepared_output_ready);
    }

    [[nodiscard]] solin::media_engine::SceneRenderTransitionPreparation
    prepare_transition(
        const solin::media_engine::OutputBus bus,
        const std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>& incoming,
        const solin::media_engine::SceneTransitionSpec& transition) override {
        requested_transition = transition;
        ++transition_prepare_count;
        static_cast<void>(incoming);
        if (transition.kind == solin::media_engine::SceneTransitionKind::cut) {
            return {.effective_transition = transition};
        }
        if (transitions_available) {
            const auto origin = active[static_cast<std::size_t>(bus)];
            if (origin != nullptr && origin->is_transition_output()) {
                if (origin->latest_gpu_frame() == nullptr) {
                    ++retarget_before_first_frame;
                } else {
                    ++retarget_after_composed_frame;
                }
            }
            return {
                .effective_transition = transition,
                .render_output = std::make_shared<FakePreparedTransition>(
                    transition_counters, origin, incoming),
            };
        }
        return {
            .effective_transition = {},
            .fallback_applied = true,
            .fallback_reason = "synthetic_transition_unavailable",
        };
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
        std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph> transition,
        const std::uint64_t sequence) noexcept override {
        if (transition == nullptr) {
            graph->set_direct_output_enabled(true);
            active[static_cast<std::size_t>(bus)] = std::move(graph);
        } else {
            transition->start_transition();
            active[static_cast<std::size_t>(bus)] = std::move(transition);
        }
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

    void set_system_memory_output_enabled(
        solin::media_engine::OutputBus,
        solin::media_engine::SystemMemoryOutputConsumer,
        bool) noexcept override {}

    [[nodiscard]] bool refresh_prepared(
        const solin::media_engine::OutputBus bus,
        const std::shared_ptr<solin::media_engine::PreparedSceneRenderGraph>& graph,
        const std::chrono::steady_clock::time_point deadline) noexcept override {
        ++refresh_count;
        return refresh_succeeds &&
               solin::media_engine::SceneRenderer::refresh_prepared(
                   bus, graph, deadline);
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    [[nodiscard]] std::shared_ptr<const solin::media_engine::SourceFrame>
    latest_gpu_frame(solin::media_engine::OutputBus) const noexcept override {
        return {};
    }

    void shutdown() noexcept override {
        active = {};
        ++shutdown_count;
    }

    bool prepared_output_ready{true};

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
    std::uint64_t transition_prepare_count{0U};
    std::uint64_t refresh_count{0U};
    bool refresh_succeeds{true};
    bool transitions_available{false};
    solin::media_engine::SceneTransitionSpec requested_transition{};
    std::shared_ptr<TransitionCounters> transition_counters{
        std::make_shared<TransitionCounters>()};
    std::uint64_t retarget_before_first_frame{0U};
    std::uint64_t retarget_after_composed_frame{0U};
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

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot content_snapshot(
    std::string handle_token) {
    auto value = snapshot();
    value.sources = {{
        .id = std::string{solin::media_engine::kSolinContentSourceId},
        .kind = solin::media_engine::SceneSourceKind::solin_content,
        .enabled = true,
        .frame_channel = solin::media_engine::FrameChannelConfiguration{
            .channel_id = "content-channel",
            .generation = 1U,
            .producer_kind = "solin_offscreen",
            .transport = "shared_memory_video",
            .handle_token = std::move(handle_token),
            .width = 1920U,
            .height = 1080U,
            .pixel_format = "nv12",
            .color_space = "bt709",
            .color_range = "limited",
        },
    }};
    value.scenes = {{
        .id = "content-scene",
        .layers = {layer("content-layer", std::string{
            solin::media_engine::kSolinContentSourceId})},
    }};
    value.active_scene_ids = {"content-scene", "content-scene"};
    value.outputs[0].default_scene_id = "content-scene";
    value.outputs[1].default_scene_id = "content-scene";
    return value;
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot content_return_snapshot(
    std::string handle_token, const std::string_view active_scene_id) {
    auto value = content_snapshot(std::move(handle_token));
    value.sources.push_back({
        .id = "default-color",
        .kind = solin::media_engine::SceneSourceKind::color,
        .enabled = true,
        .configuration =
            solin::media_engine::ColorSourceConfiguration{"#000000FF"},
    });
    value.scenes.push_back({
        .id = "default-scene",
        .layers = {layer("default-layer", "default-color")},
    });
    value.active_scene_ids = {std::string{active_scene_id},
                              std::string{active_scene_id}};
    value.outputs[0].default_scene_id = "default-scene";
    value.outputs[1].default_scene_id = "default-scene";
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
        [&runtime, &cancelled] { runtime.take(cancelled, 1U, 3U); },
        "preparation_not_found", "a cancelled token cannot be committed");

    const auto prepared = runtime.prepare(solin::media_engine::OutputBus::media_windows,
                                          "scene-c", 1U, "prepare-committed", 4U);
    runtime.take(prepared, 1U, 5U);
    expect(runtime.active_scene(solin::media_engine::OutputBus::media_windows) ==
               "scene-c" &&
               runtime.active_scene(solin::media_engine::OutputBus::virtual_camera) ==
                   "scene-a",
           "Take changes exactly one output bus atomically");
    expect(runtime.pending_preparation_count() == 0U,
           "Take consumes its preparation token");
    expect_error(
        [&runtime, &prepared] { runtime.take(prepared, 1U, 6U); },
        "preparation_not_found", "a consumed preparation cannot be replayed");

    const auto superseded = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-a", 1U,
        "prepare-superseded", 7U);
    const auto newest = runtime.prepare(solin::media_engine::OutputBus::media_windows,
                                        "scene-b", 1U, "prepare-newest", 8U);
    expect_error(
        [&runtime, &superseded] { runtime.take(superseded, 1U, 9U); },
        "preparation_not_found", "a newer preparation invalidates an older bus token");
    runtime.take(newest, 1U, 10U);

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

void test_hydration_replaces_runtime_content_transport_at_the_same_document_revision() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(content_snapshot("shm-token"), 1U);
    renderer->prepared_output_ready = false;
    expect_error(
        [&runtime] { runtime.hydrate(content_snapshot("d3d11-token"), 2U); },
        "content_ingress_output_unavailable",
        "a content transport handoff rejects graphs that produced no causal output");
    expect(renderer->hydration_commit_count == 1U,
           "a rejected content handoff preserves the previously committed graphs");

    renderer->prepared_output_ready = true;
    runtime.hydrate(content_snapshot("d3d11-token"), 2U);

    expect(runtime.document_revision() == 1U &&
               renderer->hydration_commit_count == 2U &&
               counters->created == 3U,
           "a runtime-only content channel switch atomically rebuilds the active graphs without changing the scene revision");
}

void test_retired_scene_graph_is_not_reused_after_content_transport_changes() {
    using solin::media_engine::OutputBus;

    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};

    runtime.hydrate(content_return_snapshot("d3d11-token", "content-scene"),
                    1U);
    const auto away = runtime.prepare(OutputBus::virtual_camera,
                                      "default-scene", 1U, "leave-content", 2U);
    runtime.take(away, 1U, 3U);

    // Runtime transport is deliberately not part of the authored document
    // revision. Hydration must nevertheless retire every prepared graph that
    // still leases the previous source generation.
    runtime.hydrate(content_return_snapshot("shm-token", "default-scene"), 4U);
    const auto preparations_before_return = renderer->prepare_count;
    const auto runtimes_before_return = counters->created.load();

    const auto returned = runtime.prepare(OutputBus::virtual_camera,
                                          "content-scene", 1U,
                                          "return-to-content", 5U);

    expect(renderer->prepare_count == preparations_before_return + 1U &&
               counters->created.load() == runtimes_before_return + 1U,
           "a prompt return after a content transport handoff prepares the current source generation instead of reclaiming the retired graph");
    runtime.take(returned, 1U, 6U);
}

void test_content_prepare_requires_the_requested_presentation_before_reuse_or_preroll() {
    using solin::media_engine::OutputBus;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(content_return_snapshot("d3d11-token", "content-scene"),
                    1U);

    const auto away = runtime.prepare(OutputBus::virtual_camera,
                                      "default-scene", 1U,
                                      "activation-away", 2U);
    runtime.take(away, 1U, 3U);
    const auto preparations_before_return = renderer->prepare_count;

    expect_error(
        [&runtime] {
            static_cast<void>(runtime.prepare(
                OutputBus::virtual_camera, "content-scene", 1U,
                "activation-stale", 4U, {}, 42U));
        },
        "source_unavailable",
        "a retained content graph cannot activate before the requested presentation");
    expect(renderer->refresh_count == 0U &&
               renderer->prepare_count == preparations_before_return &&
               renderer->active_scene(OutputBus::virtual_camera) ==
                   "default-scene",
           "a rejected presentation leaves Program and the renderer untouched");

    const auto content_runtime = factory->content_runtime();
    expect(content_runtime != nullptr,
           "the content presentation test owns the canonical content runtime");
    if (content_runtime == nullptr) {
        return;
    }
    content_runtime->publish_activation_frame(42U);
    const auto ready = runtime.prepare(OutputBus::virtual_camera,
                                       "content-scene", 1U,
                                       "activation-ready", 5U, {}, 42U);
    expect(renderer->prepare_count == preparations_before_return + 1U,
           "a discarded stale graph is cold-prerolled only after the requested "
           "presentation arrives");
    runtime.take(ready, 1U, 6U);
    expect(renderer->active_scene(OutputBus::virtual_camera) ==
               "content-scene",
           "the activation-safe cold graph remains committable");
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
    runtime.take(prepared, 1U, 4U);
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

    auto idle = snapshot(4U);
    idle.sources.push_back({
        .id = "solin.idle.current",
        .kind = solin::media_engine::SceneSourceKind::idle_screen,
        .enabled = true,
    });
    idle.scenes.push_back(
        {.id = "scene-idle", .layers = {layer("idle-layer", "solin.idle.current")}});
    idle.active_scene_ids = {"scene-idle", "scene-idle"};
    expect_error([&runtime, &idle] { runtime.hydrate(idle, 4U); },
                 "source_not_implemented", "idle requires libobs without creating a native decoder");
    expect(runtime.document_revision() == 1U,
           "unsupported idle content preserves the applied document");
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
    runtime.take(prepared, 1U, 3U);
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

void test_hidden_program_takes_skip_animated_transition_work() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    renderer->transitions_available = true;
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    const auto hidden = runtime.prepare(
        solin::media_engine::OutputBus::virtual_camera, "scene-c", 1U,
        "hidden-program-take", 2U,
        {.kind = solin::media_engine::SceneTransitionKind::dissolve,
         .duration_ms = 500U});
    expect(hidden.effective_transition.kind ==
               solin::media_engine::SceneTransitionKind::cut &&
               renderer->transition_prepare_count == 0U,
           "a Program bus without render demand does not prepare an invisible transition");

    runtime.take(hidden, 1U, 3U);
    expect(renderer->transition_counters->started == 0U &&
               runtime.active_scene(
                   solin::media_engine::OutputBus::virtual_camera) == "scene-c",
           "a hidden Take commits the destination scene without running animation");
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
    runtime.take(away, 1U, 3U);
    expect(renderer->prepare_count == 3U,
           "the first cut prepares the destination graph once");

    const auto back = runtime.prepare(
        solin::media_engine::OutputBus::media_windows, "scene-a", 1U,
        "prepare-back", 4U);
    expect(renderer->prepare_count == 3U && renderer->refresh_count == 1U,
           "a prompt return refreshes and reuses the retained graph without another preroll");
    expect(back.preparation_token != away.preparation_token,
           "a reused graph receives a fresh transaction identity");
    runtime.take(back, 1U, 5U);
    expect(renderer->active_scene(
               solin::media_engine::OutputBus::media_windows) == "scene-a",
           "the retained graph remains committable as the active scene");
}

void test_unrefreshable_retained_graph_falls_back_to_a_clean_preroll() {
    using solin::media_engine::OutputBus;
    auto counters = std::make_shared<RuntimeCounters>();
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{
        std::make_shared<FakeRuntimeFactory>(counters), renderer};
    runtime.hydrate(snapshot(), 1U);

    const auto away = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                      "refresh-fallback-away", 2U);
    runtime.take(away, 1U, 3U);
    const auto preparations_before_return = renderer->prepare_count;
    renderer->refresh_succeeds = false;

    const auto back = runtime.prepare(OutputBus::virtual_camera, "scene-a", 1U,
                                      "refresh-fallback-back", 4U);

    expect(renderer->refresh_count == 1U &&
               renderer->prepare_count == preparations_before_return + 1U &&
               renderer->active_scene(OutputBus::virtual_camera) == "scene-c",
           "an unrefreshable cache entry is rebuilt without changing the on-air scene");
    runtime.take(back, 1U, 5U);
    expect(renderer->active_scene(OutputBus::virtual_camera) == "scene-a",
           "the clean fallback preroll remains committable");
}

void test_inactive_scene_graph_remains_warm_beyond_transition_safety_window() {
    using namespace std::chrono_literals;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    const auto away = runtime.prepare(
        solin::media_engine::OutputBus::virtual_camera, "scene-c", 1U,
        "prepare-cold-away", 2U);
    runtime.take(away, 1U, 3U);
    const auto preparations_before_return = renderer->prepare_count;

    std::this_thread::sleep_for(3'200ms);
    const auto back = runtime.prepare(
        solin::media_engine::OutputBus::virtual_camera, "scene-a", 1U,
        "prepare-warm-return", 4U);

    expect(renderer->prepare_count == preparations_before_return,
           "a bounded inactive scene cache avoids a cold compositor rebuild after three seconds");
    runtime.take(back, 1U, 5U);
}

void test_only_the_immediately_previous_on_air_graph_remains_warm() {
    using solin::media_engine::OutputBus;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    const auto first = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                       "bounded-first", 2U);
    runtime.take(first, 1U, 3U);
    const auto second = runtime.prepare(OutputBus::virtual_camera, "scene-b", 1U,
                                        "bounded-second", 4U);
    runtime.take(second, 1U, 5U);
    const auto preparations_before_historical_return = renderer->prepare_count;

    const auto historical = runtime.prepare(OutputBus::virtual_camera, "scene-a", 1U,
                                             "bounded-historical", 6U);

    expect(renderer->prepare_count == preparations_before_historical_return + 1U,
           "a third historical graph is rebuilt instead of retaining an unbounded pipeline history");
    runtime.take(historical, 1U, 7U);
}

void test_cancelled_preparations_never_enter_the_warm_graph_slot() {
    using solin::media_engine::OutputBus;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{factory, renderer};
    runtime.hydrate(snapshot(), 1U);

    static_cast<void>(runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                      "cancelled-warm-candidate", 2U));
    const auto preparations_before_cancel = renderer->prepare_count;
    runtime.cancel("cancelled-warm-candidate");
    const auto replacement = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                             "fresh-after-cancel", 3U);

    expect(renderer->prepare_count == preparations_before_cancel + 1U,
           "a graph that was never on air is destroyed instead of becoming a warm return candidate");
    runtime.take(replacement, 1U, 4U);
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
    runtime.take(preserved, 1U, 4U);
    expect(renderer->active_scene(
               solin::media_engine::OutputBus::media_windows) == "scene-b",
           "the preserved renderer graph remains committable");
}

void test_transition_contract_is_bound_during_program_preparation() {
    using solin::media_engine::OutputBus;
    using solin::media_engine::SceneTransitionKind;
    using solin::media_engine::SceneTransitionSpec;

    expect_error(
        [] {
            try {
                solin::media_engine::validate_scene_transition(
                    {.kind = SceneTransitionKind::cut, .duration_ms = 1U});
            } catch (const std::invalid_argument&) {
                throw solin::media_engine::SceneGraphError{
                    "invalid_transition", "Synthetic invalid transition"};
            }
        },
        "invalid_transition", "CUT rejects a non-zero duration");
    const solin::media_engine::BgraPixel red{0U, 0U, 255U, 255U};
    const solin::media_engine::BgraPixel blue{255U, 0U, 0U, 255U};
    const auto dissolve_midpoint = solin::media_engine::scene_transition_blend_pixel(
        {.kind = SceneTransitionKind::dissolve, .duration_ms = 350U}, 0.5,
        red, blue);
    expect(dissolve_midpoint ==
               solin::media_engine::BgraPixel{128U, 0U, 128U, 255U},
           "Dissolve produces the expected purple BGRA midpoint from red and blue");
    const auto dissolve_midpoint_alphas =
        solin::media_engine::scene_transition_source_over_alphas(
            solin::media_engine::scene_transition_weights(
                {.kind = SceneTransitionKind::dissolve, .duration_ms = 350U},
                0.5));
    expect(dissolve_midpoint_alphas ==
               solin::media_engine::SceneTransitionPadAlphas{1.0, 0.5},
           "Dissolve keeps the opaque outgoing frame under the incoming source-over pad");
    const auto identical_midpoint =
        solin::media_engine::scene_transition_blend_pixel(
            {.kind = SceneTransitionKind::dissolve, .duration_ms = 350U}, 0.5,
            red, red);
    expect(identical_midpoint == red,
           "Dissolve preserves pixels shared by both scenes without a luminance dip");
    const auto fade_midpoint = solin::media_engine::scene_transition_blend_pixel(
        {.kind = SceneTransitionKind::fade_to_black, .duration_ms = 350U}, 0.5,
        red, blue);
    expect(fade_midpoint == solin::media_engine::BgraPixel{0U, 0U, 0U, 255U},
           "Fade through black produces an opaque black BGRA midpoint");
    const auto final_frame = solin::media_engine::scene_transition_blend_pixel(
        {.kind = SceneTransitionKind::fade_to_black, .duration_ms = 350U}, 1.0,
        red, blue);
    expect(final_frame == blue,
           "the final transition frame is byte-identical to its destination");
    const auto output_format = snapshot().outputs[1].video_format;
    expect(solin::media_engine::scene_transition_frame_interval(output_format) ==
               std::chrono::nanoseconds{16'666'666},
           "transition feeding follows the configured 60 fps output clock instead of 250 Hz");

    auto counters = std::make_shared<RuntimeCounters>();
    auto renderer = std::make_shared<FakeRenderer>();
    solin::media_engine::SceneGraphRuntime runtime{
        std::make_shared<FakeRuntimeFactory>(counters), renderer};
    runtime.hydrate(snapshot(), 1U);
    runtime.set_output_enabled(OutputBus::virtual_camera, true, 1U, 2U);

    const SceneTransitionSpec dissolve{
        .kind = SceneTransitionKind::dissolve,
        .duration_ms = 350U,
    };
    const auto preview = runtime.prepare(OutputBus::media_windows, "scene-c", 1U,
                                         "preview-transition", 3U, dissolve);
    expect(preview.effective_transition.kind == SceneTransitionKind::cut &&
               renderer->transition_prepare_count == 0U,
           "the editor Preview always resolves an animated request to CUT");
    runtime.take(preview, 1U, 4U);

    const auto fallback = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                          "program-fallback", 5U, dissolve);
    expect(fallback.effective_transition.kind == SceneTransitionKind::cut &&
               fallback.fallback_applied &&
               fallback.fallback_reason == "synthetic_transition_unavailable",
           "effect preparation failure binds a typed CUT fallback to the token");
    runtime.take(fallback, 1U, 6U);
    expect(runtime.active_scene(OutputBus::virtual_camera) == "scene-c",
           "a prepared fallback still converges Program to its destination");

    renderer->transitions_available = true;
    const SceneTransitionSpec fade{
        .kind = SceneTransitionKind::fade_to_black,
        .duration_ms = 500U,
    };
    const auto animated = runtime.prepare(OutputBus::virtual_camera, "scene-b", 1U,
                                          "program-animated", 7U, fade);
    expect(animated.effective_transition == fade && !animated.fallback_applied &&
               renderer->requested_transition == fade,
           "Program preparation binds the resolved transition before Take");
    runtime.take(animated, 1U, 8U);
    expect(runtime.active_scene(OutputBus::virtual_camera) == "scene-b",
            "Take consumes the token without accepting a replacement transition");
    expect(renderer->transition_counters->origin_rendering_enabled_at_start,
           "Take transfers A/B ownership before retiring the former on-air graph");
}

void test_rapid_program_retargets_are_latest_wins_and_resource_bounded() {
    using solin::media_engine::OutputBus;
    using solin::media_engine::SceneTransitionKind;
    const solin::media_engine::SceneTransitionSpec dissolve{
        .kind = SceneTransitionKind::dissolve,
        .duration_ms = 350U,
    };
    auto counters = std::make_shared<RuntimeCounters>();
    auto renderer = std::make_shared<FakeRenderer>();
    renderer->transitions_available = true;
    solin::media_engine::SceneGraphRuntime runtime{
        std::make_shared<FakeRuntimeFactory>(counters), renderer};
    runtime.hydrate(snapshot(), 1U);
    runtime.set_output_enabled(OutputBus::virtual_camera, true, 1U, 2U);

    const auto first = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                       "rapid-first", 3U, dissolve);
    const auto stable_origin = std::dynamic_pointer_cast<FakePreparedRenderGraph>(
        renderer->active[static_cast<std::size_t>(OutputBus::virtual_camera)]);
    expect(stable_origin != nullptr && stable_origin->direct_output_enabled &&
               stable_origin->direct_output_change_count == 0U,
           "preparing an effect does not mutate the on-air direct output branch");
    runtime.take(first, 1U, 4U);
    const auto active_first = std::dynamic_pointer_cast<FakePreparedTransition>(
        renderer->active[static_cast<std::size_t>(OutputBus::virtual_camera)]);
    const auto first_target = active_first == nullptr
                                  ? nullptr
                                  : std::dynamic_pointer_cast<FakePreparedRenderGraph>(
                                        active_first->transition_target());
    expect(stable_origin != nullptr && !stable_origin->direct_output_enabled &&
               first_target != nullptr && !first_target->direct_output_enabled,
           "Take disables both direct NV12 branches before the temporary compositor starts");
    const auto second = runtime.prepare(OutputBus::virtual_camera, "scene-b", 1U,
                                        "rapid-second", 5U, dissolve);
    expect(renderer->retarget_before_first_frame == 1U,
           "a retarget before the first composed frame uses the stable transition origin");
    runtime.take(second, 1U, 6U);
    expect(renderer->transition_counters->overlapped_handoff,
           "a retarget starts the replacement compositor before retiring the previous one");

    const auto active_second = std::dynamic_pointer_cast<FakePreparedTransition>(
        renderer->active[static_cast<std::size_t>(OutputBus::virtual_camera)]);
    expect(active_second != nullptr,
           "the newest prepared animated destination is the only active transition");
    if (active_second != nullptr) {
        active_second->publish_composed_frame();
        active_second->complete();
    }
    const auto completed_target = active_second == nullptr
                                      ? nullptr
                                      : std::dynamic_pointer_cast<FakePreparedRenderGraph>(
                                            active_second->transition_target());
    const auto third = runtime.prepare(OutputBus::virtual_camera, "scene-c", 1U,
                                       "rapid-third", 7U, dissolve);
    expect(renderer->retarget_after_composed_frame == 1U,
           "a retarget after composition observes the latest composed GPU frame");
    expect(completed_target != nullptr && completed_target->direct_output_enabled,
           "a completed/frozen retarget leaves the current direct branch enabled during prepare");
    runtime.take(third, 1U, 8U);
    expect(completed_target != nullptr && !completed_target->direct_output_enabled,
           "the completed wrapper delegates direct-output gating when the retarget is taken");

    const auto cut = runtime.prepare(OutputBus::virtual_camera, "scene-a", 1U,
                                     "rapid-cut", 9U);
    runtime.take(cut, 1U, 10U);
    expect(runtime.active_scene(OutputBus::virtual_camera) == "scene-a",
           "the final request wins without a transition queue");
    expect(renderer->transition_counters->active == 0U &&
               renderer->transition_counters->started == 3U &&
               renderer->transition_counters->stopped == 3U &&
               renderer->transition_counters->maximum_active == 2U,
           "rapid retargets use a bounded one-compositor handoff overlap");
}

} // namespace

int main() {
    test_compiler_preserves_reference_groups_without_expanding_the_graph();
    test_hydrate_prepare_cancel_and_take_are_transactional();
    test_hydration_replaces_runtime_content_transport_at_the_same_document_revision();
    test_retired_scene_graph_is_not_reused_after_content_transport_changes();
    test_content_prepare_requires_the_requested_presentation_before_reuse_or_preroll();
    test_preview_geometry_updates_the_active_renderer_without_rebuilding_it();
    test_failed_or_stale_prepare_preserves_the_previous_preparation();
    test_failed_hydration_preserves_the_previous_applied_graph();
    test_new_document_identity_accepts_an_independent_revision_sequence();
    test_invisible_reference_groups_do_not_start_unused_sources();
    test_renderer_prepare_and_commits_follow_scene_transactions();
    test_hidden_program_takes_skip_animated_transition_work();
    test_recent_scene_graph_is_reused_for_a_return_cut();
    test_unrefreshable_retained_graph_falls_back_to_a_clean_preroll();
    test_inactive_scene_graph_remains_warm_beyond_transition_safety_window();
    test_only_the_immediately_previous_on_air_graph_remains_warm();
    test_cancelled_preparations_never_enter_the_warm_graph_slot();
    test_renderer_failure_preserves_applied_and_pending_graphs();
    test_transition_contract_is_bound_during_program_preparation();
    test_rapid_program_retargets_are_latest_wins_and_resource_bounded();
    if (failures != 0) {
        std::cerr << failures << " scene graph test(s) failed\n";
        return 1;
    }
    return 0;
}
