#include "solin/media_engine/source_registry.hpp"

#include <array>
#include <atomic>
#include <barrier>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <condition_variable>
#include <exception>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <ranges>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (condition) {
        return;
    }
    ++failures;
    std::cerr << "FAILED: " << description << '\n';
}

template <typename Predicate> bool wait_until(Predicate&& predicate) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{2};
    while (std::chrono::steady_clock::now() < deadline) {
        if (std::forward<Predicate>(predicate)()) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds{2});
    }
    return false;
}

template <typename Callback> void expect_rejected(Callback&& callback, const char* description) {
    try {
        std::forward<Callback>(callback)();
        expect(false, description);
    } catch (const std::exception&) {
        expect(true, description);
    }
}

template <typename Callback>
void expect_error_code(Callback&& callback, const std::string_view error_code,
                       const char* description) {
    try {
        std::forward<Callback>(callback)();
        expect(false, description);
    } catch (const std::runtime_error& error) {
        expect(error.what() == error_code, description);
    } catch (const std::exception&) {
        expect(false, description);
    }
}

struct RuntimeCounters final {
    std::atomic_uint64_t created{0U};
    std::atomic_uint64_t started{0U};
    std::atomic_uint64_t stopped{0U};
    std::atomic_bool fail_start{false};
};

class FakeRuntime final : public solin::media_engine::SourceRuntime {
  public:
    explicit FakeRuntime(std::shared_ptr<RuntimeCounters> counters)
        : counters_(std::move(counters)) {}

    void start() override {
        ++counters_->started;
        if (counters_->fail_start.exchange(false)) {
            throw std::runtime_error("synthetic source start failure");
        }
        status_.store(solin::media_engine::SourceRuntimeStatus::ready);
    }

    void stop() noexcept override {
        if (!stopped_.exchange(true)) {
            ++counters_->stopped;
            status_.store(solin::media_engine::SourceRuntimeStatus::stopped);
        }
    }

    [[nodiscard]] solin::media_engine::SourceRuntimeHealth health() const override {
        return {.status = status_.load(), .frame_sequence = 12U};
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
    create(const solin::media_engine::SceneSource&, std::uint64_t) override {
        ++counters_->created;
        if (creation_delay_.count() > 0) {
            std::this_thread::sleep_for(creation_delay_);
        }
        if (fail_next_.exchange(false)) {
            throw std::runtime_error("synthetic source creation failure");
        }
        return std::make_shared<FakeRuntime>(counters_);
    }

    void fail_next() noexcept { fail_next_.store(true); }
    void fail_next_start() noexcept { counters_->fail_start.store(true); }
    void set_creation_delay(const std::chrono::milliseconds delay) noexcept {
        creation_delay_ = delay;
    }

  private:
    std::shared_ptr<RuntimeCounters> counters_{};
    std::atomic_bool fail_next_{false};
    std::chrono::milliseconds creation_delay_{0};
};

struct BlockingStopState final {
    std::atomic_uint64_t created{0U};
    std::atomic_uint64_t live{0U};
    std::atomic_uint64_t maximum_live{0U};
    std::atomic_uint64_t stopped{0U};
    std::atomic_bool stop_entered{false};
    std::mutex mutex{};
    std::condition_variable wakeup{};
    bool allow_stop{false};
};

class BlockingStopRuntime final : public solin::media_engine::SourceRuntime {
  public:
    explicit BlockingStopRuntime(std::shared_ptr<BlockingStopState> state)
        : state_(std::move(state)) {}

    void start() override {
        const auto live = state_->live.fetch_add(1U) + 1U;
        auto maximum = state_->maximum_live.load();
        while (maximum < live &&
               !state_->maximum_live.compare_exchange_weak(maximum, live)) {
        }
    }

    void stop() noexcept override {
        if (stopped_.exchange(true)) {
            return;
        }
        state_->stop_entered.store(true);
        state_->wakeup.notify_all();
        {
            std::unique_lock lock{state_->mutex};
            state_->wakeup.wait(lock, [this] { return state_->allow_stop; });
        }
        state_->live.fetch_sub(1U);
        state_->stopped.fetch_add(1U);
    }

    [[nodiscard]] solin::media_engine::SourceRuntimeHealth health() const override {
        return {.status = solin::media_engine::SourceRuntimeStatus::ready};
    }

  private:
    std::shared_ptr<BlockingStopState> state_{};
    std::atomic_bool stopped_{false};
};

class BlockingStopFactory final : public solin::media_engine::SourceRuntimeFactory {
  public:
    explicit BlockingStopFactory(std::shared_ptr<BlockingStopState> state)
        : state_(std::move(state)) {}

    [[nodiscard]] std::shared_ptr<solin::media_engine::SourceRuntime>
    create(const solin::media_engine::SceneSource&, std::uint64_t) override {
        state_->created.fetch_add(1U);
        return std::make_shared<BlockingStopRuntime>(state_);
    }

  private:
    std::shared_ptr<BlockingStopState> state_{};
};

[[nodiscard]] solin::media_engine::SceneSource color_source(const std::string& color,
                                                             const bool enabled = true,
                                                             std::string id = "color-1") {
    return {
        .id = std::move(id),
        .kind = solin::media_engine::SceneSourceKind::color,
        .enabled = enabled,
        .configuration = solin::media_engine::ColorSourceConfiguration{color},
    };
}

[[nodiscard]] solin::media_engine::SceneSource rtsp_source(
    std::string id, const bool keep_active = false) {
    return {
        .id = std::move(id),
        .kind = solin::media_engine::SceneSourceKind::rtsp_camera,
        .enabled = true,
        .configuration = solin::media_engine::RtspCameraSourceConfiguration{
            .uri = "rtsp://127.0.0.1/camera",
            .keep_active = keep_active,
        },
    };
}

[[nodiscard]] solin::media_engine::SceneSource local_camera_source(
    std::string id, std::string device_id = "camera://one",
    const bool keep_active = false) {
    return {
        .id = std::move(id),
        .kind = solin::media_engine::SceneSourceKind::local_camera,
        .enabled = true,
        .configuration = solin::media_engine::LocalCameraSourceConfiguration{
            .device_id = std::move(device_id),
            .keep_active = keep_active,
        },
    };
}

[[nodiscard]] solin::media_engine::SceneSource content_source(
    std::string handle_token) {
    return {
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
    };
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot snapshot(
    const std::uint64_t revision,
    std::vector<solin::media_engine::SceneSource> sources) {
    return {
        .document_id = "document-1",
        .document_revision = revision,
        .sources = std::move(sources),
    };
}

[[nodiscard]] solin::media_engine::SceneHydrationSnapshot
snapshot(const std::uint64_t revision, solin::media_engine::SceneSource source) {
    return {
        .document_id = "document-1",
        .document_revision = revision,
        .sources = {std::move(source)},
    };
}

void test_active_consumers_share_one_runtime() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));

    {
        auto media = registry.acquire("color-1", "media-windows");
        auto camera = registry.acquire("color-1", "virtual-camera");
        expect(&media.runtime() == &camera.runtime(),
               "two consumers share one source runtime generation");
        expect(media.generation() == camera.generation(),
               "two consumers share one source generation");
        const auto entries = registry.entries();
        expect(entries.size() == 1U && entries[0].consumer_count == 2U,
               "registry reports both active consumers");
        expect(entries[0].health.status ==
                   solin::media_engine::SourceRuntimeStatus::ready,
               "registry reports runtime health");
        expect_rejected(
            [&registry] {
                static_cast<void>(registry.acquire("color-1", "media-windows"));
            },
            "duplicate consumer leases are rejected");
    }

    expect(counters->created == 1U && counters->started == 1U,
           "one runtime is created and started");
    expect(wait_until([&counters] { return counters->stopped == 1U; }),
           "the runtime stops after its final lease");
}

void test_scene_registry_keeps_an_opted_in_camera_active() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{
        factory,
        solin::media_engine::SourceRegistryLimits{
            .maximum_retained_idle_cameras = 1U,
        }};
    registry.replace_snapshot(
        snapshot(1U, local_camera_source("camera-1", "camera://one", true)));
    std::uint64_t generation = 0U;
    {
        auto lease = registry.acquire("camera-1", "first-scene");
        generation = lease.generation();
    }

    std::this_thread::sleep_for(std::chrono::milliseconds{100});
    expect(counters->stopped == 0U,
           "an opted-in camera remains active without a consumer");

    {
        auto lease = registry.acquire("camera-1", "second-scene");
        expect(lease.generation() == generation && counters->created == 1U,
               "the next scene reuses the persistent camera runtime");
        registry.replace_snapshot(
            snapshot(2U, local_camera_source("camera-1", "camera://one", false)));
        expect(lease.generation() == generation && counters->created == 1U,
               "changing only retention policy does not restart an active camera");
    }
    expect(wait_until([&counters] { return counters->stopped == 1U; }),
           "disabling persistent retention restores shutdown after the final lease");
}

void test_configuration_changes_create_a_new_generation_without_invalidating_old_leases() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    auto old_generation = registry.acquire("color-1", "prepared-scene-old");

    registry.replace_snapshot(snapshot(2U, color_source("#FFFFFFFF")));
    auto new_generation = registry.acquire("color-1", "prepared-scene-new");

    const auto entries = registry.entries();
    expect(entries.size() == 2U &&
               std::ranges::count_if(entries, [](const auto& entry) { return entry.current; }) ==
                   1,
           "diagnostics include both current and leased stale generations");

    expect(old_generation.generation() != new_generation.generation(),
           "configuration changes advance the source generation");
    expect(&old_generation.runtime() != &new_generation.runtime(),
           "old and new graph preparations do not share changed sources");
    expect(std::get<solin::media_engine::ColorSourceConfiguration>(
               old_generation.source().configuration) ==
               solin::media_engine::ColorSourceConfiguration{"#000000FF"},
           "an in-flight graph retains its immutable old source definition");
    expect(std::get<solin::media_engine::ColorSourceConfiguration>(
               new_generation.source().configuration) ==
               solin::media_engine::ColorSourceConfiguration{"#FFFFFFFF"},
           "new graph preparations receive the updated source definition");
}

void test_staged_updates_are_isolated_until_commit() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    auto active = registry.acquire("color-1", "active-graph");

    auto update =
        registry.stage_snapshot(snapshot(2U, color_source("#FFFFFFFF")));
    auto candidate = update.acquire("color-1", "candidate-graph");
    auto still_active = registry.acquire("color-1", "current-before-commit");
    expect(registry.document_revision() == 1U &&
               &active.runtime() == &still_active.runtime() &&
               &candidate.runtime() != &active.runtime(),
           "a staged source generation is invisible to the applied registry");
    expect(std::get<solin::media_engine::ColorSourceConfiguration>(
               candidate.source().configuration) ==
               solin::media_engine::ColorSourceConfiguration{"#FFFFFFFF"},
           "candidate preparations use the staged source definition");

    update.commit();
    auto committed = registry.acquire("color-1", "current-after-commit");
    expect(registry.document_revision() == 2U &&
               &committed.runtime() == &candidate.runtime(),
           "commit atomically promotes the already prepared source generation");

    {
        auto discarded_update =
            registry.stage_snapshot(snapshot(3U, color_source("#336699FF")));
        auto discarded_candidate =
            discarded_update.acquire("color-1", "discarded-candidate");
        expect(std::get<solin::media_engine::ColorSourceConfiguration>(
                   discarded_candidate.source().configuration) ==
                   solin::media_engine::ColorSourceConfiguration{"#336699FF"},
               "a rejected update may prepare its isolated candidate generation");
    }
    auto after_discard = registry.acquire("color-1", "current-after-discard");
    expect(registry.document_revision() == 2U &&
               &after_discard.runtime() == &candidate.runtime() &&
               std::get<solin::media_engine::ColorSourceConfiguration>(
                   after_discard.source().configuration) ==
                   solin::media_engine::ColorSourceConfiguration{"#FFFFFFFF"},
           "discarding a staged update preserves the applied revision and source runtime");
}

void test_idempotent_revisions_preserve_generations_and_conflicts_are_rejected() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    const auto initial = snapshot(4U, color_source("#000000FF"));
    registry.replace_snapshot(initial);
    const auto initial_generation = registry.entries()[0].generation;

    registry.replace_snapshot(initial);
    expect(registry.entries()[0].generation == initial_generation,
           "idempotent hydration keeps the source generation stable");
    expect_rejected(
        [&registry] {
            registry.replace_snapshot(snapshot(4U, color_source("#FFFFFFFF")));
        },
        "one document revision cannot describe conflicting source definitions");
    expect_rejected(
        [&registry] {
            registry.replace_snapshot(snapshot(3U, color_source("#000000FF")));
        },
        "stale document revisions are rejected");

    registry.replace_snapshot(snapshot(5U, color_source("#000000FF")));
    expect(registry.entries()[0].generation == initial_generation,
           "unchanged definitions keep their generation across document revisions");
}

void test_runtime_content_channel_can_change_without_forging_a_document_revision() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(4U, content_source("shm-token")));
    auto previous = registry.acquire(solin::media_engine::kSolinContentSourceId,
                                     "previous-graph");
    const auto initial_generation = registry.entries()[0].generation;
    expect(registry.is_current(previous),
           "a lease acquired from the installed content route is current");

    registry.replace_snapshot(snapshot(4U, content_source("d3d11-token")));
    auto replacement = registry.acquire(solin::media_engine::kSolinContentSourceId,
                                         "replacement-graph");
    const auto entries = registry.entries();
    const auto current = std::ranges::find_if(
        entries, [](const solin::media_engine::SourceRegistryEntry& entry) {
            return entry.current;
        });
    expect(registry.document_revision() == 4U &&
               current != entries.end() &&
               current->generation != initial_generation,
           "runtime content transport replacement keeps the document revision and advances the source generation");
    expect(!registry.is_current(previous) && registry.is_current(replacement),
           "content transport replacement makes the retired graph lease observably stale");
}

void test_new_document_identity_resets_revision_order_and_reuses_stable_sources() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(9U, color_source("#000000FF")));
    auto first = registry.acquire("color-1", "document-one");
    const auto generation = first.generation();

    auto next = snapshot(0U, color_source("#000000FF"));
    next.document_id = "document-2";
    registry.replace_snapshot(next);
    auto second = registry.acquire("color-1", "document-two");

    expect(registry.document_revision() == 0U,
           "a new document identity starts its independent revision sequence");
    expect(second.generation() == generation && &second.runtime() == &first.runtime(),
           "an unchanged stable source is reused across scene documents");
}

void test_creation_failure_does_not_poison_the_registry_cache() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    factory->fail_next();

    expect_rejected(
        [&registry] { static_cast<void>(registry.acquire("color-1", "consumer-1")); },
        "source creation failures are reported");
    {
        auto retry = registry.acquire("color-1", "consumer-1");
        expect(static_cast<bool>(retry), "a failed source can be retried");
    }
    expect(counters->created == 2U && counters->started == 1U,
           "retry creates a clean source runtime");
}

void test_staged_creation_failure_remains_source_scoped_until_recovery() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF", true, "base")));
    factory->fail_next();

    {
        auto update = registry.stage_snapshot(
            snapshot(2U, color_source("#FFFFFFFF", true, "camera-source")));
        expect_rejected(
            [&update] {
                static_cast<void>(update.acquire("camera-source", "renderer-consumer"));
            },
            "staged source creation failures are reported");
    }
    const auto failed_entries = registry.entries();
    const auto failure = std::ranges::find(
        failed_entries, "camera-source",
        &solin::media_engine::SourceRegistryEntry::source_id);
    expect(failure != failed_entries.end() && failure->current &&
               failure->consumer_count == 1U &&
               failure->health.status == solin::media_engine::SourceRuntimeStatus::failed &&
               failure->health.error_code == "source_runtime_start_failed",
           "an early staged failure remains observable with its source identity");

    auto retry = registry.stage_snapshot(
        snapshot(2U, color_source("#FFFFFFFF", true, "camera-source")));
    auto lease = retry.acquire("camera-source", "renderer-consumer");
    retry.commit();
    const auto recovered_entries = registry.entries();
    const auto recovered = std::ranges::find(
        recovered_entries, "camera-source",
        &solin::media_engine::SourceRegistryEntry::source_id);
    expect(static_cast<bool>(lease) && recovered != recovered_entries.end() &&
               recovered->health.status == solin::media_engine::SourceRuntimeStatus::ready &&
               recovered->health.error_code.empty(),
           "a successful retry clears the staged source failure");
}

void test_start_failure_stops_the_partial_runtime_and_allows_retry() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    factory->fail_next_start();

    expect_rejected(
        [&registry] { static_cast<void>(registry.acquire("color-1", "consumer-1")); },
        "source start failures are reported");
    expect(counters->stopped == 1U,
           "a partially started runtime is stopped before the failure escapes");
    {
        auto retry = registry.acquire("color-1", "consumer-1");
        expect(static_cast<bool>(retry), "a failed source start can be retried");
    }
    expect(counters->created == 2U && counters->started == 2U,
           "retry creates and starts a clean runtime after start failure");
}

void test_disabled_unknown_and_invalid_identifiers_are_rejected() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF", false)));

    expect_rejected(
        [&registry] { static_cast<void>(registry.acquire("color-1", "consumer-1")); },
        "disabled sources cannot be acquired");
    expect_rejected(
        [&registry] { static_cast<void>(registry.acquire("missing", "consumer-1")); },
        "unknown sources cannot be acquired");
    expect_rejected(
        [&registry] { static_cast<void>(registry.acquire("color-1", "consumer\n1")); },
        "control characters are rejected in runtime identities");
}

void test_concurrent_acquisition_still_creates_one_runtime() {
    constexpr std::size_t consumer_count = 8U;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    factory->set_creation_delay(std::chrono::milliseconds{20});
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));

    std::barrier gate{static_cast<std::ptrdiff_t>(consumer_count)};
    std::array<std::optional<solin::media_engine::SourceLease>, consumer_count> leases{};
    std::array<std::thread, consumer_count> workers{};
    for (std::size_t index = 0U; index < consumer_count; ++index) {
        workers[index] = std::thread([&registry, &gate, &leases, index] {
            gate.arrive_and_wait();
            leases[index].emplace(
                registry.acquire("color-1", "consumer-" + std::to_string(index)));
        });
    }
    for (auto& worker : workers) {
        worker.join();
    }

    const auto* first_runtime = &leases[0]->runtime();
    expect(std::ranges::all_of(leases, [first_runtime](const auto& lease) {
               return lease.has_value() && &lease->runtime() == first_runtime;
           }),
           "all concurrent consumers receive the same source runtime");
    expect(counters->created == 1U && counters->started == 1U,
           "concurrent source acquisition creates one runtime");
    for (auto& lease : leases) {
        lease.reset();
    }
    expect(wait_until([&counters] { return counters->stopped == 1U; }),
           "the concurrently shared runtime stops after all consumers release it");
}

void test_shutdown_waits_for_a_release_already_being_reaped() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    auto lease = registry.acquire("color-1", "consumer-1");

    std::thread releaser([lease_value = std::move(lease)] {
        static_cast<void>(static_cast<bool>(lease_value));
    });
    registry.shutdown();
    releaser.join();

    expect(counters->stopped == 1U,
           "shutdown is linearized after a concurrent final lease release");
}

void test_reacquire_waits_until_the_previous_runtime_has_stopped() {
    auto state = std::make_shared<BlockingStopState>();
    auto factory = std::make_shared<BlockingStopFactory>(state);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));
    {
        auto lease = registry.acquire("color-1", "consumer-1");
        expect(static_cast<bool>(lease), "the initial blocking runtime is acquired");
    }
    expect(wait_until([&state] { return state->stop_entered.load(); }),
           "the asynchronous stop worker begins runtime teardown");

    std::atomic_bool reacquired{false};
    std::thread acquirer([&registry, &reacquired] {
        auto lease = registry.acquire("color-1", "consumer-1");
        reacquired.store(static_cast<bool>(lease));
    });
    std::this_thread::sleep_for(std::chrono::milliseconds{20});
    const auto entries = registry.entries();
    expect(!reacquired.load() && state->created == 1U,
           "reacquire does not overlap the runtime that is still stopping");
    expect(entries.size() == 1U && entries[0].consumer_count == 0U &&
               entries[0].health.status ==
                   solin::media_engine::SourceRuntimeStatus::degraded &&
               entries[0].health.error_code == "source_runtime_stopping",
           "diagnostics keep a stopping generation visible");

    {
        std::scoped_lock lock{state->mutex};
        state->allow_stop = true;
    }
    state->wakeup.notify_all();
    acquirer.join();
    expect(reacquired.load() && state->created == 2U && state->maximum_live == 1U,
           "reacquire starts only after the prior runtime is fully stopped");
    expect(wait_until([&state] { return state->stopped == 2U; }),
           "both sequential runtimes are reaped");
}

void test_shutdown_waits_for_an_in_flight_runtime_creation() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    factory->set_creation_delay(std::chrono::milliseconds{100});
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));

    std::atomic_bool acquisition_rejected{false};
    std::thread acquirer([&registry, &acquisition_rejected] {
        try {
            static_cast<void>(registry.acquire("color-1", "consumer-1"));
        } catch (const std::logic_error&) {
            acquisition_rejected.store(true);
        }
    });
    expect(wait_until([&counters] { return counters->created == 1U; }),
           "the shutdown race reaches in-flight creation");
    registry.shutdown();
    acquirer.join();

    expect(acquisition_rejected.load(),
           "an acquisition cannot commit after registry shutdown starts");
    expect(counters->created == 1U && counters->started == 1U &&
               counters->stopped == 1U,
           "shutdown reaps the runtime completed by an in-flight creation");
}

void test_aggregate_runtime_and_consumer_budgets_are_enforced() {
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{
        factory,
        {
            .maximum_runtime_slots = 2U,
            .maximum_consumers_per_runtime = 2U,
            .maximum_active_frame_pixels = 3'840ULL * 2'160ULL,
            .maximum_decoder_sessions = 1U,
        },
    };
    registry.replace_snapshot(snapshot(
        1U, {color_source("#000000FF", true, "color-1"),
             color_source("#FFFFFFFF", true, "color-2")}));

    auto first = registry.acquire("color-1", "consumer-1");
    auto second = registry.acquire("color-1", "consumer-2");
    expect_error_code(
        [&registry] { static_cast<void>(registry.acquire("color-1", "consumer-3")); },
        "source_consumer_capacity_exceeded",
        "a source generation has a bounded number of consumers");
    expect_error_code(
        [&registry] { static_cast<void>(registry.acquire("color-2", "consumer-1")); },
        "source_frame_pixel_budget_exceeded",
        "aggregate frame pixels bound concurrently active source generations");

    auto slot_counters = std::make_shared<RuntimeCounters>();
    auto slot_factory = std::make_shared<FakeRuntimeFactory>(slot_counters);
    solin::media_engine::SourceRegistry slot_registry{
        slot_factory,
        {
            .maximum_runtime_slots = 1U,
            .maximum_consumers_per_runtime = 2U,
            .maximum_active_frame_pixels = 2ULL * 3'840ULL * 2'160ULL,
            .maximum_decoder_sessions = 2U,
        },
    };
    slot_registry.replace_snapshot(snapshot(
        1U, {color_source("#000000FF", true, "color-1"),
             color_source("#FFFFFFFF", true, "color-2")}));
    {
        auto slot_lease = slot_registry.acquire("color-1", "consumer-1");
        expect_error_code(
            [&slot_registry] {
                static_cast<void>(slot_registry.acquire("color-2", "consumer-1"));
            },
            "source_runtime_slot_capacity_exceeded",
            "runtime slot capacity is enforced independently of frame pixels");
    }
    expect(wait_until([&slot_registry] {
               const auto entries = slot_registry.entries();
               const auto source = std::ranges::find(entries, "color-1",
                                                     &solin::media_engine::SourceRegistryEntry::source_id);
               return source != entries.end() && source->consumer_count == 0U &&
                      source->health.status ==
                          solin::media_engine::SourceRuntimeStatus::stopped;
           }),
           "slot budget is released after asynchronous teardown");
    {
        auto reused_slot = slot_registry.acquire("color-2", "consumer-1");
        expect(static_cast<bool>(reused_slot),
               "a released runtime slot can be reassigned to another source");
    }

    auto decoder_counters = std::make_shared<RuntimeCounters>();
    auto decoder_factory = std::make_shared<FakeRuntimeFactory>(decoder_counters);
    solin::media_engine::SourceRegistry decoder_registry{
        decoder_factory,
        {
            .maximum_runtime_slots = 2U,
            .maximum_consumers_per_runtime = 2U,
            .maximum_active_frame_pixels = 2ULL * 3'840ULL * 2'160ULL,
            .maximum_decoder_sessions = 1U,
        },
    };
    decoder_registry.replace_snapshot(
        snapshot(1U, {rtsp_source("rtsp-1"), rtsp_source("rtsp-2")}));
    {
        auto decoder_lease = decoder_registry.acquire("rtsp-1", "consumer-1");
        expect_error_code(
            [&decoder_registry] {
                static_cast<void>(decoder_registry.acquire("rtsp-2", "consumer-1"));
            },
            "source_decoder_session_budget_exceeded",
            "decoder session capacity is enforced independently of slots and pixels");
    }
    expect(wait_until([&decoder_registry] {
               const auto entries = decoder_registry.entries();
               const auto source = std::ranges::find(entries, "rtsp-1",
                                                     &solin::media_engine::SourceRegistryEntry::source_id);
               return source != entries.end() && source->consumer_count == 0U &&
                      source->health.status ==
                          solin::media_engine::SourceRuntimeStatus::stopped;
           }),
           "decoder budget is released after asynchronous teardown");
    {
        auto reused_decoder = decoder_registry.acquire("rtsp-2", "consumer-1");
        expect(static_cast<bool>(reused_decoder),
               "a released decoder session can be assigned to another source");
    }
}

void test_repeated_release_and_reacquire_uses_bounded_stack_and_never_overlaps() {
    constexpr std::uint64_t iteration_count = 1'000U;
    auto counters = std::make_shared<RuntimeCounters>();
    auto factory = std::make_shared<FakeRuntimeFactory>(counters);
    solin::media_engine::SourceRegistry registry{factory};
    registry.replace_snapshot(snapshot(1U, color_source("#000000FF")));

    for (auto iteration = std::uint64_t{0U}; iteration < iteration_count; ++iteration) {
        auto lease = registry.acquire("color-1", "consumer-1");
        expect(static_cast<bool>(lease), "a churned runtime can be reacquired iteratively");
    }
    registry.shutdown();
    expect(counters->created == iteration_count && counters->stopped == iteration_count,
           "runtime churn finishes every generation without recursive retry or overlap");
}

} // namespace

int main() {
    test_active_consumers_share_one_runtime();
    test_scene_registry_keeps_an_opted_in_camera_active();
    test_configuration_changes_create_a_new_generation_without_invalidating_old_leases();
    test_staged_updates_are_isolated_until_commit();
    test_idempotent_revisions_preserve_generations_and_conflicts_are_rejected();
    test_runtime_content_channel_can_change_without_forging_a_document_revision();
    test_new_document_identity_resets_revision_order_and_reuses_stable_sources();
    test_creation_failure_does_not_poison_the_registry_cache();
    test_staged_creation_failure_remains_source_scoped_until_recovery();
    test_start_failure_stops_the_partial_runtime_and_allows_retry();
    test_disabled_unknown_and_invalid_identifiers_are_rejected();
    test_concurrent_acquisition_still_creates_one_runtime();
    test_shutdown_waits_for_a_release_already_being_reaped();
    test_reacquire_waits_until_the_previous_runtime_has_stopped();
    test_shutdown_waits_for_an_in_flight_runtime_creation();
    test_aggregate_runtime_and_consumer_budgets_are_enforced();
    test_repeated_release_and_reacquire_uses_bounded_stack_and_never_overlaps();
    if (failures != 0) {
        std::cerr << failures << " source registry test(s) failed\n";
        return 1;
    }
    return 0;
}
